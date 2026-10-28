"""The single web page served by the harness.

A small polling client over the same JSON API the terminal CLI uses. The
browser holds no state of its own: everything renders from GET /api/state and
mutations go through the same endpoints the CLI uses. Chat is updated
incrementally; the side panel is re-rendered from state, but never while a
form field inside it has focus.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Assistant</title>
<style>
  :root { --bg:#101418; --panel:#1a2027; --panel2:#141a20; --line:#2b333d;
          --text:#e8ecef; --dim:#93a1ad; --accent:#5aa9e6; --warn:#e0b050;
          --ok:#69c07d; --bad:#e07b7b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif; height:100vh;
         display:flex; flex-direction:column; overflow:hidden; }
  header { display:flex; align-items:center; gap:12px; padding:8px 14px;
           background:var(--panel); border-bottom:1px solid var(--line);
           flex-wrap:wrap; }
  header .title { font-weight:600; }
  header .meta { color:var(--dim); font-size:12px; flex:1; min-width:180px; }
  nav { display:flex; gap:4px; flex-wrap:wrap; }
  nav button { background:none; border:1px solid var(--line); color:var(--dim);
    padding:4px 10px; border-radius:4px; cursor:pointer; font:inherit;
    font-size:12px; }
  nav button.active { color:var(--text); border-color:var(--accent);
    background:#1d2b38; }
  #banner { padding:6px 14px; background:#3a2f1a; color:var(--warn);
            border-bottom:1px solid var(--line); font-size:13px;
            display:none; cursor:pointer; }
  main { flex:1; display:flex; min-height:0; }
  #chatwrap { flex:1; display:flex; flex-direction:column; min-width:0; }
  #chat { flex:1; overflow-y:auto; padding:14px; display:flex;
          flex-direction:column; gap:10px; }
  .msg { max-width:80%; padding:8px 12px; border-radius:10px;
         white-space:pre-wrap; overflow-wrap:anywhere; }
  .msg.user { align-self:flex-end; background:#274156; }
  .msg.assistant { align-self:flex-start; background:var(--panel);
                    border:1px solid var(--line); }
  .msg.note { align-self:flex-start; background:#22261b;
               border:1px solid #4a4a2f; color:#d8d3ae; font-size:13px; }
  .msg .src { display:block; font-size:10px; color:var(--dim);
              text-align:right; margin-top:3px; }
  #composer { display:flex; gap:8px; padding:10px; border-top:1px solid var(--line);
              align-items:flex-end; }
  #composer textarea { flex:1; resize:none; background:var(--panel);
    color:var(--text); border:1px solid var(--line); border-radius:6px;
    padding:8px; font:inherit; min-height:44px; max-height:180px; }
  #composer .sendcol { display:flex; flex-direction:column; gap:6px; }
  #composer button { background:var(--accent); color:#08121b; border:none;
    border-radius:6px; padding:8px 18px; font-weight:600; cursor:pointer; }
  #composer button:disabled { opacity:.5; cursor:default; }
  #composer select { background:var(--panel); color:var(--dim);
    border:1px solid var(--line); border-radius:6px; padding:6px; font:inherit;
    font-size:12px; max-width:220px; }
  #side { width:min(660px, 58vw); border-left:1px solid var(--line);
          overflow-y:auto; padding:12px; background:var(--panel); display:none; }
  #side.open { display:block; }
  .card { border:1px solid var(--line); border-radius:6px; padding:10px;
          margin-bottom:10px; background:var(--panel2); }
  .card h3 { margin:0 0 6px; font-size:13px; color:var(--accent); }
  .card h4 { margin:10px 0 4px; font-size:12px; color:var(--dim);
             text-transform:uppercase; letter-spacing:.04em; }
  .card .small { font-size:12px; color:var(--dim); }
  .card .row { display:flex; gap:6px; align-items:center; flex-wrap:wrap;
               margin-top:6px; }
  .card button { background:#243041; color:var(--text); border:1px solid var(--line);
    border-radius:4px; padding:3px 10px; cursor:pointer; font-size:12px; }
  .card button.ok { border-color:var(--ok); color:var(--ok); }
  .card button.no { border-color:var(--bad); color:var(--bad); }
  .card button:hover { border-color:var(--accent); }
  input, select, textarea { background:#0d1116; color:var(--text);
    border:1px solid var(--line); border-radius:4px; padding:4px 6px;
    font:inherit; font-size:12px; }
  textarea { width:100%; resize:vertical; }
  input[type=checkbox] { width:auto; }
  .field { display:flex; gap:8px; align-items:center; margin:5px 0; }
  .field label { flex:0 0 190px; color:var(--dim); font-size:12px;
                 overflow-wrap:anywhere; }
  .field input[type=text], .field input[type=number] { flex:1; min-width:0; }
  .field textarea { flex:1; min-width:0; font-family:ui-monospace,Consolas,monospace; }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px;
          font-size:11px; border:1px solid var(--line); color:var(--dim); }
  .pill.active, .pill.completed, .pill.succeeded, .pill.approved,
  .pill.executed { color:var(--ok); border-color:var(--ok); }
  .pill.waiting_user, .pill.waiting_approval, .pill.paused,
  .pill.interrupted, .pill.limit_reached { color:var(--warn);
    border-color:var(--warn); }
  .pill.failed, .pill.cancelled, .pill.rejected, .pill.expired,
  .pill.denied, .pill.unknown { color:var(--bad); border-color:var(--bad); }
  .pill.confirm { color:var(--warn); border-color:var(--warn); }
  .pill.low { color:var(--ok); border-color:var(--ok); }
  pre { white-space:pre-wrap; word-break:break-word; font-size:11.5px;
        color:var(--dim); margin:4px 0; }
  code { font-family:ui-monospace,Consolas,monospace; font-size:11.5px; }
  #toast { position:fixed; right:16px; bottom:16px; max-width:440px;
           background:#243041; border:1px solid var(--accent); color:var(--text);
           padding:9px 12px; border-radius:6px; font-size:13px; display:none;
           z-index:20; box-shadow:0 4px 16px #0008; white-space:pre-wrap; }
  .empty { color:var(--dim); font-size:12px; padding:8px; }
  .split { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:8px; }
  .stat { border:1px solid var(--line); border-radius:6px; padding:8px;
          background:#11171d; }
  .stat .n { font-size:20px; font-weight:600; }
  .stat .k { font-size:11px; color:var(--dim); }
  .ev { font-size:11.5px; color:var(--dim); padding:2px 0;
        border-bottom:1px dotted #232a32; overflow-wrap:anywhere; }
  .ev b { color:var(--text); font-weight:500; }
  .ev.warn b { color:var(--warn); }
  .ev.error b { color:var(--bad); }
  .btns { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
</style>
</head>
<body>
<header>
  <span class="title" id="who">Assistant</span>
  <span class="meta" id="statusmeta">connecting...</span>
  <nav id="nav">
    <button data-tab="chat" class="active">Chat</button>
    <button data-tab="tasks">Tasks</button>
    <button data-tab="approvals">Approvals</button>
    <button data-tab="domains">Domains</button>
    <button data-tab="persona">Persona</button>
    <button data-tab="settings">Settings</button>
    <button data-tab="schedules">Schedules</button>
    <button data-tab="memory">Memory</button>
    <button data-tab="system">System</button>
  </nav>
</header>
<div id="banner"></div>
<main>
  <div id="chatwrap">
    <div id="chat"></div>
    <div id="composer">
      <textarea id="input" rows="2"
        placeholder="Message the assistant... (Enter sends, Shift+Enter adds a line)"></textarea>
      <div class="sendcol">
        <select id="domainpick" title="Route this message to a domain">
          <option value="">Auto-route</option>
        </select>
        <button id="send">Send</button>
      </div>
    </div>
  </div>
  <div id="side"></div>
</main>
<div id="toast"></div>
<script>
'use strict';
let state = null;
let tab = 'chat';
let busy = false;
let lastChatKey = '';
let openTaskId = null;
let taskDetail = {};
let memorySearch = null;
let configMeta = null;
let toastTimer = null;

const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  opts = opts || {};
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers = Object.assign({'Content-Type': 'application/json'},
                                 opts.headers || {});
    opts.body = JSON.stringify(opts.body);
  }
  let res;
  try { res = await fetch(path, opts); }
  catch (err) { toast('Network error: ' + err.message); return null; }
  if (res.status === 401) {
    const token = prompt('Access token:');
    if (token) location.href = '/?token=' + encodeURIComponent(token);
    return null;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast('Error: ' + (data.error || res.status)); return null; }
  return data;
}

function toast(text) {
  const node = $('toast');
  node.textContent = text;
  node.style.display = 'block';
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.style.display = 'none'; }, 6000);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function pill(status) {
  const safe = String(status || 'unknown');
  return el('span', 'pill ' + safe, safe.replace(/_/g, ' '));
}

function short(ts) {
  if (!ts) return '-';
  return String(ts).replace('T', ' ').slice(0, 19);
}

function domainName(id) {
  if (!id) return 'General';
  const found = (state.domains || []).find(d => d.id === id);
  return found ? found.name : id;
}

function activeDomains() {
  return (state.domains || []).filter(d => d.status === 'active');
}

function pct(n) { return Number(n).toLocaleString(); }

/* ------------------------------------------------------------------ chat */

function renderHeader() {
  const persona = state.config.persona || {};
  $('who').textContent = persona.speaker_label || persona.name || 'Assistant';
  const open = state.open_tasks || [];
  const current = open[0];
  const counts = state.counts || {};
  const bits = [];
  if (current) {
    bits.push('domain: ' + domainName(current.domain_id));
    bits.push('task: ' + (current.objective || '').slice(0, 56));
  } else {
    bits.push('no open work');
  }
  bits.push(pct(counts.open_tasks || 0) + ' open');
  bits.push(pct(counts.pending_approvals || 0) + ' approvals');
  if (busy) bits.push('working...');
  $('statusmeta').textContent = bits.join('  |  ');

  const banner = $('banner');
  const pending = (state.approvals || []).length;
  if (pending) {
    banner.style.display = 'block';
    banner.textContent = pending + ' action(s) waiting for your decision - ' +
      'open Approvals. Nothing runs until you approve it.';
  } else {
    banner.style.display = 'none';
  }
  renderDomainPicker();
}

function renderDomainPicker() {
  const pick = $('domainpick');
  const wanted = pick.value;
  const options = ['<option value="">Auto-route</option>'].concat(
    activeDomains().map(d =>
      '<option value="' + d.id.replace(/"/g, '&quot;') + '">' +
      (d.name || d.id).replace(/</g, '&lt;') + '</option>')
  );
  const html = options.join('');
  if (pick.dataset.html !== html) {
    pick.innerHTML = html;
    pick.dataset.html = html;
    pick.value = wanted || '';
  }
}

function renderChat() {
  const msgs = state.messages || [];
  const key = msgs.length + ':' + (msgs.length ? msgs[msgs.length - 1].id : '');
  if (key === lastChatKey) return;
  const chat = $('chat');
  const atBottom = !lastChatKey ||
    (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 90);
  chat.innerHTML = '';
  for (const m of msgs) {
    const cls = m.role === 'user' ? 'user' :
      (m.role === 'note' ? 'note' : 'assistant');
    const node = el('div', 'msg ' + cls, m.content);
    const meta = (m.source || '') + (m.task_id ? '  ' + m.task_id : '');
    node.appendChild(el('span', 'src', meta + '  ' + short(m.ts)));
    chat.appendChild(node);
  }
  lastChatKey = key;
  if (atBottom) chat.scrollTop = chat.scrollHeight;
}

/* ------------------------------------------------------------- side panel */

function renderSide(force) {
  const side = $('side');
  if (!state) return;
  if (tab === 'chat') { side.classList.remove('open'); return; }
  side.classList.add('open');
  const active = document.activeElement;
  const editing = active && side.contains(active) &&
    ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName);
  if (!force && editing) return;
  const scroll = side.scrollTop;
  side.innerHTML = '';
  const builders = {
    tasks: renderTasks, approvals: renderApprovals, domains: renderDomains,
    persona: renderPersona, settings: renderSettings,
    schedules: renderSchedules, memory: renderMemory, system: renderSystem,
  };
  (builders[tab] || (() => {}))(side);
  side.scrollTop = scroll;
}

function empty(side, text) {
  side.appendChild(el('div', 'card empty', text));
}

/* ------------------------------------------------------------------ tasks */

function renderTasks(side) {
  const tasks = state.tasks || [];
  const counts = state.counts || {};
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Work'));
  const grid = el('div', 'split');
  for (const [k, v] of [['open', counts.open_tasks || 0],
                        ['paused', counts.paused || 0],
                        ['pending approvals', counts.pending_approvals || 0]]) {
    const stat = el('div', 'stat');
    stat.appendChild(el('div', 'n', pct(v)));
    stat.appendChild(el('div', 'k', k));
    grid.appendChild(stat);
  }
  head.appendChild(grid);
  side.appendChild(head);

  if (!tasks.length) { empty(side, 'No tasks yet.'); return; }
  for (const t of tasks) {
    const card = el('div', 'card');
    const title = el('h3', null, (t.objective || '(no objective)').slice(0, 110));
    card.appendChild(title);
    const row = el('div', 'row');
    row.appendChild(pill(t.status));
    row.appendChild(el('span', 'small', domainName(t.domain_id)));
    row.appendChild(el('span', 'small', 'origin ' + (t.origin || '?')));
    if (t.retry_count) {
      row.appendChild(el('span', 'small',
        'retries ' + t.retry_count +
        (t.next_retry_at ? ' | next ' + short(t.next_retry_at) : '')));
    }
    card.appendChild(row);
    if (t.status_reason) card.appendChild(el('div', 'small', t.status_reason));
    card.appendChild(el('div', 'small',
      'updated ' + short(t.updated_at) + '  |  ' + t.id));

    const btns = el('div', 'btns');
    const detail = el('button', null,
      openTaskId === t.id ? 'Hide details' : 'Details');
    detail.onclick = () => {
      if (openTaskId === t.id) { openTaskId = null; renderSide(true); return; }
      openTaskId = t.id;
      api('/api/tasks/' + encodeURIComponent(t.id)).then(data => {
        if (data) { taskDetail[t.id] = data; renderSide(true); }
      });
    };
    btns.appendChild(detail);

    if (['active', 'waiting_user', 'waiting_approval', 'paused'].includes(t.status)) {
      const cancel = el('button', 'no', 'Cancel task');
      cancel.onclick = async () => {
        if (!confirm('Cancel this task and its pending approvals?')) return;
        const out = await api('/api/tasks/' + encodeURIComponent(t.id) + '/cancel',
                              {method: 'POST'});
        if (out) { toast('Task cancelled.'); refresh(true); }
      };
      btns.appendChild(cancel);
    }
    if (t.status === 'paused' || t.status === 'failed') {
      const resume = el('button', 'ok', 'Resume now');
      resume.onclick = async () => {
        resume.disabled = true;
        const out = await api('/api/tasks/' + encodeURIComponent(t.id) + '/resume',
                              {method: 'POST'});
        if (out) toast('Resume: ' + out.status);
        refresh(true);
      };
      btns.appendChild(resume);
    }
    card.appendChild(btns);

    const domains = activeDomains();
    if (domains.length) {
      const pick = el('select');
      const auto = el('option', null, 'Move to domain...');
      auto.value = '';
      pick.appendChild(auto);
      for (const d of domains) {
        const o = el('option', null, d.name);
        o.value = d.id;
        if (d.id === t.domain_id) o.selected = true;
        pick.appendChild(o);
      }
      pick.onchange = async () => {
        if (!pick.value) return;
        const out = await api('/api/tasks/' + encodeURIComponent(t.id) + '/domain',
          {method: 'POST', body: {domain_id: pick.value}});
        if (out) toast('Task moved to ' + domainName(pick.value));
        refresh(true);
      };
      card.appendChild(pick);
    }

    if (openTaskId === t.id && taskDetail[t.id]) {
      card.appendChild(renderTaskDetail(taskDetail[t.id]));
    }
    side.appendChild(card);
  }
}

function renderTaskDetail(detail) {
  const box = el('div');
  box.appendChild(el('h4', null, 'Runs'));
  if (!(detail.runs || []).length) {
    box.appendChild(el('div', 'small', 'No runs recorded.'));
  }
  for (const r of detail.runs || []) {
    box.appendChild(el('div', 'small',
      short(r.started_at) + '  ' + r.status + '  iterations ' + r.iterations +
      '  tools ' + r.tool_calls + (r.error ? '  ' + r.error : '')));
  }
  box.appendChild(el('h4', null, 'Tool executions'));
  if (!(detail.executions || []).length) {
    box.appendChild(el('div', 'small', 'No tool executions.'));
  }
  for (const x of (detail.executions || []).slice(-12)) {
    box.appendChild(el('div', 'small',
      x.tool + '  ' + x.status + '  ' + short(x.started_at) +
      (x.error ? '  ' + x.error : '')));
  }
  if ((detail.approvals || []).length) {
    box.appendChild(el('h4', null, 'Pending approvals'));
    for (const a of detail.approvals) {
      box.appendChild(el('div', 'small',
        a.tool + '  ' + JSON.stringify(a.arguments)));
    }
  }
  const sources = detail.context_sources || [];
  box.appendChild(el('h4', null, 'Context sources (' + sources.length + ')'));
  for (const s of sources.slice(0, 12)) {
    box.appendChild(el('div', 'small',
      (s.section || '?') + ': ' + (s.reason || 'included')));
  }
  return box;
}

/* -------------------------------------------------------------- approvals */

function renderApprovals(side) {
  const pending = state.approvals || [];
  const head = el('div', 'card');
  head.appendChild(el('h3', null,
    pending.length ? pending.length + ' action(s) waiting' : 'Nothing waiting'));
  head.appendChild(el('div', 'small',
    'Approving authorizes exactly the action shown. Nothing executes until ' +
    'the runtime performs it, and a stale approval cannot run.'));
  side.appendChild(head);
  if (!pending.length) return;

  for (const a of pending) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, a.summary || a.tool));
    const owner = (state.tasks || []).find(t => t.id === a.task_id);
    card.appendChild(el('div', 'small',
      'task ' + (a.task_id || '?') + '  |  domain ' +
      domainName(owner ? owner.domain_id : null) + '  |  ' + a.id));
    if (a.reason) card.appendChild(el('div', 'small', a.reason));
    const pre = el('pre', null, JSON.stringify(a.arguments || {}, null, 1));
    card.appendChild(pre);
    card.appendChild(el('div', 'small',
      'requested ' + short(a.created_at) +
      (a.expires_at ? '  |  expires ' + short(a.expires_at) : '')));
    const btns = el('div', 'btns');
    const ok = el('button', 'ok', 'Approve');
    ok.onclick = () => decide(a.id, 'approve');
    const no = el('button', 'no', 'Reject');
    no.onclick = () => decide(a.id, 'reject');
    const cancel = el('button', null, 'Cancel');
    cancel.onclick = () => decide(a.id, 'cancel');
    btns.appendChild(ok); btns.appendChild(no); btns.appendChild(cancel);
    card.appendChild(btns);
    side.appendChild(card);
  }
}

async function decide(id, decision) {
  const out = await api('/api/approvals/' + encodeURIComponent(id) + '/' + decision,
                        {method: 'POST'});
  if (out) toast('Approval ' + decision + ': ' + out.status +
    (out.message ? '\n' + out.message : ''));
  refresh(true);
}

/* ---------------------------------------------------------------- domains */

function renderDomains(side) {
  const domains = state.domains || [];
  const counts = {};
  for (const t of state.tasks || []) {
    counts[t.domain_id] = (counts[t.domain_id] || 0) + 1;
  }

  const form = el('div', 'card');
  form.appendChild(el('h3', null, 'New domain'));
  const name = el('input');
  name.placeholder = 'Name (e.g. Homelab)';
  const desc = el('textarea');
  desc.rows = 2;
  desc.placeholder = 'What belongs here (routing evidence, not prose)';
  const add = el('button', 'ok', 'Create domain');
  add.onclick = async () => {
    if (!name.value.trim()) { toast('A name is required.'); return; }
    const out = await api('/api/domains', {method: 'POST', body: {
      name: name.value.trim(), description: desc.value.trim(),
    }});
    if (out) { toast('Domain created.'); refresh(true); }
  };
  form.appendChild(name); form.appendChild(desc);
  form.appendChild(el('div', 'btns')).appendChild(add);
  side.appendChild(form);

  for (const group of [['Active', 'active'], ['Archived', 'archived']]) {
    const list = domains.filter(d => d.status === group[1]);
    if (!list.length) continue;
    side.appendChild(el('h4', null, group[0] + ' domains'));
    for (const d of list) {
      const card = el('div', 'card');
      card.appendChild(el('h3', null, d.name));
      card.appendChild(el('div', 'small',
        (counts[d.id] || 0) + ' task(s)  |  created ' + short(d.created_at) +
        '  |  ' + d.id));
      const nameInput = el('input');
      nameInput.value = d.name;
      const descInput = el('textarea');
      descInput.rows = 2;
      descInput.value = d.description || '';
      card.appendChild(el('div', 'small', 'Name'));
      card.appendChild(nameInput);
      card.appendChild(el('div', 'small', 'Description'));
      card.appendChild(descInput);
      const btns = el('div', 'btns');
      const save = el('button', 'ok', 'Save');
      save.onclick = async () => {
        const out = await api('/api/domains/' + encodeURIComponent(d.id),
          {method: 'PATCH', body: {
            name: nameInput.value.trim(),
            description: descInput.value,
          }});
        if (out) { toast('Domain saved.'); refresh(true); }
      };
      btns.appendChild(save);
      const flip = el('button', group[1] === 'active' ? 'no' : 'ok',
        group[1] === 'active' ? 'Archive' : 'Restore');
      flip.onclick = async () => {
        const status = group[1] === 'active' ? 'archived' : 'active';
        if (status === 'archived' &&
            !confirm('Archive this domain? Existing tasks keep their domain; ' +
                     'new work stops routing here.')) return;
        const out = await api('/api/domains/' + encodeURIComponent(d.id),
          {method: 'PATCH', body: {status}});
        if (out) { toast('Domain ' + status + '.'); refresh(true); }
      };
      btns.appendChild(flip);
      card.appendChild(btns);
      side.appendChild(card);
    }
  }
  if (!domains.length) empty(side, 'No domains yet. General work stays in General.');
}

/* ---------------------------------------------------------------- persona */

function renderPersona(side) {
  const persona = state.config.persona || {};
  const card = el('div', 'card');
  card.appendChild(el('h3', null, 'Persona'));
  card.appendChild(el('div', 'small',
    'This is the standing instruction block sent with every run. Keep it ' +
    'short: principles are one per line.'));

  const fields = {};
  const add = (key, label, kind, rows) => {
    const row = el('div', 'field');
    row.appendChild(el('label', null, label));
    const input = kind === 'area' ? el('textarea') : el('input');
    if (kind === 'area') input.rows = rows || 3;
    input.value = persona[key] == null ? '' : String(persona[key]);
    if (key === 'principles') {
      input.value = Array.isArray(persona.principles)
        ? persona.principles.join('\n') : (persona.principles || '');
    }
    fields[key] = {node: input, kind};
    row.appendChild(input);
    card.appendChild(row);
  };
  add('name', 'Name', 'text');
  add('speaker_label', 'Speaker label', 'text');
  add('user_label', 'User label', 'text');
  add('purpose', 'Purpose', 'area', 4);
  add('principles', 'Principles (one per line)', 'area', 5);
  add('style', 'Style', 'area', 2);
  add('response_preferences', 'Response preferences', 'area', 2);

  const btns = el('div', 'btns');
  const save = el('button', 'ok', 'Save persona');
  save.onclick = async () => {
    const value = {};
    for (const key of Object.keys(fields)) {
      const f = fields[key];
      if (key === 'principles') {
        value[key] = f.node.value.split('\n').map(s => s.trim())
          .filter(Boolean);
      } else {
        value[key] = f.node.value;
      }
    }
    const out = await api('/api/config',
      {method: 'POST', body: {group: 'persona', value}});
    if (out) { toast('Persona saved.'); refresh(true); }
  };
  btns.appendChild(save);
  card.appendChild(btns);
  side.appendChild(card);
}

/* --------------------------------------------------------------- settings */

const SETTING_GROUPS = ['limits', 'context', 'domains', 'memory', 'tools',
                        'scheduler', 'consolidation', 'approvals'];

function renderSettings(side) {
  if (!configMeta) {
    api('/api/config').then(data => {
      if (data) { configMeta = data; renderSide(true); }
    });
  }
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Settings'));
  head.appendChild(el('div', 'small',
    'Editable operational groups. Values are validated against the schema ' +
    'before being written to config.yaml; unknown keys are rejected.'));
  if (configMeta && configMeta.config_path) {
    head.appendChild(el('div', 'small', 'File: ' + configMeta.config_path));
  }
  side.appendChild(head);

  for (const group of SETTING_GROUPS) {
    const values = state.config[group];
    if (!values || typeof values !== 'object' || Array.isArray(values)) continue;
    side.appendChild(settingsCard(group, values));
  }
  side.appendChild(timezoneCard());
}

function settingsCard(group, values) {
  const card = el('div', 'card');
  card.appendChild(el('h3', null, group));
  const fields = [];
  for (const key of Object.keys(values)) {
    const value = values[key];
    const row = el('div', 'field');
    row.appendChild(el('label', null, key));
    let node;
    let kind;
    if (typeof value === 'boolean') {
      node = el('input');
      node.type = 'checkbox';
      node.checked = value;
      kind = 'bool';
    } else if (typeof value === 'number') {
      node = el('input');
      node.type = 'number';
      node.step = 'any';
      node.value = String(value);
      kind = 'number';
    } else if (typeof value === 'string') {
      node = el('input');
      node.type = 'text';
      node.value = value;
      kind = 'string';
    } else {
      node = el('textarea');
      node.rows = 3;
      node.value = JSON.stringify(value, null, 1);
      kind = 'json';
    }
    fields.push({key, node, kind});
    row.appendChild(node);
    card.appendChild(row);
  }
  const btns = el('div', 'btns');
  const save = el('button', 'ok', 'Save ' + group);
  save.onclick = async () => {
    const value = {};
    for (const f of fields) {
      if (f.kind === 'bool') value[f.key] = f.node.checked;
      else if (f.kind === 'number') {
        const num = Number(f.node.value);
        if (f.node.value === '' || Number.isNaN(num)) {
          toast(group + '.' + f.key + ' must be a number.'); return;
        }
        value[f.key] = num;
      } else if (f.kind === 'json') {
        try { value[f.key] = JSON.parse(f.node.value); }
        catch (err) {
          toast(group + '.' + f.key + ' is not valid JSON: ' + err.message);
          return;
        }
      } else value[f.key] = f.node.value;
    }
    const out = await api('/api/config',
      {method: 'POST', body: {group, value}});
    if (out) { toast(group + ' saved.'); refresh(true); }
  };
  btns.appendChild(save);
  card.appendChild(btns);
  return card;
}

function timezoneCard() {
  const card = el('div', 'card');
  card.appendChild(el('h3', null, 'timezone'));
  const row = el('div', 'field');
  row.appendChild(el('label', null, 'IANA timezone'));
  const input = el('input');
  input.type = 'text';
  input.value = state.config.timezone || '';
  row.appendChild(input);
  card.appendChild(row);
  const btns = el('div', 'btns');
  const save = el('button', 'ok', 'Save timezone');
  save.onclick = async () => {
    const out = await api('/api/config',
      {method: 'POST', body: {group: 'timezone', value: input.value}});
    if (out) { toast('Timezone saved.'); refresh(true); }
  };
  btns.appendChild(save);
  card.appendChild(btns);
  return card;
}

/* -------------------------------------------------------------- schedules */

function renderSchedules(side) {
  const form = el('div', 'card');
  form.appendChild(el('h3', null, 'New schedule'));
  const name = el('input'); name.placeholder = 'name';
  const prompt_ = el('textarea'); prompt_.rows = 2;
  prompt_.placeholder = 'prompt submitted to the normal runtime';
  const kind = el('select');
  for (const k of ['daily', 'interval', 'weekly', 'once']) {
    const o = el('option', null, k); o.value = k; kind.appendChild(o);
  }
  const time = el('input'); time.placeholder = 'HH:MM';
  const interval = el('input'); interval.placeholder = 'seconds';
  interval.type = 'number';
  const weekday = el('input'); weekday.placeholder = 'monday';
  const at = el('input'); at.placeholder = 'ISO time';
  const tz = el('input'); tz.value = state.config.timezone || '';
  tz.placeholder = 'IANA timezone';
  const domain = el('select');
  const auto = el('option', null, 'Auto-route'); auto.value = '';
  domain.appendChild(auto);
  for (const d of activeDomains()) {
    const o = el('option', null, d.name); o.value = d.id; domain.appendChild(o);
  }

  const rows = [
    ['name', name], ['kind', kind], ['time of day', time],
    ['interval s', interval], ['weekday', weekday], ['at', at],
    ['timezone', tz], ['domain', domain],
  ];
  for (const [label, node] of rows) {
    const row = el('div', 'field');
    row.appendChild(el('label', null, label));
    row.appendChild(node);
    form.appendChild(row);
  }
  form.appendChild(el('div', 'small', 'Prompt'));
  form.appendChild(prompt_);
  const btns = el('div', 'btns');
  const add = el('button', 'ok', 'Add schedule');
  add.onclick = async () => {
    const body = {
      name: name.value.trim(), prompt: prompt_.value.trim(), kind: kind.value,
      tz: tz.value.trim() || null,
      time_of_day: time.value.trim() || null,
      interval_s: interval.value ? Number(interval.value) : null,
      weekday: weekday.value.trim() || null,
      at_ts: at.value.trim() || null,
      domain_id: domain.value || null,
    };
    if (!body.name || !body.prompt) {
      toast('A schedule needs a name and a prompt.'); return;
    }
    const out = await api('/api/schedules', {method: 'POST', body});
    if (out) { toast('Schedule added.'); refresh(true); }
  };
  btns.appendChild(add);
  form.appendChild(btns);
  side.appendChild(form);

  const list = state.schedules || [];
  if (!list.length) { empty(side, 'No schedules.'); return; }
  for (const s of list) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, s.name + ' [' + s.kind + ']'));
    card.appendChild(el('div', 'small', s.prompt));
    card.appendChild(el('div', 'small',
      (s.enabled ? 'enabled' : 'disabled') + '  |  next ' +
      short(s.next_due_at) + '  |  tz ' + s.tz +
      (s.domain_id ? '  |  ' + domainName(s.domain_id) : '')));
    const btns = el('div', 'btns');
    const toggle = el('button', null, s.enabled ? 'Disable' : 'Enable');
    toggle.onclick = async () => {
      await api('/api/schedules/' + encodeURIComponent(s.id) + '/' +
        (s.enabled ? 'disable' : 'enable'), {method: 'POST'});
      refresh(true);
    };
    const del = el('button', 'no', 'Delete');
    del.onclick = async () => {
      if (!confirm('Delete this schedule?')) return;
      await api('/api/schedules/' + encodeURIComponent(s.id) + '/delete',
                {method: 'POST'});
      refresh(true);
    };
    btns.appendChild(toggle); btns.appendChild(del);
    card.appendChild(btns);
    side.appendChild(card);
  }
}

/* ----------------------------------------------------------------- memory */

function renderMemory(side) {
  const card0 = el('div', 'card');
  card0.appendChild(el('h3', null, 'Memory'));
  const btns = el('div', 'btns');
  const run = el('button', null, 'Run consolidation now');
  run.onclick = async () => {
    run.disabled = true;
    const out = await api('/api/consolidate', {method: 'POST'});
    if (out) toast('Consolidation ' + out.status + ': applied ' +
      (out.applied || []).length + ', rejected ' +
      (out.rejected || []).length);
    refresh(true);
  };
  btns.appendChild(run);
  card0.appendChild(btns);

  const searchRow = el('div', 'row');
  const query = el('input');
  query.placeholder = 'Search memories and history';
  query.style.flex = '1';
  const go = el('button', null, 'Search');
  go.onclick = async () => {
    if (!query.value.trim()) { memorySearch = null; renderSide(true); return; }
    const out = await api('/api/memories/search',
      {method: 'POST', body: {query: query.value.trim()}});
    if (out) { memorySearch = out; renderSide(true); }
  };
  query.onkeydown = (e) => { if (e.key === 'Enter') go.onclick(); };
  searchRow.appendChild(query); searchRow.appendChild(go);
  card0.appendChild(searchRow);
  side.appendChild(card0);

  if (memorySearch) {
    const results = memorySearch.results || [];
    const history = memorySearch.history || [];
    const card = el('div', 'card');
    card.appendChild(el('h3', null,
      'Search: ' + results.length + ' memor' + (results.length === 1 ? 'y' : 'ies') +
      ', ' + history.length + ' history hit(s)'));
    for (const m of results) {
      card.appendChild(el('div', 'small',
        '[' + m.kind + '/' + m.scope + '] ' + m.content));
      card.appendChild(el('div', 'small',
        m.id + ' rev ' + m.revision + ' | ' + short(m.updated_at)));
    }
    for (const h of history) {
      card.appendChild(el('div', 'small',
        'history ' + short(h.ts) + ' ' + (h.role || '') + ': ' +
        String(h.content || '').slice(0, 240)));
    }
    side.appendChild(card);
  }

  const list = state.memories || [];
  if (!list.length) { empty(side, 'No active memories.'); return; }
  for (const m of list) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, m.kind + ' | ' + m.scope));
    card.appendChild(el('div', 'small', m.content));
    card.appendChild(el('div', 'small',
      m.id + ' rev ' + m.revision + ' | ' + short(m.updated_at) +
      (m.expires_at ? ' | expires ' + short(m.expires_at) : '')));
    side.appendChild(card);
  }
}

/* ----------------------------------------------------------------- system */

function renderSystem(side) {
  const counts = state.counts || {};
  const sched = state.scheduler || {};
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Status'));
  const grid = el('div', 'split');
  for (const [k, v] of [
    ['open tasks', counts.open_tasks || 0],
    ['paused', counts.paused || 0],
    ['pending approvals', counts.pending_approvals || 0],
    ['domains', counts.domains || 0],
  ]) {
    const stat = el('div', 'stat');
    stat.appendChild(el('div', 'n', pct(v)));
    stat.appendChild(el('div', 'k', k));
    grid.appendChild(stat);
  }
  head.appendChild(grid);
  head.appendChild(el('div', 'small',
    'Scheduler ' + (sched.enabled ? 'enabled' : 'disabled') +
    ' | last tick ' + short(sched.last_tick)));
  if (sched.last_report) {
    head.appendChild(el('pre', null, JSON.stringify(sched.last_report, null, 1)));
  }
  side.appendChild(head);

  const actions = el('div', 'card');
  actions.appendChild(el('h3', null, 'Maintenance'));
  const btns = el('div', 'btns');
  const heartbeat = el('button', null, 'Run heartbeat now');
  heartbeat.onclick = async () => {
    heartbeat.disabled = true;
    const out = await api('/api/heartbeat', {method: 'POST'});
    if (out && out.report) {
      const r = out.report;
      toast('Heartbeat: ' + (r.submitted || []).length + ' submitted, ' +
        (r.resumed || []).length + ' resumed, ' +
        (r.retried || []).length + ' retried, ' +
        (r.deferred || 0) + ' deferred.');
    }
    refresh(true);
  };
  const backup = el('button', null, 'Create backup');
  backup.onclick = async () => {
    const out = await api('/api/backup', {method: 'POST'});
    if (out) toast('Backup written: ' + out.backup);
  };
  btns.appendChild(heartbeat); btns.appendChild(backup);
  actions.appendChild(btns);
  side.appendChild(actions);

  const ep = el('div', 'card');
  ep.appendChild(el('h3', null, 'Model endpoint'));
  ep.appendChild(el('div', 'small',
    (state.config.model && state.config.model.base_url) || ''));
  const probe = el('button', null, 'Probe endpoint');
  const out = el('pre');
  probe.onclick = async () => {
    out.textContent = 'probing...';
    const data = await api('/api/endpoint-probe');
    if (data) out.textContent = JSON.stringify(data, null, 1);
  };
  ep.appendChild(probe); ep.appendChild(out);
  side.appendChild(ep);

  const tools = el('div', 'card');
  tools.appendChild(el('h3', null, 'Tools (' + (state.tools || []).length + ')'));
  for (const t of state.tools || []) {
    const row = el('div', 'row');
    row.appendChild(pill(t.risk));
    row.appendChild(el('code', null, t.name));
    tools.appendChild(row);
    if (t.description) tools.appendChild(el('div', 'small', t.description));
  }
  side.appendChild(tools);

  const events = el('div', 'card');
  events.appendChild(el('h3', null, 'Recent events'));
  const evs = (state.events || []).slice(-80).reverse();
  if (!evs.length) events.appendChild(el('div', 'small', 'No events.'));
  for (const e of evs) {
    const line = el('div', 'ev ' + (e.level || 'info'));
    line.appendChild(el('b', null, e.type));
    const detail = e.data && Object.keys(e.data).length
      ? '  ' + JSON.stringify(e.data).slice(0, 220) : '';
    line.appendChild(document.createTextNode('  ' + short(e.ts) + detail));
    events.appendChild(line);
  }
  side.appendChild(events);
}

/* ------------------------------------------------------------------ input */

async function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text || busy) return;
  busy = true;
  $('send').disabled = true;
  input.value = '';
  renderHeader();
  try {
    const body = {text, source: 'web'};
    const domain = $('domainpick').value;
    if (domain) body.domain_id = domain;
    const out = await api('/api/message', {method: 'POST', body});
    if (out && out.status && out.status !== 'completed') {
      toast('[' + out.status + ']' + (out.message ? '\n' + out.message : ''));
    }
  } finally {
    busy = false;
    $('send').disabled = false;
    $('domainpick').value = '';
    refresh(true);
  }
}

async function refresh(forceSide) {
  const data = await api('/api/state');
  if (!data) return;
  state = data;
  renderHeader();
  renderChat();
  renderSide(!!forceSide);
}

$('send').onclick = send;
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('banner').onclick = () => {
  tab = 'approvals';
  document.querySelectorAll('#nav button').forEach(x =>
    x.classList.toggle('active', x.dataset.tab === tab));
  renderSide(true);
};
document.querySelectorAll('#nav button').forEach((b) => {
  b.onclick = () => {
    tab = b.dataset.tab;
    document.querySelectorAll('#nav button').forEach(x =>
      x.classList.toggle('active', x.dataset.tab === tab));
    $('side').scrollTop = 0;
    renderSide(true);
  };
});
refresh(true);
setInterval(() => { if (!busy) refresh(false); }, 3000);
</script>
</body>
</html>
"""
