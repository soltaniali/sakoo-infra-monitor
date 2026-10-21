#!/usr/bin/env python3
"""Sakoo ops dashboard: team login, JSON API and the page; runs the collector (ops.py) in a thread. Stdlib only.

  ops_web.py                 serve on $OPS_BIND:$OPS_PORT (default 0.0.0.0:8443; compose publishes it on 127.0.0.1 only)
  ops_web.py adduser NAME    add a user or change their password (asked twice, hidden)
  ops_web.py deluser NAME
  ops_web.py --once          one collector round, print the overview tiles, exit
  ops_web.py status          what the running dashboard has stored: open incidents, changes, inventory (read-only)
Users: $OPS_DATA/ops-users.json (scrypt hashes). HTTPS when /secrets/ops-tls.crt and /secrets/ops-tls.key exist.
OPS_ALLOW_CIDRS="a.b.c.d/24,...": only these client addresses may connect (when the port is published to a network).
The API only reads: the collector sends GET requests to Kubernetes, and nothing here can change the cluster.
"""
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ops

HERE = Path(__file__).resolve().parent
USERS = ops.DATA / "ops-users.json"
TLS = (Path("/secrets/ops-tls.crt"), Path("/secrets/ops-tls.key"))
SESSION_TTL, MAX_FAILS, BLOCK_S = 12 * 3600, 5, 900
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")  # Kubernetes object names
HEADERS = {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
           "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
ALLOW = [ipaddress.ip_network(c.strip(), strict=False) for c in os.environ.get("OPS_ALLOW_CIDRS", "").split(",") if c.strip()]
sessions, fails, lock = {}, {}, threading.Lock()
COL = None  # the collector, set in main()
USE_TLS = False


class BadRequest(Exception):
    pass


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    return "scrypt$%s$%s" % (salt.hex(), hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex())


def check_pw(user, pw):
    try:
        stored = json.loads(USERS.read_text())[user]
        salt = bytes.fromhex(stored.split("$")[1])
    except (OSError, KeyError, ValueError, IndexError):
        hash_pw(pw)  # same work for an unknown user, so timing does not tell which users exist
        return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def allowed(ip):
    return not ALLOW or any(ipaddress.ip_address(ip) in n for n in ALLOW)


def name(q, key, required=True):
    v = q.get(key, "")
    if (v or required) and not NAME_RE.match(v):
        raise BadRequest("bad %s" % key)
    return v


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "sakoo-ops", ""

    def send(self, code, body, ctype="application/json", extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, default=str).encode()
        self.send_response(code)
        for k, v in list(HEADERS.items()) + list(extra):
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ops":
                return v
        return ""

    def user(self):
        with lock:
            s = sessions.get(self.token())
        return s[0] if s and s[1] > time.time() else None

    def body(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):  # forms cannot send this cross-site
            raise BadRequest("JSON body required")
        return json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20000)) or b"{}")

    def do_GET(self):
        if not allowed(self.client_address[0]):
            return self.send(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if path == "/healthz":
            return self.send(200, b"ok\n", "text/plain")
        if path in ("/", "/index.html"):
            return self.send(200, (HERE / "ops.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/ops.js":
            return self.send(200, (HERE / "ops.js").read_bytes(), "text/javascript; charset=utf-8")
        if not path.startswith("/api/"):
            return self.send(404, {"error": "not found"})
        user = self.user()
        if not user:
            return self.send(401, {"error": "login required"})
        try:
            return self.send(200, self.api(path[5:], q, user))
        except (BadRequest, KeyError, ValueError) as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            print("[ops] GET %s failed: %s" % (path, ops.probe.err(e)), flush=True)
            return self.send(502, {"error": ops.probe.err(e)})

    def api(self, what, q, user):
        if what == "me":
            return {"user": user, "integrations": COL.integrations()}
        if what == "state":
            return COL.state()
        if what == "incidents":
            return COL.incidents(q.get("state", "open"), q.get("kind"))
        if what == "incident":
            return COL.incident(int(q["id"]))
        if what == "changes":
            return COL.changes(min(max(int(q.get("days", 1)), 1), 30), q.get("type") or None)
        if what == "models":
            return COL.models()
        if what == "namespaces":
            return COL.namespaces_info()
        if what == "services":
            return COL.services(self.known_ns(q))
        if what == "infra":
            return COL.infra()
        if what == "logs":
            ns, pod, container = self.known_ns(q), name(q, "pod"), name(q, "container")
            tail = min(max(int(q.get("tail", 300)), 1), 3000)
            return {"text": COL.log_text(ns, pod, container, tail, q.get("previous") == "1")}
        raise BadRequest("unknown API " + what)

    @staticmethod
    def known_ns(q):
        ns = name(q, "ns")
        if ns not in COL.inv:  # only namespaces the collector could read
            raise BadRequest("unknown namespace")
        return ns

    def do_POST(self):
        if not allowed(self.client_address[0]):
            return self.send(403, {"error": "forbidden"})
        ip = self.client_address[0]
        try:
            if self.path == "/api/login":
                return self.login(ip, self.body())
            if self.path == "/api/logout":
                with lock:
                    sessions.pop(self.token(), None)
                return self.send(200, {"ok": True}, extra=[("Set-Cookie", "ops=; Max-Age=0; Path=/")])
            user = self.user()
            if not user:
                return self.send(401, {"error": "login required"})
            if self.path == "/api/summarize":
                return self.summarize(self.body(), user)
            return self.send(404, {"error": "not found"})
        except (BadRequest, KeyError, ValueError) as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            print("[ops] POST %s failed: %s" % (self.path, ops.probe.err(e)), flush=True)
            return self.send(502, {"error": ops.probe.err(e)})

    def login(self, ip, b):
        with lock:
            n, until = fails.get(ip, [0, 0])
        if time.time() < until:
            return self.send(429, {"error": "too many wrong passwords, try again in %d min" % ((until - time.time()) // 60 + 1)})
        user = str(b.get("user", ""))[:64]
        if not check_pw(user, str(b.get("password", ""))[:256]):
            with lock:
                fails[ip] = [0, time.time() + BLOCK_S] if n + 1 >= MAX_FAILS else [n + 1, 0]
            print("[ops] login failed for %r from %s" % (user, ip), flush=True)
            return self.send(403, {"error": "wrong user or password"})
        tok = secrets.token_urlsafe(32)
        with lock:
            fails.pop(ip, None)
            now = time.time()
            for k in [k for k, s in sessions.items() if s[1] < now]:
                del sessions[k]
            sessions[tok] = (user, now + SESSION_TTL)
        print("[ops] %s logged in from %s" % (user, ip), flush=True)
        cookie = "ops=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d%s" % (tok, SESSION_TTL, "; Secure" if USE_TLS else "")
        return self.send(200, {"user": user}, extra=[("Set-Cookie", cookie)])

    def summarize(self, b, user):
        if not COL.integrations()["litellm"]:
            raise BadRequest("log summaries are off: set LITELLM_URL, LITELLM_KEY and LITELLM_MODEL")
        if "incident" in b:
            text = COL.incident_text(int(b["incident"]))
        else:
            q = {k: str(b.get(k, "")) for k in ("ns", "pod", "container")}
            ns = self.known_ns(q)
            text = "Kubernetes pod %s/%s, container %s. Log:\n%s" % (ns, name(q, "pod"), name(q, "container"),
                                                                    COL.log_text(ns, q["pod"], q["container"], 400, bool(b.get("previous"))))
        print("[ops] %s asked for a summary" % user, flush=True)
        summary = COL.llm(text)
        if "incident" in b:
            COL.exec("UPDATE incidents SET summary=? WHERE id=?", (summary, int(b["incident"])))
        return self.send(200, {"summary": summary})

    def log_message(self, fmt, *args):  # errors only
        if len(args) > 1 and not str(args[1]).startswith(("2", "3", "401")):
            super().log_message(fmt, *args)


def users_cli(cmd, who):
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", who or ""):
        sys.exit("name: 2-32 characters of a-z 0-9 . _ -")
    users = json.loads(USERS.read_text()) if USERS.exists() else {}
    if cmd == "deluser":
        users.pop(who, None)
    else:
        pw = getpass.getpass("password for %s (at least 10 characters): " % who)
        if len(pw) < 10:
            sys.exit("too short")
        if getpass.getpass("again: ") != pw:
            sys.exit("the two passwords differ")
        users[who] = hash_pw(pw)
    USERS.parent.mkdir(parents=True, exist_ok=True)
    USERS.write_text(json.dumps(users, indent=1))
    os.chmod(USERS, 0o600)
    print("users:", ", ".join(sorted(users)) or "(none)")


def status_cli():
    import sqlite3
    c = sqlite3.connect("file:%s?mode=ro" % (ops.DATA / "ops.db"), uri=True)
    q = lambda s, a=(): c.execute(s, a).fetchall()
    t = time.time()
    print("namespaces read: %d · inventory: %s" % (q("SELECT count(*) FROM meta WHERE k LIKE 'seen:%'")[0][0],
          ", ".join("%s %d" % r for r in q("SELECT substr(key, 1, instr(key, '/') - 1) k, count(*) FROM inventory GROUP BY k"))))
    print("changes 24 h: %s" % (", ".join("%s %d" % r for r in q("SELECT type, count(*) FROM changes WHERE ts>? GROUP BY type", (t - 86400,))) or "none"))
    print("error signatures: %d · users: %s" % (q("SELECT count(*) FROM signatures")[0][0],
          ", ".join(json.loads(USERS.read_text())) if USERS.exists() else "none"))
    for r in q("SELECT id, symptom, ns, obj, owner, alerted, cause FROM incidents WHERE state='open' ORDER BY id"):
        print("OPEN #%d %-13s %s/%s · owner %s%s\n     %s" % (r[0], r[1], r[2], r[3], r[4], " · alerted" if r[5] else "", r[6][:160]))


def main():
    global COL, USE_TLS
    if sys.argv[1:2] in (["adduser"], ["deluser"]):
        return users_cli(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    if sys.argv[1:2] == ["status"]:
        return status_cli()
    COL = ops.Collector()
    if "--once" in sys.argv:
        COL.run_round()
        print(json.dumps({"tiles": COL.state()["tiles"], "status": COL.status}, indent=1, default=str))
        return
    threading.Thread(target=COL.run_forever, daemon=True).start()
    port = int(os.environ.get("OPS_PORT", 8443))
    srv = ThreadingHTTPServer((os.environ.get("OPS_BIND", "0.0.0.0"), port), Handler)
    if TLS[0].exists() and TLS[1].exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*TLS)
        # handshake in the request thread, so a slow client cannot block accept()
        srv.socket, USE_TLS = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False), True
    print("[ops] dashboard on :%d (%s), users: %d" % (port, "https" if USE_TLS else "http",
                                                      len(json.loads(USERS.read_text())) if USERS.exists() else 0), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
