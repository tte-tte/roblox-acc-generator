from __future__ import annotations

import base64
import json
import threading as _t
import uuid as _uuid
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, log


AUTH_URL      = "https://auth.roblox.com"
CHALLENGE_URL = "https://apis.roblox.com/challenge/v1/continue"

SIGNUP_TIMEOUT_S = 10.0


@dataclass
class RobloxChallenge:
    challenge_id:       str
    challenge_type:     str
    session_id:         str
    unified_captcha_id: str
    blob:               str
    action_type:        str
    request_path:       str
    request_method:     str
    shared_parameters:  Any = None
    cookies:            str = ""


_DEFAULT_SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


class RobloxAuth:
    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        user_agent: str = _DEFAULT_SAFARI_UA,
        accept_language: str = "en-GB,en;q=0.9",
        sec_ch_ua: str | None = None,
        sec_ch_ua_platform: str = "\"macOS\"",
        sec_ch_ua_mobile: str = "?0",
    ):
        self.client             = client or HttpClient(impersonate="safari180")
        self.csrf_token:  str | None = None
        self.challenge:   RobloxChallenge | None = None
        self.user_agent         = user_agent
        self.accept_language    = accept_language
        self.sec_ch_ua          = sec_ch_ua
        self.sec_ch_ua_platform = sec_ch_ua_platform
        self.sec_ch_ua_mobile   = sec_ch_ua_mobile

    def _auth_headers(self) -> dict:
        is_chromium = bool(self.sec_ch_ua)
        h: dict = {
            "Content-Type": "application/json",
            "Accept":       "application/json, text/plain, */*",
        }
        if is_chromium:
            h["sec-ch-ua"]          = self.sec_ch_ua
            h["sec-ch-ua-mobile"]   = self.sec_ch_ua_mobile
            h["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        h["Sec-Fetch-Site"]    = "same-site"
        h["Accept-Language"]   = self.accept_language
        h["Accept-Encoding"]   = "gzip, deflate, br, zstd" if is_chromium else "gzip, deflate, br"
        h["Sec-Fetch-Mode"]    = "cors"
        h["Origin"]            = "https://www.roblox.com"
        h["User-Agent"]        = self.user_agent
        h["Referer"]           = "https://www.roblox.com/"
        h["Connection"]        = "keep-alive"
        h["Sec-Fetch-Dest"]    = "empty"
        if self.csrf_token:
            h["x-csrf-token"] = self.csrf_token
        return h

    def warmup_signup_page(self):
        s = self.client._session
        is_chromium = bool(self.sec_ch_ua)

        page_headers: dict = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if is_chromium:
            page_headers["sec-ch-ua"]          = self.sec_ch_ua
            page_headers["sec-ch-ua-mobile"]   = self.sec_ch_ua_mobile
            page_headers["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        page_headers["Accept-Language"] = self.accept_language
        page_headers["Accept-Encoding"] = "gzip, deflate, br, zstd" if is_chromium else "gzip, deflate, br"
        page_headers["User-Agent"]      = self.user_agent
        page_headers["Sec-Fetch-Dest"]  = "document"
        page_headers["Sec-Fetch-Mode"]  = "navigate"
        page_headers["Sec-Fetch-Site"]  = "none"

        try:
            s.get("https://www.roblox.com/", headers=page_headers)
        except Exception:
            pass

        jar = s.cookies
        jar.set("RBXcb",
                "RBXViralAcquisition%3Dfalse%26RBXSource%3Dfalse%26GoogleAnalytics%3Dfalse",
                domain=".roblox.com")
        jar.set("RBXPaymentsFlowContext",
                str(_uuid.uuid4()) + ",",
                domain=".roblox.com")

        api_headers: dict = {
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": self.accept_language,
            "Accept-Encoding": "gzip, deflate, br, zstd" if is_chromium else "gzip, deflate, br",
            "Origin":          "https://www.roblox.com",
            "Referer":         "https://www.roblox.com/",
            "User-Agent":      self.user_agent,
        }
        if is_chromium:
            api_headers["sec-ch-ua"]          = self.sec_ch_ua
            api_headers["sec-ch-ua-mobile"]   = self.sec_ch_ua_mobile
            api_headers["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        ca_headers = {**page_headers, "Sec-Fetch-Site": "same-origin", "Referer": "https://www.roblox.com/"}

        targets = [
            ("https://www.roblox.com/CreateAccount", ca_headers),
            ("https://apis.roblox.com/product-experimentation-platform/v1/projects/1/layers/Website.CreateAccount?urlLocale=en_us", api_headers),
            ("https://apis.roblox.com/guac-v2/v1/bundles/user-heartbeats?urlLocale=en_us",        api_headers),
            ("https://apis.roblox.com/guac-v2/v1/bundles/intl-auth-compliance?urlLocale=en_us",   api_headers),
        ]

        def _fire(url: str, headers: dict):
            try:
                s.get(url, headers=headers, timeout=15)
            except Exception:
                pass

        threads = [_t.Thread(target=_fire, args=(u, h), daemon=True) for u, h in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

    def validate_username(self, username: str, birthday_iso: str) -> tuple[bool, str]:
        """Pre-flight check via /v1/usernames/validate.

        Returns (available, message). Lets the caller skip /v2/signup (and any
        captcha solve) on names Roblox will reject anyway.

        Roblox response code: 0 = valid, 1 = taken, 2 = invalid/banned, etc.
        Also harvests x-csrf-token so the subsequent signup() call doesn't
        need to validate again.
        """
        s = self.client._session
        resp = s.post(
            f"{AUTH_URL}/v1/usernames/validate?urlLocale=en_us",
            data=json.dumps({"username": username, "birthday": birthday_iso, "context": "Signup"}),
            headers=self._auth_headers(),
            timeout=SIGNUP_TIMEOUT_S,
        )
        new_csrf = resp.headers.get("x-csrf-token")
        if new_csrf:
            self.csrf_token = new_csrf
        try:
            data = resp.json()
        except Exception:
            return False, f"http {resp.status_code}"
        return data.get("code") == 0, str(data.get("message") or "")

    def signup(self, username: str, password: str, birthday_iso: str, gender: int = 2) -> RobloxChallenge | None:
        s = self.client._session

        val_resp = s.post(
            f"{AUTH_URL}/v1/usernames/validate?urlLocale=en_us",
            data=json.dumps({"username": username, "birthday": birthday_iso, "context": "Signup"}),
            headers=self._auth_headers(),
            timeout=SIGNUP_TIMEOUT_S,
        )
        self.csrf_token = val_resp.headers.get("x-csrf-token") or self.csrf_token or ""

        body = {
            "username":               username,
            "password":               password,
            "birthday":               birthday_iso,
            "gender":                 gender,
            "isTosAgreementBoxChecked": True,
            "agreementIds": [
                "306cc852-3717-4996-93e7-086daafd42f6",
                "2ba6b930-4ba8-4085-9e8c-24b919701f15",
            ],
        }
        resp = s.post(
            f"{AUTH_URL}/v2/signup?urlLocale=en_us",
            data=json.dumps(body),
            headers={**self._auth_headers(), "Content-Type": "application/json;charset=utf-8"},
            timeout=SIGNUP_TIMEOUT_S,
        )

        new_csrf = resp.headers.get("x-csrf-token")
        if new_csrf and new_csrf != self.csrf_token:
            self.csrf_token = new_csrf
            resp = s.post(
                f"{AUTH_URL}/v2/signup?urlLocale=en_us",
                data=json.dumps(body),
                headers={**self._auth_headers(), "Content-Type": "application/json;charset=utf-8"},
            )

        if resp.status_code == 200:
            return None

        challenge_id      = resp.headers.get("rblx-challenge-id")
        challenge_type    = resp.headers.get("rblx-challenge-type")
        challenge_meta_b64 = resp.headers.get("rblx-challenge-metadata")

        if not challenge_id or not challenge_meta_b64:
            raise RobloxError(f"signup failed {resp.status_code}: {resp.text[:200]}")

        meta = json.loads(base64.b64decode(challenge_meta_b64).decode())

        if challenge_type == "captcha":
            return self._parse_captcha_challenge(challenge_id, meta)

        raise RobloxError(f"unexpected challenge type on signup: {challenge_type}")

    def _parse_captcha_challenge(self, challenge_id: str, meta: dict) -> RobloxChallenge:
        blob = meta.get("dataExchangeBlob", "")
        if not blob:
            raise RobloxError(f"no dataExchangeBlob in challenge: {meta}")

        jar = self.client._session.cookies
        try:
            cookies_str = "; ".join(f"{k}={v}" for k, v in jar.items())
        except Exception:
            cookies_str = str(jar)

        self.challenge = RobloxChallenge(
            challenge_id       = challenge_id,
            challenge_type     = "captcha",
            session_id         = meta.get("sessionId", ""),
            unified_captcha_id = meta.get("unifiedCaptchaId", challenge_id),
            blob               = blob,
            cookies            = cookies_str,
            action_type        = meta.get("actionType", "Signup"),
            request_path       = meta.get("requestPath", "/v2/signup"),
            request_method     = meta.get("requestMethod", "POST"),
            shared_parameters  = meta.get("sharedParameters"),
        )
        return self.challenge

    def submit_arkose_token(self, arkose_token: str) -> dict:
        if not self.challenge:
            raise RobloxError("no active challenge to submit token for")

        body = json.dumps({
            "challengeId":       self.challenge.challenge_id,
            "challengeType":     "captcha",
            "challengeMetadata": json.dumps({
                "unifiedCaptchaId": self.challenge.unified_captcha_id,
                "captchaToken":     arkose_token,
            }),
        })

        resp = self.client._session.post(
            CHALLENGE_URL,
            data=body,
            headers=self._auth_headers(),
        )
        return resp.json()


class RobloxError(Exception):
    pass
