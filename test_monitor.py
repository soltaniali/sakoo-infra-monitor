"""End-to-end test: probe -> receiver over HTTP with HMAC, alert state, probe-silent watchdog. Run: python3 test_monitor.py"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

here = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp())
shutil.copy(here / "k8s" / "probe.py", tmp)
shutil.copy(here / "receiver.py", tmp)
shutil.copy(here / "dashboard.py", tmp)
(tmp / "probe.json").write_text(json.dumps({"receiver_url": "http://127.0.0.1:18090/report", "nodes": False}))
os.environ.update(PROBE_CONFIG=str(tmp / "probe.json"), PROBE_KEY="k1", TG_TOKEN="x", TG_CHAT_ID="1")
sys.path.insert(0, str(tmp))
import probe  # noqa: E402
import receiver  # noqa: E402

sent = []
receiver.telegram = sent.append
srv = ThreadingHTTPServer(("127.0.0.1", 18090), receiver.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def report(ok):
    probe.report({"harbor health": [ok, "detail", "infra"]})


report(False)
assert sent == [], sent                                   # 1st failure: quiet
report(False)
assert len(sent) == 1 and "INFRA DOWN" in sent[0], sent
report(False)
assert len(sent) == 1, sent                               # no repeat
report(True)
assert len(sent) == 2 and "RECOVERED" in sent[1], sent

import urllib.request  # noqa: E402                        # bad signature is rejected
body = json.dumps({"ts": int(time.time()), "results": {"x": [False, "d", "infra"]}}).encode()
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18090/report", data=body,
                                                  headers={"X-Signature": "00" * 32}), timeout=5)
    raise AssertionError("bad signature accepted")
except urllib.error.HTTPError as e:
    assert e.code == 403
body = json.dumps({"ts": int(time.time()), "text": "AIP ops\n\nOPEN #1"}).encode()   # /alert relays the ops dashboard's text
sig = probe.hmac.new(b"k1", body, probe.hashlib.sha256).hexdigest()
urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18090/alert", data=body, headers={"X-Signature": sig}), timeout=5)
assert sent[-1] == "AIP ops\n\nOPEN #1", sent
sent.pop()
body = json.dumps({"ts": int(time.time()), "text": "dup", "id": "ops-7"}).encode()      # same batch id twice: sent once
sig = probe.hmac.new(b"k1", body, probe.hashlib.sha256).hexdigest()
for _ in range(2):
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18090/alert", data=body, headers={"X-Signature": sig}), timeout=5)
assert sent.count("dup") == 1, sent
sent.remove("dup")

st = json.loads(receiver.STATE.read_text())               # probe silent, then back
st["last_report"] = time.time() - 1000
receiver.STATE.write_text(json.dumps(st))
receiver.watchdog_tick()
assert "PROBE SILENT" in sent[-1], sent
receiver.watchdog_tick()
assert sum("PROBE SILENT" in m for m in sent) == 1, sent  # once
report(True)
assert "PROBE BACK" in sent[-1], sent

probe.time.time = lambda real=probe.time.time: real() + 30 * 86400   # host clock a month ahead
probe.sync_clock()
assert abs(probe.now() - time.time()) < 5, probe.CLOCK          # corrected from the receiver's Date header
report(True)                                              # accepted despite the wrong host clock
probe.time.time = time.time
res = probe.run_checks()                                  # probe check functions on this machine
assert probe.check_dns("localhost")[0] and not probe.check_dns("no-such-host.invalid")[0]

# --- asset checks: ready pods behind the Service, URL answers, route not replaced by traefik's fallback page
probe.CFG.update(assets=[{"name": "a", "person": "P", "value": "Critical", "url": "https://h/a"},
                         {"name": "b", "person": "Q", "value": "Low"},
                         {"name": "c", "url": "https://h/c"}, {"name": "d", "url": "https://h/d"}])
probe.kube = lambda: ("https://api", None)
eps = {"items": [{"metadata": {"name": n}, "subsets": [{"addresses": [{}] * k}] if k else []}
                 for n, k in (("a", 1), ("b", 0), ("c", 1), ("d", 1))]}
probe.get = lambda url, **kw: (200, json.dumps(eps).encode())
pages = {"https://h/sakoo-probe-no-such-path": (200, b"<html>dashboard"), "https://h/a": (404, b'{"detail":"Not Found"}'),
         "https://h/c": (200, b"<html>dashboard"), "https://h/d": (502, b"Bad Gateway")}
probe.fetch = lambda url, ip=None: pages[url]
probe.CFG["assets"].append({"name": "e", "url": "https://h/e/health", "expect": "healthy"})
probe.CFG["assets"].append({"name": "f", "url": "https://h/f/health", "expect": "healthy"})
eps["items"] += [{"metadata": {"name": n}, "subsets": [{"addresses": [{}]}]} for n in ("e", "f")]
pages.update({"https://h/e/health": (200, b'{"status":"healthy"}'), "https://h/f/health": (200, b'{"status":"degraded"}')})
r = probe.check_assets()
assert r["e"][0] and r["e"][3]["test"] == "h/e/health 200", r     # health endpoint OK
assert not r["f"][0], r                                     # 200 but not healthy
assert r["a"][0] and r["a"][2] == "asset", r                                 # app's own 404 = alive
assert not r["b"][0] and "no ready pod" in r["b"][1], r
assert not r["c"][0] and "no route" in r["c"][1], r  # route gone: traefik fallback page
assert not r["d"][0] and "502" in r["d"][1], r

import tempfile as _t                                      # VPN check: interface up but OpenVPN reconnecting = down
log = Path(_t.mkdtemp()) / "vpn.log"
LOG = ["x Initialization Sequence Completed", "x [srv] Inactivity timeout (--ping-restart), restarting",
       "2026-11-20 01:00:43 TCP: connect to [AF_INET]5.202.5.212:7071 failed: Operation timed out",
       "x SIGUSR1[connection failed(soft)] received, process restarting"]
log.write_text("\n".join(LOG) + "\n")
probe.Path = Path
real_read = Path.read_text
Path.read_text = lambda self, *a, **k: "unknown" if str(self).endswith("operstate") else real_read(self, *a, **k)
ok, why = probe.check_vpn("tun9", str(log))
assert not ok and "5.202.5.212" in why, why
log.write_text("\n".join(LOG + ["x Initialization Sequence Completed"]) + "\n")
assert probe.check_vpn("tun9", str(log))[0]
Path.read_text = real_read
srv.shutdown()

# --- bot: /login with password, rate limit, broadcast to subscribers, /status, /logout
import hashlib  # noqa: E402

salt = "00" * 16
os.environ["BOT_PASSWORD_HASH"] = "pbkdf2$%s$%s" % (salt, hashlib.pbkdf2_hmac("sha256", b"s3cret", bytes.fromhex(salt), 200_000).hex())
calls, blocked = [], set()


def fake_tg(method, _timeout=20, **p):
    calls.append((method, p))
    if method == "sendMessage" and p["chat_id"] in blocked:
        raise urllib.error.HTTPError("u", 403, "blocked", {}, None)
    return []


import importlib  # noqa: E402

receiver = importlib.reload(receiver)                     # fresh module: real telegram(), no patches
receiver.tg = fake_tg
os.environ.pop("TG_CHAT_ID")                              # start with no subscribers


def msg(chat, text, mid=1):
    receiver.handle({"message": {"message_id": mid, "chat": {"id": chat}, "from": {"username": "u%s" % chat}, "text": text}})
    return calls[-1][1].get("text", "")


assert "Wrong password" in msg(42, "/login nope")
assert ("deleteMessage", {"chat_id": "42", "message_id": 1}) in calls      # password message removed
assert "Logged in" in msg(42, "/login s3cret")
assert receiver.subscribers() == {"42": "u42"}
for _ in range(5):
    msg(7, "/login bad")
assert "Too many wrong" in msg(7, "/login s3cret")       # blocked even with the right password
assert "Log in first" in msg(7, "/status")
assert "Logged in" in msg(666, "/login s3cret")
blocked.add("666")                                        # then 666 blocks the bot

receiver.telegram = lambda t: None
receiver.apply({"harbor health": [True, "HTTP 200", "infra"], "host x": [False, "HTTP 503", "us"]})
receiver.telegram = receiver.__dict__["telegram"]
receiver = importlib.reload(receiver)
receiver.tg = fake_tg
calls.clear()
receiver.telegram("🔴 INFRA DOWN test")                   # broadcast: 42 gets it, 666 (403) is dropped
assert [p["chat_id"] for m, p in calls if m == "sendMessage"] == ["42", "666"]
assert receiver.subscribers() == {"42": "u42"}
st = msg(42, "/status")
assert "INFRA" in st and "OURS" in st and "❌ host x" in st and "✅ harbor health" in st, st
assert calls[-1][1].get("parse_mode") == "HTML"
receiver.apply({"svc-a": [True, "1 ready pod(s), /a 200", "asset", {"person": "Armin", "value": "Critical", "test": "/a 200"}],
                "svc-b": [False, "no ready pod <x>", "asset", {"person": "Dariush", "value": "Critical", "test": "pods"}]})
st = receiver.status_text()
assert "<b>Critical</b>" in st and "svc-a  Armin" in st and "↳ no ready pod &lt;x&gt;" in st, st   # aligned, escaped
assert st.index("❌ svc-b") < st.index("✅ svc-a"), st                                            # down first
real_tg = receiver.tg                                     # /channel: a channel becomes a subscriber, gets every alert
receiver.tg = lambda m, _timeout=20, **p: {"id": -100123, "title": "AIP alerts"} if m == "getChat" else real_tg(m, _timeout, **p)
assert "Log in first" in msg(9, "/channel @aipalerts")
assert "added" in msg(42, "/channel @aipalerts") and receiver.subscribers()["-100123"] == "channel AIP alerts"
calls.clear()
receiver.telegram("🔴 test")
assert "-100123" in [p["chat_id"] for m, p in calls if m == "sendMessage"]
assert "removed" in msg(42, "/unchannel @aipalerts") and "-100123" not in receiver.subscribers()
receiver.tg = real_tg
assert "Logged out" in msg(42, "/logout") and receiver.subscribers() == {}
receiver.telegram("nobody listens")                       # no subscribers: no crash

# --- dashboard: history of changes, incidents, snapshot, signed links
import dashboard  # noqa: E402
dashboard.DB.unlink(missing_ok=True)
T = time.time()
dashboard.record("api", "infra", True, "HTTP 200", T - 7200)
dashboard.record("api", "infra", False, "timed out <x>", T - 3600)
dashboard.record("api", "infra", True, "HTTP 200", T - 1800)
segs = dashboard.intervals(T - 5400, T)["api"][1]
assert [s[2] for s in segs] == [1, 0, 1] and abs(segs[1][1] - segs[1][0] - 1800) < 1, segs   # up, down 30 min, up
tok = dashboard.make_token()
assert dashboard.token_ok(tok) and not dashboard.token_ok("1.abc") and not dashboard.token_ok(dashboard.make_token(T - 2 * 86400))
pg = dashboard.page("t=%s&range=6h" % tok, {"api"})
assert "Incidents in the last 6h · 1" in pg and "timed out &lt;x&gt;" in pg and "<svg" in pg, pg[:500]
dashboard.record("training-api", "asset", True, "ok", T - 100)
pa = dashboard.page("t=%s&range=1h" % tok, {"api", "training-api"}, {"training-api": {"person": "Dariush", "value": "Critical", "test": "/train/ping 200"}})
assert "training-api <span class=mut>· Dariush</span>" in pa and "Dariush · Critical" in pa and "/train/ping 200" in pa and ">Critical</div>" in pa, pa[-1500:]
dashboard.record("postgres running", "infra", True, "ok", T - 100)        # infra check with meta: flat, no importance groups
pi = dashboard.page("t=%s&range=1h" % tok, {"postgres running"}, {"postgres running": {"person": "Infra", "value": "Critical", "test": "pods"}})
assert "postgres running" in pi and ">Critical</div>" not in pi and "Infra · Critical" not in pi, pi[-1500:]
snap = dashboard.page("t=%s&at=%d" % (tok, T - 3000), {"api"})
assert "Snapshot at" in snap and "❌ api: timed out &lt;x&gt;" in snap and "0 up, 1 down" in snap, snap[-800:]   # same list as /status
assert "<b" not in dashboard.page("t=%s&range=bogus" % tok, {"api"})[:0]   # unknown range falls back to 24h
print("ok")
