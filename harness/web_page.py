"""The single web page served by the harness.

A small polling client over the same JSON API the terminal CLI uses. The
browser holds no state of its own: everything renders from GET /api/state.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Assistant</title>
<style>
  :root { --bg:#101418; --panel:#1a2027; --line:#2b333d; --text:#e8ecef;
          --dim:#93a1ad; --accent:#5aa9e6; --warn:#e0b050; --ok:#69c07d; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif; height:100vh;
         display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:12px; padding:8px 14px;
           background:var(--panel); border-bottom:1px solid var(--line); }
  header .title { font-weight:600; }
  header .meta { color:var(--dim); font-size:12px; flex:1; }
  nav button { background:none; border:1px solid var(--line); color:var(--dim);
    padding:4px 10px; border-radius:4px; cursor:pointer; margin-left:4px; }
  nav button.active { color:var(--text); border-color:var(--accent); }
  main { flex:1; display:flex; min-height:0; }
  #chatwrap { flex:1; display:flex; flex-direction:column; min-width:0; }
  #chat { flex:1; overflow-y:auto; padding:14px; display:flex;
          flex-direction:column; gap:10px; }
  .msg { max-width:80%; padding:8px 12px; border-radius:10px; white-space:pre-wrap; }
  .msg.user { align-self:flex-end; background:#274156; }
  .msg.assistant { align-self:flex-start; background:var(--panel);
                    border:1px solid var(--line); }
  .msg.note { align-self:flex-start; background:#22261b;
               border:1px solid #4a4a2f; color:#d8d3ae; font-size:13px; }
  .msg .src { display:block; font-size:10px; color:var(--dim);
              text-align:right; margin-top:2px; }
  #composer { display:flex; gap:8px; padding:10px; border-top:1px solid var(--line); }
  #composer textarea { flex:1; resize:none; background:var(--panel);
    color:var(--text); border:1px solid var(--line); border-radius:6px;
    padding:8px; font:inherit; }
  #composer button { background:var(--accent); color:#08121b; border:none;
    border-radius:6px; padding:8px 18px; font-weight:600; cursor:pointer; }
  #banner { padding:6px 14px; background:#3a2f1a; color:var(--warn);
            border-bottom:1px solid var(--line); font-size:13px;
            display:none; }
  #side { width:420px; border-left:1px solid var(--line); overflow-y:auto;
          padding:12px; background:var(--panel); display:none; }
  #side.open { display:block; }
  .card { border:1px solid var(--line); border-radius:6px; padding:10px;
          margin-bottom:10px; background:#141a20; }
  .card h3 { margin:0 0 6px; font-size:13px; color:var(--accent); }
  .card .small { font-size:12px; color:var(--dim); }
  .card button { background:#243041; color:var(--text); border:1px solid var(--line);
    border-radius:4px; padding:3px 10px; cursor:pointer; font-size:12px; margin:2px; }
  .card button.ok { border-color:var(--ok); }
  .card button.no { border-color:#c96a6a; }
  input, select { background:#0d1116; color:var(--text);
    border:1px solid var(--line); border-radius:4px; padding:4px 6px;
    font:inherit; font-size:12px; }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px;
          font-size:11px; border:1px solid var(--line); color:var(--dim); }
  .pill.active { color:var(--ok); border-color:var(--ok); }
  .pill.paused { color:var(--warn); border-color:var(--warn); }
  .pill.failed { color:#e07b7b; border-color:#e07b7b; }
  pre { white-space:pre-wrap; word-break:break-word; font-size:11.5px;
        color:var(--dim); }
</style>
</head>
<body>
<header>
  <span class="title" id="who">Assistant</span>
  <span class="meta" id="statusmeta">connecting...</span>
  <nav>
    <button data-tab="tasks">Tasks</button>
    <button data-tab="approvals">Approvals</button>
    <button data-tab="schedules">Schedules</button>
    <button data-tab="memories">Memory</button>
    <button data-tab="system">System</button>
  </nav>
</header>
<div id="banner"></div>
<main>
  <div id="chatwrap">
    <div id="chat"></div>
    <div id="composer">
      <textarea id="input" rows="2" placeholder="Message the assistant..."></textarea>
      <button id="send">Send</button>
    </div>
  </div>
  <div id="side"></div>
</main>
<script>
let state = null;
let tab = null;
let busy = false;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = '/?token=' +
      encodeURIComponent(prompt('Access token:') || ''); return null; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { note('API error: ' + (data.error || res.status)); return null; }
  return data;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function render() {
  if (!state) return;
  const persona = state.config.persona || {};
  document.getElementById('who').textContent = persona.speaker_label || 'Assistant';
  const open = state.open_tasks || [];
  const current = open[0];
  const domain = (state.domains || []).find(d => current && d.id === current.domain_id);
  document.getElementById('statusmeta').textContent =
    (domain ? ('domain: ' + domain.name + '  ') : '') +
    (current ? ('task: ' + (current.objective || '').slice(0, 60)) : 'no open work');

  const banner = document.getElementById('banner');
  if ((state.approvals || []).length) {
    banner.style.display = 'block';
    banner.textContent = state.approvals.length + ' action(s) waiting for your approval - see Approvals.';
  } else { banner.style.display = 'none'; }

  const chat = document.getElementById('chat');
  chat.innerHTML = '';
  const nearBottom = true;
  for (const m of state.messages || []) {
    const cls = m.role === 'user' ? 'user' : (m.role === 'note' ? 'note' : 'assistant');
    const node = el('div', 'msg ' + cls, m.content);
    node.appendChild(el('span', 'src', m.source || ''));
    chat.appendChild(node);
  }
  chat.scrollTop = chat.scrollHeight;
  renderSide();
}

function note(text) {
  const chat = document.getElementById('chat');
  chat.appendChild(el('div', 'msg note', text));
  chat.scrollTop = chat.scrollHeight;
}

function pill(status) {
  const node = el('span', 'pill ' + status, status);
  return node;
}

function renderSide() {
  const side = document.getElementById('side');
  if (!tab) { side.classList.remove('open'); return; }
  side.classList.add('open');
  side.innerHTML = '';
  if (tab === 'tasks') {
    for (const t of state.tasks || []) {
      const card = el('div', 'card');
      const head = el('h3', null, (t.objective || '').slice(0, 70));
      card.appendChild(head);
      card.appendChild(pill(t.status));
      card.appendChild(el('div', 'small',
        (t.status_reason || '') + '  updated ' + (t.updated_at || '')));
      const btn = el('button', null, 'details');
      btn.onclick = async () => {
        const detail = await api('/api/tasks/' + t.id);
        if (!detail) return;
        card.appendChild(el('pre', null, JSON.stringify(
          {runs: detail.runs, executions: detail.executions,
           sources: (detail.context_sources || []).length}, null, 1)));
      };
      card.appendChild(btn);
      side.appendChild(card);
    }
  } else if (tab === 'approvals') {
    if (!(state.approvals || []).length)
      side.appendChild(el('div', 'card small', 'No pending approvals.'));
    for (const a of state.approvals || []) {
      const card = el('div', 'card');
      card.appendChild(el('h3', null, a.tool));
      card.appendChild(el('div', 'small', JSON.stringify(a.arguments)));
      card.appendChild(el('div', 'small', (a.reason || '') + ' (' + a.id + ')'));
      const ok = el('button', 'ok', 'Approve');
      const no = el('button', 'no', 'Reject');
      ok.onclick = () => decide(a.id, 'approve');
      no.onclick = () => decide(a.id, 'reject');
      card.appendChild(ok); card.appendChild(no);
      side.appendChild(card);
    }
  } else if (tab === 'schedules') {
    const form = el('div', 'card');
    form.appendChild(el('h3', null, 'New schedule'));
    const name = el('input'); name.placeholder = 'name';
    const prompt_ = el('input'); prompt_.placeholder = 'prompt to run';
    prompt_.style.width = '95%';
    const kind = el('select');
    for (const k of ['daily', 'interval', 'weekly', 'once']) {
      const o = el('option', null, k); o.value = k; kind.appendChild(o);
    }
    const time = el('input'); time.placeholder = 'HH:MM (daily/weekly)';
    const interval = el('input'); interval.placeholder = 'seconds (interval)';
    const at = el('input'); at.placeholder = 'ISO time (once)';
    const add = el('button', 'ok', 'Add');
    add.onclick = async () => {
      const body = {name: name.value, prompt: prompt_.value, kind: kind.value,
        time_of_day: time.value || null,
        interval_s: parseFloat(interval.value) || null,
        at_ts: at.value || null};
      const out = await api('/api/schedules',
        {method: 'POST', body: JSON.stringify(body)});
      if (out) refresh();
    };
    for (const n of [name, kind, time, interval, at]) form.appendChild(n);
    form.appendChild(el('br'));
    form.appendChild(prompt_);
    form.appendChild(add);
    side.appendChild(form);
    for (const s of state.schedules || []) {
      const card = el('div', 'card');
      card.appendChild(el('h3', null, s.name + ' [' + s.kind + ']'));
      card.appendChild(el('div', 'small', s.prompt));
      card.appendChild(el('div', 'small',
        (s.enabled ? 'enabled' : 'disabled') +
        ' | next: ' + (s.next_due_at || '-') + ' | tz ' + s.tz));
      const toggle = el('button', null, s.enabled ? 'Disable' : 'Enable');
      toggle.onclick = async () => {
        await api('/api/schedules/' + s.id + '/' + (s.enabled ? 'disable' : 'enable'),
          {method: 'POST'});
        refresh();
      };
      const del = el('button', 'no', 'Delete');
      del.onclick = async () => {
        await api('/api/schedules/' + s.id + '/delete', {method: 'POST'});
        refresh();
      };
      card.appendChild(toggle); card.appendChild(del);
      side.appendChild(card);
    }
  } else if (tab === 'memories') {
    const card0 = el('div', 'card');
    const btn = el('button', 'ok', 'Run consolidation now');
    btn.onclick = async () => {
      note('Running consolidation...');
      const out = await api('/api/consolidate', {method: 'POST'});
      if (out) note('Consolidation ' + out.status + ': applied ' +
        (out.applied || []).length + ', rejected ' + (out.rejected || []).length);
      refresh();
    };
    card0.appendChild(el('h3', null, 'Maintenance'));
    card0.appendChild(btn);
    side.appendChild(card0);
    for (const m of state.memories || []) {
      const card = el('div', 'card');
      card.appendChild(el('h3', null, m.kind + ' | ' + m.scope));
      card.appendChild(el('div', 'small', m.content));
      card.appendChild(el('div', 'small', m.id + ' rev ' + m.revision +
        ' | ' + (m.updated_at || '')));
      side.appendChild(card);
    }
  } else if (tab === 'system') {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, 'Tools'));
    const ul = el('div', 'small');
    for (const t of state.tools || [])
      ul.appendChild(el('div', null, t.name + ' [' + t.risk + ']'));
    card.appendChild(ul);
    side.appendChild(card);
    const ep = el('div', 'card');
    ep.appendChild(el('h3', null, 'Model endpoint'));
    const probe = el('button', null, 'Probe endpoint');
    const out = el('pre');
    probe.onclick = async () => {
      out.textContent = 'probing...';
      const data = await api('/api/endpoint-probe');
      if (data) out.textContent = JSON.stringify(data, null, 1);
    };
    ep.appendChild(probe); ep.appendChild(out);
    side.appendChild(ep);
    const bk = el('div', 'card');
    bk.appendChild(el('h3', null, 'Backup'));
    const doBackup = el('button', null, 'Create backup');
    const bout = el('div', 'small');
    doBackup.onclick = async () => {
      const data = await api('/api/backup', {method: 'POST'});
      if (data) bout.textContent = 'written: ' + data.backup;
    };
    bk.appendChild(doBackup); bk.appendChild(bout);
    side.appendChild(bk);
    const ev = el('div', 'card');
    ev.appendChild(el('h3', null, 'Recent events'));
    const pre = el('pre');
    for (const e of (state.events || []).slice(-40))
      pre.appendChild(el('div', null,
        e.ts + ' ' + e.type + ' ' + (e.data || '').slice(0, 120)));
    ev.appendChild(pre);
    side.appendChild(ev);
    const cf = el('div', 'card');
    cf.appendChild(el('h3', null, 'Configuration (read-only view)'));
    const cpre = el('pre');
    cpre.textContent = JSON.stringify(state.config, null, 1);
    cf.appendChild(cpre);
    side.appendChild(cf);
  }
}

async function decide(id, decision) {
  const out = await api('/api/approvals/' + id + '/' + decision, {method: 'POST'});
  if (out) note('Approval ' + decision + ' -> ' + out.status);
  refresh();
}

async function send() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text || busy) return;
  busy = true;
  document.getElementById('send').disabled = true;
  input.value = '';
  try {
    const out = await api('/api/message',
      {method: 'POST', body: JSON.stringify({text, source: 'web'})});
    if (out && out.status) note('[' + out.status + ']');
  } finally {
    busy = false;
    document.getElementById('send').disabled = false;
    refresh();
  }
}

async function refresh() {
  const data = await api('/api/state');
  if (data) { state = data; render(); }
}

document.getElementById('send').onclick = send;
document.getElementById('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
document.querySelectorAll('nav button').forEach((b) => {
  b.onclick = () => {
    tab = (tab === b.dataset.tab) ? null : b.dataset.tab;
    document.querySelectorAll('nav button').forEach(x =>
      x.classList.toggle('active', x.dataset.tab === tab));
    renderSide();
  };
});
refresh();
setInterval(() => { if (!busy) refresh(); }, 3000);
</script>
</body>
</html>
"""
