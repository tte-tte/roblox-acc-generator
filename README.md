<!-- SPONSOR-START -->
---

<div align="center">

### 🌐 Need Proxies? Check out my services

<a href="https://vaultproxies.com" target="_blank" rel="noopener noreferrer">
  <img src="https://i.imgur.com/TF165pP.gif" alt="VaultProxies">
</a>
<p></p>

<table>
  <tr>
    <th>Service</th>
    <th>Pricing</th>
    <th>Features</th>
  </tr>
  <tr>
    <td><b><a href="https://vaultproxies.com" target="_blank" rel="noopener noreferrer">🔮 VaultProxies</a></b></td>
    <td><code>$1.00/GB</code> residential</td>
    <td>Residential · IPv6 · Residential Unlimited · Datacenter</td>
  </tr>
  <tr>
    <td><b><a href="https://nullproxies.com" target="_blank" rel="noopener noreferrer">🌑 NullProxies</a></b></td>
    <td><code>$0.75/GB</code> residential</td>
    <td>Residential · Residential Unlimited · DC Unlimited · Mobile Proxies</td>
  </tr>
  <tr>
    <td><b><a href="https://strikeproxy.net" target="_blank" rel="noopener noreferrer">⚡ StrikeProxy</a></b></td>
    <td><code>$0.75/GB</code> residential</td>
    <td>Residential · Residential Unlimited · DC Unlimited · Mobile Proxies</td>
  </tr>
</table>
</div>

<!-- SPONSOR-END -->

<div align="center">
  <h1 align="center">Roblox Account Creator — Solvex Edition</h1>
  <p align="center">
    Bulk Roblox signup driver. Uses <a href="https://solvex.run">Solvex</a> for the Arkose FunCaptcha challenge and <code>curl_cffi</code> Safari 18 / Chrome 146 TLS impersonation (rotated per worker) for the actual signup calls. The codebase is bad as well as the code, it has been entirely vibecoded.
    <br />
    <br />
    <a href="https://discord.cyberious.xyz">Discord</a>
    ·
    <a href="https://solvex.run">Solvex</a>
    ·
    <a href="#changelog">Changelog</a>
  </p>
</div>

---

## Contents

- [What it does](#what-it-does)
- [Layout](#layout)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration](#configuration)
- [CLI flags](#cli-flags)
- [Running](#running)
- [Signup flow](#signup-flow)
- [Output](#output)
- [Outcome tags](#outcome-tags)
- [Notes](#notes)
- [Changelog](#changelog)

---

## What it does

Creates Roblox accounts in bulk by driving the real `auth.roblox.com/v2/signup` endpoint and resolving the Arkose challenge through the Solvex API.

For each account the worker:

1. Warms `www.roblox.com` so the session has the cookies + CSRF token Roblox expects.
2. Generates `username` / `password` / `birthday` / `gender` (configurable).
3. POSTs `/v2/signup`, gets a `403` with an Arkose `dataExchangeBlob`.
4. Sends the blob to Solvex (`FunCaptchaTask`, public key `A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F` — Roblox `WEB_SIGNUP`).
5. Submits the token to `/challenge/v1/continue`, then re-POSTs `/v2/signup` with the `rblx-challenge-*` headers.
6. On `200`, dumps the resulting cookie jar into `output/accounts.txt` and `output/cookies.txt`.

Captcha tokens are only accepted if Solvex returns them as **suppressed** (or PoW-verified). Non-suppressed tokens are rejected — they will not pass `/v2/signup` reliably and waste proxies.

---

## Layout

```
Roblox-Account-Creator-Solvex/
├── main.py                 # argparse driver + ThreadPoolExecutor loop
├── config.toml             # default config (CLI flags override)
├── requirements.txt        # requests, curl_cffi
├── modules/
│   ├── http.py             # curl_cffi Session, Safari 18 / Chrome 146 TLS impersonation
│   ├── roblox.py           # warmup + /v2/signup + /challenge/v1/continue
│   ├── solvex_client.py    # POST /createTask + poll /getTaskResult
│   └── logger.py           # thread-safe terminal UI (regular + compact bar)
├── input/
│   └── proxies.txt         # one proxy per line
└── output/
    ├── accounts.txt        # username:password:cookies
    └── cookies.txt         # cookies (one per successful signup)
```

---

## Requirements

- Python 3.11+ (uses `tomllib` from stdlib)
- A Solvex API key — get one at [solvex.run](https://solvex.run)
- Residential proxies. Direct connections will rate-limit (`429`) almost immediately.

---

## Install

```bash
pip install -r requirements.txt
```

Dependencies:

- `requests` — used by `solvex_client` to talk to `api.solvex.run`
- `curl_cffi` — used by `modules/http.py` to impersonate Safari 18 or Chrome 146 against Roblox (selectable via `[run].browser`)

---

## Configuration

All defaults live in `config.toml`. Any value can be overridden by a CLI flag.

```toml
[run]
threads = 32              # concurrent workers
count = 0                 # 0 = infinite (Ctrl-C to stop)
debug = false             # full tracebacks on errors
compact = false           # single-line live status bar
birthday = "auto"         # "auto" = random 16–40yo, or ISO string e.g. "1995-06-15T00:00:00.000Z"
gender = "auto"           # "auto" = random F/M/N, or 1=F, 2=M, 3=N/A
browser = "auto"          # "auto" = random Safari/Chrome per worker, or pin "safari" / "chrome"
retry_on_failure = false  # re-solve captcha if username flagged AFTER token consumed (RENAME)

[solvex]
url = "https://api.solvex.run"
api_key = ""              # required — set here or via SOLVEX_API_KEY env var
timeout_s = 120           # per-task deadline
poll_interval_s = 0.5     # /getTaskResult poll cadence
solve_pow = false         # let Solvex solve PoW challenges instead of failing
disable_http3 = false     # force HTTP/2 in the Solvex worker if QUIC is mangled by your proxy

[proxy]
mode = "pool"             # "pool" | "fixed" | "none"
file = "input/proxies.txt"
url = ""                  # used when mode = "fixed"

[output]
dir = "output"
accounts_file = "accounts.txt"
cookies_file = "cookies.txt"
```

### Proxy file format

`input/proxies.txt` — one proxy per line. All of these work:

```
host:port
host:port:user:pass
http://user:pass@host:port
socks5://user:pass@host:port
```

`host:port` on port `1080` is auto-detected as `socks5`; everything else defaults to `http`.

### Environment variables

| Variable          | Effect                                                                  |
| ----------------- | ----------------------------------------------------------------------- |
| `SOLVEX_API_KEY`  | Used if `[solvex].api_key` is empty in config.                          |
| `SOLVEX_URL`      | Used if `[solvex].url` is unset and `--solvex-url` not passed.          |

---

## CLI flags

```
--config FILE               path to TOML config (default: config.toml)
--threads N                 worker count
-n, --count N               number of accounts; 0 = infinite
--debug                     full tracebacks on errors
--compact / --no-compact    single-line live status bar (toggle)
--solvex-url URL            override [solvex].url
--solvex-key KEY            override [solvex].api_key (sk_…)
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
python main.py -n 0                  # infinite — Ctrl-C to stop
python main.py --compact             # single-line bar instead of per-event lines
python main.py --no-proxy            # direct (will get 429'd fast)
```

`Ctrl-C` once = drain in-flight tasks then exit. `Ctrl-C` twice = force exit.

---

## Signup flow

The full path inside `run_one()` (in `main.py`):

1. **Warmup** — `RobloxAuth.warmup_signup_page()` fires four parallel GETs (`www.roblox.com/`, `/CreateAccount`, two `apis.roblox.com` bundles) to seat the cookie jar and any CSRF state. Two `RBX*` cookies are also stamped manually.
2. **Username validate + signup attempt** — `POST /v1/usernames/validate` (harvests the `x-csrf-token`), then `POST /v2/signup`. If Roblox rotates the CSRF mid-handshake the call is retried once with the new token.
3. **Username retry loop** — if `/v2/signup` returns `403 UsernameTaken` or `UsernameInvalid` *before* the captcha, a fresh username is generated and the signup is retried. Up to 5 attempts.
4. **Captcha challenge** — on `403 + rblx-challenge-*` headers, `dataExchangeBlob` and the cookie jar are sent to Solvex via `FunCaptchaTask`.
5. **Token check** — only `suppressed` tokens (or PoW-verified ones, when `solve_pow = true`) are accepted. Non-suppressed tokens log a `CHALLENGED` event and the attempt fails fast.
6. **Submit + retry** — `POST /challenge/v1/continue` with the token, then re-POST `/v2/signup` with `rblx-challenge-id`, `rblx-challenge-type`, `rblx-challenge-metadata` headers. `200` = account created, cookies in the jar.
7. **Post-captcha rename** — if `/v2/signup` *now* returns `UsernameInvalid` / `UsernameTaken`, the captcha token is already consumed. With `retry_on_failure = true` a fresh username is tried (costs another captcha); otherwise the attempt is logged as `RENAME` and abandoned.

### Why Solvex tokens are validated as "suppressed"

Solvex returns one of three challenge types in `solution.challengeType`:

- `suppressed` — Arkose let the request through without showing a visual puzzle. These are the only tokens that pass `/v2/signup` consistently.
- `pow` — Arkose required a Proof-of-Work. Solvex can solve it if `solve_pow = true`; the resulting token is treated as suppressed when `verified = true`.
- (anything else) — visual puzzle was solved. These tokens **do not** work for signup. The attempt is rejected locally with `CHALLENGED` to avoid wasting another `/v2/signup`.

### What gets sent to Solvex

`SolvexClient.arkose()` forwards the worker's UA via the `userAgent` field so the wire claim matches the Roblox session that produced the cookies. It deliberately does **not** set the `Browser` field — with `Browser` empty the upstream solver-service falls through to its chrome default (~97 unique devices vs ~4 on safari1, which burns almost instantly under per-pk pinning). UA is a wire claim, `Browser` is a routing hint — they are not the same lever.

---

## Output

`output/accounts.txt` — append-only, one line per created account:

```
<username>:<password>:<cookies>
```

`output/cookies.txt` — append-only, one line per created account, cookies only.

Both files are written under a global lock (`_WRITE_LOCK` in `main.py`), so concurrent workers are safe.

The end-of-run summary prints:

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

Each attempt logs one of:

| Tag          | Meaning                                                                       |
| ------------ | ----------------------------------------------------------------------------- |
| `SUPPRESS`   | Suppressed Arkose token, account created. Happy path.                         |
| `POW`        | PoW-verified token (`solve_pow = true`), account created.                     |
| `SOLVED`     | Non-suppressed verified token that *somehow* still passed signup. Rare.       |
| `DIRECT`     | Roblox returned 200 with no captcha. Very rare.                               |
| `CHALLENGED` | Solver returned a non-suppressed token. Attempt aborted to save a captcha.    |
| `RENAME`     | Username flagged *after* the token was consumed. Account not created.        |
| `FAIL`       | Final `/v2/signup` returned non-200.                                          |
| `SOLVER`     | Solvex task did not complete (timeout, network, or `errorId`).                |
| `API`        | `SolverError` raised — usually a missing/invalid API key.                     |
| `ERROR`      | Anything else. Use `--debug` to get the full traceback.                       |

---

## Notes

- **Roblox impersonation.** `main.py` picks one of two `curl_cffi` profiles per worker: `safari180` (Safari 18 on macOS, no `sec-ch-ua`, `Accept-Encoding: gzip, deflate, br`) or `chrome146` (Chrome 146 on Windows, full `sec-ch-ua` trio, `Accept-Encoding: gzip, deflate, br, zstd`). The TLS fingerprint, UA, and `sec-ch-ua` headers are kept consistent — Roblox flags Chrome-TLS-with-Safari-UA (or vice versa) as bot-like and bumps the Arkose risk score, which costs you suppression rate. Don't half-mix profiles.
- **`shared_parameters`.** Captured from the captcha challenge metadata. The current code does not include it in the `/challenge/v1/continue` body, but it is parsed and stored on `RobloxChallenge` for future use. Do not strip it from `_parse_captcha_challenge`.
- **No 2FA / verification flow.** This creates accounts only. Email confirmation, captcha-on-login, age verification, and any post-signup steps are out of scope.
- **No retry on transport errors.** A single `requests` failure at the `signup` POST will surface as `ERROR`. Re-run with the same proxy pool.
- **Compact mode + non-TTY.** If stdout is not a TTY, the compact bar prints a progress line every 20 attempts instead of redrawing in place.

---

## Changelog

```diff
v0.3.0
+ Browser rotation: Safari 18 + Chrome 146 (curl_cffi safari180 / chrome146)
+ [run].browser config knob: "auto" (random per worker), "safari", or "chrome"
+ warmup_signup_page now respects the configured UA / Accept-Language / sec-ch-ua
  on the apis.roblox.com bundle calls (was hardcoded Safari before — would have
  flagged any Chrome-impersonated session)
+ Solvex FunCaptchaTask now sends the worker's UA via userAgent so the wire
  claim matches the Roblox session (Browser routing hint still left unset)

v0.2.0
+ Solvex-only solver path (LocalSolver and Go wire-shape backend factored out)
+ Username retry loop on pre-captcha rejections (max 5 attempts)
+ retry_on_failure: opt-in re-solve when Roblox flags the username post-captcha
+ Compact mode: single-line spinner + counters + ETA
+ Per-attempt captcha_ms tracking, summary now reports avg captcha latency

v0.1.0
+ Initial Solvex-driven signup driver
+ curl_cffi Safari 18 impersonation for /v2/signup
+ Parallel warmup of www.roblox.com + CreateAccount + apis bundles
+ FunCaptchaTask via Solvex /createTask + /getTaskResult
+ Pool/fixed/none proxy modes, accounts.txt + cookies.txt output
```

---

## License

MIT.

For educational and authorized testing use only. Mass account creation may violate Roblox's Terms of Service — this codebase is not an endorsement of doing so.
