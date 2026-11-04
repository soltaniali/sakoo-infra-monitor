'use strict';
// AIP Ops page. Every value from the API goes into the DOM as text (never as HTML): logs can contain anything.
const TZ = 'Asia/Tehran';
const app = document.getElementById('app');
let me = null, timer = null;

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k in e && k !== 'list') e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}
const fmtT = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
const fmt = t => t ? fmtT.format(new Date(t * 1000)) : '–';
const fmtFull = new Intl.DateTimeFormat('en-GB', {timeZone: TZ, weekday: 'short', year: 'numeric', month: 'short', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false});
// a time made easy to see: how long ago in bold (green under 1 h, grey after 7 d), the Tehran clock time next to it
function T(t) {
  if (!t) return h('span', {class: 'mut'}, '–');
  const s = Date.now() / 1000 - t;
  return h('span', {class: 't' + (s < 3600 ? ' fresh' : s > 7 * 86400 ? ' old' : ''), title: fmtFull.format(new Date(t * 1000)) + ' · Tehran time'},
    h('b', {}, ago(t)), h('span', {class: 'abs'}, fmt(t)));
}
function ago(t) {
  if (!t) return '–';
  const s = Date.now() / 1000 - t;
  if (s < 0) return 'in ' + ago(t + 2 * s).replace(' ago', '');
  return s < 90 ? Math.round(s) + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
}
const OWNER = o => o === 'us' ? 'MLOps (us)' : o === 'infra' ? 'Infra team' : o === 'asset' ? 'MLOps asset' : o === 'monitor' ? 'Probe' : 'Team ' + o;
const SYM = {crashloop: 'Crash loop', oomkilled: 'Out of memory', 'image-pull': 'Image pull', pending: 'Pending', flapping: 'Restarting',
  'probe-failing': 'Probe failing', down: 'Down', 'model-down': 'Model down', 'new-error': 'New error', endpoint: 'Endpoint check', infra: 'Infra check', 'llm-failing': 'LLM calls failing'};
const CHG = {release: 'Release', config: 'Config change', 'new-service': 'New service', 'new-host': 'New hostname', 'new-namespace': 'New namespace',
  removed: 'Removed', 'removed-namespace': 'Namespace removed', 'new-model': 'New model', 'model-changed': 'Model changed', 'model-removed': 'Model removed'};
const verdictChip = v => v ? h('span', {class: 'chip ' + ({bad: 'bad', 'not ready': 'bad', healthy: 'ok', ready: 'ok', watching: 'warn'}[v] || '')}, v) : '';

async function api(path, opts) {
  const r = await fetch(path, Object.assign({credentials: 'same-origin'}, opts));
  if (r.status === 401) { showLogin(); throw new Error('login'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const post = (path, body) => api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const qs = o => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '' && v != null)).toString();

function table(cols, rows, onclick) {
  if (!rows.length) return h('div', {class: 'mut'}, 'None.');
  return h('table', {}, h('tr', {}, cols.map(c => h('th', {}, c[0]))),
    rows.map((r, i) => h('tr', {class: onclick ? 'click' : '', onclick: onclick && (() => onclick(r))}, cols.map(c => h('td', {}, c[1](r, i, rows))))));
}

// --- login and navigation ------------------------------------------------------------------------------------------
function showLogin(msg) {
  me = null; clearTimeout(timer);
  document.getElementById('tabs').replaceChildren();
  document.getElementById('who').replaceChildren();
  const u = h('input', {autocomplete: 'username', required: true}), p = h('input', {type: 'password', autocomplete: 'current-password', required: true});
  const err = h('div', {class: 'err'}, msg || '');
  app.replaceChildren(h('form', {class: 'card login', onsubmit: async ev => {
    ev.preventDefault();
    try { await post('/api/login', {user: u.value, password: p.value}); render(); } catch (e) { if (e.message !== 'login') err.textContent = e.message; }
  }}, h('h1', {}, 'AIP Ops'), h('label', {}, 'User', u), h('label', {}, 'Password', p), h('button', {class: 'primary'}, 'Log in'), err));
  u.focus();
}

const TABS = [['overview', 'Overview'], ['incidents', 'Incidents'], ['changes', 'Changes'], ['models', 'Models'],
  ['services', 'Services'], ['logs', 'Logs'], ['saved-logs', 'Saved logs'], ['infra', 'Infra or us'], ['users', 'Users'], ['trainings', 'Trainings'], ['keys', 'Keys'], ['tickets', 'Tickets'], ['capacity', 'Capacity'], ['trends', 'Trends']];
setInterval(() => { const c = document.getElementById('clock'); if (c) c.textContent = 'Tehran ' + fmt(Date.now() / 1000); }, 15000);
function nav(page) {
  document.getElementById('tabs').replaceChildren(...TABS.map(([k, t]) => h('a', {href: '#/' + k, class: page === k || (page === 'incident' && k === 'incidents') || (page === 'ticket' && k === 'tickets') ? 'on' : ''}, t)));
  document.getElementById('who').replaceChildren(h('span', {id: 'clock'}, 'Tehran ' + fmt(Date.now() / 1000)), ' · ' + me.user + ' · ', h('a', {href: '#', onclick: async ev => { ev.preventDefault(); await post('/api/logout', {}); showLogin(); }}, 'log out'));
}
function route() {
  const [p, q] = (location.hash.slice(2) || 'overview').split('?');
  return [p, Object.fromEntries(new URLSearchParams(q || ''))];
}
async function render() {
  clearTimeout(timer);
  if (!me) { try { me = await api('/api/me'); } catch (e) { return; } }
  const [page, q] = route();
  nav(page);
  try { app.replaceChildren(await (VIEWS[page] || VIEWS.overview)(q)); }
  catch (e) { if (e.message !== 'login') app.replaceChildren(h('div', {class: 'card err'}, 'Error: ' + e.message)); }
  if (['overview', 'incidents', 'infra'].includes(page)) timer = setTimeout(render, 30000);
}
window.addEventListener('hashchange', render);

// --- shared pieces -------------------------------------------------------------------------------------------------
// Liveness: a status, since when it has been so, and when it was last checked.
function live(state, since, checked, bad) {
  return h('span', {}, h('span', {class: bad ? 'err' : ''}, state),
    since ? h('span', {class: 'small'}, ' · since ', T(since)) : '', checked ? h('span', {class: 'small'}, ' · checked ', T(checked)) : '');
}
const incidentCols = [
  ['#', r => r.id], ['Opened', r => T(r.opened)], ['What', r => SYM[r.symptom] || r.symptom],
  ['Where', r => (r.ns === '-' ? '' : r.ns + ' / ') + r.obj], ['Owner', r => OWNER(r.owner)],
  ['Last seen', r => r.closed ? h('span', {class: 'mut'}, 'closed ', T(r.closed)) : T(r.last_seen)],
  ['Cause', r => h('span', {class: 'small'}, r.cause)]];
const incidentTable = rows => table(incidentCols, rows, r => { location.hash = '#/incident?id=' + r.id; });
function changeTable(rows) {
  return table([
    ['Time', r => T(r.ts)], ['Change', r => CHG[r.type] || r.type], ['Where', r => r.ns + ' / ' + r.name],
    ['Before → after', r => h('span', {class: 'small'}, r.before && r.after ? r.before + ' → ' + r.after : (r.after || r.before))],
    ['Result', r => verdictChip(r.verdict)],
    ['Who', r => { const i = r.info || {};
      return i.mr_url ? h('a', {href: i.mr_url, target: '_blank', rel: 'noopener'}, (i.mr_author || i.author) + ': ' + (i.mr_title || i.commit))
        : i.author ? h('span', {}, i.author + ': ' + i.commit) : i.commits_in_window ? h('span', {class: 'small mut'}, 'commits around then: ' + i.commits_in_window.join(' · ')) : ''; }],
    ['Notes', r => h('span', {class: 'small mut'}, ((r.info || {}).gaps || []).join(', '))]], rows);
}
function podTable(pods, ns, checked) {
  return table([
    ['Pod', p => p.name], ['Phase', p => p.phase], ['Ready', p => live(p.ready ? 'ready' : 'not ready', p.since, checked, !p.ready)],
    ['Restarts', p => p.restarts], ['Problem', p => [...p.waiting, ...p.last.map(l => 'last exit: ' + l[0])].join(', ')],
    ['Node', p => p.node || ''], ['Age', p => ago(p.created)],
    ['', p => ns && p.bad ? h('a', {href: '#/logs?' + qs({ns, pod: p.name, container: p.bad})}, 'logs') : '']], pods);
}
function eventTable(events) {
  return table([['Time', e => fmt(e.ts)], ['Reason', e => e.reason], ['Object', e => e.kind + ' ' + e.name],
    ['Message', e => h('span', {class: 'small'}, e.msg + (e.count > 1 ? ' (×' + e.count + ')' : ''))]], events);
}

// --- views ---------------------------------------------------------------------------------------------------------
const VIEWS = {};
VIEWS.overview = async () => {
  const s = await api('/api/state');
  const st = s.status || {}, ig = s.integrations || {};
  const chips = ['ai', 'litellm', 'keys', 'mlops', 'casdoor', 'gitlab', 'grafana', 'langfuse', 'tickets'].map(k => h('span', {class: 'chip ' + (ig[k] ? (ig.errors[k] ? 'warn' : 'ok') : ''),
    title: ig.errors[k] || (ig[k] ? 'on' : 'off: set its env in secrets/ops.env')}, {ai: 'AI summaries', litellm: 'model test calls', gitlab: 'who changed it', grafana: 'GPU % · usage', langfuse: 'LLM request log', keys: 'LiteLLM keys', mlops: 'trainings', casdoor: 'logins', tickets: 'ticket site'}[k]));
  return h('div', {},
    h('div', {class: 'row mut small'}, 'Last round ' + ago(st.last) + (st.took != null ? ' (' + st.took + ' s)' : '') + ' · ' +
      (st.namespaces || 0) + ' namespaces read · full read ' + ago(st.slow_last) + ' · probe report ' + ago(s.probe_ts), ' ', chips,
      ig.errors.alerts ? h('span', {class: 'err'}, ' · Telegram relay: ' + ig.errors.alerts) : ''),
    st.errors && st.errors.length ? h('div', {class: 'card small err'}, 'Read errors: ' + st.errors.join(' · ')) : '',
    h('div', {class: 'tiles'}, s.tiles.map(t => h('a', {class: 'tile ' + t.status, href: t.link},
      h('div', {class: 'tn'}, t.n + ' · ' + t.title), h('div', {class: 'tv'}, t.value), h('div', {class: 'ts mut'}, t.sub)))),
    s.unsent && s.unsent.length ? [h('h2', {}, 'Alerts not sent yet · ' + s.unsent.length),
      h('div', {class: 'card small'}, h('p', {class: 'err'}, 'The Telegram relay (London) is unreachable; these go out together when it is back.'),
        s.unsent.map(u => h('pre', {}, fmt(u.ts) + '  ' + u.text)))] : '',
    h('h2', {}, 'Open incidents · ' + s.incidents.length), h('div', {class: 'card'}, incidentTable(s.incidents)),
    h('h2', {}, 'Changes in the last 24 h · ' + s.changes.length), h('div', {class: 'card'}, changeTable(s.changes)));
};

VIEWS.incidents = async q => {
  const state = q.state || 'open', kind = q.kind || '';
  const rows = await api('/api/incidents?' + qs({state, kind}));
  const sel = (key, val, opts) => h('select', {onchange: e => { location.hash = '#/incidents?' + qs(Object.assign({state, kind}, {[key]: e.target.value})); }},
    opts.map(([v, t]) => h('option', {value: v, selected: v === val}, t)));
  return h('div', {}, h('div', {class: 'row'}, sel('state', state, [['open', 'Open'], ['closed', 'Closed']]),
    sel('kind', kind, [['', 'All'], ['bug', 'Bugs (1)'], ['down', 'Down (2)']]), h('span', {class: 'mut'}, rows.length + ' incidents')),
    h('div', {class: 'card'}, incidentTable(rows)));
};

VIEWS.incident = async q => {
  const i = await api('/api/incident?id=' + encodeURIComponent(q.id));
  const ev = i.evidence || {};
  const sum = h('div', {}, i.summary ? h('pre', {}, i.summary) : '');
  const copied = h('span', {class: 'mut small'});
  const handoff = h('textarea', {readOnly: true, rows: 7, style: 'width:100%;font:12px monospace'}, i.handoff);
  const kv = [['Status', i.state === 'open' ? h('span', {class: 'chip bad'}, 'open') : h('span', {class: 'chip ok'}, 'closed ' + fmt(i.closed))],
    ['Opened', fmt(i.opened) + ' (' + ago(i.opened) + '), last seen ' + ago(i.last_seen)], ['Owner', OWNER(i.owner)],
    ['What we see', i.detail], ['Likely cause', h('b', {}, i.cause)], ['Suggested fix', h('b', {}, i.fix)], ['Playbook', i.playbook]];
  if (i.change) kv.push(['Linked change', (CHG[i.change.type] || i.change.type) + ' ' + i.change.name + ': ' + i.change.before + ' → ' + i.change.after + ' at ' + fmt(i.change.ts)]);
  if (ev.meta && ev.meta.person) kv.push(['Asset owner', ev.meta.person + ' · importance ' + ev.meta.value]);
  return h('div', {},
    h('p', {}, h('a', {href: '#/incidents'}, '← incidents')),
    h('h1', {}, '#' + i.id + ' ' + (SYM[i.symptom] || i.symptom) + ' · ' + (i.ns === '-' ? '' : i.ns + ' / ') + i.obj),
    h('div', {class: 'card kv'}, kv.flatMap(([k, v]) => [h('div', {}, k), h('div', {}, v)])),
    h('h2', {}, 'Summary'), h('div', {class: 'card'}, sum, me.integrations.ai ? h('button', {onclick: async e => {
      e.target.disabled = true; e.target.textContent = 'Asking the LLM…';
      try { sum.replaceChildren(h('pre', {}, (await post('/api/summarize', {incident: i.id})).summary)); } catch (x) { sum.replaceChildren(h('span', {class: 'err'}, x.message)); }
      e.target.disabled = false; e.target.textContent = 'Summarize again';
    }}, i.summary ? 'Summarize again' : 'Summarize with AIP LLM') : h('span', {class: 'mut small'}, 'LLM summaries are off (set LITELLM_* in secrets/ops.env).')),
    h('h2', {}, 'Message for the owner'), h('div', {class: 'card'}, handoff, h('div', {class: 'row'}, h('button', {onclick: async () => {
      handoff.select();
      try { await navigator.clipboard.writeText(i.handoff); copied.textContent = 'copied'; } catch (x) { document.execCommand('copy'); copied.textContent = 'copied'; }
    }}, 'Copy'), copied)),
    ev.sample ? [h('h2', {}, 'Error sample'), h('div', {class: 'card'}, h('pre', {}, ev.sample))] : '',
    ev.logs != null ? [h('h2', {}, 'Log · ' + (ev.log_from || '')), h('div', {class: 'card'}, h('pre', {}, ev.logs || '(empty)'))] : '',
    h('h2', {}, 'Pods'), h('div', {class: 'card'}, podTable(ev.pods || [], i.ns !== '-' && i.ns)),
    h('h2', {}, 'Warning events'), h('div', {class: 'card'}, eventTable(ev.events || [])),
    h('h2', {}, 'Changes in this namespace (2 h before)'), h('div', {class: 'card'}, changeTable(ev.changes || [])),
    h('h2', {}, 'Probe checks failing at that time'), h('div', {class: 'card'}, table([['Check', r => r[0]], ['Owner', r => OWNER(r[2])], ['Detail', r => r[1]]], ev.probe_failing || [])),
    ev.error ? h('div', {class: 'err'}, 'Evidence error: ' + ev.error) : '');
};

VIEWS.changes = async q => {
  const days = q.days || '1', type = q.type || '';
  const rows = await api('/api/changes?' + qs({days, type}));
  const go = o => { location.hash = '#/changes?' + qs(Object.assign({days, type}, o)); };
  return h('div', {}, h('div', {class: 'row'},
    h('select', {onchange: e => go({days: e.target.value})}, [['1', 'Last 24 h'], ['7', 'Last 7 days'], ['30', 'Last 30 days']].map(([v, t]) => h('option', {value: v, selected: v === days}, t))),
    h('select', {onchange: e => go({type: e.target.value})}, [['', 'All changes'], ['release', 'Features / releases (3)'], ['new-service', 'New services (4)'],
      ['new-model', 'Models (5)'], ['removed', 'Removed']].map(([v, t]) => h('option', {value: v, selected: v === type}, t))),
    h('span', {class: 'mut'}, rows.length + ' changes')), h('div', {class: 'card'}, changeTable(rows)),
    h('p', {class: 'mut small'}, 'Result: a release is "watching" for 15 min, then "healthy" or "bad" (an incident opened on it). ' +
      'Changes in user-* namespaces are listed but not sent to Telegram.'));
};

VIEWS.models = async () => {
  const ms = await api('/api/models');
  const cols = [['Model', m => m.name], ['Ready', m => live(m.ready + '/' + m.min, m.since, m.checked, m.min && m.ready < m.min)],
    ['Engine', m => m.engine], ['Resource profile', m => m.profile], ['Pods', m => m.pods.map(p => p.name + (p.restarts ? ' (' + p.restarts + ' restarts)' : '') + (p.node ? ' on ' + p.node : '')).join(', ')],
    ['Source', m => h('span', {class: 'small mut'}, m.url)], ['Age', m => ago(m.created)],
    ['Open incidents', m => m.open ? h('a', {href: '#/incidents'}, m.open) : '0']];
  if (me.integrations.litellm) cols.splice(4, 0, ['Test call', m => !m.test ? '–' : m.test.ok ? m.test.ms + ' ms' : h('span', {class: 'err', title: m.test.detail}, 'failed')]);
  if (me.integrations.grafana) cols.splice(4, 0, ['GPU %', m => m.gpu == null ? '–' : m.gpu + ' %']);
  if (me.integrations.litellm) cols.splice(4, 0, ['In litellm', m => m.litellm == null ? '?' : m.litellm ? 'yes' : h('span', {class: 'err'}, 'no')]);
  return h('div', {}, h('h1', {}, 'KubeAI models · ' + ms.length), h('div', {class: 'card'}, table(cols, ms)),
    !me.integrations.grafana ? h('p', {class: 'mut small'}, 'GPU % is off: run set-grafana-login.sh (Grafana login) or set GRAFANA_TOKEN.') : '');
};

async function nsSelect(current, onpick) {
  const all = await api('/api/namespaces');
  return h('select', {onchange: e => onpick(e.target.value)}, h('option', {value: ''}, 'namespace…'),
    all.map(n => h('option', {value: n.ns, selected: n.ns === current}, n.ns + (n.open ? ' (' + n.open + ' open)' : ''))));
}

VIEWS.services = async q => {
  const ns = q.ns || 'aip-mlops-service';
  const s = await api('/api/services?' + qs({ns}));
  return h('div', {}, h('div', {class: 'row'}, await nsSelect(ns, v => { location.hash = '#/services?' + qs({ns: v}); }),
    h('span', {class: 'mut'}, 'read ' + ago(s.read))),
    h('h2', {}, 'Workloads · ' + s.workloads.length), h('div', {class: 'card'}, table([
      ['Kind', w => w.kind], ['Name', w => w.name],
      ['Ready', w => w.kind === 'Model' ? live(w.ready + '/' + w.min, w.since, w.checked, w.ready < w.min)
        : w.kind === 'CronJob' ? h('span', {class: 'mut'}, 'runs on schedule') : live(w.ready + '/' + w.desired, w.since, w.checked, w.ready < w.desired)],
      ['Image / source', w => h('span', {class: 'small'}, w.images ? w.images.join(', ') : w.url)], ['Restarts', w => w.restarts],
      ['Gaps', w => h('span', {class: 'small mut'}, (w.gaps || []).join(', '))], ['Age', w => ago(w.created)]], s.workloads)),
    s.ingresses.length ? [h('h2', {}, 'Hostnames'), h('div', {class: 'card'}, table([['Ingress', i => i.name], ['Hosts', i => i.hosts.join(', ')], ['TLS', i => i.tls ? 'yes' : 'no']], s.ingresses))] : '',
    h('h2', {}, 'Pods · ' + s.pods.length), h('div', {class: 'card'}, podTable(s.pods, ns, s.read)),
    h('h2', {}, 'Warning events (last hour)'), h('div', {class: 'card'}, eventTable(s.events)));
};

// --- logs the station saved itself: also for pods that are gone -----------------------------------------------------------
VIEWS['saved-logs'] = async q => {
  const a = await api('/api/archive?' + qs({ns: q.ns})), mb = b => (b / 1048576).toFixed(1) + ' MB';
  const names = Object.keys(a.namespaces).sort();
  const top = [h('h1', {}, 'Saved logs'), h('p', {class: 'mut small'}, a.from ? 'Saved by the station from ' + a.from + ' (UTC days), kept 30 days, at most 5 GB. ' +
    'Up to 500 lines per container every 5 min, so a very busy pod can have gaps. Secrets are masked before saving.' : 'Nothing saved yet: the first logs arrive within 5 minutes.'),
    h('div', {class: 'row'}, h('select', {onchange: e => { location.hash = '#/saved-logs?' + qs({ns: e.target.value}); }}, h('option', {value: ''}, 'Choose a namespace…'),
      names.map(n => h('option', {value: n, selected: n === q.ns}, n + ' · ' + mb(a.namespaces[n])))))];
  if (!q.ns) return h('div', {}, top);
  if (!q.pod) return h('div', {}, top, h('div', {class: 'card'}, table([['Pod', r => h('b', {}, r.pod)], ['Container', r => r.container], ['Size', r => mb(r.bytes)],
    ['Days', r => r.days.map(d => [h('a', {href: '#/saved-logs?' + qs({ns: q.ns, pod: r.pod, container: r.container, day: d})}, d), ' '])]], a.pods)));
  const find = h('input', {type: 'search', placeholder: 'Find in this day…', value: q.q || ''}), out = h('pre', {}, 'Loading…');
  const load = async () => {
    try { out.textContent = (await api('/api/archive_read?' + qs({ns: q.ns, pod: q.pod, container: q.container, day: q.day, q: find.value}))).text || '(no lines)'; }
    catch (e) { out.textContent = 'Error: ' + e.message; }
  };
  load();
  return h('div', {}, top, h('p', {}, h('a', {href: '#/saved-logs?' + qs({ns: q.ns})}, '← ' + q.ns), ' · ', h('b', {}, q.pod + ' / ' + q.container), ' · ' + q.day),
    h('form', {class: 'row', onsubmit: e => { e.preventDefault(); load(); }}, find, h('button', {}, 'Find')), h('div', {class: 'card'}, out));
};

VIEWS.logs = async q => {
  const ns = q.ns || 'aip-mlops-service';
  const s = await api('/api/services?' + qs({ns}));
  const pods = s.pods.map(p => p.name);
  const pod = q.pod && pods.includes(q.pod) ? q.pod : pods[0] || '';
  const p = s.pods.find(x => x.name === pod);
  const container = p && p.containers.includes(q.container) ? q.container : (p ? p.bad || p.containers[0] : '');
  const tail = h('input', {type: 'number', min: 1, max: 3000, value: q.tail || 300, style: 'width:90px'});
  const prev = h('input', {type: 'checkbox', checked: q.previous === '1'});
  const only = h('input', {type: 'checkbox', checked: q.errors === '1'});
  const out = h('pre', {}, ''), sum = h('div', {});
  const go = o => { location.hash = '#/logs?' + qs(Object.assign({ns, pod, container}, o)); };
  async function load() {
    out.textContent = 'Loading…';
    try {
      const r = await api('/api/logs?' + qs({ns, pod, container, tail: tail.value, previous: prev.checked ? '1' : ''}));
      const lines = r.text.split('\n');
      const lvl = /\b(ERROR|CRITICAL|FATAL|PANIC)\b|level=(error|fatal|critical)|"level"\s*:\s*"(error|fatal|critical)"|^Traceback|^\s*[A-Za-z_.]+(Error|Exception): /;
      const re = {test: l => lvl.test(l.slice(0, 160))};  // the level sits at the start of a line, not in the message
      out.textContent = (only.checked ? lines.filter(l => re.test(l)) : lines).join('\n') || '(empty)';
    } catch (e) { out.textContent = 'Error: ' + e.message; }
  }
  const view = h('div', {},
    h('div', {class: 'row'}, await nsSelect(ns, v => { location.hash = '#/logs?' + qs({ns: v}); }),
      h('select', {onchange: e => go({pod: e.target.value, container: ''})}, pods.map(n => h('option', {value: n, selected: n === pod}, n))),
      h('select', {onchange: e => go({container: e.target.value})}, (p ? p.containers : []).map(c => h('option', {value: c, selected: c === container}, c))),
      h('label', {}, 'lines ', tail), h('label', {}, prev, ' previous container'), h('label', {}, only, ' errors only'),
      h('button', {class: 'primary', onclick: load}, 'Load'),
      me.integrations.ai ? h('button', {onclick: async e => {
        e.target.disabled = true; sum.replaceChildren(h('span', {class: 'mut'}, 'Asking the LLM…'));
        try { sum.replaceChildren(h('div', {class: 'card'}, h('pre', {}, (await post('/api/summarize', {ns, pod, container, previous: prev.checked})).summary))); }
        catch (x) { sum.replaceChildren(h('span', {class: 'err'}, x.message)); }
        e.target.disabled = false;
      }}, 'Summarize') : ''),
    sum, h('div', {class: 'card'}, out));
  if (pod) load(); else out.textContent = 'No pods in this namespace.';
  return view;
};

const bar = (v, max) => h('span', {style: 'display:inline-block;height:8px;border-radius:4px;background:var(--info);width:' + Math.round(80 * Math.min(v / (max || 1), 1)) + 'px'});
// What we cannot see yet: shown greyed, with what unlocks it, so it is not forgotten.
const LOCKED_USER = [  // what our access still cannot show (2026-10-31: kubeconfig, Grafana Viewer, Langfuse, Casdoor log)
  {key: 'llm', title: 'LLM usage', shows: 'models used, tokens, LLM errors', needs: 'a Grafana login on the station: sh station/set-grafana-login.sh'},
  {key: 'train', title: 'Trainings', shows: 'queued, running and failed mlops-core tasks', needs: 'a read-only mlops-core API key (tasks are in its Elasticsearch; its logs show only hashed user ids)'},
  {key: 'failed-login', title: 'Failed logins', shows: 'wrong passwords per user', needs: 'Casdoor writes failed logins as anonymous; only its database has them (access request 45)'},
];
// LLM usage comes from Grafana's Prometheus (LiteLLM counters per key) once a Grafana login is set
const userLocked = () => LOCKED_USER.filter(l => !(l.key === 'llm' && me.integrations.grafana) && !(l.key === 'failed-login' && me.integrations.casdoor) && !(l.key === 'train' && me.integrations.mlops));
const big = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : String(n);
const llmCell = u => !u || !u.requests ? '–' : [big(u.requests) + ' calls · ' + big(u.tokens) + ' tok · $' + u.spend,
  u.failed ? h('span', {class: 'err'}, ' · ' + u.failed + ' failed') : ''];
const lockedCell = l => h('span', {class: 'locked-cell', title: l.title + ': not visible yet. Needs ' + l.needs}, '—');
const lockedCard = l => h('div', {class: 'card locked'}, h('div', {}, h('b', {}, l.title), h('span', {class: 'chip'}, 'not visible yet')),
  h('div', {class: 'small'}, 'Will show: ' + l.shows), h('div', {class: 'small'}, 'Needs: ' + l.needs));

VIEWS.users = async () => {
  const us = await api('/api/users'), lg = me.integrations.casdoor ? await api('/api/logins') : null;
  const groups = [];  // one card per person from the name list (several namespaces), or per account not in the list
  for (const u of us) {
    const k = u.person ? 'p:' + u.person.name : 'u:' + u.user;
    let g = groups.find(x => x.k === k);
    if (!g) groups.push(g = {k, name: u.person && u.person.name, rows: []});
    g.rows.push(u);
  }
  const sum = (g, f) => g.rows.reduce((n, u) => n + (f(u) || 0), 0);
  for (const g of groups) Object.assign(g, {open: sum(g, u => u.open), running: sum(g, u => u.running), restarts: sum(g, u => u.restarts),
    req: sum(g, u => u.use && u.use.requests), failed: sum(g, u => u.use && u.use.failed), spend: sum(g, u => u.use && u.use.spend),
    cpu_h: sum(g, u => u.use && u.use.cpu_h), gpu_h: sum(g, u => u.use && u.use.gpu_h), seen: Math.max(0, ...g.rows.map(u => u.sso ? u.sso.last : 0)),
    lastLogin: Math.max(0, ...g.rows.map(u => u.login && u.login.last_ok || 0)), loginFail: sum(g, u => u.login && u.login.failed),
    trainRun: sum(g, u => u.train && u.train.running), trainFail: sum(g, u => u.train && u.train.failed), trainDone: sum(g, u => u.train && u.train.completed)});
  groups.sort((a, b) => b.open - a.open || b.seen - a.seen || b.req - a.req);
  const stat = (k, ...v) => h('div', {class: 'stat'}, h('span', {class: 'k'}, k), h('span', {class: 'v'}, ...v));
  const num = n => Math.round(n).toLocaleString('en');
  const cards = groups.map(g => [g, h('div', {class: 'card person' + (g.open ? ' bad' : g.running || g.req ? '' : ' idle')},
    h('div', {class: 'ph'}, h('span', {class: 'name', dir: 'auto'}, g.name || g.rows[0].user),
      g.rows.length > 1 ? h('span', {class: 'chip group'}, 'one person · ' + g.rows.length + ' namespaces') : '',
      g.name ? '' : h('span', {class: 'small mut'}, 'not in the name list')),
    h('div', {class: 'stats'},
      stat('Last active (SSO)', T(g.seen || null)), me.integrations.casdoor ? stat('Last password login', T(g.lastLogin || null)) : '',
      me.integrations.casdoor ? stat('Failed logins · 30 d', g.loginFail ? h('span', {class: 'err'}, g.loginFail) : '0') : '', stat('Open incidents', g.open ? h('span', {class: 'err'}, g.open) : '0'),
      me.integrations.mlops ? stat('Tasks: running · done · failed', g.trainRun + ' · ' + g.trainDone + ' · ', g.trainFail ? h('span', {class: 'err'}, g.trainFail) : '0') : '',
      stat('Running pods', g.running), stat('LLM calls · 30 d', num(g.req), g.failed ? h('span', {class: 'err'}, ' · ' + num(g.failed) + ' failed') : ''),
      stat('Spend · 30 d', '$' + g.spend.toFixed(2)), stat('CPU-h · GPU-h · 30 d', num(g.cpu_h) + ' · ' + num(g.gpu_h))),
    table([['Namespace / account', u => h('a', {href: '#/user?' + qs({name: u.user})}, h('b', {}, u.user))], ['Runs', u => u.kinds.join(', ')],
      ['Pods', u => u.running + ' running' + (u.pending ? ', ' + u.pending + ' pending' : '')], ['CPU used / reserved', u => u.cpu_use + ' / ' + u.cpu_req],
      ['RAM', u => u.mem_use_gib + ' GiB'], ['GPU', u => u.gpu || '–'], ['Restarts', u => u.restarts ? h('span', {class: 'err'}, u.restarts) : 0],
      ['Incidents', u => u.open ? h('span', {class: 'err'}, u.open) : 0], ['LLM 30 d', u => llmCell(u.use)],
      ['CPU-h · GPU-h', u => u.use ? u.use.cpu_h + ' · ' + u.use.gpu_h : '–'], ['Last active (SSO)', u => T(u.sso && u.sso.last)], ...(me.integrations.casdoor ? [['Failed logins 30 d', u => u.login && u.login.failed ? h('span', {class: 'err'}, u.login.failed) : 0]] : []),
      ['Changes 7 d', u => u.changes_7d ? [u.changes_7d + ' · ', T(u.last_change)] : 0], ['Running since', u => T(u.started)]], g.rows))]);
  const search = h('input', {type: 'search', placeholder: 'Search name or namespace…', oninput: () => {
    const v = search.value.trim().toLowerCase();
    for (const [g, el] of cards) el.hidden = !!v && !(g.name || '').toLowerCase().includes(v) && !g.rows.some(u => u.user.includes(v));
  }});
  const today = groups.filter(g => g.seen && Date.now() / 1000 - g.seen < 86400).length, locked = userLocked();
  return h('div', {}, h('h1', {}, 'Users'),
    h('div', {class: 'stats'}, stat('People', groups.length), stat('Namespaces / accounts', us.length), stat('Active in SSO last 24 h', today),
      stat('With open incidents', groups.filter(g => g.open).length)),
    h('p', {class: 'mut small'}, 'One card per person: all their namespaces are inside it. Red edge = open incident, grey = nothing running and no LLM use. ' +
      'Real names: the team\'s name list. LLM and CPU/GPU: Grafana (30 d, Prometheus data from 21 Oct). Last active: Casdoor log.'),
    lg ? h('div', {class: 'card', style: 'margin-bottom:14px'}, h('b', {}, 'Logins · last 7 days (Casdoor)'),
      h('div', {class: 'stats'}, stat('Password logins', lg.ok), stat('Failed', lg.failed ? h('span', {class: 'err'}, lg.failed) : 0), stat('History from', T(lg.from))),
      lg.top.length ? table([['Failed for', t => t.user], ['Times', t => t.n], ['Last', t => T(t.last)]], lg.top) : '',
      h('div', {class: 'small mut'}, 'A login through a running session is not counted here. "(unknown name)" = not one of our accounts; the typed name is not stored. No IPs: Casdoor sees only its proxy.')) : '',
    h('div', {class: 'row'}, search), cards.map(c => c[1]),
    locked.length ? [h('h2', {class: 'locked-title'}, 'Not visible yet · ' + locked.length), h('div', {class: 'locked-grid'}, locked.map(lockedCard))] : '');
};

// --- LiteLLM keys: owner, limits, warnings. Dashboard only, nothing goes to the bot ------------------------------------
const KEY_FLAG = {blocked: 'bad', expired: 'bad', 'over budget': 'bad', 'near budget': 'warn', 'expires soon': 'warn', 'account to delete': 'warn'};
const keyFlags = k => k.flags.length ? k.flags.map(f => [h('span', {class: 'chip ' + (KEY_FLAG[f] || '')}, f), ' ']) : h('span', {class: 'mut'}, 'ok');
const keyLimits = k => (k.rpm || k.tpm ? (k.rpm || '–') + ' rpm · ' + (k.tpm || '–') + ' tpm' : 'no limit');
const keyModels = k => !k.models.length || k.models.includes('all-team-models') ? 'all' : h('span', {title: k.models.join(', ')}, k.models.length + ' models');
const keyBudget = k => k.budget == null ? h('span', {class: 'mut'}, 'no limit') : '$' + k.spend + ' of $' + k.budget;
// --- mlops-core tasks: read every 5 min, dashboard only -----------------------------------------------------------------
const TASK_CHIP = {failed: 'bad', stopped: 'warn', completed: 'ok', in_progress: 'ok', queued: 'warn'};
const taskStatus = x => [h('span', {class: 'chip ' + (TASK_CHIP[x.status] || '')}, x.status === 'in_progress' ? 'running' : x.status), x.stuck ? [' ', h('span', {class: 'chip warn'}, 'no update')] : ''];
const taskLogLink = x => h('a', {href: '#/tasklog?' + qs({id: x.id})}, 'log');
VIEWS.tasklog = async q => {
  const r = await api('/api/task_log?' + qs({id: q.id}));
  return h('div', {}, h('p', {}, h('a', {href: '#/trainings'}, '← trainings')), h('h1', {}, 'Task log'),
    h('p', {class: 'mut small'}, 'Newest ' + r.lines.length + ' of ' + (r.total == null ? '?' : r.total) + ' lines, from mlops-core. Secrets are masked. Older tasks may have no lines left.'),
    h('div', {class: 'card'}, h('pre', {}, r.lines.map(l => new Date(l.ts * 1000).toISOString().slice(0, 19) + ' ' + (l.level || '') + ' ' + l.msg).join('\n') || '(no lines)')));
};
const taskTable = rows => table([['Task', x => h('span', {dir: 'auto'}, x.name)], ['', taskLogLink], ['Type', x => x.type], ['Status', taskStatus], ['Reason', x => h('span', {class: 'small', dir: 'auto'}, x.reason || '')],
  ['Created', x => T(x.created)], ['Last update', x => T(x.last)]], rows);
VIEWS.trainings = async q => {
  const r = await api('/api/tasks'), f = q.status || '', user = q.user || '';
  const stuck = x => x.stuck, counts = {in_progress: 0, queued: 0, failed: 0, completed: 0};
  for (const x of r.tasks) if (x.status in counts) counts[x.status]++;
  const rows = r.tasks.filter(x => (f === 'stuck' ? stuck(x) : !f || x.status === f) && (!user || x.user === user));
  const go = s => { location.hash = '#/trainings' + (s ? '?' + qs({status: s}) : ''); };
  const btn = (s, label) => h('button', {class: f === s ? 'primary' : '', onclick: () => go(s)}, label);
  return h('div', {}, h('h1', {}, 'Trainings · ' + r.tasks.length),
    h('p', {class: 'mut small'}, 'Tasks in mlops-core, read every 5 min. Read ', T(r.at), '. "No update" = running, but nothing reported for 6 h. Scripts and settings of tasks are not read.'),
    r.error ? h('div', {class: 'card err small'}, 'Last read failed: ' + r.error + ' (showing the last good read)') : '',
    h('div', {class: 'row'}, btn('', 'All'), btn('in_progress', 'Running · ' + counts.in_progress), btn('queued', 'Queued · ' + counts.queued), btn('failed', 'Failed · ' + counts.failed),
      btn('completed', 'Completed · ' + counts.completed), btn('stuck', 'No update · ' + r.tasks.filter(stuck).length)),
    h('div', {class: 'card'}, table([['Owner', x => [h('a', {href: '#/user?' + qs({name: x.user})}, x.user), x.person ? h('div', {class: 'small mut', dir: 'auto'}, x.person) : '']],
      ['Task', x => h('span', {dir: 'auto'}, x.name)], ['', taskLogLink], ['Type', x => x.type], ['Status', taskStatus], ['Reason', x => h('span', {class: 'small', dir: 'auto'}, x.reason || '')],
      ['Created', x => T(x.created)], ['Last update', x => T(x.last)]], rows)));
};

VIEWS.keys = async q => {
  const r = await api('/api/keys'), flag = q.flag || '';
  const flags = [...new Set(r.keys.flatMap(k => k.flags))].sort();
  const rows = flag ? r.keys.filter(k => k.flags.includes(flag)) : r.keys;
  const go = f => { location.hash = '#/keys' + (f ? '?' + qs({flag: f}) : ''); };
  return h('div', {}, h('h1', {}, 'Keys · ' + r.keys.length),
    h('p', {class: 'mut small'}, 'Every LiteLLM key, read with a read-only key every 5 min. Read ', T(r.at), '. Warnings stay on this page; nothing goes to the bot.'),
    r.error ? h('div', {class: 'card err small'}, 'Last read failed: ' + r.error + ' (showing the last good read)') : '',
    h('div', {class: 'row'}, h('button', {class: flag ? '' : 'primary', onclick: () => go('')}, 'All'),
      flags.map(f => h('button', {class: flag === f ? 'primary' : '', onclick: () => go(f)}, f + ' · ' + r.keys.filter(k => k.flags.includes(f)).length))),
    h('div', {class: 'card'}, table([['Key', k => h('b', {dir: 'auto'}, k.alias)],
      ['Owner', k => [h('a', {href: '#/user?' + qs({name: k.user})}, k.user), k.person ? h('div', {class: 'small mut', dir: 'auto'}, k.person) : '']],
      ['Warnings', keyFlags], ['Spend / budget', k => k.budget == null ? '$' + k.spend : keyBudget(k)], ['Rate limit', keyLimits], ['Models', keyModels],
      ['Expires', k => k.expires ? T(k.expires) : h('span', {class: 'mut'}, 'never')], ['Last used', k => k.active ? T(k.active) : h('span', {class: 'mut'}, 'never')],
      ['Created', k => T(k.created)]], rows)));
};

VIEWS.user = async q => {
  const u = await api('/api/user?' + qs({name: q.name}));
  return h('div', {}, h('p', {}, h('a', {href: '#/users'}, '← users')),
    h('h1', {dir: 'auto'}, u.person ? u.person.name : u.user, u.person ? h('span', {class: 'mut'}, ' · ' + u.user) : '', u.person && u.person.remove ? [' ', h('span', {class: 'chip warn'}, 'marked to delete')] : ''),
    u.person && u.person.users.length > 1 ? h('p', {}, h('span', {class: 'chip group'}, 'one person · ' + u.person.users.length + ' namespaces'), ' other namespaces: ',
      u.person.users.filter(x => x !== u.user).map(x => [h('a', {href: '#/user?' + qs({name: x})}, x), ' '])) : '',
    h('h2', {}, 'Sign-in activity (Casdoor)'), h('div', {class: 'card'}, u.sso ? h('div', {class: 'stats'},
      h('div', {class: 'stat'}, h('span', {class: 'k'}, 'Last active'), h('span', {class: 'v'}, T(u.sso.last))),
      h('div', {class: 'stat'}, h('span', {class: 'k'}, 'SSO requests'), h('span', {class: 'v'}, u.sso.requests)),
      h('div', {class: 'stat'}, h('span', {class: 'k'}, 'Log-outs'), h('span', {class: 'v'}, u.sso.logouts)),
      h('div', {class: 'stat'}, h('span', {class: 'k'}, 'Denied requests'), h('span', {class: 'v'}, u.sso.denied ? h('span', {class: 'err'}, u.sso.denied) : 0)))
      : h('span', {class: 'mut'}, 'No sign-in in the Casdoor log'), h('div', {class: 'small mut'}, 'The log covers ', T(u.sso_from), ' until now (the running Casdoor pod).'),
      (u.login_rows || []).length ? [h('b', {}, 'Password logins'), table([['Time', x => T(x.ts)], ['Result', x => x.result === 'ok' ? h('span', {class: 'chip ok'}, 'ok') : h('span', {class: 'chip bad'}, x.result)]], u.login_rows)] : ''),
    h('p', {class: 'mut'}, 'Namespaces: ' + (u.namespaces.join(', ') || 'none (API key only)')),
    u.use ? [h('h2', {}, 'LLM and compute · 30 d' + (u.use_from ? ' (Prometheus data from ' + fmt(u.use_from) + ', Tehran)' : '')),
      h('div', {class: 'card'}, h('p', {}, llmCell(u.use), ' · CPU ' + u.use.cpu_h + ' core-h · GPU ' + u.use.gpu_h + ' h'),
        table([['Key', k => k[0]], ['Used by', k => h('span', {dir: 'auto'}, u.key_people[k[0]] || '–')], ['Requests', k => k[1].requests], ['Tokens', k => k[1].tokens], ['Spend $', k => k[1].spend], ['Budget', k => k[1].budget == null ? h('span', {class: 'mut'}, 'no limit') : '$' + (k[1].left == null ? '?' : k[1].left) + ' left of $' + k[1].budget],
          ['Failed', k => k[1].failed ? h('span', {class: 'err'}, k[1].failed) : 0], ['Key hash', k => h('span', {class: 'small mut'}, k[1].hash.slice(0, 8) + '…')]],
          Object.entries(u.use.keys)),
        h('p', {class: 'small'}, 'Requests per model: ' + (Object.entries(u.use.models).map(([m, n]) => m + ' ' + n).join(', ') || '–')),
        h('p', {class: 'small'}, 'API routes on the LLM gateway: ' + (Object.entries(u.use.routes || {}).sort((a, b) => b[1] - a[1]).map(([r, n]) => r + ' ' + n).join(', ') || '–')))] : '',
    (u.key_rows || []).length ? [h('h2', {}, 'Keys · limits and warnings (LiteLLM)'), h('div', {class: 'card'}, table([['Key', k => h('b', {dir: 'auto'}, k.alias)],
      ['Warnings', keyFlags], ['Budget', keyBudget], ['Rate limit', keyLimits], ['Models', keyModels], ['Expires', k => k.expires ? T(k.expires) : h('span', {class: 'mut'}, 'never')],
      ['Last used', k => k.active ? T(k.active) : h('span', {class: 'mut'}, 'never')]], u.key_rows))] : '',
    (u.task_rows || []).length ? [h('h2', {}, 'Tasks · newest ' + u.task_rows.length + ' (mlops-core)'), h('div', {class: 'card'}, taskTable(u.task_rows))] : '',
    h('h2', {}, 'Pods · ' + u.pods.length), h('div', {class: 'card'}, table([['Namespace', p => p.ns], ['Pod', p => p.name], ['Phase', p => p.phase],
      ['Ready', p => live(p.ready ? 'ready' : 'not ready', p.since, null, !p.ready)], ['Restarts', p => p.restarts],
      ['CPU / RAM now', p => p.use_cpu == null ? '–' : p.use_cpu.toFixed(2) + ' cores / ' + (p.use_mem / 1073741824).toFixed(1) + ' GiB'],
      ['Problem', p => p.waiting.join(', ')], ['Started', p => T(p.created)],
      ['', p => p.bad ? h('a', {href: '#/logs?' + qs({ns: p.ns, pod: p.name, container: p.bad})}, 'logs') : '']], u.pods)),
    me.integrations.langfuse ? [h('h2', {}, 'LLM requests · last 7 days (Langfuse)'), llmLog(u.llm_log, u.llm_error)] : '',
    h('h2', {}, 'Incidents'), h('div', {class: 'card'}, incidentTable(u.incidents)),
    h('h2', {}, 'Changes'), h('div', {class: 'card'}, changeTable(u.changes.map(c => Object.assign(c, {info: JSON.parse(c.info || '{}')})))),
    h('h2', {class: 'locked-title'}, 'Activity · not visible yet'),
    h('div', {class: 'locked-grid'}, userLocked().map(lockedCard)));
};

// --- support tickets: six questions per ticket, answered from our data ------------------------------------------------
const TQ = [['q1', '1 · Filed correctly?'], ['q2', '2 · How used · logs'], ['q3', '3 · Infra used'], ['q4', '4 · Services used'],
  ['q5', '5 · Down / problems then'], ['q6', '6 · Used for']];
const TSTATUS = {open: 'open', pending: 'waiting for staff', answered: 'answered', closed: 'closed'};
const qChip = a => h('span', {class: 'chip ' + ({ok: 'ok', bad: 'bad', warn: 'warn'}[a.status] || '')}, a.answer);
// list view: short chip, full answer on hover (the detail page shows everything)
const shortChip = (k, a) => {
  const n = a.answer.split('; ').length;
  const label = k === 'q1' ? (a.status === 'ok' ? 'complete' : n + (n === 1 ? ' issue' : ' issues'))
    : a.answer.length > 22 ? a.answer.slice(0, 20) + '…' : a.answer;
  return h('span', {class: 'chip ' + ({ok: 'ok', bad: 'bad', warn: 'warn'}[a.status] || ''), title: a.answer}, label);
};
const lockedNote = why => !why ? '' : h('div', {class: 'small locked-cell', title: 'Needs ' + why}, 'The user\'s own part: not visible yet. Needs ' + why);

VIEWS.tickets = async () => {
  const r = await api('/api/tickets');
  if (!r.enabled) return h('div', {}, h('h1', {}, 'Tickets'), h('div', {class: 'card locked'}, h('b', {}, 'Ticket site not connected'),
    h('div', {class: 'small'}, 'Set TICKETS_URL, TICKETS_USER and TICKETS_PASSWORD on the station: ssh -t AIP-IRAN sh station/set-ticket-login.sh')));
  return h('div', {}, h('h1', {}, 'Tickets · ' + r.tickets.length),
    h('p', {class: 'mut'}, 'Read from the ticket site every 10 min (', live('synced', null, r.synced), '). Each ticket is checked against our monitoring at the time it was filed. ',
      r.ai ? 'AI check: on.' : 'AI check: off (set AI_URL, AI_KEY, AI_MODEL: sh station/set-ai-key.sh).'),
    r.error ? h('div', {class: 'card err small'}, 'Last sync failed: ' + r.error) : '',
    h('div', {class: 'card'}, table([
      ['Ticket', t => h('div', {style: 'max-width:260px'}, h('b', {dir: 'auto'}, t.subject), h('div', {class: 'small mut'}, t.number + ' · ' + t.requester))],
      ['Status', t => [TSTATUS[t.status] || t.status, h('div', {class: 'small'}, t.priority + ' · last message ', T(t.last))]],
      ...TQ.map(([k, title]) => [title, t => shortChip(k, t.answers[k])])], r.tickets, t => { location.hash = '#/ticket?id=' + t.id; })));
};

// incidents next to a ticket: open at the moment the user wrote, or how close before/after it
const whenTable = rows => table([['#', r => r.id], ['When vs ticket', r => h('span', {class: r.when === 'active' ? 'err' : 'mut'},
  r.when === 'active' ? ['open at ', T(r.at)] : r.when)], ['What', r => SYM[r.symptom] || r.symptom], ['Where', r => r.ns + ' / ' + r.obj],
  ['Owner', r => OWNER(r.owner)], ['Opened → closed', r => [T(r.opened), ' → ', r.closed ? T(r.closed) : 'open']]],
  rows, r => { location.hash = '#/incident?id=' + r.id; });

VIEWS.ticket = async q => {
  const t = await api('/api/ticket?' + qs({id: q.id})), a = t.answers;
  return h('div', {}, h('p', {}, h('a', {href: '#/tickets'}, '← tickets')), h('h1', {}, t.subject),
    h('p', {class: 'mut'}, t.number + ' · ' + t.requester + ' · ' + (TSTATUS[t.status] || t.status) + ' · ' + t.priority + ' · filed ' + fmt(t.created) +
      ' · assigned: ' + (t.assignee || '–') + ' · read ' + ago(t.seen)),
    h('h2', {}, TQ[0][1]), h('div', {class: 'card'}, h('ul', {}, a.q1.answer.split('; ').map(x => h('li', {class: a.q1.status === 'ok' ? '' : 'err'}, x))), h('p', {}, a.q1.check), a.q1.ai ? h('p', {class: 'small'}, 'AI: ' + a.q1.ai) : ''),
    h('h2', {}, TQ[1][1]), h('div', {class: 'card'}, qChip(a.q2),
      a.q2.model_pods.length ? h('p', {}, 'Logs of the model pods: ', a.q2.model_pods.map(p => [h('a', {href: '#/logs?' + qs(p)}, p.pod), ' '])) : '',
      a.q2.log ? [h('p', {class: 'small mut'}, 'LLM calls of the user ' + fmt(a.q2.log.from) + ' → ' + fmt(a.q2.log.to) + ' (Langfuse):'), llmLog(a.q2.log)] : '',
      lockedNote(a.q2.locked)),
    h('h2', {}, TQ[2][1]), h('div', {class: 'card'}, qChip(a.q3),
      a.q3.user ? h('p', {class: 'small'}, 'User ', h('a', {href: '#/user?' + qs({name: a.q3.user})}, a.q3.user),
        {key: ' (owner of the API key in the ticket)', 'name list': ' (requester name matched the name list)'}[a.q3.user_found] || ' (from data/ticket-users.json)') : '',
      a.q3.pods.length ? table([['Model', p => p.model], ['Pod', p => p.pod], ['Ready', p => live(p.ready ? 'ready' : 'not ready', p.since, null, !p.ready)],
        ['GPU', p => p.gpu], ['CPU now', p => p.cpu_use == null ? '–' : p.cpu_use.toFixed(2)], ['Node', p => p.node]], a.q3.pods) : '',
      lockedNote(a.q3.locked)),
    h('h2', {}, TQ[3][1]), h('div', {class: 'card'}, qChip(a.q4), h('p', {class: 'small'}, 'Named in the ticket: ' + a.q4.named),
      a.q4.usage.length ? table([['Period', u => u.period], ['Model', u => u.model], ['What', u => u.metric], ['Amount', u => u.value],
        ['Key', u => h('span', {class: 'small mut'}, 'sha256 ' + u.key + '…')]], a.q4.usage) : '',
      a.q4.models.length ? table([['Model', m => m.name], ['Namespace', m => m.ns], ['Match', m => m.exact ? 'name' : 'family'],
        ['Now', m => live(m.state || '–', m.since, m.checked, m.state !== 'ready')]], a.q4.models) : '',
      lockedNote(a.q4.locked)),
    h('h2', {}, TQ[4][1]), h('div', {class: 'card'}, qChip(a.q5),
      h('p', {class: 'small mut'}, 'Matched at the moments the user wrote: ' + a.q5.moments.map(fmt).join(', ') +
        ' · our data starts ' + fmt(a.q5.data_from) +
        (a.q5.first_answer_h != null ? ' · first staff answer after ' + a.q5.first_answer_h + ' h' : ' · no staff answer yet')),
      a.q5.user_errors && a.q5.user_errors.length ? [h('b', {}, 'Failed calls of the user then'), table([['Error', e => h('span', {class: 'small', dir: 'auto'}, e.error)],
        ['Times', e => e.count], ['Model', e => e.model || '–'], ['Last', e => T(e.last)]], a.q5.user_errors)] : '',
      a.q5.platform_llm ? [h('b', {}, 'All LLM calls on the platform, 30 min around the filing time (Langfuse)'),
        table([['Model', m => m.ticket ? h('b', {}, m.model + ' ← in the ticket') : m.model], ['Kind', m => m.kind], ['Calls', m => m.count],
          ['Failed', m => m.failed ? h('span', {class: m.count >= 10 && m.failed >= 0.3 * m.count ? 'err' : ''}, m.failed + ' (' + Math.round(100 * m.failed / m.count) + ' %)') : 0]],
          a.q5.platform_llm)] : '',
      h('b', {}, 'On what the user uses'), whenTable(a.q5.incidents), h('b', {}, 'Platform-wide (ours or infra)'), whenTable(a.q5.platform)),
    h('h2', {}, TQ[5][1]), h('div', {class: 'card'}, qChip(a.q6)),
    h('h2', {}, 'Conversation · ' + t.messages.length), h('div', {class: 'card'}, t.messages.map(m =>
      h('div', {style: 'margin-bottom:12px'}, h('div', {class: 'small mut'}, m.name + ' · ' + m.role + ' · ', T(m.ts)),
        h('div', {dir: 'auto', style: 'white-space:pre-wrap'}, m.text)))));
};

VIEWS.capacity = async () => {
  const c = await api('/api/capacity');
  const maxCpu = Math.max(...c.namespaces.map(r => r.cpu_use), 1);
  const noMetrics = !c.namespaces.some(r => r.metrics);
  return h('div', {}, h('h1', {}, 'Capacity'),
    h('p', {class: 'mut'}, 'Live use from metrics.k8s.io against what pods requested. Not one of the 8 events: a view to spot waste and pods about to run out of memory.'),
    noMetrics ? h('div', {class: 'card err small'}, 'No usage numbers: metrics.k8s.io answered nothing.') : '',
    c.services.length ? [h('h2', {}, 'Non-LLM services · whole service, not per user'), h('div', {class: 'card'}, table([['Service', r => h('a', {href: '#/services?' + qs({ns: r.ns})}, r.ns)],
      ['CPU hours · 30 d', r => r.cpu_h], ['RAM now', r => r.mem_gib + ' GiB'], ['Traffic in · 7 d', r => r.net_gb + ' GB']], c.services),
      h('div', {class: 'small mut'}, 'From Prometheus (data from 21 Oct). These services\' logs do not say which user, so there is no per-user split. Traffic includes their internal traffic.'))] : '',
    h('h2', {}, 'Pods near their memory limit (85% or more) · ' + c.near_limit.length),
    h('div', {class: 'card'}, table([['Namespace', r => r.ns], ['Pod', r => r.pod], ['Used', r => r.used_gib + ' GiB'], ['Limit', r => r.limit_gib + ' GiB'],
      ['%', r => h('span', {class: r.pct >= 95 ? 'err' : ''}, r.pct + ' %')]], c.near_limit)),
    h('h2', {}, 'GPUs held · ' + c.gpus.reduce((a, g) => a + g.count, 0)),
    h('div', {class: 'card'}, table([['Namespace', g => g.ns], ['Workload', g => g.workload], ['GPU', g => g.gpu + ' × ' + g.count],
      ['CPU now', g => g.cpu == null ? '–' : g.cpu.toFixed(2) + ' cores']], c.gpus)),
    h('h2', {}, 'Idle pods that reserve 1+ CPU (using under 2%) · ' + c.idle.length),
    h('div', {class: 'card'}, table([['Namespace', r => r.ns], ['Pod', r => r.pod], ['CPU reserved', r => r.cpu_req], ['CPU used', r => r.cpu_use],
      ['GPU', r => r.gpu]], c.idle)),
    h('h2', {}, 'By namespace'),
    h('div', {class: 'card'}, table([['Namespace', r => r.ns], ['Pods', r => r.pods], ['CPU used / requested', r => [bar(r.cpu_use, maxCpu), ' ', r.cpu_use + ' / ' + r.cpu_req]],
      ['RAM used / requested', r => r.mem_use_gib + ' / ' + r.mem_req_gib + ' GiB'], ['GPUs', r => r.gpu]], c.namespaces)));
};

VIEWS.trends = async () => {
  const tr = await api('/api/trends');
  const maxO = Math.max(...tr.days.map(d => d.opened || 0), 1), maxR = Math.max(...tr.days.map(d => d.restarts || 0), 1);
  return h('div', {}, h('h1', {}, 'Trends'),
    h('p', {class: 'mut'}, 'Last 14 days, Tehran calendar. Restart counts start from the day this tab was added.'),
    h('div', {class: 'card'}, table([['Day', d => d.day], ['Incidents opened', d => [bar(d.opened || 0, maxO), ' ', d.opened || 0]], ['Closed', d => d.closed || 0],
      ['Avg time to close', d => d.fix_min == null ? '–' : d.fix_min + ' min'], ['Changes', d => d.changes || 0],
      ['Restarts', d => [bar(d.restarts || 0, maxR), ' ', d.restarts || 0]]], tr.days)),
    h('h2', {}, 'Services with the most incidents (30 days)'),
    h('div', {class: 'card'}, table([['Namespace', r => r.ns], ['Service', r => r.obj], ['Incidents', r => r.n], ['Open now', r => r.open]], tr.top_incidents)),
    h('h2', {}, 'Services that restart most (7 days)'),
    h('div', {class: 'card'}, table([['Namespace', r => r.ns], ['Service', r => r.wl], ['Restarts', r => r.n]], tr.top_restarts)));
};

VIEWS.infra = async () => {
  const r = await api('/api/infra');
  const groups = [['infra', 'Infra team owns these'], ['us', 'Ours: endpoints'], ['asset', 'Ours: MLOps assets'], ['monitor', 'The probe itself']];
  return h('div', {}, h('h1', {}, 'Infra or us?'),
    h('p', {class: 'mut'}, 'Probe report ' + ago(r.ts) + '. Rule: platform down or out of capacity = infra; platform up but our service fails = us.'),
    groups.map(([o, title]) => {
      const rows = r.results.filter(x => x.owner === o).sort((a, b) => a.ok - b.ok || a.name.localeCompare(b.name));
      if (!rows.length) return '';
      return [h('h2', {}, title + ' · ' + rows.filter(x => !x.ok).length + ' down / ' + rows.length),
        h('div', {class: 'card'}, table([['Status', x => live(h('span', {class: 'chip ' + (x.ok ? 'ok' : 'bad')}, x.ok ? 'up' : 'down'), x.since, x.checked)], ['Check', x => x.name],
          ['Owner', x => x.meta && x.meta.person ? x.meta.person : ''], ['Detail', x => h('span', {class: 'small'}, x.detail)]], rows))];
    }));
};

render();
