<div align="center">

# Roblox Account Creator

Bulk Roblox signup driver with pluggable captcha solvers, TLS impersonation, and optional post-signup group join.

Forked and extended from [sexfrance/Roblox-Account-Creator](https://github.com/sexfrance/Roblox-Account-Creator) (originally a Solvex-only edition). This fork replaces Solvex with **2Captcha** and **Anti-Captcha**, adds automatic group joining, a standalone batch joiner, PM2 deployment config, and several reliability improvements.

</div>

---

## Contents

- [What it does](#what-it-does)
- [What's new vs upstream](#whats-new-vs-upstream)
- [Layout](#layout)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration](#configuration)
- [CLI flags](#cli-flags)
- [Running](#running)
- [Group join](#group-join)
- [Signup flow](#signup-flow)
- [Output](#output)
- [Outcome tags](#outcome-tags)
- [PM2 deployment](#pm2-deployment)
- [Notes](#notes)
- [Changelog](#changelog)
- [License](#license)

---

## What it does

Creates Roblox accounts in bulk by driving the real `auth.roblox.com/v2/signup` endpoint and resolving the Arkose FunCaptcha challenge through a configurable captcha provider.

For each account the worker:

1. Warms `www.roblox.com` so the session has the cookies and CSRF token Roblox expects.
2. Generates `username` / `password` / `birthday` / `gender` (configurable).
3. Optionally pre-validates the username via `/v1/usernames/validate`.
4. POSTs `/v2/signup`, gets a `403` with an Arkose `dataExchangeBlob`.
5. Sends the blob to **2Captcha** or **Anti-Captcha** (`FunCaptchaTask`, public key `A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F`).
6. Submits the token to `/challenge/v1/continue`, then re-POSTs `/v2/signup` with the `rblx-challenge-*` headers.
7. On `200`, writes credentials to `output/accounts.txt` and `output/cookies.txt`.
8. Optionally joins a Roblox community group immediately using the warm signup session.

Captcha tokens are only accepted if the solver returns them as **suppressed** (or PoW-verified when enabled). Non-suppressed tokens are rejected locally to avoid wasting proxies on attempts that will not pass `/v2/signup` reliably.

---

## What's new vs upstream

| Feature | Upstream (Solvex edition) | This fork |
| --- | --- | --- |
| Captcha solver | Solvex only | **2Captcha** or **Anti-Captcha** (switch via config/CLI) |
| Post-signup group join | — | Built-in auto-join + `join_group.py` batch runner |
| Solver UA handling | Solvex pinned Safari UA | Per-worker UA passed to solver (Safari/Chrome consistent end-to-end) |
| Username pre-check | Optional | Optional (`check_username`) |
| Post-captcha rename retry | Optional | Optional (`retry_on_failure`) |
| PM2 config | — | `ecosystem.config.js` for long-running VPS jobs |
| Output tracking | accounts + cookies | accounts + cookies + **joined.txt** |

---

## Layout

```
Roblox-Account-Creator/
├── main.py                      # argparse driver + ThreadPoolExecutor loop
├── join_group.py                # standalone batch group-joiner for accounts.txt
├── config.toml                  # default config (CLI flags override; not committed)
├── ecosystem.config.js          # PM2 process definitions
├── requirements.txt             # requests, curl_cffi
├── modules/
│   ├── http.py                  # curl_cffi Session, Safari 18 / Chrome 146 TLS impersonation
│   ├── roblox.py                # warmup + /v2/signup + /challenge/v1/continue
│   ├── twocaptcha_client.py     # 2Captcha createTask + getTaskResult
│   ├── anticaptcha_client.py    # Anti-Captcha createTask + getTaskResult
│   ├── group_join.py            # POST groups.roblox.com/v1/groups/{id}/users
│   └── logger.py                # thread-safe terminal UI (regular + compact bar)
├── input/
│   └── proxies.txt              # one proxy per line (not committed)
└── output/
    ├── accounts.txt             # username:password:cookies
    ├── cookies.txt              # cookies only
    └── joined.txt               # usernames that successfully joined the target group
```

---

## Requirements

- Python 3.11+ (uses `tomllib` from stdlib)
- A **2Captcha** or **Anti-Captcha** API key with balance
- Residential proxies — direct connections will rate-limit (`429`) almost immediately

---

## Install

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy and edit `config.toml` with your API keys and proxy settings. See `.gitignore` — secrets and output files are not tracked in git.

Dependencies:

- `requests` — captcha provider API calls
- `curl_cffi` — Safari 18 / Chrome 146 TLS impersonation against Roblox

---

## Configuration

All defaults live in `config.toml`. Any value can be overridden by a CLI flag.

```toml
[run]
solver = "anticaptcha"      # "2captcha" | "anticaptcha"
threads = 32
count = 0                   # 0 = infinite (Ctrl-C to stop)
debug = false
compact = false             # single-line live status bar
birthday = "auto"           # "auto" = random 19–40yo, or ISO string
gender = "auto"             # "auto" = random F/M/N, or 1=F, 2=M, 3=N/A
browser = "auto"              # "auto" | "safari" | "chrome"
check_username = false      # pre-flight via /v1/usernames/validate
retry_on_failure = false    # re-solve captcha if username flagged AFTER token consumed

[twocaptcha]
url = "https://api.2captcha.com"
api_key = ""
timeout_s = 900
poll_interval_s = 5
solve_pow = false           # ignored by 2Captcha; kept for interface parity

[anticaptcha]
url = "https://api.anti-captcha.com"
api_key = ""
timeout_s = 900
poll_interval_s = 3
solve_pow = false           # ignored by Anti-Captcha; kept for interface parity

[group]
enabled = true
id = 0                      # Roblox community group ID; 0 or empty = disabled

[proxy]
mode = "pool"               # "pool" | "fixed" | "none"
file = "input/proxies.txt"
url = ""                    # used when mode = "fixed"

[output]
dir = "output"
accounts_file = "accounts.txt"
cookies_file = "cookies.txt"
joined_file = "joined.txt"
```

### Proxy file format

`input/proxies.txt` — one proxy per line:

```
host:port
host:port:user:pass
http://user:pass@host:port
socks5://user:pass@host:port
```

`host:port` on port `1080` is auto-detected as `socks5`; everything else defaults to `http`.

### Environment variables

| Variable | Effect |
| --- | --- |
| `TWOCAPTCHA_USE_PROXY` | `1` = pass worker proxy to 2Captcha task; `0` = proxyless (default in PM2 config) |
| `ANTICAPTCHA_USE_PROXY` | Same for Anti-Captcha |

---

## CLI flags

```
--config FILE               path to TOML config (default: config.toml)
--threads N                 worker count
-n, --count N               number of accounts; 0 = infinite
--debug                     full tracebacks on errors
--compact / --no-compact    single-line live status bar (toggle)
--solver {2captcha,anticaptcha}
--twocaptcha-url URL
--twocaptcha-key KEY
--anticaptcha-url URL
--anticaptcha-key KEY
--group-id ID               auto-join this group after each signup
--no-join                   disable post-signup group join
--proxy-mode {pool,fixed,none}
--proxy-file FILE
--proxy-url URL             implies --proxy-mode fixed
--no-proxy                  implies --proxy-mode none
--output-dir DIR
```

Paths in flags and config are resolved relative to `main.py`, not the shell's CWD.

---

## Running

```bash
# Use config.toml as-is
python main.py

# Override at runtime
python main.py --threads 8 -n 100
python main.py --solver 2captcha --twocaptcha-key YOUR_KEY
python main.py -n 0                  # infinite — Ctrl-C to stop
python main.py --compact             # single-line bar instead of per-event lines
python main.py --group-id 34792238   # auto-join group after signup
python main.py --no-join             # signup only, skip group join
python main.py --no-proxy            # direct (will get 429'd fast)
```

`Ctrl-C` once = drain in-flight tasks then exit. `Ctrl-C` twice = force exit.

---

## Group join

Two ways to join accounts to a Roblox community group:

### 1. Auto-join during signup (`main.py`)

Enable in config:

```toml
[group]
enabled = true
id = 34792238
```

Or pass `--group-id` on the CLI. After a successful signup, the worker reuses the warm session (cookies + TLS fingerprint) to POST `groups.roblox.com/v1/groups/{id}/users`. Successful joins are recorded in `output/joined.txt`.

Group join may trigger its own Arkose challenge; the same captcha solver configured for signup is used.

### 2. Standalone batch joiner (`join_group.py`)

For accounts already in `output/accounts.txt`:

```bash
python join_group.py                       # uses [group].id from config.toml
python join_group.py --group-id 34792238
python join_group.py --threads 20
python join_group.py --dry-run           # parse + count only, no POSTs
```

Skips usernames already listed in `output/joined.txt`. Rotates proxies from `input/proxies.txt`.

---

## Signup flow

The full path inside `run_one()` (in `main.py`):

1. **Warmup** — parallel GETs to `www.roblox.com`, `/CreateAccount`, and `apis.roblox.com` bundles to seat the cookie jar and CSRF state.
2. **Username validate** (optional) — `POST /v1/usernames/validate` before signup to avoid burning captcha solves on bad names.
3. **Signup attempt** — `POST /v2/signup`. CSRF rotation is retried once automatically.
4. **Username retry loop** — if `/v2/signup` returns `403 UsernameTaken` or `UsernameInvalid` *before* captcha, a fresh username is generated (up to 5 attempts).
5. **Captcha challenge** — on `403 + rblx-challenge-*` headers, `dataExchangeBlob` is sent to the configured solver with the worker's actual UA (must match the signup session).
6. **Token check** — only suppressed tokens are accepted. Non-suppressed tokens log `CHALLENGED` and fail fast.
7. **Submit + retry** — `POST /challenge/v1/continue`, then re-POST `/v2/signup` with challenge headers. `200` = account created.
8. **Post-captcha rename** — if the username is flagged after the token is consumed, optionally retry with a fresh name (`retry_on_failure`).
9. **Group join** (optional) — immediately join the configured community group while the session is still warm.

### Why tokens must be suppressed

Solvers return one of:

- `suppressed` — Arkose let the request through without a visual puzzle. These pass `/v2/signup` consistently.
- `pow` — Proof-of-Work was required and solved. Treated as suppressed when verified.
- anything else — visual puzzle was solved. These tokens **do not** work reliably for signup and are rejected locally.

### Browser / UA consistency

Each worker picks Safari 18 or Chrome 146 (`curl_cffi` `safari180` / `chrome146`). The TLS fingerprint, `User-Agent`, and `sec-ch-ua` headers stay matched — mixing Chrome TLS with a Safari UA (or vice versa) raises Arkose risk scores and kills suppression rate.

Unlike the upstream Solvex edition, this fork passes the worker's real profile UA to 2Captcha/Anti-Captcha so the token is bound to the same browser claim used on `/v2/signup`.

---

## Output

`output/accounts.txt` — append-only, one line per created account:

```
<username>:<password>:<cookies>
```

`output/cookies.txt` — append-only, cookies only.

`output/joined.txt` — one username per line for accounts that successfully joined the target group.

All files are written under a global lock, so concurrent workers are safe.

End-of-run summary:

```
attempts     <total>
created      <ok>
suppressed   <n>  (<pct>%)
errors       <n>
avg captcha  <ms>ms
rate         <n>/s
wall time    <s>
output       <path>
```

---

## Outcome tags

| Tag | Meaning |
| --- | --- |
| `SUPPRESS` | Suppressed Arkose token, account created |
| `POW` | PoW-verified token, account created |
| `SOLVED` | Non-suppressed verified token that still passed signup (rare) |
| `DIRECT` | Roblox returned 200 with no captcha (very rare) |
| `CHALLENGED` | Solver returned a non-suppressed token; attempt aborted |
| `RENAME` | Username flagged after the token was consumed |
| `JOIN` | Post-signup group join succeeded |
| `JOIN-FAIL` | Account created but group join failed (non-fatal) |
| `FAIL` | Final `/v2/signup` returned non-200 |
| `SOLVER` | Captcha task did not complete (timeout, network, or API error) |
| `API` | `SolverError` — usually a missing or invalid API key |
| `ERROR` | Anything else; use `--debug` for full traceback |

---

## PM2 deployment

For long-running VPS jobs:

```bash
pm2 start ecosystem.config.js --only roblox-generator
pm2 logs roblox-generator
pm2 restart roblox-generator
pm2 stop roblox-generator
```

The PM2 config sets `ANTICAPTCHA_USE_PROXY=0` so Anti-Captcha solves from its own IP pool (often cleaner for Arkose than residential proxies). Edit `ecosystem.config.js` paths if your install directory differs from `/root/Roblox-Account-Creator`.

---

## Notes

- **No 2FA / email verification flow.** This creates accounts only. Email confirmation, captcha-on-login, and age verification are out of scope.
- **Group join is best-effort.** A failed join does not roll back a created account; use `join_group.py` to retry later.
- **Secrets stay local.** `config.toml`, proxies, and output files are gitignored. Never commit API keys or account credentials.
- **Compact mode + non-TTY.** If stdout is not a TTY, the compact bar prints a progress line every 20 attempts instead of redrawing in place.

---

## Changelog

```diff
v0.4.0 (this fork)
+ Replaced Solvex with 2Captcha and Anti-Captcha providers (--solver flag)
+ Post-signup auto group join ([group] config + --group-id / --no-join)
+ Standalone join_group.py batch runner with joined.txt deduplication
+ PM2 ecosystem.config.js for VPS deployment
+ Per-worker UA forwarded to captcha solver (fixes Chrome/Safari mismatch vs Solvex)
+ output/joined.txt tracking for group membership
+ Removed modules/solvex_client.py

v0.3.0 (upstream)
+ Browser rotation: Safari 18 + Chrome 146 (curl_cffi safari180 / chrome146)
+ [run].browser config knob: "auto", "safari", or "chrome"
+ Username pre-validation and post-captcha rename retry options

v0.2.0 (upstream)
+ Solvex-only solver path
+ Compact mode, per-attempt captcha_ms tracking

v0.1.0 (upstream)
+ Initial Solvex-driven signup driver
+ curl_cffi impersonation, pool/fixed/none proxy modes
```

---

## License

MIT.

Forked from [sexfrance/Roblox-Account-Creator](https://github.com/sexfrance/Roblox-Account-Creator).

For educational and authorized testing use only. Mass account creation may violate Roblox's Terms of Service.
