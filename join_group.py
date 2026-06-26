#!/usr/bin/env python3.11
"""
Standalone batch group-joiner for accounts produced by main.py.

Reads accounts from output/accounts.txt (lines of `username:password:cookies`),
joins each account to a Roblox community group, and records successes in
output/joined.txt so re-runs skip already-joined accounts.

Usage:
    python3.11 join_group.py                       # uses [group].id from config.toml
    python3.11 join_group.py --group-id 34792238
    python3.11 join_group.py --threads 20 --accounts output/accounts.txt
    python3.11 join_group.py --dry-run             # parses + counts, doesn't POST

Notes:
  - The .ROBLOSECURITY cookie is extracted from each account line and
    injected into a fresh curl_cffi session, impersonating Chrome 146 (the
    same profile rotation main.py uses; any modern UA will do — group join
    is far less fingerprint-sensitive than signup).
  - Proxies from input/proxies.txt are rotated. Each account uses one
    9proxy sticky session; if a session is dead the join will fail and we
    move on (Roblox tolerates this; the cookie is what matters, not the IP).
  - Accept-Encoding intentionally omits brotli — curl_cffi sometimes
    de-frames brotli poorly in worker threads and Roblox is happy with
    gzip.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from modules.http import HttpClient
from modules.group_join import join_group, JoinResult
from modules.anticaptcha_client import AntiCaptchaClient
from modules.twocaptcha_client import TwoCaptchaClient


# Same Chrome 146 profile as main.py's [_BROWSER_PROFILES]["chrome"]. Hardcoded
# here because main.py defines them inline; copying avoids importing main and
# pulling in its argparse / banner side effects.
_CHROME_PROFILE = {
    "impersonate":        "chrome146",
    "user_agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "accept_language":    "en-US,en;q=0.9",
    "sec_ch_ua":          "\"Chromium\";v=\"146\", \"Not?A_Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
    "sec_ch_ua_platform": "\"Windows\"",
    "sec_ch_ua_mobile":   "?0",
}


_WRITE_LOCK = threading.Lock()


@dataclass
class AccountRow:
    username:        str
    password:        str
    roblosecurity:   str
    raw_cookies:     str
    line_no:         int


# ─── parsing ──────────────────────────────────────────────────────────────────


def _extract_roblosecurity(cookies_str: str) -> str | None:
    """Pull the .ROBLOSECURITY value out of a `;`-joined cookies blob. The
    cookie value itself contains `=` and other delimiters, so we use a
    permissive scan rather than a clean k=v split."""
    needle = ".ROBLOSECURITY="
    idx = cookies_str.find(needle)
    if idx < 0:
        return None
    start = idx + len(needle)
    # Cookie ends at the next "; " boundary (or EOL).
    end = cookies_str.find("; ", start)
    if end < 0:
        end = len(cookies_str)
    val = cookies_str[start:end].strip()
    return val or None


def load_accounts(path: str) -> list[AccountRow]:
    out: list[AccountRow] = []
    if not os.path.exists(path):
        print(f"! accounts file not found: {path}", file=sys.stderr)
        return out
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            # Format: username:password:cookies-string-with-many-colons
            # Split at most twice so the cookies portion stays intact.
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            username, password, cookies = parts
            rblx = _extract_roblosecurity(cookies)
            if not rblx:
                continue
            out.append(AccountRow(
                username      = username,
                password      = password,
                roblosecurity = rblx,
                raw_cookies   = cookies,
                line_no       = n,
            ))
    return out


def load_joined(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def load_proxies(path: str) -> list[str]:
    """Same normalization main.py uses: accept host:port[:user:pass] and
    return as proper http://user:pass@host:port URLs."""
    if not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "://" in line:
                out.append(line)
                continue
            parts = line.split(":")
            if len(parts) == 2:
                out.append(f"http://{parts[0]}:{parts[1]}")
            elif len(parts) >= 4:
                host, port, user, pw = parts[0], parts[1], parts[2], ":".join(parts[3:])
                out.append(f"http://{user}:{pw}@{host}:{port}")
    return out


def load_group_id(config_path: str) -> int | str | None:
    if not os.path.exists(config_path):
        return None
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    return ((cfg.get("group") or {}).get("id"))


def build_solver_from_config(config_path: str) -> Any:
    """Construct the same captcha solver main.py uses, picked by
    [run].solver in config.toml. Returns None on misconfiguration so the
    caller can fall back to retry-only joining."""
    if not os.path.exists(config_path):
        return None
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    solver_name = ((cfg.get("run") or {}).get("solver") or "anticaptcha").strip().lower()
    try:
        if solver_name == "2captcha":
            tc = cfg.get("twocaptcha") or {}
            return TwoCaptchaClient(
                api_key       = tc.get("api_key"),
                base_url      = tc.get("url"),
                poll_interval = float(tc.get("poll_interval_s", 5.0)),
                timeout_s     = float(tc.get("timeout_s", 900.0)),
            )
        ac = cfg.get("anticaptcha") or {}
        return AntiCaptchaClient(
            api_key       = ac.get("api_key"),
            base_url      = ac.get("url"),
            poll_interval = float(ac.get("poll_interval_s", 3.0)),
            timeout_s     = float(ac.get("timeout_s", 900.0)),
        )
    except Exception as e:
        print(f"! solver init failed ({solver_name}): {e}; group-join will run retry-only", file=sys.stderr)
        return None


# ─── per-account worker ──────────────────────────────────────────────────────


def _seed_session_cookies(client: HttpClient, roblosecurity: str) -> None:
    """Inject the account's .ROBLOSECURITY into the fresh session. Domain
    is set on .roblox.com so it's visible to both groups.roblox.com and
    auth.roblox.com (CSRF probe)."""
    client._session.cookies.set(
        ".ROBLOSECURITY",
        roblosecurity,
        domain=".roblox.com",
        path="/",
    )


def join_one(
    row: AccountRow,
    proxy: str | None,
    group_id: int | str,
    dry_run: bool,
    arkose_solver: Any = None,
) -> tuple[AccountRow, JoinResult | None, float]:
    t0 = time.time()
    if dry_run:
        return row, None, 0.0

    client = HttpClient(proxy=proxy, impersonate=_CHROME_PROFILE["impersonate"])
    try:
        _seed_session_cookies(client, row.roblosecurity)
        result = join_group(
            client._session,
            group_id,
            user_agent         = _CHROME_PROFILE["user_agent"],
            accept_language    = _CHROME_PROFILE["accept_language"],
            sec_ch_ua          = _CHROME_PROFILE["sec_ch_ua"],
            sec_ch_ua_platform = _CHROME_PROFILE["sec_ch_ua_platform"],
            sec_ch_ua_mobile   = _CHROME_PROFILE["sec_ch_ua_mobile"],
            arkose_solver      = arkose_solver,
            proxy              = proxy,
        )
    except Exception as e:
        return row, JoinResult(ok=False, status=0, code=None, message=f"{type(e).__name__}: {e}"), time.time() - t0
    finally:
        try:
            client.close()
        except Exception:
            pass

    return row, result, time.time() - t0


# ─── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch-join Roblox community group with previously-created accounts.")
    ap.add_argument("--config",    default="config.toml")
    ap.add_argument("--accounts",  default="output/accounts.txt")
    ap.add_argument("--joined",    default="output/joined.txt")
    ap.add_argument("--proxies",   default="input/proxies.txt")
    ap.add_argument("--group-id",  default=None, help="Override group id from config")
    ap.add_argument("--threads",   type=int, default=10)
    ap.add_argument("--dry-run",   action="store_true", help="Parse and report; do not POST")
    ap.add_argument("--no-solver", action="store_true", help="Skip captcha solving; rely on retry only")
    args = ap.parse_args()

    group_id = args.group_id or load_group_id(args.config)
    arkose_solver = None if args.no_solver else build_solver_from_config(args.config)
    if not group_id:
        print("! no group id provided (set [group].id in config.toml or pass --group-id)", file=sys.stderr)
        return 2

    accounts = load_accounts(args.accounts)
    if not accounts:
        print(f"! no usable accounts in {args.accounts} (need lines like username:password:cookies)", file=sys.stderr)
        return 2

    joined_set = load_joined(args.joined)
    proxies = load_proxies(args.proxies)

    pending = [a for a in accounts if a.username not in joined_set]
    skipped = len(accounts) - len(pending)

    print(f"━━━ Roblox Group Joiner ━━━")
    print(f"  group       {group_id}")
    print(f"  accounts    {len(accounts)} total ({skipped} already joined → skipped)")
    print(f"  pending     {len(pending)}")
    print(f"  proxies     {len(proxies)}  ({'rotating' if proxies else 'direct'})")
    print(f"  threads     {args.threads}")
    print(f"  solver      {type(arkose_solver).__name__ if arkose_solver else 'off (retry-only)'}")
    print(f"  output      {args.joined}")
    print(f"  mode        {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print()

    if not pending:
        return 0

    # Stable proxy ordering per worker — shuffle once then round-robin so
    # repeated runs (post-failure retry, etc.) don't keep hammering the
    # same gateway with the same accounts.
    if proxies:
        random.shuffle(proxies)
    def _proxy_for(i: int) -> str | None:
        if not proxies:
            return None
        return proxies[i % len(proxies)]

    os.makedirs(os.path.dirname(args.joined) or ".", exist_ok=True)

    counts = {"joined": 0, "already": 0, "pending": 0, "failed": 0}
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as ex:
        futs = [
            ex.submit(join_one, row, _proxy_for(i), group_id, args.dry_run, arkose_solver)
            for i, row in enumerate(pending)
        ]
        for fut in as_completed(futs):
            row, result, elapsed = fut.result()
            if args.dry_run:
                print(f"  [dry-run] would join {row.username}")
                continue

            assert result is not None
            if result.already:
                counts["already"] += 1
                tag, color = "ALREADY", "\033[33m"
            elif result.pending:
                counts["pending"] += 1
                tag, color = "PENDING", "\033[33m"
            elif result.ok:
                counts["joined"] += 1
                tag, color = "JOINED ", "\033[32m"
            else:
                counts["failed"] += 1
                tag, color = "FAILED ", "\033[31m"

            print(f"  {color}{tag}\033[0m {row.username:<24} ({elapsed*1000:.0f}ms)  {result}")

            # On any success-class outcome (joined OR already-member OR
            # pending-approval) record the username so subsequent runs skip
            # it. Approval-pending groups still produce a "request submitted"
            # state that won't progress until a group admin acts.
            if result.ok:
                with _WRITE_LOCK:
                    with open(args.joined, "a", encoding="utf-8") as f:
                        f.write(f"{row.username}\n")

    wall = time.time() - t_start
    print()
    print(f"━━━ Done in {wall:.1f}s ━━━")
    print(f"  joined  {counts['joined']}   already {counts['already']}   pending {counts['pending']}   failed {counts['failed']}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
