"""
HKUST Sports Facility Booking (fbs.hkust.edu.hk) watcher with Telegram alerts + remote control.
Notify-only: it never books anything. You book yourself.

Settings come from ~/.fbs_env (TG_BOT_TOKEN, TG_CHAT_ID, optional FBS_COOKIE).
Cookie is kept in ~/.fbs_cookie, watch settings in ~/.fbs_config.json.

Telegram commands (only accepted from TG_CHAT_ID):
  /watch squash 3 18-21   sport, days (incl. today), start-time window  (any part optional)
  /cookie <value>         new session cookie (just the value, or SSESS...=value)
  /status                 current settings, last check, what's free
  /check                  check right now
  /pause  /resume
  /help

How the site works:
  1. POST select_date_<YYYY-MM-DD>  -> list of courts (<div class="item [not-available]">, select_facility_<id>)
  2. POST select_facility_<id>      -> start-time radios (disabled = taken)
  Each response carries a new form_build_id that the next request must use.
"""
import argparse, datetime as dt, http.server, json, os, random, re, sys, threading, time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HK = ZoneInfo("Asia/Hong_Kong")   # server may be in the US; dates/times must be HK
BASE = "https://fbs.hkust.edu.hk"
PAGE = f"{BASE}/facilities?check_logged_in=1"
AJAX = PAGE + "&ajax_form=1&_wrapper_format=drupal_ajax"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/27.3 Safari/605.1.15")
COOKIE_NAME = "SSESS243a03a7c17a34cf7edbb376cf542187"
SPORTS = {"badminton": "3", "squash": "8", "tennis": "12", "tabletennis": "6",
          "pickleball": "41", "basketball": "2", "volleyball": "29", "climbing": "40"}
SPORT_NAMES = {v: k for k, v in SPORTS.items()}
HOME = os.path.expanduser("~")
ENV_FILE, COOKIE_FILE, CONFIG_FILE = (os.path.join(HOME, f) for f in (".fbs_env", ".fbs_cookie", ".fbs_config.json"))
DEFAULTS = {"sport": "3", "days": 3, "from": "00:00", "to": "23:59", "courts": None,
            "every": 5, "paused": False}


def now_hk():
    return dt.datetime.now(HK)


def log(*a):
    print(f"{now_hk():%Y-%m-%d %H:%M:%S}", *a, flush=True)


# ---------------------------------------------------------------- persistence
def load_env_file():
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def private_write(path, text):
    old = os.umask(0o077)
    try:
        with open(path, "w") as f:
            f.write(text)
    finally:
        os.umask(old)


def normalize_cookie(c):
    c = "".join(c.split())
    if not c:
        return ""
    return c if (c.startswith("SSESS") and "=" in c) else f"{COOKIE_NAME}={c}"


def load_cookie():
    if os.path.exists(COOKIE_FILE):
        c = open(COOKIE_FILE).read().strip()
        if "=" in c:
            return c
    return normalize_cookie(os.environ.get("FBS_COOKIE", ""))


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            cfg.update(json.load(open(CONFIG_FILE)))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    private_write(CONFIG_FILE, json.dumps(cfg, indent=2))


def norm_time(t):
    t = t.strip()
    if re.fullmatch(r"\d{1,2}", t):
        t += ":00"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return f"{int(m[1]):02d}:{m[2]}"


# ---------------------------------------------------------------- site access
class SessionExpired(Exception):
    pass


class FBS:
    def __init__(self, cookie):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Cookie": cookie})

    def load_form(self):
        r = self.s.get(PAGE, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        tok = soup.find("input", attrs={"name": "form_token"})
        if tok is None or "microsoftonline" in r.url or "login" in r.url.lower():
            raise SessionExpired(f"not logged in (landed on {r.url})")
        val = lambda n: (soup.find("input", attrs={"name": n}) or {}).get("value", "")
        aps = {}
        el = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if el and el.string:
            aps = json.loads(el.string).get("ajaxPageState", {})
        self.base_fields = {
            "form_token": tok.get("value", ""),
            "form_id": val("form_id") or "fbs_facilities_booking_form",
            "ajax_page_state[theme]": aps.get("theme", "hkust_fbs"),
            "ajax_page_state[theme_token]": aps.get("theme_token", ""),
            "ajax_page_state[libraries]": aps.get("libraries", ""),
        }
        return val("form_build_id")

    def ajax(self, build_id, sport, date, trigger):
        data = {"filters[facility_type]": sport, "filters[location]": "", "filters[facility]": "",
                "filters[date]": date, "form_build_id": build_id, "_triggering_element_name": trigger,
                "_triggering_element_value": "", "_drupal_ajax": "1", **self.base_fields}
        r = self.s.post(AJAX, data=data, timeout=30, headers={
            "X-Requested-With": "XMLHttpRequest", "Origin": BASE, "Referer": PAGE,
            "Accept": "application/json, text/javascript, */*; q=0.01"})
        if r.status_code in (401, 403) or "json" not in r.headers.get("content-type", ""):
            raise SessionExpired(f"HTTP {r.status_code} {r.headers.get('content-type')}")
        cmds = r.json()
        new_build = next((c.get("new") for c in cmds if c.get("command") == "update_build_id"), build_id)
        html = "\n".join(c["data"] for c in cmds if isinstance(c.get("data"), str))
        return new_build, html


def parse_facilities(html):
    """-> {facility_id: (name, location, available_bool)}"""
    out = {}
    for btn in BeautifulSoup(html, "html.parser").select('input[name^="select_facility_"]'):
        fid = btn["name"].rsplit("_", 1)[1]
        item = btn.find_parent("div", class_="item")
        name = item.select_one(".name") if item else None
        loc = item.select_one(".location") if item else None
        avail = not btn.has_attr("disabled") and "not-available" not in (item.get("class", []) if item else [])
        out[fid] = (name.get_text(strip=True) if name else fid, loc.get_text(strip=True) if loc else "", avail)
    return out


def parse_timeslots(html):
    soup = BeautifulSoup(html, "html.parser")
    return [r["value"] for r in soup.select('input[name="selection[timeslot_wrapper][timeslots]"]')
            if not r.has_attr("disabled")]


def check_once(fbs, cfg):
    """-> set of 'YYYY-MM-DD HH:MM | Court (Location)' free and matching cfg"""
    free, sport = set(), cfg["sport"]
    courts = set(cfg["courts"]) if cfg.get("courts") else None
    today = now_hk().date()
    for i in range(int(cfg["days"])):
        d = (today + dt.timedelta(days=i)).isoformat()
        b = fbs.load_form()
        _, html = fbs.ajax(b, sport, d, f"select_date_{d}")
        for fid, (name, loc, avail) in parse_facilities(html).items():
            if not avail or (courts and fid not in courts):
                continue
            b = fbs.load_form()
            b, _ = fbs.ajax(b, sport, d, f"select_date_{d}")
            _, slot_html = fbs.ajax(b, sport, d, f"select_facility_{fid}")
            for t in parse_timeslots(slot_html):
                if cfg["from"] <= t <= cfg["to"]:
                    free.add(f"{d} {t} | {name} ({loc})")
            time.sleep(random.uniform(1, 3))
        time.sleep(random.uniform(1, 3))
    return free


# ---------------------------------------------------------------- telegram
def tg(method, http_timeout=20, **data):
    tok = os.environ.get("TG_BOT_TOKEN")
    if not tok:
        return {}
    try:
        return requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=data, timeout=http_timeout).json()
    except Exception as e:
        log("telegram error:", repr(e))
        return {}


def send(msg):
    if not os.environ.get("TG_BOT_TOKEN") or not os.environ.get("TG_CHAT_ID"):
        print(msg, flush=True)
        return
    for i in range(0, len(msg), 3900):   # Telegram limit ~4096 chars
        r = tg("sendMessage", chat_id=os.environ["TG_CHAT_ID"], text=msg[i:i + 3900])
        if r and not r.get("ok"):
            log("telegram send failed:", r)


def fmt_slots(slots):
    return "\n".join(sorted(slots)) if slots else "(none)"


def describe(cfg):
    sport = SPORT_NAMES.get(cfg["sport"], cfg["sport"])
    courts = ",".join(cfg["courts"]) if cfg.get("courts") else "all courts"
    return f"{sport}, {cfg['days']} day(s), {cfg['from']}-{cfg['to']}, {courts}, every {cfg['every']} min"


HELP = ("Commands:\n"
        "/watch squash 3 18-21  (sport, days incl. today, start-time window; any part optional)\n"
        "/watch badminton courts 79,80\n"
        "/cookie <value>  new session cookie\n"
        "/status  /check  /pause  /resume  /help\n"
        "Sports: " + ", ".join(SPORTS))


class Watcher:
    def __init__(self, cfg, cookie):
        self.cfg, self.cookie = cfg, cookie
        self.fbs = FBS(cookie) if cookie else None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.seen, self.first = set(), True
        self.last_check, self.last_free, self.expired = None, set(), not cookie

    # ---- command handling
    def handle(self, text, msg_id):
        chat = os.environ["TG_CHAT_ID"]
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1:]
        if cmd == "/cookie":
            tg("deleteMessage", chat_id=chat, message_id=msg_id)   # don't leave it in the chat
            c = normalize_cookie("".join(args))
            if not c:
                return send("Send: /cookie <value from Safari>")
            private_write(COOKIE_FILE, c)
            with self.lock:
                self.cookie, self.fbs, self.expired = c, FBS(c), False
            send("Cookie saved (your message was deleted). Checking now...")
            self.wake.set()
        elif cmd == "/watch":
            new, err = dict(self.cfg), []
            nums = []
            i = 0
            while i < len(args):
                a = args[i].lower()
                if a in SPORTS:
                    new["sport"] = SPORTS[a]
                elif a in ("courts", "court") and i + 1 < len(args):
                    i += 1
                    new["courts"] = None if args[i].lower() == "all" else args[i].split(",")
                elif "-" in a:
                    f, t = (norm_time(x) for x in a.split("-", 1))
                    if f and t:
                        new["from"], new["to"] = f, t
                    else:
                        err.append(f"bad time range '{a}'")
                elif a == "any":
                    new["from"], new["to"] = "00:00", "23:59"
                elif a.isdigit():
                    nums.append(int(a))
                else:
                    err.append(f"didn't understand '{a}'")
                i += 1
            if nums:
                new["days"] = max(1, min(nums[0], 14))
            if err:
                return send("; ".join(err) + "\n\n" + HELP)
            if new.get("sport") != self.cfg["sport"] and not any(a.lower() in ("courts", "court") for a in args):
                new["courts"] = None   # court ids differ per sport
            with self.lock:
                self.cfg = new
                self.seen, self.first = set(), True
            save_config(new)
            send("Now watching: " + describe(new) + "\nChecking now...")
            self.wake.set()
        elif cmd == "/status":
            lc = self.last_check.strftime("%a %H:%M") if self.last_check else "not yet"
            state = "PAUSED" if self.cfg["paused"] else ("WAITING FOR /cookie" if self.expired else "running")
            send(f"Status: {state}\nWatching: {describe(self.cfg)}\nLast check: {lc}\n\nFree now:\n{fmt_slots(self.last_free)}")
        elif cmd == "/check":
            send("Checking now...")
            with self.lock:
                self.first = True
            self.wake.set()
        elif cmd in ("/pause", "/resume"):
            with self.lock:
                self.cfg["paused"] = cmd == "/pause"
            save_config(self.cfg)
            send("Paused." if self.cfg["paused"] else "Resumed. Checking now...")
            self.wake.set()
        else:
            send(HELP)

    def poll_commands(self):
        chat = str(os.environ["TG_CHAT_ID"])
        upd = tg("getUpdates", http_timeout=15).get("result", [])     # skip old messages
        offset = upd[-1]["update_id"] + 1 if upd else None
        while True:
            data = {"timeout": 50}
            if offset:
                data["offset"] = offset
            res = tg("getUpdates", http_timeout=70, **data)
            if res.get("error_code") == 409:
                log("Another copy of the watcher is running with this bot. Stop it.")
                time.sleep(30)
                continue
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) != chat or not m.get("text", "").startswith("/"):
                    continue
                try:
                    self.handle(m["text"], m["message_id"])
                except Exception as e:
                    log("command error:", repr(e))
                    send(f"Error: {e!r}")
            if not res:
                time.sleep(5)

    # ---- main loop
    def run(self):
        if os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"):
            threading.Thread(target=self.poll_commands, daemon=True).start()
        send("FBS watcher started.\nWatching: " + describe(self.cfg) + "\nSend /help for commands."
             + ("\n\nNo cookie yet: send /cookie <value>" if self.expired else ""))
        while True:
            with self.lock:
                cfg, fbs, paused, expired = dict(self.cfg), self.fbs, self.cfg["paused"], self.expired
            if not paused and not expired:
                try:
                    now = check_once(fbs, cfg)
                    new = set()
                    with self.lock:
                        if cfg == self.cfg:   # ignore result if settings changed mid-check
                            new = now - self.seen
                            if self.first:
                                send(f"Free now ({describe(cfg)}):\n{fmt_slots(now)}")
                            elif new:
                                send("Slot(s) just opened:\n" + fmt_slots(new) + f"\nBook: {PAGE}")
                            self.seen, self.first = now, False
                            self.last_free, self.last_check = now, now_hk()
                    log(f"{len(now)} free, {len(new)} new")
                except SessionExpired as e:
                    log("session expired:", e)
                    with self.lock:
                        self.expired = True
                    send("FBS login expired. Log in on fbs.hkust.edu.hk (Safari → Web Inspector → Storage → "
                         "Cookies), then send:\n/cookie <value>")
                except Exception as e:
                    log("error:", repr(e))
            self.wake.wait(int(cfg["every"]) * 60 + random.uniform(0, 60))
            self.wake.clear()


def start_keepalive_server(watcher):
    """Hosts like Render need an HTTP port and sleep after ~15 min without visits.
    An external pinger (e.g. cron-job.org) hitting this URL every 5 min keeps it awake."""
    port = int(os.environ["PORT"])

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            lc = watcher.last_check.strftime("%Y-%m-%d %H:%M") if watcher.last_check else "not yet"
            body = f"FBS watcher alive. Last check (HK): {lc}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"keep-alive server on port {port}")


def main():
    load_env_file()
    ap = argparse.ArgumentParser(description="Watch HKUST FBS for free slots")
    ap.add_argument("--once", action="store_true", help="one check, print results, exit")
    ap.add_argument("--sport", help="/".join(SPORTS))
    ap.add_argument("--courts", help="comma-separated court ids, e.g. 79,80 (or 'all')")
    ap.add_argument("--from", dest="tfrom", help="earliest start time, e.g. 18:00")
    ap.add_argument("--to", dest="tto", help="latest start time, e.g. 21:00")
    ap.add_argument("--days", type=int, help="days ahead incl. today")
    ap.add_argument("--every", type=int, help="minutes between checks (min 3)")
    a = ap.parse_args()

    cfg = load_config()
    if a.sport:
        cfg["sport"] = SPORTS.get(a.sport.lower().replace(" ", ""), a.sport)
        cfg["courts"] = None
    if a.courts:
        cfg["courts"] = None if a.courts == "all" else a.courts.split(",")
    if a.tfrom:
        cfg["from"] = norm_time(a.tfrom) or cfg["from"]
    if a.tto:
        cfg["to"] = norm_time(a.tto) or cfg["to"]
    if a.days:
        cfg["days"] = a.days
    if a.every:
        cfg["every"] = max(3, a.every)
    cfg["paused"] = False
    save_config(cfg)

    cookie = load_cookie()
    if cookie:
        private_write(COOKIE_FILE, cookie)

    if a.once:
        if not cookie:
            sys.exit("No cookie. Put FBS_COOKIE in ~/.fbs_env or run the watcher and send /cookie.")
        print(fmt_slots(check_once(FBS(cookie), cfg)))
        return
    w = Watcher(cfg, cookie)
    if os.environ.get("PORT"):
        start_keepalive_server(w)
    w.run()


if __name__ == "__main__":
    main()
