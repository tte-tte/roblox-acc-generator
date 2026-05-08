from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import random
import signal
import string
import sys
import threading
import time
import tomllib
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from modules.http import HttpClient
from modules.logger import UI, C, c
from modules.roblox import RobloxAuth, RobloxError
from modules.solvex_client import SolvexClient, SolverError

_ARKOSE_PK = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
_ARKOSE_SURL = "https://arkoselabs.roblox.com"
_ROBLOX_SITE = "https://www.roblox.com"
_AUTH_URL = "https://auth.roblox.com"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WRITE_LOCK = threading.Lock()


def _resolve(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(_SCRIPT_DIR, p)


_BROWSER_PROFILES: dict[str, dict] = {
    "safari": {
        "impersonate":        "safari180",
        "user_agent":         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "accept_language":    "en-GB,en;q=0.9",
        "sec_ch_ua":          None,
        "sec_ch_ua_platform": "\"macOS\"",
        "sec_ch_ua_mobile":   "?0",
    },
    "chrome": {
        "impersonate":        "chrome146",
        "user_agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "accept_language":    "en-US,en;q=0.9",
        "sec_ch_ua":          "\"Chromium\";v=\"146\", \"Not?A_Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
        "sec_ch_ua_platform": "\"Windows\"",
        "sec_ch_ua_mobile":   "?0",
    },
}


def pick_browser(cfg_val) -> tuple[str, dict]:
    if isinstance(cfg_val, str):
        key = cfg_val.strip().lower()
        if key in _BROWSER_PROFILES:
            return key, _BROWSER_PROFILES[key]
    name = random.choice(list(_BROWSER_PROFILES.keys()))
    return name, _BROWSER_PROFILES[name]


_ADJ = [
    "Swift", "Brave", "Clever", "Epic", "Fuzzy", "Glitch", "Hyper", "Jolly",
    "Keen", "Lucky", "Mystic", "Neon", "Orbit", "Pixel", "Quirky", "Rapid",
    "Savage", "Turbo", "Ultra", "Vivid", "Wild", "Zen",
]
_NOUN = [
    "Arrow", "Blaze", "Comet", "Dash", "Echo", "Falcon", "Glide", "Hawk",
    "Ion", "Jet", "King", "Lynx", "Max", "Nova", "Owl", "Pulse", "Quest",
    "Raven", "Spark", "Titan", "Viper", "Wolf",
]


def gen_username() -> str:
    suffix = "".join(random.choices("0123456789abcdef", k=4))
    return f"{random.choice(_ADJ)}{random.choice(_NOUN)}{suffix}"[:20]


def gen_password() -> str:
    chars = (
        random.choices(string.ascii_lowercase, k=6)
        + random.choices(string.ascii_uppercase, k=3)
        + random.choices(string.digits, k=3)
        + random.choices("!_-", k=1)
    )
    random.shuffle(chars)
    return "".join(chars)


def gen_birthday() -> str:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc)
    age = random.randint(16, 40)
    year = today.year - age
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}T22:00:00.000Z"


def gen_gender() -> int:
    return random.choices([1, 2, 3], weights=[45, 45, 10])[0]


def _is_auto(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("", "auto", "random")
    return False


def resolve_birthday(cfg_val) -> str:
    return gen_birthday() if _is_auto(cfg_val) else str(cfg_val)


def resolve_gender(cfg_val) -> int:
    if _is_auto(cfg_val):
        return gen_gender()
    try:
        g = int(cfg_val)
        return g if g in (1, 2, 3) else gen_gender()
    except (TypeError, ValueError):
        return gen_gender()


def _normalize_proxy_line(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    if "@" in line:
        return f"http://{line}"
    return None


def load_proxies(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            p = _normalize_proxy_line(raw)
            if p:
                out.append(p)
    return out


@dataclass
class Attempt:
    worker: int
    username: str = ""
    password: str = ""
    proxy: str | None = None
    browser: str = ""
    created: bool = False
    suppressed: bool = False
    pow_required: bool = False
    cookies: str = ""
    error: str | None = None
    duration_ms: int = 0
    captcha_ms: int = 0


def run_one(
    worker: int,
    solver: SolvexClient,
    proxy: str | None,
    birthday_cfg,
    gender_cfg,
    debug: bool,
    ui: UI,
    retry_on_failure: bool = False,
    solve_pow: bool = False,
    browser_cfg: str = "auto",
    check_username: bool = False,
) -> Attempt:
    a = Attempt(worker=worker, proxy=proxy)
    a.username = gen_username()
    a.password = gen_password()
    birthday = resolve_birthday(birthday_cfg)
    gender = resolve_gender(gender_cfg)
    t0 = time.time()

    browser_name, profile = pick_browser(browser_cfg)
    a.browser = browser_name

    client = HttpClient(proxy=proxy, impersonate=profile["impersonate"])
    auth = RobloxAuth(
        client             = client,
        user_agent         = profile["user_agent"],
        accept_language    = profile["accept_language"],
        sec_ch_ua          = profile["sec_ch_ua"],
        sec_ch_ua_platform = profile["sec_ch_ua_platform"],
        sec_ch_ua_mobile   = profile["sec_ch_ua_mobile"],
    )

    try:
        auth.warmup_signup_page()

        MAX_NAME_RETRIES = 5
        retry_resp = None
        suppress_fail_log = False

        if check_username:
            for _ in range(MAX_NAME_RETRIES):
                ok, _vmsg = auth.validate_username(a.username, birthday)
                if ok:
                    break
                a.username = gen_username()
                auth.challenge = None
            else:
                raise RuntimeError(f"{MAX_NAME_RETRIES} username pre-validations exhausted (last: {a.username})")

        for _ in range(MAX_NAME_RETRIES):
            try:
                challenge = auth.signup(a.username, a.password, birthday, gender)
            except RobloxError as e:
                msg = str(e)
                if "UsernameTaken" in msg or "UsernameInvalid" in msg:
                    a.username = gen_username()
                    auth.challenge = None
                    continue
                raise

            if challenge is None:
                jar = client._session.cookies
                a.cookies = "; ".join(f"{k}={v}" for k, v in jar.items())
                a.created = True
                ui.success(worker, "DIRECT", a.username, f"{a.username}:{a.password}", C.GREEN)
                return a

            _captcha_t0 = time.time()
            arkose_resp = solver.arkose(
                public_key=_ARKOSE_PK,
                surl=_ARKOSE_SURL,
                site=_ROBLOX_SITE,
                blob=challenge.blob,
                cookies=challenge.cookies,
                proxy=proxy,
                location_href="https://www.roblox.com/CreateAccount",
                referrer="https://www.roblox.com/CreateAccount",
                solve_pow=solve_pow,
                user_agent=profile["user_agent"],
            )
            a.captcha_ms += int((time.time() - _captcha_t0) * 1000)

            if arkose_resp.get("status") != "done":
                a.error = (arkose_resp.get("error") or "solver task failed")[:200]
                ui.event(worker, "SOLVER", f"{a.username} — {a.error}", C.RED)
                return a

            a.suppressed = bool(arkose_resp.get("suppressed"))
            a.pow_required = bool(arkose_resp.get("pow_required"))

            if not a.suppressed:
                pow_note = "pow_ok" if arkose_resp.get("pow_solved") else (
                    "pow_required" if a.pow_required else "no_pow"
                )
                a.error = f"Not suppressed ({pow_note})"
                ui.event(worker, "CHALLENGED", f"{a.username} — {a.error}", C.YELLOW)
                return a

            token = arkose_resp["token"]

            try:
                auth.submit_arkose_token(token)
            except Exception:
                pass

            meta_b64 = base64.b64encode(json.dumps({
                "unifiedCaptchaId": challenge.unified_captcha_id,
                "captchaToken": token,
            }).encode()).decode()

            body = json.dumps({
                "username": a.username,
                "password": a.password,
                "birthday": birthday,
                "gender": gender,
                "isTosAgreementBoxChecked": True,
                "agreementIds": [
                    "306cc852-3717-4996-93e7-086daafd42f6",
                    "2ba6b930-4ba8-4085-9e8c-24b919701f15",
                ],
            })
            headers = {
                **auth._auth_headers(),
                "Content-Type": "application/json;charset=utf-8",
                "rblx-challenge-id": challenge.challenge_id,
                "rblx-challenge-type": "captcha",
                "rblx-challenge-metadata": meta_b64,
            }
            retry_resp = client._session.post(
                f"{_AUTH_URL}/v2/signup?urlLocale=en_us",
                data=body,
                headers=headers,
                timeout=30.0,
            )

            if retry_resp.status_code == 200:
                break

            text = retry_resp.text or ""
            if retry_resp.status_code == 403 and (
                "UsernameInvalid" in text or "UsernameTaken" in text
            ):
                if not retry_on_failure:
                    a.error = f"{a.username} flagged post-captcha (captcha={a.captcha_ms}ms)"
                    ui.event(worker, "RENAME", a.error, C.YELLOW)
                    suppress_fail_log = True
                    break
                ui.event(worker, "RENAME", f"{a.username} flagged post-captcha — retrying", C.YELLOW)
                a.username = gen_username()
                auth.challenge = None
                continue

            break
        else:
            raise RuntimeError(f"{MAX_NAME_RETRIES} username retries exhausted (last: {a.username})")

        a.created = retry_resp is not None and retry_resp.status_code == 200

        if a.created:
            jar = client._session.cookies
            a.cookies = "; ".join(f"{k}={v}" for k, v in jar.items())
            tag = "SUPPRESS" if a.suppressed else ("POW" if a.pow_required else "SOLVED")
            ui.success(
                worker, tag, a.username,
                f"{a.username}:{a.password}  (captcha={a.captcha_ms}ms · {a.browser})",
                C.GREEN,
            )
        elif not suppress_fail_log:
            a.error = f"signup_status={retry_resp.status_code}: {retry_resp.text[:120]}"
            ui.event(worker, "FAIL", f"{a.username} — {a.error}  (captcha={a.captcha_ms}ms)", C.YELLOW)

    except SolverError as e:
        a.error = f"SolverError: {e}"
        ui.event(worker, "API", a.error, C.RED)
    except Exception as e:
        a.error = f"{type(e).__name__}: {e}"
        ui.event(worker, "ERROR", a.error, C.RED)
        if debug:
            traceback.print_exc()
    finally:
        a.duration_ms = int((time.time() - t0) * 1000)
        client.close()

    return a


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _g(cfg: dict, *keys, default=None):
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _write_result(a: Attempt, accounts_path: str, cookies_path: str):
    if not a.created:
        return
    line = f"{a.username}:{a.password}"
    if a.cookies:
        line += f":{a.cookies}"
    with _WRITE_LOCK:
        with open(accounts_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if a.cookies:
            with open(cookies_path, "a", encoding="utf-8") as f:
                f.write(f"{a.cookies}\n")


def main():
    ap = argparse.ArgumentParser(description="Roblox account creator (Solvex edition)")
    ap.add_argument("--config", default="config.toml", metavar="FILE")
    ap.add_argument("--threads", type=int, metavar="N")
    ap.add_argument("-n", "--count", type=int, metavar="N", help="accounts to create; 0 = infinite")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--compact", action="store_true", help="single-line live status bar")
    ap.add_argument("--no-compact", action="store_true")
    ap.add_argument("--solvex-url", metavar="URL")
    ap.add_argument("--solvex-key", metavar="KEY", help="Solvex API key (sk_…)")
    ap.add_argument("--proxy-mode", choices=["pool", "fixed", "none"])
    ap.add_argument("--proxy-file", metavar="FILE")
    ap.add_argument("--proxy-url", metavar="URL")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--output-dir", metavar="DIR")
    args = ap.parse_args()

    cfg = load_config(_resolve(args.config))

    threads       = args.threads  if args.threads  is not None else int(_g(cfg, "run", "threads",          default=8))
    count         = args.count    if args.count    is not None else int(_g(cfg, "run", "count",            default=20))
    debug         = args.debug    or bool(_g(cfg, "run", "debug",                                          default=False))
    compact       = bool(_g(cfg, "run", "compact",                                                         default=False))
    birthday_cfg  = _g(cfg, "run", "birthday",   default="auto")
    gender_cfg    = _g(cfg, "run", "gender",     default="auto")
    browser_cfg   = str(_g(cfg, "run", "browser", default="auto"))
    check_user    = bool(_g(cfg, "run", "check_username",                                                  default=False))
    retry_on_fail = bool(_g(cfg, "run", "retry_on_failure",                                                default=False))

    if args.compact:
        compact = True
    if args.no_compact:
        compact = False

    timeout_s     = float(_g(cfg, "solvex", "timeout_s",        default=120))
    poll_interval = float(_g(cfg, "solvex", "poll_interval_s",  default=0.5))
    solve_pow     = bool(_g(cfg,  "solvex", "solve_pow",         default=False))
    solvex_url    = args.solvex_url or _g(cfg, "solvex", "url",     default="https://api.solvex.run")
    solvex_key    = args.solvex_key or _g(cfg, "solvex", "api_key", default=None)
    disable_http3 = bool(_g(cfg, "solvex", "disable_http3",     default=False))

    proxy_mode = args.proxy_mode or _g(cfg, "proxy", "mode", default="pool")
    if args.no_proxy:
        proxy_mode = "none"
    if args.proxy_url:
        proxy_mode = "fixed"
    proxy_file = _resolve(args.proxy_file or _g(cfg, "proxy", "file", default="input/proxies.txt"))
    proxy_url  = args.proxy_url or _g(cfg, "proxy", "url", default="")

    output_dir    = _resolve(args.output_dir or _g(cfg, "output", "dir",           default="output"))
    accounts_name = _g(cfg, "output", "accounts_file", default="accounts.txt")
    cookies_name  = _g(cfg, "output", "cookies_file",  default="cookies.txt")

    proxy_pool: list[str] = []
    if proxy_mode == "pool":
        proxy_pool = load_proxies(proxy_file)
        if not proxy_pool:
            print(c(C.RED, f"  ! no proxies loaded from {proxy_file} — running DIRECT (expect 429s)"))
            proxy_mode = "none"

    _idx = [0]
    _idx_lock = threading.Lock()

    def proxy_for() -> str | None:
        if proxy_mode == "none":
            return None
        if proxy_mode == "fixed":
            return proxy_url or None
        with _idx_lock:
            p = proxy_pool[_idx[0] % len(proxy_pool)]
            _idx[0] += 1
        return p

    os.makedirs(output_dir, exist_ok=True)
    accounts_path = os.path.join(output_dir, accounts_name)
    cookies_path  = os.path.join(output_dir, cookies_name)

    total = count if count > 0 else None
    ui = UI(title="Roblox Account Creator", compact=compact, total=total)

    target        = str(count) if count > 0 else "∞"
    bday_label    = "auto (random 16-40yo)" if _is_auto(birthday_cfg) else str(birthday_cfg)
    gender_label  = "auto (random F/M/N)"  if _is_auto(gender_cfg) else {1: "F", 2: "M", 3: "N/A"}.get(int(gender_cfg), str(gender_cfg))
    browser_label = "auto (safari/chrome rotation)" if browser_cfg.strip().lower() not in _BROWSER_PROFILES else browser_cfg.strip().lower()

    try:
        client = SolvexClient(
            api_key=solvex_key,
            base_url=solvex_url,
            poll_interval=poll_interval,
            timeout_s=timeout_s,
            use_http3=not disable_http3,
        )
        solver_label = f"solvex ({client.base})"
    except SolverError as e:
        print(c(C.RED, f"  ! solver init failed: {e}"))
        sys.exit(1)

    ui.banner(
        subtitle=f"{target} accounts × {threads} threads" + ("  ·  compact" if compact else ""),
        fields=[
            ("solver",       solver_label),
            ("proxy",        proxy_mode + (f"  ({len(proxy_pool)} proxies)" if proxy_mode == "pool" else "")),
            ("browser",      browser_label),
            ("birthday",     bday_label),
            ("gender",       gender_label),
            ("check_user",   "ON" if check_user    else "off"),
            ("retry_fail",   "ON" if retry_on_fail else "off"),
            ("solve_pow",    "ON" if solve_pow     else "off"),
            ("output",       accounts_path),
            ("debug",        "ON" if debug         else "off"),
        ],
    )

    results: list[Attempt] = []
    stop_event = threading.Event()

    def _handle_sigint(signum, frame):
        if not stop_event.is_set():
            ui.note("stop signal — finishing in-flight tasks", C.YELLOW)
            stop_event.set()
        else:
            ui.note("forced exit", C.RED)
            os._exit(130)

    signal.signal(signal.SIGINT, _handle_sigint)

    t0 = time.time()

    def _finalize(a: Attempt):
        results.append(a)
        _write_result(a, accounts_path, cookies_path)
        ui.tick(a.created, suppressed=a.suppressed, label=a.username)

    with ThreadPoolExecutor(max_workers=threads) as ex:
        iterator = itertools.count() if count == 0 else range(count)
        in_flight: set[Future] = set()

        for i in iterator:
            if stop_event.is_set():
                break
            while len(in_flight) >= threads and not stop_event.is_set():
                done = [f for f in in_flight if f.done()]
                if done:
                    for f in done:
                        in_flight.discard(f)
                        _finalize(f.result())
                else:
                    time.sleep(0.05)
            if stop_event.is_set():
                break
            fut = ex.submit(
                run_one, i % threads, client, proxy_for(),
                birthday_cfg, gender_cfg, debug, ui,
                retry_on_fail, solve_pow, browser_cfg, check_user,
            )
            in_flight.add(fut)

        for f in in_flight:
            try:
                _finalize(f.result())
            except Exception:
                if debug:
                    traceback.print_exc()

    wall_ms    = int((time.time() - t0) * 1000)
    created    = sum(1 for r in results if r.created)
    suppressed = sum(1 for r in results if r.suppressed)
    errs       = sum(1 for r in results if r.error)
    avg_cap    = (sum(r.captcha_ms for r in results if r.captcha_ms) // max(1, len(results)))
    rate       = len(results) / (wall_ms / 1000) if wall_ms else 0
    sup_rate   = f"{suppressed / max(1, len(results)) * 100:.0f}%" if results else "n/a"

    ui.summary([
        ("attempts",    str(len(results))),
        ("created",     c(C.GREEN + C.BOLD, str(created))),
        ("suppressed",  f"{suppressed}  ({sup_rate})"),
        ("errors",      c(C.RED + C.BOLD, str(errs))),
        ("avg captcha", f"{avg_cap}ms"),
        ("rate",        f"{rate:.2f}/s"),
        ("wall time",   f"{wall_ms / 1000:.1f}s"),
        ("output",      accounts_path),
    ])
    ui.close()


if __name__ == "__main__":
    main()
