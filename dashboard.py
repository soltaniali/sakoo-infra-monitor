"""History of up/down changes (SQLite) and the /dashboard page for the receiver. Stdlib only.

Stores one row per state change of a check, not one per report, so weeks of history stay small.
Page: timeline bars per check, list of incidents, and a snapshot of every check at a chosen moment.
Access: signed links that expire (the bot's /dashboard command hands them out), so no password crosses plain HTTP.
"""
import contextlib
import hashlib
import hmac
import html
import os
import sqlite3
import time
import urllib.parse
from pathlib import Path

DB = Path(__file__).resolve().parent / "history.db"
TZ = 3.5 * 3600  # Tehran (UTC+03:30, no DST since 2022)
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "3d": 3 * 86400, "7d": 7 * 86400}
SECTIONS = [("infra", "🛠 Infra (infra team)"), ("us", "🧩 Ours: endpoints"), ("asset", "📦 Ours: MLOps assets"),
            ("monitor", "📡 Probe (the monitor itself)")]
LINK_TTL = 24 * 3600
OWNER = {"infra": "Infra team", "us": "Ours", "asset": "Ours (asset)", "monitor": "Probe"}


@contextlib.contextmanager
def db():
    """Connection that commits and always closes (sqlite3's own context manager does not close)."""
    c = sqlite3.connect(DB, timeout=10)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS changes (ts REAL, name TEXT, owner TEXT, ok INTEGER, detail TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS changes_name_ts ON changes (name, ts)")
        yield c
        c.commit()
    finally:
        c.close()


def record(name, owner, ok, detail, ts=None):
    record_many([(name, owner, ok, detail)], ts)


def record_many(rows, ts=None):
    """All changes from one report: one timestamp, one transaction (so a snapshot shows the report as a whole)."""
    if rows:
        ts = ts or time.time()
        with db() as c:
            c.executemany("INSERT INTO changes VALUES (?, ?, ?, ?, ?)", [(ts, n, o, int(bool(ok)), d) for n, o, ok, d in rows])


# --- signed links --------------------------------------------------------------------------------------------------
def _sig(exp):
    return hmac.new(os.environ["PROBE_KEY"].encode(), b"dashboard:%d" % exp, hashlib.sha256).hexdigest()[:32]


def make_token(now=None):
    exp = int((now or time.time()) + LINK_TTL)
    return "%d.%s" % (exp, _sig(exp))


def token_ok(tok):
    try:
        exp, sig = tok.split(".")
        return int(exp) > time.time() and hmac.compare_digest(sig, _sig(int(exp)))
    except (ValueError, KeyError):
        return False


# --- data ----------------------------------------------------------------------------------------------------------
def intervals(start, end):
    """{name: (owner, [(t0, t1, ok, detail), ...])} covering [start, end]; gaps before the first record are omitted."""
    out = {}
    with db() as c:
        names = [r[0] for r in c.execute("SELECT DISTINCT name FROM changes WHERE ts <= ?", (end,))]
        for n in names:
            before = c.execute("SELECT ts, owner, ok, detail FROM changes WHERE name=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                               (n, start)).fetchone()
            rows = c.execute("SELECT ts, owner, ok, detail FROM changes WHERE name=? AND ts>? AND ts<=? ORDER BY ts",
                             (n, start, end)).fetchall()
            pts = ([(start,) + before[1:]] if before else []) + rows
            if not pts:
                continue
            segs = [(t, (pts[i + 1][0] if i + 1 < len(pts) else end), ok, d) for i, (t, _, ok, d) in enumerate(pts)]
            out[n] = (pts[-1][1], segs)
    return out


# --- page ----------------------------------------------------------------------------------------------------------
def fmt(ts, with_date=True):
    return time.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M", time.gmtime(ts + TZ))


def dur(s):
    s = int(s)
    return "%dd %dh" % (s // 86400, s % 86400 // 3600) if s >= 86400 else "%dh %dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm" % max(1, s // 60)


CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e4e4e0;--up:#2f9e44;--down:#e03131;--none:#d9d9d4}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecece8;--mut:#9a9a94;--card:#1e1e1c;--line:#2e2e2b;--none:#3a3a37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1680px;margin:0 auto;padding:20px 16px 60px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px}
.mut{color:var(--mut)}a{color:inherit}.tabs a{display:inline-block;padding:4px 10px;margin:8px 6px 0 0;border:1px solid var(--line);
border-radius:6px;text-decoration:none;background:var(--card)}.tabs a.on{background:var(--fg);color:var(--bg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(390px,100%),1fr));gap:0 16px;align-items:start}
.row{display:grid;grid-template-columns:190px 1fr 46px;gap:8px;align-items:center;padding:1px 0;font-size:13px}
.row .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.row .p{text-align:right;color:var(--mut);font-size:12px}
svg{width:100%;height:12px;display:block}svg a rect:hover{opacity:.75}.axis{display:flex;justify-content:space-between;
color:var(--mut);font-size:11px;margin:2px 54px 0 198px}table{width:100%;border-collapse:collapse}td,th{text-align:left;
padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:500}
.sub{font-size:11.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.imp{font-size:11px;color:var(--mut);margin:6px 0 1px;text-transform:uppercase;letter-spacing:.04em}
.status{white-space:pre-wrap;line-height:1.6}.status pre{margin:0;font-size:13px;line-height:1.5}
.jump a{margin-right:12px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}.up{background:var(--up)}.down{background:var(--down)}
@media (max-width:700px){.row{grid-template-columns:1fr}.axis{margin:2px 0}.row .p{text-align:left}}
"""


IMPORTANCE = ["Critical", "Low", "No", "?"]
IMP_LABEL = {"No": "Not rated", "?": "Unknown"}


def label(n, m, e):
    """One-line row label: hosts without "host " and the domain; assets with their owner. Hover shows everything."""
    short = n[5:].removesuffix(".aipsharif.ir") if n.startswith("host ") else n
    if not m:
        return "<div class=n title='%s'>%s</div>" % (e(n), e(short))
    tip = "%s · %s · %s · %s" % (n, m.get("person", "?"), IMP_LABEL.get(m.get("value", "?"), m.get("value")), m.get("test", ""))
    return "<div class=n title='%s'>%s <span class=mut>· %s</span></div>" % (e(tip), e(short), e(m.get("person", "?")))


def grouped(rows, meta):
    """Rows with asset meta grouped by importance: [(heading or None, rows)]."""
    if not any(n in meta for n, _ in rows):
        return [(None, rows)]
    imps = IMPORTANCE + sorted({meta.get(n, {}).get("value", "?") for n, _ in rows} - set(IMPORTANCE))
    return [(IMP_LABEL.get(i, i), [r for r in rows if meta.get(r[0], {}).get("value", "?") == i]) for i in imps
            if any(meta.get(r[0], {}).get("value", "?") == i for r in rows)]


STATUS_SECTIONS = [("infra", "🛠 INFRA (infra team)"), ("us", "🧩 OURS (MLOps endpoints)"),
                   ("asset", "📦 MLOPS ASSETS"), ("monitor", "📡 PROBE (the monitor itself)")]


def asset_table(rows):
    """Assets as aligned monospace rows, grouped by importance: name, owner, what was tested. Down rows: reason below."""
    e = html.escape
    w = max(len(n) for n, _ in rows)
    pw = max(len(c.get("meta", {}).get("person", "")) for _, c in rows)
    lines = []
    for imp in IMPORTANCE + sorted({c.get("meta", {}).get("value", "?") for _, c in rows} - set(IMPORTANCE)):
        group = [(n, c) for n, c in rows if c.get("meta", {}).get("value", "?") == imp]
        if not group:
            continue
        lines.append("<b>%s</b>" % e(IMP_LABEL.get(imp, imp)))
        body = []
        for n, c in sorted(group, key=lambda r: (not r[1]["fails"], r[0])):  # down first
            m = c.get("meta", {})
            body.append("%s %s  %s  %s" % ("❌" if c["fails"] else "✅", n.ljust(w), m.get("person", "?").ljust(pw),
                                           "" if c["fails"] else m.get("test", "")))
            if c["fails"]:
                body.append("   ↳ " + c.get("detail", ""))
        lines.append("<pre>%s</pre>" % e("\n".join(body)))
    return lines


def status_lines(checks):
    """The bot's /status list (Telegram HTML), also shown as the page's snapshot.
    checks: {name: {"owner", "fails", "detail", "meta"}}."""
    e = html.escape
    out = []
    for owner, title in STATUS_SECTIONS:
        rows = sorted((n, c) for n, c in checks.items() if c.get("owner") == owner)
        if not rows:
            continue
        down = [(n, c) for n, c in rows if c["fails"]]
        out.append("\n<b>%s</b> · %d up, %d down" % (e(title), len(rows) - len(down), len(down)))
        if owner == "asset":
            out += asset_table(rows)
            continue
        out += ["❌ %s: %s" % (e(n), e(c.get("detail", ""))) for n, c in down]
        out += ["✅ %s" % e(n) for n, c in rows if not c["fails"]]
    return out


def page(query, current, meta=None):
    """current: names of the checks in the latest report (renamed or removed checks are hidden).
    meta: {name: {"person", "value", "test"}} for MLOps assets, from the latest report."""
    meta = meta or {}
    q = urllib.parse.parse_qs(query)
    tok = q.get("t", [""])[0]
    rng = q.get("range", ["24h"])[0]
    rng = rng if rng in RANGES else "24h"
    at = q.get("at", [None])[0]
    end = time.time()
    start = end - RANGES[rng]
    e, qs = html.escape, lambda **kw: "?" + urllib.parse.urlencode(dict({"t": tok, "range": rng}, **kw))
    data = intervals(start, end)
    out = ["<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>AIP uptime</title><style>%s</style></head><body><main>" % CSS,
           "<h1>AIP uptime</h1><div class=mut>Tehran time · updated %s · link valid until %s</div>"
           % (fmt(end), fmt(int(tok.split(".")[0])) if "." in tok else "?"),
           "<div class='jump mut'>Jump to: %s</div>" % " ".join("<a href='#%s'>%s</a>" % (o, e(t)) for o, t in SECTIONS),
           "<div class=tabs>" + "".join("<a class='%s' href='%s'>%s</a>" % ("on" if r == rng and not at else "", qs(range=r), r)
                                         for r in RANGES) + "</div>"]

    if at:  # snapshot of every check at one moment
        t = float(at)
        snap = intervals(t - 1, t)
        out.append("<h2>Snapshot at %s</h2><p><a href='%s'>← back to timeline</a></p>" % (fmt(t), qs()))
        checks = {n: {"owner": o, "fails": not s[-1][2], "detail": s[-1][3], "meta": meta.get(n, {})}
                  for n, (o, s) in snap.items() if n in current}
        out.append("<div class='card status'>%s</div>" % "\n".join(status_lines(checks)).strip())
        return "".join(out + ["</main></body></html>"])

    # timeline: one card per section, side by side on a wide screen so every check fits on one page
    span = end - start
    ticks = "".join("<span>%s</span>" % fmt(start + span * i / 2, rng in ("3d", "7d")) for i in range(3))
    out.append("<div class=grid>")
    for owner, title in SECTIONS:
        # same order as /status: down first, then by name; only assets are grouped by importance
        rows = sorted(((n, segs) for n, (o, segs) in data.items() if o == owner and n in current),
                      key=lambda r: (r[1][-1][2], r[0]))
        if not rows:
            continue
        m_own = meta if owner == "asset" else {}
        down = sum(not segs[-1][2] for _, segs in rows)
        out.append("<section id='%s'><h2>%s · %d up, %d down</h2><div class=card>" % (owner, e(title), len(rows) - down, down))
        for head, grp in grouped(rows, m_own):
            if head:
                out.append("<div class=imp>%s</div>" % e(head))
            for n, segs in grp:
                up = sum(t1 - t0 for t0, t1, ok, _ in segs if ok)
                known = sum(t1 - t0 for t0, t1, _, _ in segs)
                bars = []
                for t0, t1, ok, d in segs:
                    x, w = (t0 - start) / span * 1000, max((t1 - t0) / span * 1000, 1.5)
                    tip = "%s %s → %s (%s)%s" % ("UP" if ok else "DOWN", fmt(t0), fmt(t1), dur(t1 - t0), "" if ok else ": " + d[:200])
                    bars.append("<a href='%s'><rect x='%.1f' y='0' width='%.1f' height='16' fill='var(--%s)'><title>%s</title></rect></a>"
                                % (qs(at=int(t0) + 1), x, w, "up" if ok else "down", e(tip)))
                out.append("<div class=row>%s<svg viewBox='0 0 1000 16' preserveAspectRatio='none'>"
                           "<rect width='1000' height='16' fill='var(--none)'/>%s</svg><div class=p>%s</div></div>"
                           % (label(n, m_own.get(n), e), "".join(bars), ("%.1f%%" % (100 * up / known)) if known else "–"))
        out.append("<div class=axis>%s</div></div></section>" % ticks)
    out.append("</div>")
    # incidents: every down interval in the range, newest first
    inc = [(t0, t1, n, owner, d) for n, (owner, segs) in data.items() if n in current for t0, t1, ok, d in segs if not ok]
    inc.sort(reverse=True)
    out.append("<h2>Incidents in the last %s · %d</h2>" % (rng, len(inc)))
    if inc:
        out.append("<div class=card><table><tr><th>Start</th><th>Duration</th><th>Check</th><th>Owner</th><th>Reason</th><th></th></tr>")
        for t0, t1, n, owner, d in inc[:200]:
            ongoing = t1 >= end - 1
            out.append("<tr><td>%s</td><td>%s%s</td><td>%s</td><td class=mut>%s</td><td class=mut>%s</td><td><a href='%s'>snapshot</a></td></tr>"
                       % (fmt(t0), dur(t1 - t0), " · ongoing" if ongoing else "", e(n), e(OWNER.get(owner, owner)), e(d[:160]), qs(at=int(t0) + 1)))
        out.append("</table></div>")
    else:
        out.append("<div class='card mut'>No incidents.</div>")

    out.append("<p class=mut>Grey = no data yet. Click a bar or an incident for the snapshot of all checks at that moment.</p>")
    return "".join(out + ["</main></body></html>"])
