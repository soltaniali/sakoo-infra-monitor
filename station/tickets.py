"""Support tickets (the team's ticket site, support.aict-lab.ir) checked against what the ops collector knows.
The site has no API: we log in like a browser and read its HTML pages. GET only after the login POST; we never reply,
close or change a ticket. Opening a ticket page marks it read for the account we use, so we open a ticket only when it
changed (new message, new status). On when TICKETS_URL, TICKETS_USER and TICKETS_PASSWORD are set (secrets/ops.env).

For each ticket, six answers: 1 filed correctly? 2 how it was used + logs, 3 infra used, 4 services used, 5 down/problems
at that time, 6 what for (fine-tune, LLM inference, ...). Answers that need access we do not have yet are 'locked'.
"""
import calendar
import hashlib
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SYNC_S = 600
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
SECRET_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{6,}|(?:hf|ghp|glpat)[-_][A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]+)")
FAMILIES = ("qwen", "gemma", "deepseek", "llama", "mistral", "phi", "glm", "gpt", "whisper", "e5", "bge", "embed", "minicpm", "aya")
PURPOSES = [  # first match wins; words in English and Persian as users write them
    ("fine-tune", r"fine.?tun|finetun|فاین|آموزش مدل|ترین|\btrain|lora|dataset|دیتاست|mlops-core|clearml|epoch"),
    ("embedding", r"embed|امبد|\be5\b|\bbge\b|vector|بردار"),
    ("LLM inference", r"base_url|/v1\b|chat|completion|prompt|پرامپت|reasoning|thinking|context|کانتکست|توکن ورودی|max_len|"
                      r"پاسخ.?گو|inference|استنتاج|qwen|gemma|deepseek|llama|مدل"),
    ("access / account", r"دسترسی|access|حساب|account|قطع سرویس|login|ورود|api key|کلید"),
]
ERROR_HINT = re.compile(r"\b[45]\d\d\b|error|exception|traceback|timeout|خطا|ارور|قطع|پاسخ.?گو نیست|loop|لوپ|none", re.I)
LOCKED = {
    "user_logs": "the requester's AIP username or key (data/ticket-users.json or the name list) and Langfuse keys: sh station/set-langfuse-keys.sh",
    "user_infra": "the requester's AIP username: add it to data/ticket-users.json on the station ({\"name on ticket\": \"username\"})",
    "user_services": "the requester's AIP username (data/ticket-users.json on the station) or their API key hash",
    "key_usage": "a Grafana login on the station: sh station/set-grafana-login.sh",
}
# LiteLLM's Prometheus counters, per API key (label hashed_api_key = sha256 of the key); names differ between versions
USAGE_Q = ('sum by (__name__, model) (increase({__name__=~"litellm_(requests|total_tokens|spend|proxy_failed_requests)_metric(_total)?'
           '|litellm_total_tokens(_total)?", %s}[%s]))')
USERS_FILE = "ticket-users.json"  # in OPS_DATA, edited by hand: {"requester name on the ticket site": "AIP username" or "key alias"}


def key_usage(col, selectors, at):
    """Requests, tokens, spend and failures per model, last 30 days and the day around the ticket.
    selectors: (label shown, PromQL label matcher), one per key hash or user."""
    out = []
    for key, sel in selectors:
        for label, rng, when in (("last 30 days", "30d", None), ("24 h around the ticket", "24h", at + 12 * 3600)):
            rows = col.cached("key:%s:%s" % (sel, label), 900, lambda: col.prom(USAGE_Q % (sel, rng), when)) or []
            for r in rows:
                v = float(r["value"][1])
                if v:
                    name = r["metric"].get("__name__", "")
                    out.append({"key": key, "period": label, "model": r["metric"].get("model", "?"), "value": round(v, 2),
                                "metric": next(k for k in ("failed", "tokens", "spend", "requests") if k in name)})
    return out


def ticket_user(col, d, use):
    """AIP username of the requester: from the hand-kept map, else the owner of an API key posted in the ticket."""
    try:
        known = json.loads((Path(os.environ.get("OPS_DATA", "/data")) / USERS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if known.get(d.get("requester")):
        return known[d["requester"]], "map"
    # the team's name list: a requester "<person> - <university>" contains the university of "دانشگاه <university>"
    req = re.sub(r"\s+", " ", d.get("requester") or "")
    for p in col.people():
        core = re.sub(r"^(دانشگاه|شرکت)\s+", "", p["name"])
        if len(core) >= 4 and core in req and (p["keys"] or p["users"]):
            return (p["keys"] or p["users"])[0], "name list"
    for who, r in use.items():
        for alias, k in r["keys"].items():
            if k["hash"] in (d.get("key_hashes") or []):
                return alias, "key"
    return None, None




def text_of(fragment):
    s = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def mask(s):
    """Keys users paste into tickets: keep only a hint, never the key."""
    return SECRET_RE.sub(lambda m: m.group(0)[:3] + "…" + m.group(0)[-4:], s or "")


def site_time(s):
    """'۲۰۲۶/۱۰/۰۴ - ۰۹:۰۳' (Tehran wall time, Gregorian) to epoch seconds; None if it does not parse."""
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\D+(\d{1,2}):(\d{2})", (s or "").translate(FA_DIGITS))
    return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0, 0)) - 3.5 * 3600 if m else None


def parse_list(page):
    """Rows of /tickets/?page=N, and whether a next page exists."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        m = re.search(r'href="/tickets/(\d+)/"', tr)
        if not m:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        g = lambda rx: (re.search(rx, tr, re.S) or [None, None])[1]
        out.append({"id": int(m.group(1)), "subject": text_of(g(r"<strong>(.*?)</strong>")), "number": text_of(g(r"<small>(.*?)</small>")),
                    "requester": text_of(re.sub(r"<i>.*?</i>", "", tds[1], flags=re.S)) if len(tds) > 1 else "",
                    "assignee": text_of(tds[2]) if len(tds) > 7 else "", "category": text_of(tds[3]) if len(tds) > 7 else "",
                    "status": g(r"status-(\w+)"), "priority": g(r"priority-(\w+)"), "last": site_time(text_of(g(r"<time>(.*?)</time>")))})
    return out, bool(re.search(r"[?&]page=\d+[^\"']*\"[^>]*>\s*بعدی", page))


def parse_ticket(page):
    """Thread of /tickets/<id>/: who wrote what, when. Keys are masked here, so they are never stored."""
    msgs, secret, hashes = [], False, set()
    for cls, body in re.findall(r'<article class="message ([^"]*)"[^>]*>(.*?)</article>', page, re.S):
        g = lambda rx: text_of((re.search(rx, body, re.S) or [None, ""])[1])
        raw = g(r'<div class="message-body">(.*?)</div>')
        secret = secret or bool(SECRET_RE.search(raw))
        hashes |= {hashlib.sha256(k.encode()).hexdigest() for k in re.findall(r"\bsk-[A-Za-z0-9_-]{6,}", raw)}  # LiteLLM's hashed_api_key
        msgs.append({"from": "staff" if "admin" in cls else "internal" if "internal" in cls else "user", "name": g(r"<strong>(.*?)</strong>"),
                     "role": g(r"<small>(.*?)</small>"), "ts": site_time(g(r"<time>(.*?)</time>")), "text": mask(raw)})
    created = re.search(r"ایجادشده توسط\s*<strong>(.*?)</strong>\s*در\s*([^<]+)", page, re.S)
    return {"messages": msgs, "created": site_time(created.group(2)) if created else (msgs[0]["ts"] if msgs else None),
            "secret_posted": secret, "key_hashes": sorted(hashes)}


class Site:
    def __init__(self, base, user, password):
        self.base, self.user, self.password = base.rstrip("/"), user, password
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                              urllib.request.ProxyHandler({}))

    def get(self, path):
        for i in range(3):  # the host's DNS fails now and then ("Try again"), mostly right after a restart
            try:
                with self.op.open(urllib.request.Request(self.base + path, headers={"User-Agent": "aip-ops"}), timeout=30) as r:
                    return r.geturl(), r.read().decode("utf-8", "replace")
            except urllib.error.URLError as e:
                if isinstance(e, urllib.error.HTTPError) or i == 2:
                    raise
                time.sleep(5)

    def login(self):
        _, page = self.get("/login/")
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
        body = urllib.parse.urlencode({"csrfmiddlewaretoken": token, "username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.base + "/login/", body, {"Referer": self.base + "/login/", "User-Agent": "aip-ops"})
        with self.op.open(req, timeout=30) as r:
            if "/login" in urllib.parse.urlparse(r.geturl()).path:
                raise RuntimeError("ticket site login failed (check TICKETS_USER / TICKETS_PASSWORD)")

    def tickets(self):
        out, n = [], 1
        while n <= 50:
            url, page = self.get("/tickets/?page=%d" % n)
            if "/login" in url:
                raise RuntimeError("ticket site session expired")
            rows, more = parse_list(page)
            out += rows
            if not more:
                return out
            n += 1
        return out

    def ticket(self, tid):
        return parse_ticket(self.get("/tickets/%d/" % tid)[1])


def enabled():
    e = os.environ.get
    return bool(e("TICKETS_URL") and e("TICKETS_USER") and e("TICKETS_PASSWORD"))


def sync(col):
    """Read the list; open only new or changed tickets; ask the LLM once per changed ticket. Runs on the collector's
    background thread."""
    site = Site(os.environ["TICKETS_URL"], os.environ["TICKETS_USER"], os.environ["TICKETS_PASSWORD"])
    site.login()
    t = time.time()
    for row in site.tickets():
        sig = "%s|%s|%s" % (row["last"], row["status"], row["assignee"])
        old = col.one("SELECT sig, data FROM tickets WHERE id=?", (row["id"],))
        if old and old["sig"] == sig:
            col.exec("UPDATE tickets SET seen=? WHERE id=?", (t, row["id"]))
            ai = col.one("SELECT ai FROM tickets WHERE id=?", (row["id"],))["ai"]
            if col.integrations()["ai"] and (not ai or "error" in json.loads(ai)):  # free tiers fail now and then: ask again
                col.exec("UPDATE tickets SET ai=? WHERE id=?", (json.dumps(ask_llm(col, json.loads(old["data"])), ensure_ascii=False), row["id"]))
            continue
        data = dict(row, **site.ticket(row["id"]))
        col.exec("INSERT OR REPLACE INTO tickets (id, sig, data, fetched, seen, ai) VALUES (?, ?, ?, ?, ?, NULL)",
                 (row["id"], sig, json.dumps(data, ensure_ascii=False), t, t))
        if col.integrations()["ai"]:
            col.exec("UPDATE tickets SET ai=? WHERE id=?", (json.dumps(ask_llm(col, data), ensure_ascii=False), row["id"]))
    col.set_meta("tickets_synced", str(t))


def ask_llm(col, d):
    thread = "\n\n".join("[%s] %s" % (m["from"], m["text"]) for m in d["messages"])[:5000]
    sysmsg = ("You check support tickets of AIP, an AI platform (LLM API through LiteLLM, fine-tuning through mlops-core, notebooks). "
              "Tickets are in Persian. Reply with one JSON object only, plain text, no markdown: "
              '{"filed_ok": true|false, "missing": [], "purpose": "fine-tune"|"LLM inference"|"embedding"|"access / account"|"other", '
              '"summary": "one English sentence, at most 25 words: what the user wants and what is wrong"}. '
              '"missing" lists only what is really absent from the ticket, at most 4 items, each 2 to 5 words, chosen from: '
              '"model or service name", "error text or code", "time it happened", "request example", "steps to reproduce". '
              "If the ticket names something, it is not missing. Do not guess dates.")
    try:
        raw = col.llm("Subject: %s\n\n%s" % (d["subject"], thread), system=sysmsg, max_tokens=300)
        out = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        out["missing"] = [re.sub(r"[*_`]", "", str(x))[:40] for x in out.get("missing") or []][:4]
        out["summary"] = re.sub(r"[*_`]", "", str(out.get("summary") or ""))[:300]
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


# --- the six answers -----------------------------------------------------------------------------------------------
NEAR_S = 7200  # an incident that ended or started within 2 h of the moment the user wrote is shown as "near"


def tehran(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t + 3.5 * 3600))


def when_vs(i, moments):
    """How an incident relates to the moments the user wrote: ('active', t) if open at that moment, else
    ('ended N min before' / 'started N min after', t) if within NEAR_S. The closest moment wins."""
    best = None
    for m in moments:
        if i["opened"] <= m and (i["closed"] is None or i["closed"] >= m):
            return ("active", m)
        gap = m - i["closed"] if i["closed"] is not None and i["closed"] < m else i["opened"] - m
        if gap <= NEAR_S and (best is None or gap < best[0]):
            best = (gap, ("ended %d min before" if i["opened"] < m else "started %d min after") % round(gap / 60), m)
    return best and best[1:]
def words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def matched_models(col, text):
    """Models the ticket names: the full name, or a family word (qwen, gemma, ...) that is in the model name too."""
    low, ws, out = text.lower(), words(text), []
    for ns, inv in col.inv.items():
        for o in inv.values():
            if o["kind"] != "Model":
                continue
            fam = [f for f in FAMILIES if f in o["name"].lower() and f in ws]
            if o["name"].lower() in low or fam:
                out.append({"ns": ns, "name": o["name"], "exact": o["name"].lower() in low, "state": o.get("state"),
                            "since": o.get("since"), "checked": o.get("checked"), "ready": o["ready"], "min": o["min"]})
    exact = [m for m in out if m["exact"]]
    return exact or out


def analyze(col, d, ai=None):
    text = d["subject"] + "\n" + "\n".join(m["text"] for m in d["messages"] if m["from"] == "user")
    low, now = text.lower(), time.time()
    models = matched_models(col, text)
    hosts = sorted({h for h in col.hosts if h in low} | set(re.findall(r"https?://([a-z0-9.-]+)", low)))
    created = d.get("created") or d.get("last") or now
    # the moments the user reported something: when the ticket was filed and each later message of the user
    moments = sorted({created} | {m["ts"] for m in d["messages"] if m["from"] == "user" and m["ts"]})
    first_data = (col.one("SELECT min(opened) t FROM incidents") or {}).get("t") or now
    seen_moments = [m for m in moments if m >= first_data]
    objs = [(m["ns"], m["name"]) for m in models] + [(col.hosts[h], None) for h in hosts if h in col.hosts]
    incs = []
    for i in col.rows("SELECT id, ns, obj, symptom, owner, cause, opened, closed FROM incidents WHERE opened<=? AND (closed IS NULL OR closed>=?) "
                      "ORDER BY opened", (moments[-1] + NEAR_S, moments[0] - NEAR_S)):
        rel = when_vs(i, seen_moments)
        if rel:
            incs.append(dict(i, when=rel[0], at=rel[1]))
    mine = [i for i in incs if any(i["ns"] == ns and (o is None or i["obj"] == o or o in i["obj"]) for ns, o in objs)]
    platform = [i for i in incs if i["owner"] in ("infra", "us") and i not in mine and not i["ns"].startswith("user-")]
    covered = bool(seen_moments)
    mine_active = [i for i in mine if i["when"] == "active"]
    staff = [m["ts"] for m in d["messages"] if m["from"] == "staff" and m["ts"]]
    wait_h = round((min(staff) - created) / 3600, 1) if staff and created else None

    # 1. filed correctly?
    problems = []
    if len(re.sub(r"\W", "", text)) < 25:
        problems.append("looks like a test ticket (almost no text)")
    if not models and not hosts and not re.search(r"api|سرویس|service|notebook|jupyter|mlops", low):
        problems.append("no model or service named")
    if d.get("category") == "فنی" and not ERROR_HINT.search(text):
        problems.append("no error text, code or symptom")
    if d.get("secret_posted"):
        problems.append("an API key was posted in the ticket: ask the user to rotate it")
    if not d.get("assignee") or "تخصیص" in d.get("assignee", ""):
        problems.append("no one assigned")
    if mine_active:
        verdict = "confirmed: %d incident(s) on it were open at the exact time the user wrote" % len(mine_active)
    elif covered and models:
        verdict = "our monitoring saw no problem on %s when the user wrote%s" % (
            ", ".join(m["name"] for m in models), " (%d incident(s) within 2 h)" % len(mine) if mine else "")
    else:
        verdict = "cannot cross-check: " + ("our monitoring data starts %s, the ticket is older" % tehran(first_data)
                                            if not covered else "no model or host to match")
    if ai and ai.get("missing"):
        problems += ["AI: missing " + x for x in ai["missing"] if x]
    q1 = {"status": "bad" if d.get("secret_posted") else "warn" if problems else "ok", "answer": "; ".join(problems) or "complete",
          "check": verdict, "ai": ai.get("summary") if ai else None}

    use = col.usage().get("users", {})
    who, found = ticket_user(col, d, use)
    alias = who if who and "-" in who else None  # usernames have no '-'; key aliases are '<user>-<key name>'
    who = who.split("-", 1)[0] if who else None
    # the user's own LLM requests from 2 h before the first to 2 h after the last moment they wrote (Langfuse)
    log = col.llm_log(alias or who, moments[0] - NEAR_S, moments[-1] + NEAR_S) if who else None
    n_req = sum(m["count"] for m in log["models"]) if log else 0
    n_err = sum(m["count"] for m in log["models"] if m["level"] == "ERROR") if log else 0

    # 2. how it was used, and logs
    how = []
    if hosts:
        how.append("API through " + ", ".join(hosts))
    if re.search(r"/v1|openai|base_url", low):
        how.append("OpenAI-compatible API")
    for k, rx in (("long context", r"context|کانتکست|توکن ورودی"), ("reasoning/thinking switch", r"reasoning|thinking"),
                  ("max tokens", r"max_len|max_tokens"), ("extra_body", r"extra.?body"), ("classifier use", r"classif")):
        if re.search(rx, low):
            how.append(k)
    log_pods = [{"ns": m["ns"], "pod": p["name"], "container": p["bad"]} for m in models for p in col.pods.get(m["ns"], []) if p["workload"] == m["name"]]
    if log:
        how.insert(0, "%d LLM requests within 2 h of the ticket (%d failed)" % (n_req, n_err) if n_req
                   else "no LLM request within 2 h of the ticket")
    q2 = {"status": "warn" if n_err else "info", "answer": ", ".join(how) or "not said in the ticket", "model_pods": log_pods,
          "log": log, "locked": None if log else LOCKED["user_logs"]}

    # 3. infra used (what is behind the models they use; the user's own share is locked)
    infra = []
    for m in models:
        for p in col.pods.get(m["ns"], []):
            if p["workload"] == m["name"]:
                gpu = ", ".join("%s×%d" % (k.replace("nvidia.com/", ""), n) for k, n in p["res"]["gpu"].items())
                infra.append({"model": m["name"], "pod": p["name"], "gpu": gpu, "cpu_use": p.get("use_cpu"), "node": p.get("node"),
                              "ready": p.get("ready"), "since": p.get("since")})
    own = use.get(who) or {}
    answer = "%d model pod(s) behind what the ticket uses" % len(infra) if infra else "nothing in the cluster matched"
    if who:
        answer = "%s: %d CPU core-h, %d GPU-h in 30 d; %s" % (who, own.get("cpu_h", 0), own.get("gpu_h", 0), answer)
    q3 = {"status": "info", "answer": answer, "pods": infra, "user": who, "user_found": found,
          "user_use": {k: own[k] for k in ("cpu_h", "gpu_h")} if own else None, "locked": None if who else LOCKED["user_infra"]}

    # 4. services used
    # the key posted in the ticket if any, else every key of the user (alias '<user>-<key name>')
    # (a key alias in the map, e.g. adminc2level-tarbiatmodares: one university's key of an org account)
    sels = [(h[:8], 'hashed_api_key="%s"' % h) for h in d.get("key_hashes") or []] or (
        [(alias, 'api_key_alias="%s"' % alias.replace('"', ""))] if alias else
        [(who, 'api_key_alias=~"%s-.*"' % re.sub(r"[^\w.-]", "", who))] if who else [])
    usage = key_usage(col, sels, created) if sels and col.integrations()["grafana"] else []
    total = lambda k, p: round(sum(u["value"] for u in usage if u["metric"] == k and u["period"] == p))
    named = ", ".join([m["name"] for m in models] + hosts) or "not said in the ticket"
    q4 = {"status": "warn" if total("failed", "24 h around the ticket") else "info",
          "answer": ("%d requests, %d tokens in 30 days" % (total("requests", "last 30 days"), total("tokens", "last 30 days")) if usage
                     else named), "named": named, "models": models, "usage": usage, "user": who,
          "locked": None if usage else LOCKED["key_usage"] if sels else LOCKED["user_services"]}

    # 5. down or broken at that time
    plat_active = [i for i in platform if i["when"] == "active"]
    q5 = {"status": "bad" if mine_active else "warn" if mine or plat_active or n_err else "ok" if covered else "info",
          "answer": ("at the time: %d open on what the user uses, %d platform-wide; within 2 h: %d more"
                     % (len(mine_active), len(plat_active), len(mine) + len(platform) - len(mine_active) - len(plat_active))) if covered
          else "no data: monitoring starts %s" % tehran(first_data),
          "user_errors": log["errors"] if log else None,
          "incidents": mine, "platform": platform[:20], "moments": moments, "data_from": first_data, "first_answer_h": wait_h}
    if log:  # Langfuse keeps older history than our incidents: the user's own failed calls still answer "was it broken"
        q5["answer"] += "; the user's own LLM calls then: %d of %d failed" % (n_err, n_req)

    # 6. what for
    purpose = next((p for p, rx in PURPOSES if re.search(rx, low)), "other")
    if ai and ai.get("purpose"):
        purpose = ai["purpose"] if purpose == "other" or ai["purpose"] == purpose else "%s (AI: %s)" % (purpose, ai["purpose"])
    q6 = {"status": "info", "answer": purpose}
    return {"q1": q1, "q2": q2, "q3": q3, "q4": q4, "q5": q5, "q6": q6}
