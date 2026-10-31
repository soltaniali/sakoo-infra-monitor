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
function ago(t) {
  if (!t) return '–';
  const s = Date.now() / 1000 - t;
  return s < 90 ? Math.round(s) + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
}
const OWNER = o => o === 'us' ? 'MLOps (us)' : o === 'infra' ? 'Infra team' : o === 'asset' ? 'MLOps asset' : o === 'monitor' ? 'Probe' : 'Team ' + o;
const SYM = {crashloop: 'Crash loop', oomkilled: 'Out of memory', 'image-pull': 'Image pull', pending: 'Pending', flapping: 'Restarting',
  'probe-failing': 'Probe failing', down: 'Down', 'model-down': 'Model down', 'new-error': 'New error', endpoint: 'Endpoint check', infra: 'Infra check'};
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
    rows.map(r => h('tr', {class: onclick ? 'click' : '', onclick: onclick && (() => onclick(r))}, cols.map(c => h('td', {}, c[1](r))))));
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
  ['services', 'Services'], ['logs', 'Logs'], ['infra', 'Infra or us'], ['users', 'Users'], ['tickets', 'Tickets'], ['capacity', 'Capacity'], ['trends', 'Trends']];
function nav(page) {
  document.getElementById('tabs').replaceChildren(...TABS.map(([k, t]) => h('a', {href: '#/' + k, class: page === k || (page === 'incident' && k === 'incidents') || (page === 'ticket' && k === 'tickets') ? 'on' : ''}, t)));
  document.getElementById('who').replaceChildren(me.user + ' · ', h('a', {href: '#', onclick: async ev => { ev.preventDefault(); await post('/api/logout', {}); showLogin(); }}, 'log out'));
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
  return h('span', {title: (since ? 'since ' + fmt(since) : '') + (checked ? ', checked ' + fmt(checked) : '')},
    h('span', {class: bad ? 'err' : ''}, state), h('span', {class: 'small mut'}, (since ? ' · since ' + ago(since) : '') + (checked ? ' · checked ' + ago(checked) : '')));
}
const incidentCols = [
  ['#', r => r.id], ['Opened', r => h('span', {title: fmt(r.opened)}, ago(r.opened))], ['What', r => SYM[r.symptom] || r.symptom],
  ['Where', r => (r.ns === '-' ? '' : r.ns + ' / ') + r.obj], ['Owner', r => OWNER(r.owner)],
  ['Last seen', r => r.closed ? h('span', {class: 'mut'}, 'closed ' + ago(r.closed)) : h('span', {title: fmt(r.last_seen)}, ago(r.last_seen))],
  ['Cause', r => h('span', {class: 'small'}, r.cause)]];
const incidentTable = rows => table(incidentCols, rows, r => { location.hash = '#/incident?id=' + r.id; });
function changeTable(rows) {
  return table([
    ['Time', r => h('span', {title: fmt(r.ts)}, fmt(r.ts))], ['Change', r => CHG[r.type] || r.type], ['Where', r => r.ns + ' / ' + r.name],
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
  const chips = ['ai', 'litellm', 'gitlab', 'grafana', 'tickets'].map(k => h('span', {class: 'chip ' + (ig[k] ? (ig.errors[k] ? 'warn' : 'ok') : ''),
    title: ig.errors[k] || (ig[k] ? 'on' : 'off: set its env in secrets/ops.env')}, {ai: 'AI summaries', litellm: 'model test calls', gitlab: 'who changed it', grafana: 'GPU %', tickets: 'ticket site'}[k]));
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
    !me.integrations.grafana ? h('p', {class: 'mut small'}, 'GPU % is off: set GRAFANA_URL and GRAFANA_TOKEN in secrets/ops.env.') : '');
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
const LOCKED_USER = [
  {key: 'login', title: 'Logins', shows: 'last login, failed logins, IP', needs: 'access request 45 (Casdoor login records)'},
  {key: 'requests', title: 'API requests', shows: 'requests per service, errors, slowest calls', needs: 'access request 46 (user in gateway/Traefik access log) + audit line in our services'},
  {key: 'llm', title: 'LLM usage', shows: 'models used, tokens, LLM errors', needs: 'access request 47 (LiteLLM spend logs, Langfuse)'},
  {key: 'train', title: 'Trainings', shows: 'queued, running and failed mlops-core tasks', needs: 'a read-only mlops-core API key (our own service)'},
  {key: 'quota', title: 'Quota', shows: 'quota used vs limit', needs: 'access request 41 (RMG read-only)'},
  {key: 'org', title: 'Organisation', shows: 'university or company, contact, service level', needs: 'access request 26 (customer list)'},
  {key: 'history', title: 'Log history', shows: 'logs of pods that are gone, 30 days', needs: 'access requests 7 and 12 (central log store)'},
];
const lockedCell = l => h('span', {class: 'locked-cell', title: l.title + ': not visible yet. Needs ' + l.needs}, '—');
const lockedCard = l => h('div', {class: 'card locked'}, h('div', {}, h('b', {}, l.title), h('span', {class: 'chip'}, 'not visible yet')),
  h('div', {class: 'small'}, 'Will show: ' + l.shows), h('div', {class: 'small'}, 'Needs: ' + l.needs));

VIEWS.users = async () => {
  const us = await api('/api/users');
  const lockedCols = LOCKED_USER.slice(0, 4).map(l => [h('span', {class: 'locked-cell', title: 'Needs ' + l.needs}, l.title), () => lockedCell(l)]);
  return h('div', {}, h('h1', {}, 'Users · ' + us.length),
    h('p', {class: 'mut'}, 'What each user runs now (user-* namespaces and JupyterHub notebooks), what it uses, what is broken. Grey = not visible yet; hover to see what unlocks it.'),
    h('div', {class: 'card'}, table([
      ['User', u => h('a', {href: '#/user?' + qs({name: u.user})}, u.user)], ['Runs', u => u.kinds.join(', ')],
      ['Pods', u => u.running + ' running' + (u.pending ? ', ' + u.pending + ' pending' : '')],
      ['CPU used / reserved', u => u.cpu_use + ' / ' + u.cpu_req], ['RAM', u => u.mem_use_gib + ' GiB'], ['GPU', u => u.gpu],
      ['Restarts', u => u.restarts], ['Open incidents', u => u.open ? h('span', {class: 'err'}, u.open) : 0],
      ['Changes (7 d)', u => u.changes_7d ? u.changes_7d + ' · ' + ago(u.last_change) : 0], ['Running since', u => ago(u.started)],
      ...lockedCols], us)),
    h('h2', {class: 'locked-title'}, 'Not visible yet · ' + LOCKED_USER.length),
    h('div', {class: 'locked-grid'}, LOCKED_USER.map(lockedCard)));
};

VIEWS.user = async q => {
  const u = await api('/api/user?' + qs({name: q.name}));
  return h('div', {}, h('p', {}, h('a', {href: '#/users'}, '← users')), h('h1', {}, u.user),
    h('p', {class: 'mut'}, 'Namespaces: ' + u.namespaces.join(', ')),
    h('h2', {}, 'Pods · ' + u.pods.length), h('div', {class: 'card'}, table([['Namespace', p => p.ns], ['Pod', p => p.name], ['Phase', p => p.phase],
      ['Ready', p => live(p.ready ? 'ready' : 'not ready', p.since, null, !p.ready)], ['Restarts', p => p.restarts],
      ['CPU / RAM now', p => p.use_cpu == null ? '–' : p.use_cpu.toFixed(2) + ' cores / ' + (p.use_mem / 1073741824).toFixed(1) + ' GiB'],
      ['Problem', p => p.waiting.join(', ')], ['Age', p => ago(p.created)],
      ['', p => p.bad ? h('a', {href: '#/logs?' + qs({ns: p.ns, pod: p.name, container: p.bad})}, 'logs') : '']], u.pods)),
    h('h2', {}, 'Incidents'), h('div', {class: 'card'}, incidentTable(u.incidents)),
    h('h2', {}, 'Changes'), h('div', {class: 'card'}, changeTable(u.changes.map(c => Object.assign(c, {info: JSON.parse(c.info || '{}')})))),
    h('h2', {class: 'locked-title'}, 'Activity · not visible yet'),
    h('div', {class: 'locked-grid'}, LOCKED_USER.map(lockedCard)));
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
      ['Status', t => [TSTATUS[t.status] || t.status, h('div', {class: 'small mut'}, t.priority + ' · ' + ago(t.last))]],
      ...TQ.map(([k, title]) => [title, t => shortChip(k, t.answers[k])])], r.tickets, t => { location.hash = '#/ticket?id=' + t.id; })));
};

// incidents next to a ticket: open at the moment the user wrote, or how close before/after it
const whenTable = rows => table([['#', r => r.id], ['When vs ticket', r => h('span', {class: r.when === 'active' ? 'err' : 'mut'},
  r.when === 'active' ? 'open at ' + fmt(r.at) : r.when)], ['What', r => SYM[r.symptom] || r.symptom], ['Where', r => r.ns + ' / ' + r.obj],
  ['Owner', r => OWNER(r.owner)], ['Opened → closed', r => fmt(r.opened) + ' → ' + (r.closed ? fmt(r.closed) : 'open')]],
  rows, r => { location.hash = '#/incident?id=' + r.id; });

VIEWS.ticket = async q => {
  const t = await api('/api/ticket?' + qs({id: q.id})), a = t.answers;
  return h('div', {}, h('p', {}, h('a', {href: '#/tickets'}, '← tickets')), h('h1', {}, t.subject),
    h('p', {class: 'mut'}, t.number + ' · ' + t.requester + ' · ' + (TSTATUS[t.status] || t.status) + ' · ' + t.priority + ' · filed ' + fmt(t.created) +
      ' · assigned: ' + (t.assignee || '–') + ' · read ' + ago(t.seen)),
    h('h2', {}, TQ[0][1]), h('div', {class: 'card'}, h('ul', {}, a.q1.answer.split('; ').map(x => h('li', {class: a.q1.status === 'ok' ? '' : 'err'}, x))), h('p', {}, a.q1.check), a.q1.ai ? h('p', {class: 'small'}, 'AI: ' + a.q1.ai) : ''),
    h('h2', {}, TQ[1][1]), h('div', {class: 'card'}, qChip(a.q2),
      a.q2.model_pods.length ? h('p', {}, 'Logs of the model pods: ', a.q2.model_pods.map(p => [h('a', {href: '#/logs?' + qs(p)}, p.pod), ' '])) : '',
      lockedNote(a.q2.locked)),
    h('h2', {}, TQ[2][1]), h('div', {class: 'card'}, qChip(a.q3),
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
      h('b', {}, 'On what the user uses'), whenTable(a.q5.incidents), h('b', {}, 'Platform-wide (ours or infra)'), whenTable(a.q5.platform)),
    h('h2', {}, TQ[5][1]), h('div', {class: 'card'}, qChip(a.q6)),
    h('h2', {}, 'Conversation · ' + t.messages.length), h('div', {class: 'card'}, t.messages.map(m =>
      h('div', {style: 'margin-bottom:12px'}, h('div', {class: 'small mut'}, m.name + ' · ' + m.role + ' · ' + fmt(m.ts)),
        h('div', {dir: 'auto', style: 'white-space:pre-wrap'}, m.text)))));
};

VIEWS.capacity = async () => {
  const c = await api('/api/capacity');
  const maxCpu = Math.max(...c.namespaces.map(r => r.cpu_use), 1);
  const noMetrics = !c.namespaces.some(r => r.metrics);
  return h('div', {}, h('h1', {}, 'Capacity'),
    h('p', {class: 'mut'}, 'Live use from metrics.k8s.io against what pods requested. Not one of the 8 events: a view to spot waste and pods about to run out of memory.'),
    noMetrics ? h('div', {class: 'card err small'}, 'No usage numbers: metrics.k8s.io answered nothing.') : '',
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
