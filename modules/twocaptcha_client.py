"""
2Captcha client for Roblox FunCaptcha.

2Captcha's v2 JSON API is antiCaptcha-compatible (same clientKey + task
envelope as Solvex / CapMonster / AntiCaptcha) so this module mirrors the
prior client interfaces used by main.py — same constructor signature, same
`arkose()` method, same return-dict shape.

Key differences worth knowing if you ever swap solvers:

  - 2Captcha solves FunCaptcha primarily with HUMAN workers, with AI as a
    fallback (~2000+ workers online per their public stats). The failure
    mode "image too noisy" you get from AI-classifier-only solvers like
    CapMonster does NOT apply — humans solve any puzzle Arkose serves.
  - Two task type variants:
       FunCaptchaTask          → solver loads the captcha through YOUR proxy
       FunCaptchaTaskProxyless → solver uses 2Captcha's own IP pool
    This client auto-selects based on whether a proxy is supplied. You can
    force proxyless even when a local proxy is configured by setting the
    env var TWOCAPTCHA_USE_PROXY=0 — useful when your residential pool is
    Arkose-flagged but the rest of the signup flow still needs the proxy.
  - `data` field is a JSON-encoded `{"blob":"..."}` string, identical to
    CapMonster's format.
  - `cookies` is NOT a supported task field on 2Captcha. The cookies
    argument to .arkose() is accepted for interface parity and silently
    dropped — the BDA blob alone carries the session binding.
  - Pricing varies $1.45–$50 per 1000 with service load. The response
    `cost` field reports actual price charged per solve. Refund-on-
    failure is automatic for `errorId: 12`.
  - Polling cadence: 2Captcha recommends ~5s between getTaskResult calls
    (vs 0.5s for Solvex/CapMonster); default `poll_interval` is bumped
    accordingly.

Endpoints used:
  POST https://api.2captcha.com/createTask
  POST https://api.2captcha.com/getTaskResult
  POST https://api.2captcha.com/getBalance

Docs: https://2captcha.com/api-docs/arkoselabs-funcaptcha
"""
from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import unquote, urlparse

import requests


class SolverError(Exception):
    pass


_DEFAULT_BASE = "https://api.2captcha.com"

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

_PORT_TO_PROXY_TYPE = {1080: "socks5", 8080: "http"}


def _parse_proxy(proxy: str | None) -> dict:
    if not proxy:
        return {}
    u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host = u.hostname
    port = u.port
    if not host or not port:
        return {}
    scheme = (u.scheme or "").lower()
    if scheme == "socks5h":
        scheme = "socks5"
    elif scheme == "socks4a":
        scheme = "socks4"
    proxy_type = _PORT_TO_PROXY_TYPE.get(port, scheme or "http")
    out: dict[str, Any] = {
        "proxyType":    proxy_type,
        "proxyAddress": host,
        # 2Captcha's docs example types proxyPort as string; their API
        # actually accepts both int and string. Send as int to match the
        # antiCaptcha schema everyone else uses.
        "proxyPort":    port,
    }
    if u.username:
        out["proxyLogin"] = unquote(u.username)
    if u.password:
        out["proxyPassword"] = unquote(u.password)
    return out


def _extract_subdomain(surl: str | None) -> str | None:
    """Pull the host out of an arkoselabs URL so we can pass it as
    `funcaptchaApiJSSubdomain`. e.g.
    "https://arkoselabs.roblox.com" -> "arkoselabs.roblox.com"."""
    if not surl:
        return None
    u = urlparse(surl if "://" in surl else f"https://{surl}")
    return u.hostname or None


def _use_proxy_env_enabled() -> bool:
    """Honour TWOCAPTCHA_USE_PROXY=0 / false / no to force proxyless mode."""
    raw = os.environ.get("TWOCAPTCHA_USE_PROXY", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class TwoCaptchaClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        poll_interval: float = 5.0,
        timeout_s: float = 180.0,
        user_agent: str | None = None,
        use_http3: bool = True,  # accepted for interface parity; no-op
    ):
        self.base          = (base_url or os.environ.get("TWOCAPTCHA_URL", _DEFAULT_BASE)).rstrip("/")
        self.key           = api_key or os.environ.get("TWOCAPTCHA_API_KEY")
        self.poll_interval = poll_interval
        self.timeout_s     = timeout_s
        self.user_agent    = user_agent or _DEFAULT_UA
        if not self.key:
            raise SolverError(
                "2Captcha API key missing — set [twocaptcha].api_key in "
                "config.toml or the TWOCAPTCHA_API_KEY environment variable."
            )

    def arkose(
        self,
        public_key: str,
        surl: str,
        site: str,
        blob: str,
        cookies: str | None = None,  # accepted but dropped — see module docstring
        proxy: str | None = None,
        location_href: str | None = None,
        referrer: str | None = None,  # noqa: ARG002 — kept for interface parity
        solve_pow: bool = False,       # noqa: ARG002 — kept for interface parity
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        proxy_fields = _parse_proxy(proxy) if _use_proxy_env_enabled() else {}
        task_type = "FunCaptchaTask" if proxy_fields else "FunCaptchaTaskProxyless"

        task: dict[str, Any] = {
            "type":             task_type,
            "websiteURL":       location_href or site,
            "websitePublicKey": public_key,
            "userAgent":        user_agent or self.user_agent,
        }
        subdomain = _extract_subdomain(surl)
        if subdomain:
            task["funcaptchaApiJSSubdomain"] = subdomain
        if blob:
            # Per 2Captcha docs the `data` field is a string that JSON.stringify
            # would have produced — same shape as CapMonster.
            task["data"] = json.dumps({"blob": blob}, separators=(",", ":"))
        if proxy_fields:
            task.update(proxy_fields)

        body = {"clientKey": self.key, "task": task}
        try:
            r = requests.post(
                f"{self.base}/createTask",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
        except requests.RequestException as e:
            return _fail(f"createTask network error: {e}")

        envelope = _decode(r, "createTask")
        if "error" in envelope:
            return envelope

        task_id = envelope.get("taskId")
        if not task_id:
            return _fail(f"createTask returned no taskId: {envelope}")

        return self._poll(task_id)

    def _poll(self, task_id: int | str) -> dict[str, Any]:
        deadline = time.time() + self.timeout_s
        body = {"clientKey": self.key, "taskId": task_id}

        # 2Captcha recommends a short initial wait before the first poll —
        # human workers virtually never finish in under 5s, so polling
        # earlier just wastes API calls and risks rate-limit warnings.
        time.sleep(min(self.poll_interval, 5.0))

        while time.time() < deadline:
            try:
                r = requests.post(
                    f"{self.base}/getTaskResult",
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
            except requests.RequestException as e:
                return _fail(f"poll network error: {e}")

            js = _decode(r, "poll")
            if "error" in js:
                return js

            if js.get("status") == "ready":
                sol   = js.get("solution") or {}
                token = sol.get("token")
                if not token:
                    return {
                        "status":       "done",
                        "token":        None,
                        "suppressed":   False,
                        "pow_required": False,
                        "pow_solved":   False,
                        "error":        f"empty token in solution: {sol}",
                    }
                # 2Captcha doesn't expose challenge-type metadata. Any valid
                # token is treated as "suppressed" so main.py's success
                # branch fires.
                return {
                    "status":       "done",
                    "token":        token,
                    "suppressed":   True,
                    "pow_required": False,
                    "pow_solved":   False,
                }

            time.sleep(self.poll_interval)

        return _fail(f"timeout waiting for task {task_id} after {self.timeout_s:.0f}s")


def _fail(msg: str) -> dict[str, Any]:
    return {
        "status":       "failed",
        "error":        msg,
        "suppressed":   False,
        "pow_required": False,
        "token":        None,
    }


def _decode(r: requests.Response, label: str) -> dict[str, Any]:
    if r.status_code >= 400:
        return _fail(f"{label} HTTP {r.status_code}: {r.text[:200]}")
    try:
        js = r.json()
    except ValueError:
        return _fail(f"{label} invalid JSON: {r.text[:200]}")
    err_id = js.get("errorId")
    if err_id:
        desc = js.get("errorDescription") or js.get("errorCode") or f"errorId={err_id}"
        return _fail(f"{label}: {desc}")
    return js
