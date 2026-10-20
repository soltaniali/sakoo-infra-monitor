#!/usr/bin/env python3
"""Receives probe reports from inside the cluster and sends Telegram alerts on state changes.
Runs on the London server. Stdlib only.

  receiver.py           serve on 0.0.0.0:$PORT (default 8090), POST /report, GET /health
  receiver.py --test    send a test Telegram message to every subscriber
Env (secrets.env): TG_TOKEN, TG_CHAT_ID, PROBE_KEY, BOT_PASSWORD_HASH (set-bot-password.sh).
Optional: PORT, FAIL_AFTER (2), SILENT_AFTER_S (180), DRY_RUN=1 (log instead of sending).
Alerts: a check failing FAIL_AFTER reports in a row; its recovery; no report for SILENT_AFTER_S ("probe silent").
Bot: anyone who sends "/login <password>" gets every alert; /logout, /status, /help. Subscribers: subscribers.json.
"""
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE, SUBS, OFFSET = DIR / "state.json", DIR / "subscribers.json", DIR / "tg_offset"
FAIL_AFTER = int(os.environ.get("FAIL_AFTER", 2))
SILENT_AFTER = int(os.environ.get("SILENT_AFTER_S", 180))
MAX_SKEW = 300  # reject reports older/newer than 5 min (replays)
LABEL = {"infra": "🔴 INFRA DOWN", "us": "🟠 MLOPS ISSUE", "monitor": "⚠️ PROBE CANNOT SEE SAKOO"}
MAX_TRIES, BLOCK_S = 5, 900  # wrong /login passwords per chat before a 15 min block
lock, sub_lock = threading.Lock(), threading.Lock()
login_fails = {}  # chat id -> [wrong tries, blocked until]


def tg(method, _timeout=20, **params):
    """Call the Telegram Bot API; returns its "result"."""
    if os.environ.get("DRY_RUN"):  # testing without a bot: log what would be sent
        print("[DRY_RUN] telegram %s:" % method, str(params.get("text", params)).replace("\n", " | "), flush=True)
        return []
    data = urllib.parse.urlencode(params).encode()
    url = "https://api.telegram.org/bot%s/%s" % (os.environ["TG_TOKEN"], method)
    with urllib.request.urlopen(url, data=data, timeout=_timeout) as r:
        return json.loads(r.read())["result"]


def subscribers():
    """{chat id: name}. The first time, the chat that ran setup.sh (TG_CHAT_ID) is the only subscriber."""
    if SUBS.exists():
        return json.loads(SUBS.read_text())
    return {os.environ["TG_CHAT_ID"]: "owner"} if os.environ.get("TG_CHAT_ID") else {}


def save_subscribers(subs):
    SUBS.write_text(json.dumps(subs, indent=1))


def telegram(text):
    """Send to every subscriber. Raises only if nobody could be reached, so the alert is retried."""
    with sub_lock:
        subs = subscribers()
        if not subs:
            print("no subscribers, alert not sent:", text.replace("\n", " | "), flush=True)
            return
        reached = 0
        for chat in list(subs):
            try:
                tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview="true")
                reached += 1
            except urllib.error.HTTPError as e:
                if e.code == 403:  # user blocked the bot or left the group: drop them
                    subs.pop(chat)
                    save_subscribers(subs)
                print("telegram to %s failed: HTTP %d" % (chat, e.code), file=sys.stderr, flush=True)
            except Exception as e:
                print("telegram to %s failed: %s" % (chat, e), file=sys.stderr, flush=True)
        if not reached:
            raise RuntimeError("no subscriber reached")


def check_password(pw):
    """BOT_PASSWORD_HASH = pbkdf2$<salt hex>$<sha256 hex>, written by set-bot-password.sh."""
    try:
        _, salt, good = os.environ["BOT_PASSWORD_HASH"].split("$")
    except (KeyError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(got, good)


SECTIONS = [("infra", "🛠 INFRA (infra team)"), ("us", "🧩 OURS (MLOps)"), ("monitor", "📡 PROBE (the monitor itself)")]


def status_text():
    """Every check from the latest report, grouped by owner, down first. Down checks show why."""
    state = load()
    age = time.time() - state["last_report"]
    current = {n: c for n, c in state["checks"].items() if c.get("seen", 0) >= state["last_report"] - 1}
    out = ["Sakoo status, last report %d s ago%s" % (age, " (⚠️ probe silent)" if state.get("silent") else "")]
    for owner, title in SECTIONS:
        rows = sorted((n, c) for n, c in current.items() if c.get("owner") == owner)
        if not rows:
            continue
        down = [(n, c) for n, c in rows if c["fails"]]
        out.append("\n%s: %d up, %d down" % (title, len(rows) - len(down), len(down)))
        out += ["❌ %s: %s" % (n, c.get("detail", "")) for n, c in down]
        out += ["✅ %s" % n for n, c in rows if not c["fails"]]
    return "\n".join(out)


HELP = ("Sakoo infra monitor.\n/login <password>: get alerts\n/logout: stop alerts\n/status: what is failing now\n"
        "Alert types: 🔴 infra team, 🟠 ours (MLOps), ⚠️ the probe itself.")


def handle(update):
    """One Telegram update: the /login, /logout, /status and /help commands."""
    m = update.get("message") or {}
    chat, text = str(m.get("chat", {}).get("id", "")), (m.get("text") or "").strip()
    if not chat or not text:
        return
    cmd = text.split()[0].split("@")[0].lower()
    reply = lambda t: tg("sendMessage", chat_id=chat, text=t)
    if cmd == "/login":
        try:  # keep the password out of the chat history
            tg("deleteMessage", chat_id=chat, message_id=m["message_id"])
        except Exception:
            pass
        tries, until = login_fails.get(chat, [0, 0])
        if time.time() < until:
            return reply("Too many wrong passwords. Try again in %d min." % ((until - time.time()) // 60 + 1))
        if check_password(text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""):
            login_fails.pop(chat, None)
            frm = m.get("from", {})
            who = m.get("chat", {}).get("title") or frm.get("username") or frm.get("first_name", "?")
            with sub_lock:
                subs = subscribers()
                subs[chat] = who
                save_subscribers(subs)
            return reply("✅ Logged in. You will get Sakoo infra alerts.\n" + HELP)
        tries += 1
        login_fails[chat] = [0, time.time() + BLOCK_S] if tries >= MAX_TRIES else [tries, 0]
        return reply("❌ Wrong password.")
    if cmd == "/logout":
        with sub_lock:
            subs = subscribers()
            subs.pop(chat, None)
            save_subscribers(subs)
        return reply("Logged out. No more alerts.")
    if cmd == "/status":
        return reply(status_text() if chat in subscribers() else "Log in first: /login <password>")
    reply(HELP)


def poll_loop():
    """Long-poll Telegram for commands. Runs in a thread next to the HTTP server."""
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    while True:
        try:
            for u in tg("getUpdates", _timeout=60, offset=offset, timeout=50, allowed_updates='["message"]'):
                offset = u["update_id"] + 1
                OFFSET.write_text(str(offset))
                try:
                    handle(u)
                except Exception as e:
                    print("bot command failed:", e, file=sys.stderr, flush=True)
        except Exception as e:
            print("getUpdates failed:", e, file=sys.stderr, flush=True)
            time.sleep(10)


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"checks": {}, "last_report": time.time(), "silent": False}


def send(msgs, state):
    """Send, then persist. On Telegram failure keep the old state so the alert is retried."""
    if msgs:
        try:
            telegram("\n".join(msgs) + "\n" + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
        except Exception as e:
            print("telegram failed:", e, file=sys.stderr, flush=True)
            return
    STATE.write_text(json.dumps(state, indent=1))


def apply(results):
    """Update per-check state from one report; return the messages to send."""
    with lock:
        state, msgs = load(), []
        if state["silent"]:
            msgs.append("✅ PROBE BACK: reports arrive again")
        state.update(last_report=time.time(), silent=False)  # set before "seen", so /status can tell current checks
        for name, (ok, detail, owner) in results.items():
            s = state["checks"].setdefault(name, {"fails": 0, "alerted": False})
            s.update(detail=detail, owner=owner, seen=time.time())  # for /status
            if ok:
                if s["alerted"]:
                    msgs.append("✅ RECOVERED [%s] %s: %s" % (owner, name, detail))
                s.update(fails=0, alerted=False)
            else:
                s["fails"] += 1
                if s["fails"] >= FAIL_AFTER and not s["alerted"]:
                    msgs.append("%s %s: %s (%d reports in a row)" % (LABEL.get(owner, owner), name, detail, s["fails"]))
                    s["alerted"] = True
        send(msgs, state)
        return msgs


def watchdog_tick():
    """No report for SILENT_AFTER seconds: the cluster, its outbound network, or the probe is down."""
    with lock:
        state = load()
        quiet = time.time() - state["last_report"]
        if quiet > SILENT_AFTER and not state["silent"]:
            state["silent"] = True
            send(["🔴 PROBE SILENT: no report for %d min. Either the probe (its machine, VPN or internet) is down, "
                  "or Sakoo is unreachable from it. Check the probe first." % (quiet // 60)], state)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._reply(200 if self.path == "/health" else 404, b"ok\n" if self.path == "/health" else b"")

    def do_POST(self):
        body = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1_000_000))
        good = hmac.new(os.environ["PROBE_KEY"].encode(), body, hashlib.sha256).hexdigest()
        if self.path != "/report" or not hmac.compare_digest(good, self.headers.get("X-Signature", "")):
            return self._reply(403, b"bad signature\n")
        msg = json.loads(body)
        if abs(time.time() - msg["ts"]) > MAX_SKEW:
            return self._reply(403, b"stale report\n")
        apply(msg["results"])
        self._reply(200, b"ok\n")

    def _reply(self, code, body):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the log to errors only
        if not str(args[1]).startswith("2"):
            super().log_message(fmt, *args)


def main():
    if "--test" in sys.argv:
        telegram("Sakoo infra monitor: receiver test message")
        print("sent")
        return

    def loop():
        while True:
            time.sleep(30)
            watchdog_tick()

    if not STATE.exists():  # start the silence clock now, so a probe that never reports is noticed
        STATE.write_text(json.dumps(load()))
    threading.Thread(target=loop, daemon=True).start()
    if os.environ.get("TG_TOKEN") and not os.environ.get("DRY_RUN"):
        threading.Thread(target=poll_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8090))
    print("listening on :%d" % port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
