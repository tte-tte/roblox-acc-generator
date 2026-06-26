"""
Roblox community-group join helper.

Shared by main.py (post-signup auto-join) and join_group.py (standalone
batch-joiner for accounts.txt). Operates on an existing curl_cffi-style
session that already carries the account's cookie jar (specifically
.ROBLOSECURITY) — that way callers don't need to know anything about the
auth surface and we get TLS-impersonation parity with the signup flow
that produced the cookies in the first place.

Public API:
    join_group(session, group_id, *, user_agent, accept_language=None,
               sec_ch_ua=None, sec_ch_ua_platform=None,
               sec_ch_ua_mobile=None, timeout_s=15.0) -> JoinResult

Endpoint: POST https://groups.roblox.com/v1/groups/{groupId}/users
  - 200          → joined this call
  - 400 code=26  → already a member (treat as success, idempotent)
  - 400 code=12  → group is approval-only, request submitted (treat as success)
  - 403          → CSRF refresh dance (handled internally) or auth-denied
  - other 4xx/5xx → returned as failure with full body

Roblox CSRF is the same lazy probe pattern roblox.py uses: first POST sans
token returns 403 with `x-csrf-token` in the response header, we capture
it, and re-send. No separate auth-ticket call needed.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any


GROUPS_URL = "https://groups.roblox.com"
CHALLENGE_CONTINUE_URL = "https://apis.roblox.com/challenge/v1/continue"

# Roblox's Arkose public key. The signup pipeline uses this same key for
# its FunCaptcha challenges; we default to it here on the assumption that
# Roblox shares one Arkose project across action types (the per-action
# differentiation lives in the blob's `actionType` field, e.g. "JoinGroup"
# vs "Signup"). If JoinGroup turns out to use a separate pkey, override
# with the `arkose_public_key` argument to `join_group()`.
_ROBLOX_ARKOSE_PK   = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
_ROBLOX_ARKOSE_SURL = "https://arkoselabs.roblox.com"


# Roblox group-membership error codes worth treating as non-failure.
# Source: groups.roblox.com /v1 OpenAPI under GroupsErrorCode.
_ALREADY_MEMBER_CODES = {26}        # "You are already a member of this group."
_PENDING_REQUEST_CODES = {11, 12}   # request-pending / approval-required group


@dataclass
class JoinResult:
    ok:        bool
    status:    int
    code:      int | None
    message:   str
    already:   bool = False         # already a member before this call
    pending:   bool = False         # join request submitted, approval required

    def __str__(self) -> str:
        if self.already:
            return f"already-member (http {self.status})"
        if self.pending:
            return f"pending-approval (http {self.status})"
        if self.ok:
            return f"joined (http {self.status})"
        suffix = f" code={self.code}" if self.code is not None else ""
        return f"failed http {self.status}{suffix}: {self.message[:120]}"


def _build_headers(
    *,
    user_agent: str,
    accept_language: str | None,
    sec_ch_ua: str | None,
    sec_ch_ua_platform: str | None,
    sec_ch_ua_mobile: str | None,
    csrf: str | None,
) -> dict:
    """Match the per-worker fingerprint headers that roblox.py used during
    signup. Mixing Safari TLS + Chrome sec-ch-ua (or vice versa) drives
    Roblox's bot heuristics; we keep them aligned with whatever profile
    minted the cookies."""
    is_chromium = bool(sec_ch_ua)
    h: dict = {
        "Content-Type": "application/json",
        "Accept":       "application/json, text/plain, */*",
        "Origin":       "https://www.roblox.com",
        "Referer":      "https://www.roblox.com/",
        "User-Agent":   user_agent,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Connection":     "keep-alive",
    }
    if accept_language:
        h["Accept-Language"] = accept_language
    h["Accept-Encoding"] = "gzip, deflate, br, zstd" if is_chromium else "gzip, deflate, br"
    if is_chromium:
        h["sec-ch-ua"]          = sec_ch_ua
        if sec_ch_ua_mobile:
            h["sec-ch-ua-mobile"]   = sec_ch_ua_mobile
        if sec_ch_ua_platform:
            h["sec-ch-ua-platform"] = sec_ch_ua_platform
    if csrf:
        h["x-csrf-token"] = csrf
    return h


def _parse_body(resp) -> tuple[int | None, str]:
    """Roblox error envelope: { errors: [{code, message, userFacingMessage?}, ...] }.
    Returns (first_code, joined_message)."""
    try:
        js = resp.json()
    except Exception:
        return None, (resp.text or "")[:200]
    errs = js.get("errors") if isinstance(js, dict) else None
    if not errs or not isinstance(errs, list) or not errs:
        return None, json.dumps(js)[:200]
    first = errs[0] if isinstance(errs[0], dict) else {}
    code = first.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    msg = " | ".join(
        str(e.get("message") or e.get("userFacingMessage") or "")
        for e in errs if isinstance(e, dict)
    ) or json.dumps(errs)[:200]
    return code, msg


def _attempt_once(session, url: str, body: str, headers: dict, timeout_s: float):
    """Single POST + CSRF refresh dance. Returns (resp, refreshed_headers)."""
    resp = session.post(url, data=body, headers=headers, timeout=timeout_s)
    csrf = resp.headers.get("x-csrf-token")
    if csrf and resp.status_code == 403:
        headers["x-csrf-token"] = csrf
        # Re-attempt with the freshly-issued CSRF. If this STILL 403s, it's
        # not a CSRF problem — it's a challenge gate or auth denial.
        resp = session.post(url, data=body, headers=headers, timeout=timeout_s)
    return resp, headers


def _decode_meta(b64: str) -> dict:
    try:
        return json.loads(base64.b64decode(b64).decode())
    except Exception:
        return {}


def _encode_meta(meta: dict) -> str:
    return base64.b64encode(json.dumps(meta).encode()).decode()


def _submit_arkose_token(
    session: Any,
    headers: dict,
    *,
    challenge_id: str,
    unified_captcha_id: str,
    token: str,
    timeout_s: float,
) -> tuple[bool, str]:
    """POST the solved Arkose token to /challenge/v1/continue. Returns
    (ok, message). Roblox requires this validation step before the retry
    of the original endpoint will accept the token in its metadata header."""
    body = json.dumps({
        "challengeId":       challenge_id,
        "challengeType":     "captcha",
        "challengeMetadata": json.dumps({
            "unifiedCaptchaId": unified_captcha_id,
            "captchaToken":     token,
        }),
    })
    cont_headers = {**headers, "Content-Type": "application/json;charset=utf-8"}
    try:
        resp = session.post(CHALLENGE_CONTINUE_URL, data=body, headers=cont_headers, timeout=timeout_s)
    except Exception as e:
        return False, f"continue threw: {type(e).__name__}: {e}"
    if resp.status_code == 200:
        return True, "ok"
    try:
        js = resp.json()
    except Exception:
        js = {}
    return False, f"continue http {resp.status_code}: {str(js)[:160] or resp.text[:160]}"


def _solve_captcha_challenge(
    session: Any,
    *,
    challenge_id: str,
    meta_b64: str,
    headers: dict,
    arkose_solver: Any,
    arkose_public_key: str,
    arkose_surl: str,
    user_agent: str,
    proxy: str | None,
    timeout_s: float,
) -> tuple[str | None, str]:
    """Solve the FunCaptcha challenge attached to a JoinGroup attempt and
    return (signed_metadata_b64, status_message). The returned metadata is
    ready to drop into the `rblx-challenge-metadata` header of the retry
    POST. On any failure returns (None, message)."""
    meta = _decode_meta(meta_b64)
    blob = meta.get("dataExchangeBlob") or ""
    unified = meta.get("unifiedCaptchaId") or challenge_id
    if not blob:
        return None, "no dataExchangeBlob in challenge metadata"

    try:
        sol = arkose_solver.arkose(
            public_key    = arkose_public_key,
            surl          = arkose_surl,
            site          = "https://www.roblox.com",
            blob          = blob,
            cookies       = None,
            proxy         = proxy,
            location_href = "https://www.roblox.com/communities",
            referrer      = "https://www.roblox.com/communities",
            user_agent    = user_agent,
        )
    except Exception as e:
        return None, f"solver threw: {type(e).__name__}: {e}"

    if (sol or {}).get("status") != "done" or not sol.get("token"):
        return None, f"solver no-token: {(sol or {}).get('error') or sol}"

    token = sol["token"]

    ok, cont_msg = _submit_arkose_token(
        session, headers,
        challenge_id=challenge_id,
        unified_captcha_id=unified,
        token=token,
        timeout_s=timeout_s,
    )
    if not ok:
        return None, cont_msg

    # Build the validated metadata header for the retry. Roblox echoes
    # `unifiedCaptchaId` + the solved `captchaToken` back to itself.
    validated = {
        "unifiedCaptchaId": unified,
        "captchaToken":     token,
    }
    return _encode_meta(validated), "ok"


def join_group(
    session: Any,
    group_id: int | str,
    *,
    user_agent: str,
    accept_language: str | None = None,
    sec_ch_ua: str | None = None,
    sec_ch_ua_platform: str | None = None,
    sec_ch_ua_mobile: str | None = None,
    timeout_s: float = 15.0,
    max_attempts: int = 8,
    retry_sleep_s: float = 1.5,
    arkose_solver: Any = None,
    arkose_public_key: str = _ROBLOX_ARKOSE_PK,
    arkose_surl: str = _ROBLOX_ARKOSE_SURL,
    proxy: str | None = None,
) -> JoinResult:
    """Idempotently join `group_id` using the provided curl_cffi session
    (which must already carry .ROBLOSECURITY). Returns a JoinResult.

    Roblox's risk engine routes group-joins through one of three challenge
    types selected per-request (captcha / proofofwork / rostile). Each is
    expensive or impossible to solve programmatically without significant
    infrastructure:
      - captcha     → Arkose FunCaptcha (would need the JoinGroup pkey
                      and a paid solver call ~$0.002 each)
      - proofofwork → CPU-bound SHA256 hashcash; tractable but ~50–500ms
                      of compute plus undocumented endpoint params
      - rostile     → Roblox's in-house image puzzle, no public solver

    HOWEVER, the gate is **stochastic**. Roblox's risk score also factors
    in session age and recent activity, and a non-trivial fraction of
    attempts get waved through with no challenge. So the cheapest viable
    strategy is to retry the bare endpoint up to `max_attempts` times with
    a short backoff — eventually we hit a no-challenge window. This
    consumes only HTTP traffic, no captcha-solver spend.

    The retry loop returns immediately on success, already-member, or
    pending-approval (idempotent outcomes), and at end-of-budget it returns
    the most-informative failure (last challenge type seen)."""
    url = f"{GROUPS_URL}/v1/groups/{group_id}/users"
    body = "{}"

    base_headers = _build_headers(
        user_agent=user_agent,
        accept_language=accept_language,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_platform=sec_ch_ua_platform,
        sec_ch_ua_mobile=sec_ch_ua_mobile,
        csrf=None,
    )

    last_status   = 0
    last_code     = None
    last_message  = ""
    challenge_counts: dict[str, int] = {}

    for attempt in range(1, max_attempts + 1):
        # Reuse CSRF across attempts when we have one — saves a round-trip.
        headers = dict(base_headers)
        resp, headers = _attempt_once(session, url, body, headers, timeout_s)
        if "x-csrf-token" in headers:
            base_headers["x-csrf-token"] = headers["x-csrf-token"]

        if resp.status_code == 200:
            return JoinResult(ok=True, status=200, code=None, message="ok")

        code, raw_msg = _parse_body(resp)
        last_status  = resp.status_code
        last_code    = code
        last_message = raw_msg

        if resp.status_code == 400 and code in _ALREADY_MEMBER_CODES:
            return JoinResult(ok=True, status=400, code=code, message=raw_msg, already=True)
        if resp.status_code in (400, 403) and code in _PENDING_REQUEST_CODES:
            return JoinResult(ok=True, status=resp.status_code, code=code, message=raw_msg, pending=True)

        challenge_type = resp.headers.get("rblx-challenge-type")
        challenge_id   = resp.headers.get("rblx-challenge-id")
        challenge_meta = resp.headers.get("rblx-challenge-metadata")
        if challenge_type:
            challenge_counts[challenge_type] = challenge_counts.get(challenge_type, 0) + 1
            last_message = f"challenge_required ({challenge_type}): {raw_msg}"

            # Try to solve captcha challenges in-place. PoW and rostile are
            # not currently solvable here; we burn a retry hoping Roblox
            # rotates to a captcha or no-challenge on the next attempt.
            if (challenge_type == "captcha"
                    and arkose_solver is not None
                    and challenge_id
                    and challenge_meta):
                validated_meta, solve_msg = _solve_captcha_challenge(
                    session,
                    challenge_id      = challenge_id,
                    meta_b64          = challenge_meta,
                    headers           = headers,
                    arkose_solver     = arkose_solver,
                    arkose_public_key = arkose_public_key,
                    arkose_surl       = arkose_surl,
                    user_agent        = user_agent,
                    proxy             = proxy,
                    timeout_s         = timeout_s,
                )
                if validated_meta is not None:
                    # Re-POST the join with the solved-and-validated metadata.
                    # This mirrors signup's success path: /challenge/v1/continue
                    # validates the token, then the original endpoint accepts
                    # the same envelope echoed back in headers.
                    retry_headers = dict(headers)
                    retry_headers["rblx-challenge-id"]       = challenge_id
                    retry_headers["rblx-challenge-type"]     = "captcha"
                    retry_headers["rblx-challenge-metadata"] = validated_meta
                    retry_resp = session.post(url, data=body, headers=retry_headers, timeout=timeout_s)
                    if retry_resp.status_code == 200:
                        return JoinResult(ok=True, status=200, code=None, message="ok (solved)")
                    code2, raw_msg2 = _parse_body(retry_resp)
                    last_status, last_code, last_message = retry_resp.status_code, code2, raw_msg2
                    if retry_resp.status_code == 400 and code2 in _ALREADY_MEMBER_CODES:
                        return JoinResult(ok=True, status=400, code=code2, message=raw_msg2, already=True)
                    if retry_resp.status_code in (400, 403) and code2 in _PENDING_REQUEST_CODES:
                        return JoinResult(ok=True, status=retry_resp.status_code, code=code2, message=raw_msg2, pending=True)
                    # Token rejected — fall through to retry (Roblox may
                    # serve a different gate or simply re-issue captcha).
                    last_message = f"post-solve rejected: http {retry_resp.status_code} code={code2}: {raw_msg2[:120]}"
                else:
                    last_message = f"challenge_required (captcha; solve failed: {solve_msg})"

            if attempt < max_attempts:
                time.sleep(retry_sleep_s)
                continue

        # Non-challenge non-retryable failure (auth denied, banned, 5xx).
        # No point continuing — Roblox isn't going to change its mind here.
        if not challenge_type:
            break

    # Out of attempts. Summarise distribution so the caller can see if it's
    # always-rostile (hopeless for this session) vs. mixed (worth a paid
    # captcha solve later).
    if challenge_counts:
        summary = ", ".join(f"{t}×{n}" for t, n in sorted(challenge_counts.items()))
        last_message = f"challenge_required after {sum(challenge_counts.values())} attempts ({summary})"

    return JoinResult(ok=False, status=last_status, code=last_code, message=last_message)
