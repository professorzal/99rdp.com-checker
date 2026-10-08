<div align="center">

# 99rdp.com checker

**WHMCS credential checker · full capture**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![curl_cffi](https://img.shields.io/badge/curl__cffi-impersonate-green.svg)](https://github.com/yifeikong/curl_cffi)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

</div>

---

## overview

Multi-threaded checker for the `portal.99rdp.com` WHMCS client area.
CSRF-aware, TLS-impersonated, proxy-rotated, and rate-limit resilient.

On a valid login it captures:

| field | source |
|-------|--------|
| login | combo |
| pass | combo |
| name | `clientarea.php?action=details` → firstname + lastname |
| country | details → `<select name="country">` selected option |
| phone | details → `phonenumber` input |
| services | dashboard → active services count |
| balance | dashboard → account balance |

---

## features

- **`curl_cffi` browser impersonation** — chrome146 TLS/JA3 fingerprint
- **CSRF token harvesting** — parses `token` hidden input on `/login`
- **Proxy pool with health gates** — per-proxy use window, penalty box, round-robin selection
- **Rate-limit resilience** — exponential backoff, requeue, and a dedicated second pass for stubborn combos
- **Jittered thread pacing** — randomized inter-request delay
- **Live progress bar** — `checked N/M · hits N · fails N · rl N · err N`
- **Boxed hit output** — aligned, colored, field-by-field
- **Auto-flushing hits file** — rewritten on every new hit, nothing lost on Ctrl+C

---

## install

```bash
git clone https://github.com/<your-user>/99rdp-checker.git
cd 99rdp-checker
pip install -r requirements.txt
```

python 3.9+ required.

---

## usage

```bash
python 99rdp.py
```

the script is interactive:

```
? Use proxies? (y/n) [y] :
? Proxy file path [proxies.txt] :
? Test threads [40] :
? Combo file path [combo.txt] :
? Output txt path [99rdp_hits.txt] :
? Check threads [10] :
```

### input formats

**combo file** — one `user:pass` per line. also accepts `|` and `;` as separators. `#` lines ignored.

```
user1@mail.com:password1
user2@mail.com|password2
user3@mail.com;password3
```

**proxy file** — one per line. supported:

```
host:port
host:port:user:pass
user:pass@host:port
http://host:port
http://user:pass@host:port
socks5://user:pass@host:port
```

a `_live.txt` file is written next to the proxy file listing only the proxies that passed the initial check.

---

## output

**live hit box**

```
  ╭──────────────────────────────────────────────────────────╮
  │ ◆ HIT #1                                                 │
  ├──────────────────────────────────────────────────────────┤
  │ login     user@mail.com                                  │
  │ pass      hunter2                                        │
  │ name      Jane Doe                                       │
  │ country   Germany                                        │
  │ phone     17643496197                                    │
  │ services  2                                              │
  │ balance   $0.00 USD                                      │
  ╰──────────────────────────────────────────────────────────╯
```

**hits file** — plain text, appended on every hit, includes timestamp and all captured fields.

---

## how it works

1. **proxy check** — each proxy is tested against `api.ipify.org` and `httpbin.org/ip`, latency-sorted, live ones written to `*_live.txt`.
2. **login harvest** — `GET /login`, extract hidden `token` input.
3. **login post** — `POST /login` with `token`, `username`, `password`. success detected via the `My Dashboard` string.
4. **enrichment** — `GET /clientarea.php?action=details` for name / country / phone. dashboard response for services / balance.
5. **rate-limit handling** — 429/503 and known phrases trigger exponential backoff. proxy is cooled down. combo is requeued up to `RL_MAX_RETRIES` times, then deferred to a second pass after a cooldown window.

---

## credits

**creator** — [@mr_crkz](https://t.me/mr_crkz)

**channels**
- https://t.me/+F4lGYLLn12o0N2Rh
- https://t.me/professor_zal_projects

---

## license

MIT — see [LICENSE](LICENSE)