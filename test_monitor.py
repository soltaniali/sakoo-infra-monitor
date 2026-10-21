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
r = probe.check_assets()
assert r["a (P, Critical)"][0] and r["a (P, Critical)"][2] == "asset", r                                 # app's own 404 = alive
assert not r["b (Q, Low)"][0] and "no ready pod" in r["b (Q, Low)"][1], r
assert not r["c (?, ?)"][0] and "no route" in r["c (?, ?)"][1], r  # route gone: traefik fallback page
assert not r["d (?, ?)"][0] and "502" in r["d (?, ?)"][1], r

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
assert "Logged out" in msg(42, "/logout") and receiver.subscribers() == {}
receiver.telegram("nobody listens")                       # no subscribers: no crash
print("ok")
