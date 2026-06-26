"""
Anti-Captcha client for Roblox FunCaptcha.

Uses the original antiCaptcha JSON protocol (same envelope as 2Captcha /
CapMonster / Solvex) so this file mirrors the public surface of
twocaptcha_client.TwoCaptchaClient — `.arkose()` returns the same dict
shape main.py already consumes. To switch solvers, just toggle
`[run].solver` in config.toml.

Differences vs. 2Captcha worth knowing:

  - Endpoint host: api.anti-captcha.com
  - Pricing: ~$0.70–$2 per 1000 (typically cheaper than 2Captcha at load)
  - Larger and more battle-tested human pool; their fingerprint pools for
    Arkose have historically been maintained more aggressively than the
    AI-only solvers (CapMonster, Solvex).
  - FunCaptchaTask additionally accepts an optional `cookies` task field
    (semicolon-joined "name=value" pairs). Some Arkose deployments key
    session validation off the cookie jar that minted the BDA blob, so
    forwarding our session cookies meaningfully helps token acceptance
    when Roblox's challenge step is in a defensive mood. 2Captcha does
    NOT support this field.
  - Poll cadence: their workers ack faster than 2Captcha; 3s is a safe
    default vs. 5s for 2Captcha.
  - Recommended docs: https://anti-captcha.com/apidoc/task-types/FunCaptchaTaskProxyless
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from typing import Any
from urllib.parse import unquote, urlparse

import requests


# Tiny TTL cache so we don't hammer DNS for every signup attempt.
# Anti-Captcha rejects hostnames in proxyAddress and only accepts IPs, so
# every task created with a hostname-style proxy gets resolved here first.
_DNS_CACHE: dict[str, tuple[str, float]] = {}
_DNS_TTL_S = 300.0


def _resolve_host(host: str) -> str:
    """Resolve a hostname to an IPv4 string. Pass-through if already an IP."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and (now - cached[1]) < _DNS_TTL_S:
        return cached[0]
    info = socket.getaddrinfo(host, None, family=socket.AF_INET, proto=socket.IPPROTO_TCP)
    if not info:
        raise SolverError(f"DNS resolution failed for {host}")
    ip = info[0][4][0]
    _DNS_CACHE[host] = (ip, now)
    return ip


class SolverError(Exception):
    pass


_DEFAULT_BASE = "https://api.anti-captcha.com"

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


def _parse_proxy(proxy: str | None) -> dict:
    if not proxy:
        return {}
    u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not u.hostname or not u.port:
        return {}
    scheme = (u.scheme or "http").lower()
    if scheme == "socks5h":
        scheme = "socks5"
    elif scheme == "socks4a":
        scheme = "socks4"
    # Anti-Captcha requires a literal IP in proxyAddress (their createTask
    # validator hard-rejects hostnames). Resolve here once per host with
    # the module-level TTL cache.
    try:
        proxy_ip = _resolve_host(u.hostname)
    except (socket.gaierror, OSError):
        # Let createTask surface a clearer error than a silent connect fail.
        proxy_ip = u.hostname
    out: dict[str, Any] = {
        "proxyType":    scheme,
        "proxyAddress": proxy_ip,
        "proxyPort":    u.port,
    }
    if u.username:
        out["proxyLogin"] = unquote(u.username)
    if u.password:
        out["proxyPassword"] = unquote(u.password)
    return out


def _extract_subdomain(surl: str | None) -> str | None:
    if not surl:
        return None
    u = urlparse(surl if "://" in surl else f"https://{surl}")
    return u.hostname or None


def _use_proxy_env_enabled() -> bool:
    raw = os.environ.get("ANTICAPTCHA_USE_PROXY", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class AntiCaptchaClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        poll_interval: float = 3.0,
        timeout_s: float = 180.0,
        user_agent: str | None = None,
        use_http3: bool = True,  # accepted for interface parity; no-op
    ):
        self.base          = (base_url or os.environ.get("ANTICAPTCHA_URL", _DEFAULT_BASE)).rstrip("/")
        self.key           = api_key or os.environ.get("ANTICAPTCHA_API_KEY")
        self.poll_interval = poll_interval
        self.timeout_s     = timeout_s
        self.user_agent    = user_agent or _DEFAULT_UA
        if not self.key:
            raise SolverError(
                "Anti-Captcha API key missing — set [anticaptcha].api_key in "
                "config.toml or the ANTICAPTCHA_API_KEY environment variable."
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
        referrer: str | None = None,  # noqa: ARG002 — interface parity
        solve_pow: bool = False,       # noqa: ARG002 — interface parity
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
            # Same shape Anti-Captcha's docs spec: a JSON-stringified
            # object with the "blob" property.
            task["data"] = json.dumps({"blob": blob}, separators=(",", ":"))
        if proxy_fields:
            task.update(proxy_fields)
            # Anti-Captcha supports forwarding the originating session's
            # cookies — only meaningful for the with-proxy task type.
            if cookies:
                task["cookies"] = cookies

        body = {"clientKey": self.key, "task": task, "softId": 0}
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

        # Brief initial wait — Anti-Captcha workers are quick to ack but
        # rarely finish a FunCaptcha in <3s.
        time.sleep(min(self.poll_interval, 3.0))

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
        desc = (
            js.get("errorDescription")
            or js.get("errorCode")
            or f"errorId={err_id}"
        )
        return _fail(f"{label}: {desc}")
    return js
