from __future__ import annotations

from curl_cffi import requests as curl_requests


def _is_socks(proxy: str | None) -> bool:
    if not proxy:
        return False
    p = proxy.lower()
    return p.startswith(("socks4://", "socks4a://", "socks5://", "socks5h://"))


class HttpClient:
    def __init__(self, proxy: str | None = None, impersonate: str = "safari180"):
        self.proxy = proxy
        self._session = curl_requests.Session(impersonate=impersonate, default_headers=False)
        if proxy:
            self._session.proxies = {"https": proxy, "http": proxy}

    def get(self, url, params=None, headers=None):
        resp = self._session.get(url, params=params, headers=headers or {})
        return resp.json()

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass


class _Log:
    def ok(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def warn(self, *a, **kw): pass
    def success(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def green(self, s): return str(s)
    def red(self, s): return str(s)


log = _Log()
