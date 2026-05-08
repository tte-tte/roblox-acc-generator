from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import unquote, urlparse

import requests


class SolverError(Exception):
    pass


_DEFAULT_BASE = "https://api.solvex.run"

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
        "proxyPort":    port,
    }
    if u.username:
        out["proxyLogin"] = unquote(u.username)
    if u.password:
        out["proxyPassword"] = unquote(u.password)
    return out


class SolvexClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        poll_interval: float = 0.5,
        timeout_s: float = 120.0,
        user_agent: str | None = None,
        use_http3: bool = True,
    ):
        self.base          = (base_url or os.environ.get("SOLVEX_URL", _DEFAULT_BASE)).rstrip("/")
        self.key           = api_key or os.environ.get("SOLVEX_API_KEY")
        self.poll_interval = poll_interval
        self.timeout_s     = timeout_s
        self.user_agent    = user_agent or _DEFAULT_UA
        self.use_http3     = use_http3
        if not self.key:
            raise SolverError(
                "Solvex API key missing — set [solvex].api_key in config.toml "
                "or the SOLVEX_API_KEY environment variable."
            )

    def arkose(
        self,
        public_key: str,
        surl: str,
        site: str,
        blob: str,
        cookies: str | None = None,
        proxy: str | None = None,
        location_href: str | None = None,
        referrer: str | None = None,
        solve_pow: bool = False,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        task: dict[str, Any] = {
            "type":             "FunCaptchaTask",
            "websiteURL":       location_href or site,
            "websitePublicKey": public_key,
            "userAgent":        user_agent or self.user_agent,
            "useHttp3":         self.use_http3,
            "solvePow":         bool(solve_pow),
        }
        if blob:
            task["data"] = blob
        if cookies:
            task["cookies"] = cookies
        task.update(_parse_proxy(proxy))

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

    def _poll(self, task_id: str) -> dict[str, Any]:
        deadline = time.time() + self.timeout_s
        body = {"clientKey": self.key, "taskId": task_id}

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
                sol             = js.get("solution") or {}
                token           = sol.get("token")
                challenge_type  = (sol.get("challengeType") or "").lower()
                verified        = bool(sol.get("verified", True))
                pow_required    = challenge_type == "pow"
                suppressed      = challenge_type == "suppressed" or (pow_required and verified)

                if not token or not verified:
                    return {
                        "status":       "done",
                        "token":        token,
                        "suppressed":   False,
                        "pow_required": pow_required,
                        "pow_solved":   False,
                        "error":        f"unverified token (challenge={challenge_type})",
                    }
                return {
                    "status":       "done",
                    "token":        token,
                    "suppressed":   suppressed,
                    "pow_required": pow_required,
                    "pow_solved":   pow_required and verified,
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
