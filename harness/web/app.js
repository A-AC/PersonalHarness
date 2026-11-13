'use strict';
/* Zero-build UI. Every view is rendered from API responses; the browser
 * keeps no authoritative state, only the last payload it was shown. */

let state = {
  summary: {}, counts: {}, config: { persona: {} }, domains: [],
  messages: [], tasks: [], open_tasks: [], approvals: [], schedules: [],
  memories_all: [], memory_counts: {}, artifact_problems: [],
  skills: [], modules: [], schema_budget: null, tools: [], events: [],
  inbox_events: [], automations: [], automation_fires: [],
  consolidations: [], backups: [], system: null,
  now: null, system_timezone: 'local', scheduler: {},
};
let tab = 'chat';
let busy = false;
let lastChatKey = '';
let openTaskId = null;
let taskDetail = {};
let memorySearch = null;
let memoryFilter = 'active';
let eventLevel = 'all';
let openSkillId = null;
let skillDetails = {};
let skillPrefill = null;
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
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) {
    return String(ts).replace('T', ' ').slice(0, 19);
  }
  const pad = (n) => String(n).padStart(2, '0');
  // Stored timestamps are UTC; show them in this computer's local time.
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
    ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' +
    pad(d.getSeconds());
}

function domainName(id) {
  if (!id) return 'General';
  const found = (state.domains || []).find(d => d.id === id);
  return found ? found.name : id;
}

function activeDomains() {
  return (state.domains || []).filter(d => d.status === 'active');
}

function pct(n) { return Number(n || 0).toLocaleString(); }

function statGrid(pairs) {
  const grid = el('div', 'split');
  for (const [k, v] of pairs) {
    const stat = el('div', 'stat');
    stat.appendChild(el('div', 'n', pct(v)));
    stat.appendChild(el('div', 'k', k));
    grid.appendChild(stat);
  }
  return grid;
}

function btn(label, cls, onclick) {
  const b = el('button', cls || null, label);
  b.onclick = onclick;
  return b;
}

function field(labelText, node) {
  const row = el('div', 'field');
  row.appendChild(el('label', null, labelText));
  row.appendChild(node);
  return row;
}

/* ---------------------------------------------------------- panel loading */

function applyConfig(data) {
  state.config = data.config || state.config;
  configMeta = data;
}

const PANELS = {
  tasks: {
    url: () => '/api/tasks',
    apply: (d) => {
      state.tasks = d.tasks || [];
      state.open_tasks = d.open_tasks || [];
      state.counts = Object.assign({}, state.counts, d.counts || {});
    },
  },
  approvals: {
    url: () => '/api/approvals',
    apply: (d) => {
      state.approvals = d.approvals || [];
      state.counts = Object.assign({}, state.counts, d.counts || {});
    },
  },
  schedules: {
    url: () => '/api/schedules',
    apply: (d) => { state.schedules = d.schedules || []; },
  },
  inbox: {
    url: () => '/api/inbox',
    apply: (d) => {
      state.inbox_events = d.events || [];
      state.automations = d.automations || [];
      state.automation_fires = d.fires || [];
    },
  },
  automations: {
    url: () => '/api/automations',
    apply: (d) => {
      state.automations = d.automations || [];
      state.automation_fires = d.fires || [];
    },
  },
  memory: {
    url: () => '/api/memories?status=any&limit=300',
    apply: (d) => {
      state.memories_all = d.memories || [];
      state.memory_counts = d.counts || {};
      state.artifact_problems = d.artifact_problems || [];
    },
  },
  domains: {
    url: () => '/api/domains',
    apply: (d) => { state.domains = d.domains || []; },
  },
  skills: {
    url: () => '/api/skills',
    apply: (d) => { state.skills = d.skills || []; },
  },
  consolidation: {
    url: () => '/api/consolidations',
    apply: (d) => {
      state.consolidations = d.consolidations || [];
      state.consolidation_schedule = d.schedule_interval_s;
    },
  },
  modules: {
    url: () => '/api/modules',
    apply: (d) => {
      state.modules = d.modules || [];
      state.schema_budget = d.schema_budget || null;
      state.schema_budget_tokens = d.schema_budget_tokens;
    },
  },
  tools: {
    url: () => '/api/tools',
    apply: (d) => {
      state.tools = d.tools || [];
      state.mcp_status = d.mcp_status || {};
    },
  },
  events: {
    url: () => '/api/events?limit=200',
    apply: (d) => { state.events = d.events || []; },
  },
  persona: { url: () => '/api/config', apply: applyConfig },
  settings: { url: () => '/api/config', apply: applyConfig },
  system: {
    url: () => '/api/system',
    apply: (d) => {
      state.system = d;
      state.counts = Object.assign({}, state.counts, d.counts || {});
      state.scheduler = d.scheduler || state.scheduler;
      state.domains = d.domains || state.domains;
    },
  },
  backup: {
    url: () => '/api/backups',
    apply: (d) => { state.backups = d.backups || []; },
  },
  guard: { url: null },
  chat: { url: null },
};

function panelMode() { return tab !== 'chat'; }

async function refresh(force) {
  const panel = PANELS[tab];
  const jobs = [api('/api/summary'), api('/api/messages?limit=300')];
  jobs.push(panel && panel.url ? api(panel.url()) : Promise.resolve(null));
  const [summary, messages, data] = await Promise.all(jobs);
  if (summary) {
    state.summary = summary;
    state.counts = Object.assign({}, state.counts, summary.counts || {});
    state.domains = summary.domains || state.domains;
    state.scheduler = summary.scheduler || state.scheduler;
    state.now = summary.now;
    state.system_timezone = summary.system_timezone || 'local';
    if (!state.config) state.config = {};
    state.config.persona = summary.persona || state.config.persona || {};
  }
  if (messages) state.messages = messages.messages || [];
  if (data && panel && panel.apply) panel.apply(data);
  renderHeader();
  renderChat();
  renderWorking();
  renderSide(!!force);
}

function renderAll(force) {
  renderHeader();
  renderChat();
  renderWorking();
  renderSide(!!force);
}

/* ------------------------------------------------------------------ chat */

function renderHeader() {
  const persona = (state.config && state.config.persona) || {};
  $('who').textContent = persona.speaker_label || persona.name || 'Assistant';
  const counts = state.counts || {};
  const working = (state.summary || {}).working_task;
  const bits = [];
  if (working) {
    bits.push('domain: ' + domainName(working.domain_id));
    bits.push('task: ' + (working.objective || '').slice(0, 56));
    bits.push('[' + (working.status || '?') + ']');
  } else {
    bits.push('no open work');
  }
  bits.push(pct(counts.open_tasks) + ' open');
  bits.push(pct(counts.pending_approvals) + ' approvals');
  bits.push(pct(counts.inbox_new) + ' inbox');
  if (busy) bits.push('working...');
  $('statusmeta').textContent = bits.join('  |  ');

  const banner = $('banner');
  const pending = (state.counts || {}).pending_approvals || 0;
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
    const src = el('span', 'src');
    if (m.task_id) {
      src.appendChild(btn('task ' + String(m.task_id).slice(-10), 'tasklink',
        () => openTask(m.task_id)));
    }
    src.appendChild(document.createTextNode(
      (m.source || '') + '  ' + short(m.ts)));
    node.appendChild(src);
    chat.appendChild(node);
  }
  lastChatKey = key;
  if (atBottom) chat.scrollTop = chat.scrollHeight;
}

function renderWorking() {
  const strip = $('working');
  const working = (state.summary || {}).working_task;
  if (!working) { strip.hidden = true; return; }
  strip.hidden = false;
  const text = $('workingtext');
  text.innerHTML = '';
  text.appendChild(pill(working.status));
  text.appendChild(document.createTextNode(
    '  ' + (working.objective || '') +
    (working.run_status ? '  |  run ' + working.run_status : '') +
    (working.decision ? '  |  ' + working.decision : '')));
  $('workingdetails').onclick = () => openTask(working.id);
}

/* ------------------------------------------------------------- side panel */

function renderSide(force) {
  const side = $('side');
  const main = document.querySelector('main');
  if (!state) return;
  main.classList.toggle('panelmode', panelMode());
  if (!panelMode()) {
    side.classList.remove('open');
    return;
  }
  side.classList.add('open');
  const active = document.activeElement;
  const editing = active && side.contains(active) &&
    ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName);
  if (!force && editing) return;
  const scroll = side.scrollTop;
  side.innerHTML = '';
  const builders = {
    tasks: renderTasks, approvals: renderApprovals, schedules: renderSchedules,
    inbox: renderInbox, automations: renderAutomations, memory: renderMemory,
    domains: renderDomains, skills: renderSkills,
    consolidation: renderConsolidation, modules: renderModules,
    tools: renderTools, events: renderEvents, persona: renderPersona,
    settings: renderSettings, system: renderSystem, backup: renderBackup,
    guard: renderGuard,
  };
  (builders[tab] || (() => empty(side, 'Unknown panel')))(side);
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
  head.appendChild(statGrid([
    ['open', counts.open_tasks || 0],
    ['paused', counts.paused || 0],
    ['pending approvals', counts.pending_approvals || 0],
  ]));
  head.appendChild(el('div', 'small',
    'Every task keeps its own runs, transcript, tool calls, context sources, ' +
    'and sub-agent tree. Details opens the full record.'));
  side.appendChild(head);

  if (!tasks.length) { empty(side, 'No tasks yet.'); return; }
  for (const t of tasks) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null,
      (t.objective || '(no objective)').slice(0, 110)));
    const row = el('div', 'row');
    row.appendChild(pill(t.status));
    row.appendChild(el('span', 'small', domainName(t.domain_id)));
    row.appendChild(el('span', 'small', 'origin ' + (t.origin || '?')));
    row.appendChild(el('span', 'small', 'updated ' + short(t.updated_at)));
    if (t.retry_count) {
      row.appendChild(el('span', 'small',
        'retries ' + t.retry_count +
        (t.next_retry_at ? ' | next ' + short(t.next_retry_at) : '')));
    }
    card.appendChild(row);
    if (t.status_reason) card.appendChild(el('div', 'small', t.status_reason));
    card.appendChild(el('div', 'small',
      'created ' + short(t.created_at) + '  |  id ' + t.id));

    const btns = el('div', 'btns');
    btns.appendChild(btn(openTaskId === t.id ? 'Hide details' : 'Details', null, () => {
      if (openTaskId === t.id) { openTaskId = null; renderSide(true); return; }
      openTaskId = t.id;
      api('/api/tasks/' + encodeURIComponent(t.id)).then(data => {
        if (data) { taskDetail[t.id] = data; renderSide(true); }
      });
    }));
    btns.appendChild(btn('Open drawer', null, () => openTask(t.id)));
    if (['active', 'waiting_user', 'waiting_approval', 'paused']
        .includes(t.status)) {
      btns.appendChild(btn('Cancel task', 'no', async () => {
        if (!confirm('Cancel this task and its pending approvals?')) return;
        const out = await api('/api/tasks/' + encodeURIComponent(t.id) +
                              '/cancel', {method: 'POST'});
        if (out) { toast('Task cancelled.'); taskDetail[t.id] = null; refresh(true); }
      }));
    }
    if (t.status === 'paused' || t.status === 'failed') {
      btns.appendChild(btn('Resume now', 'ok', async () => {
        const out = await api('/api/tasks/' + encodeURIComponent(t.id) +
                              '/resume', {method: 'POST'});
        if (out) toast('Resume: ' + out.status);
        refresh(true);
      }));
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
        const out = await api('/api/tasks/' + encodeURIComponent(t.id) +
          '/domain', {method: 'POST', body: {domain_id: pick.value}});
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

function transcriptBlock(detail) {
  const box = el('div', 'transcript');
  const messages = detail.messages || [];
  box.appendChild(el('h4', null, 'Prompt and response (' + messages.length + ')'));
  if (!messages.length) {
    box.appendChild(el('div', 'small', 'No visible messages recorded.'));
    return box;
  }
  for (const m of messages) {
    const cls = m.role === 'user' ? 'user' :
      (m.role === 'note' ? 'note' : 'assistant');
    const node = el('div', 'msg ' + cls, m.content);
    node.appendChild(el('span', 'src',
      (m.role || '?') + '  ' + (m.source || '') + '  ' + short(m.ts)));
    box.appendChild(node);
  }
  return box;
}

function renderTaskDetail(detail) {
  const box = el('div');
  const task = detail.task || {};
  if (task.parent_task_id) {
    box.appendChild(el('div', 'small',
      'Sub-agent of task ' + task.parent_task_id +
      '  |  role ' + (task.role || 'subagent')));
  }
  box.appendChild(el('div', 'small',
    'status ' + (task.status || '?') +
    '  |  domain ' + domainName(task.domain_id) +
    '  |  origin ' + (task.origin || '?') +
    '  |  created ' + short(task.created_at) +
    '  |  updated ' + short(task.updated_at)));
  if (task.status_reason) {
    box.appendChild(el('div', 'small', 'reason: ' + task.status_reason));
  }

  box.appendChild(transcriptBlock(detail));

  box.appendChild(el('h4', null, 'Sub-agents (' +
    (detail.children || []).length + ')'));
  if (!(detail.children || []).length) {
    box.appendChild(el('div', 'small', 'No delegated work.'));
  }
  for (const child of detail.children || []) {
    const row = el('div', 'small');
    row.appendChild(pill(child.status));
    row.appendChild(document.createTextNode('  ' +
      (child.objective || '').slice(0, 100) + '  |  ' + child.id));
    if (child.result) {
      row.appendChild(el('div', 'small',
        'result: ' + String(child.result).slice(0, 500)));
    }
    if (child.blocked_reason) {
      row.appendChild(el('div', 'small',
        'blocked: ' + String(child.blocked_reason).slice(0, 300)));
    }
    if (child.result_path) {
      row.appendChild(el('div', 'small', 'artifact: ' + child.result_path));
    }
    box.appendChild(row);
  }

  box.appendChild(el('h4', null, 'Runs (' + (detail.runs || []).length + ')'));
  if (!(detail.runs || []).length) {
    box.appendChild(el('div', 'small', 'No runs recorded.'));
  }
  for (const r of detail.runs || []) {
    box.appendChild(el('div', 'small',
      short(r.started_at) + '  ' + r.status + '  trigger ' + r.trigger +
      '  iterations ' + r.iterations + '  tools ' + r.tool_calls +
      (r.decision ? '  |  ' + r.decision : '') +
      (r.error ? '  |  ' + r.error : '')));
    const toolset = r.toolset;
    if (toolset) {
      const excluded = (toolset.exclusions || []).length;
      box.appendChild(el('div', 'small',
        'toolset ' + (toolset.names || []).length + ' tools, ' +
        (toolset.tokens || 0) + '/' + (toolset.budget_tokens || 0) +
        ' schema tokens' + (excluded ? ', ' + excluded + ' excluded' : '')));
      for (const ex of (toolset.exclusions || []).slice(0, 8)) {
        box.appendChild(el('div', 'small',
          'excluded ' + ex.tool + ': ' + ex.reason));
      }
    }
    if ((r.tainted_sources || []).length) {
      box.appendChild(el('div', 'small',
        'tainted by: ' + r.tainted_sources.map(s => s.origin || s.tool)
          .join(', ')));
    }
  }

  box.appendChild(el('h4', null, 'Tool uses (' +
    (detail.executions || []).length + ')'));
  if (!(detail.executions || []).length) {
    box.appendChild(el('div', 'small', 'No tool executions.'));
  }
  for (const x of (detail.executions || []).slice(-40)) {
    const line = el('div', 'small');
    line.appendChild(pill(x.status));
    line.appendChild(document.createTextNode('  ' + x.tool + '  ' +
      short(x.started_at) + (x.error ? '  ' + x.error : '')));
    if (x.arguments) {
      line.appendChild(el('div', 'small mono',
        'args: ' + String(x.arguments).slice(0, 300)));
    }
    if (x.result) {
      line.appendChild(el('div', 'small',
        'result: ' + String(x.result).slice(0, 420)));
    }
    if (x.tool === 'delegate_task' && x.result) {
      line.appendChild(el('div', 'small',
        'delegated: ' + String(x.result).slice(0, 300)));
    }
    box.appendChild(line);
  }

  if ((detail.approvals || []).length) {
    box.appendChild(el('h4', null, 'Pending approvals'));
    for (const a of detail.approvals) {
      box.appendChild(el('div', 'small',
        a.tool + '  ' + JSON.stringify(a.arguments)));
    }
  }

  const sources = detail.context_sources || [];
  box.appendChild(el('h4', null,
    'Context sources (' + sources.length + ')'));
  if (!sources.length) {
    box.appendChild(el('div', 'small', 'No context sources recorded.'));
  }
  for (const s of sources.slice(0, 40)) {
    box.appendChild(el('div', 'small',
      (s.section || '?') + ': ' + (s.reason || 'included') +
      (s.tokens ? '  (' + s.tokens + ' tokens)' : '')));
  }
  return box;
}

async function loadTask(taskId) {
  if (!taskDetail[taskId]) {
    const data = await api('/api/tasks/' + encodeURIComponent(taskId));
    if (data) taskDetail[taskId] = data;
  }
  return taskDetail[taskId];
}

async function openTask(taskId) {
  const data = await loadTask(taskId);
  if (!data) return;
  $('drawertitle').textContent = 'Task ' + taskId;
  $('drawerbody').innerHTML = '';
  $('drawerbody').appendChild(renderTaskDetail(data));
  $('drawer').hidden = false;
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
    card.appendChild(el('pre', null, JSON.stringify(a.arguments || {}, null, 1)));
    card.appendChild(el('div', 'small',
      'requested ' + short(a.created_at) +
      (a.expires_at ? '  |  expires ' + short(a.expires_at) : '')));
    const btns = el('div', 'btns');
    btns.appendChild(btn('Approve', 'ok', () => decide(a.id, 'approve')));
    btns.appendChild(btn('Reject', 'no', () => decide(a.id, 'reject')));
    btns.appendChild(btn('Cancel', null, () => decide(a.id, 'cancel')));
    card.appendChild(btns);
    side.appendChild(card);
  }
}

async function decide(id, decision) {
  const out = await api('/api/approvals/' + encodeURIComponent(id) + '/' +
                        decision, {method: 'POST'});
  if (out) toast('Approval ' + decision + ': ' + out.status +
    (out.message ? '\n' + out.message : ''));
  refresh(true);
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
  const time = el('input'); time.placeholder = 'HH:MM (local)';
  const interval = el('input'); interval.placeholder = 'seconds';
  interval.type = 'number';
  const weekday = el('input'); weekday.placeholder = 'monday';
  const at = el('input');
  at.placeholder = 'local ISO, e.g. 2026-10-27 23:30';
  const tz = el('input'); tz.value = state.config.timezone || 'system';
  tz.placeholder = 'system or IANA timezone';
  const domain = el('select');
  const auto = el('option', null, 'Auto-route'); auto.value = '';
  domain.appendChild(auto);
  for (const d of activeDomains()) {
    const o = el('option', null, d.name); o.value = d.id; domain.appendChild(o);
  }

  for (const [label, node] of [
    ['name', name], ['kind', kind], ['time of day', time],
    ['interval s', interval], ['weekday', weekday], ['at', at],
    ['timezone', tz], ['domain', domain],
  ]) form.appendChild(field(label, node));
  form.appendChild(el('div', 'small',
    'Times without an explicit offset use the schedule timezone; "system" ' +
    'means this computer\'s local time.'));
  form.appendChild(el('div', 'small', 'Prompt'));
  form.appendChild(prompt_);
  const add = btn('Add schedule', 'ok', async () => {
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
  });
  const btns = el('div', 'btns'); btns.appendChild(add);
  form.appendChild(btns);
  side.appendChild(form);

  const list = state.schedules || [];
  if (!list.length) { empty(side, 'No schedules.'); return; }
  for (const s of list) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, s.name + ' [' + s.kind + ']'));
    card.appendChild(el('div', 'small', s.prompt));
    const tzShown = (s.tz === 'system' || !s.tz)
      ? 'system (' + (state.system_timezone || 'local') + ')' : s.tz;
    card.appendChild(el('div', 'small',
      (s.enabled ? 'enabled' : 'disabled') + '  |  next ' +
      short(s.next_due_at) + '  |  tz ' + tzShown +
      (s.domain_id ? '  |  ' + domainName(s.domain_id) : '')));
    const btns2 = el('div', 'btns');
    btns2.appendChild(btn(s.enabled ? 'Disable' : 'Enable', null, async () => {
      await api('/api/schedules/' + encodeURIComponent(s.id) + '/' +
        (s.enabled ? 'disable' : 'enable'), {method: 'POST'});
      refresh(true);
    }));
    btns2.appendChild(btn('Delete', 'no', async () => {
      if (!confirm('Delete this schedule?')) return;
      await api('/api/schedules/' + encodeURIComponent(s.id) + '/delete',
                {method: 'POST'});
      refresh(true);
    }));
    card.appendChild(btns2);
    side.appendChild(card);
  }
}

/* ------------------------------------------------------------------ inbox */

function renderInbox(side) {
  const events = state.inbox_events || [];
  const rules = state.automations || [];
  const fires = state.automation_fires || [];
  const counts = state.counts || {};

  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Inbox'));
  head.appendChild(statGrid([
    ['new', counts.inbox_new || 0],
    ['events', events.length],
    ['automations', rules.length],
  ]));
  head.appendChild(el('div', 'small',
    'External events are untrusted data. A matching rule may create a task; ' +
    'otherwise the default action applies. Quiet hours postpone triage and ' +
    'keep events queued.'));
  const headBtns = el('div', 'btns');
  headBtns.appendChild(btn('Triage now', null, async () => {
    const out = await api('/api/inbox/triage', {method: 'POST'});
    if (out && out.report) {
      const r = out.report;
      toast('Inbox: ' + (r.promoted || []).length + ' promoted, ' +
        (r.notified || 0) + ' notified, ' + (r.deduped || 0) + ' deduped, ' +
        (r.runs_started || 0) + ' run(s).');
    }
    refresh(true);
  }));
  headBtns.appendChild(btn('Manage rules', null, () => switchTab('automations')));
  head.appendChild(headBtns);
  side.appendChild(head);

  side.appendChild(el('h4', null, 'Events (' + events.length + ')'));
  if (!events.length) empty(side, 'No inbox events.');
  for (const e of events) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, e.source + ': ' + e.title));
    const row = el('div', 'row');
    row.appendChild(pill(e.status));
    row.appendChild(el('span', 'pill trust', e.trust || 'untrusted'));
    row.appendChild(el('span', 'small', e.kind + '  |  ' + e.severity));
    row.appendChild(el('span', 'small', short(e.received_at)));
    card.appendChild(row);
    if (e.body) card.appendChild(el('div', 'small', e.body.slice(0, 900)));
    card.appendChild(el('div', 'small',
      e.id + '  |  external ' + e.external_id));
    if (e.status !== 'promoted' && e.status !== 'ignored') {
      const btns = el('div', 'btns');
      btns.appendChild(btn('Ignore', 'no', async () => {
        const out = await api('/api/inbox/' + encodeURIComponent(e.id) +
          '/ignore', {method: 'POST', body: {reason: 'ignored in UI'}});
        if (out) { toast('Event ignored.'); refresh(true); }
      }));
      card.appendChild(btns);
    }
    side.appendChild(card);
  }

  side.appendChild(el('h4', null, 'Fire history'));
  if (!fires.length) {
    side.appendChild(el('div', 'small', 'No automation fires recorded.'));
  }
  for (const f of fires.slice(0, 30)) {
    side.appendChild(el('div', 'small',
      short(f.fired_at) + '  ' + (f.automation_name || '(default)') + '  ' +
      f.status + '  ' + (f.event_title || f.inbox_event_id) +
      (f.reason ? '  |  ' + f.reason : '')));
  }
}

/* ------------------------------------------------------------ automations */

function renderAutomations(side) {
  const rules = state.automations || [];
  const fires = state.automation_fires || [];
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Automations'));
  head.appendChild(el('div', 'small',
    'Rules match untrusted inbox events by source, kind, keywords, or regex. ' +
    'Actions create a task, write one note, or ignore the event. Cooldowns and ' +
    'the hourly cap stop loops; hitting the cap disables the rule once.'));
  side.appendChild(head);

  const form = el('div', 'card');
  form.appendChild(el('h3', null, 'New automation'));
  const name = el('input'); name.placeholder = 'name';
  const source = el('input'); source.placeholder = 'source (blank = any)';
  const kind = el('input'); kind.placeholder = 'kind (blank = any)';
  const keywords = el('input');
  keywords.placeholder = 'match keywords, comma separated';
  const regex = el('input'); regex.placeholder = 'optional regex';
  const action = el('select');
  for (const a of ['notify', 'task', 'ignore']) {
    const o = el('option', null, a); o.value = a; action.appendChild(o);
  }
  const cooldown = el('input'); cooldown.type = 'number'; cooldown.value = '0';
  const cap = el('input'); cap.type = 'number';
  cap.value = String((state.config.automations || {}).max_per_hour || 6);
  const template = el('textarea'); template.rows = 2;
  template.placeholder = 'task prompt template ({title}, {body}, {source})';
  for (const [label, node] of [
    ['name', name], ['source', source], ['kind', kind],
    ['keywords', keywords], ['regex', regex], ['action', action],
    ['cooldown s', cooldown], ['max/hour', cap],
  ]) form.appendChild(field(label, node));
  form.appendChild(el('div', 'small', 'Task prompt template'));
  form.appendChild(template);
  const add = btn('Add automation', 'ok', async () => {
    const body = {
      name: name.value.trim(),
      source: source.value.trim() || null,
      kind: kind.value.trim() || null,
      match_keywords: keywords.value.trim() || null,
      match_regex: regex.value.trim() || null,
      action: action.value,
      cooldown_s: Number(cooldown.value || 0),
      max_per_hour: Number(cap.value || 0),
      prompt_template: template.value,
    };
    if (!body.name) { toast('An automation needs a name.'); return; }
    const out = await api('/api/automations', {method: 'POST', body});
    if (out) { toast('Automation added.'); refresh(true); }
  });
  const fbtns = el('div', 'btns'); fbtns.appendChild(add);
  form.appendChild(fbtns);
  side.appendChild(form);

  side.appendChild(el('h4', null, 'Rules (' + rules.length + ')'));
  if (!rules.length) {
    side.appendChild(el('div', 'small',
      'No automation rules. The default action is ' +
      ((state.config.automations || {}).default_action || 'notify') + '.'));
  }
  for (const rule of rules) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null,
      rule.name + ' [' + rule.action + ']' + (rule.enabled ? '' : ' (off)')));
    const keywordsText = (rule.match_keywords || []).join(', ') || '*';
    card.appendChild(el('div', 'small',
      'source ' + (rule.source || '*') + '  |  kind ' + (rule.kind || '*') +
      '  |  keywords ' + keywordsText +
      (rule.match_regex ? '  |  regex ' + rule.match_regex : '') +
      '  |  cooldown ' + rule.cooldown_s + 's  |  max/hour ' +
      rule.max_per_hour));
    if (rule.prompt_template) {
      card.appendChild(el('div', 'small',
        'prompt: ' + rule.prompt_template.slice(0, 220)));
    }
    if (rule.disabled_reason) {
      card.appendChild(el('div', 'small',
        'disabled: ' + rule.disabled_reason));
    }
    const btns = el('div', 'btns');
    btns.appendChild(btn(rule.enabled ? 'Disable' : 'Enable', null, async () => {
      await api('/api/automations/' + encodeURIComponent(rule.id) + '/' +
        (rule.enabled ? 'disable' : 'enable'), {method: 'POST'});
      refresh(true);
    }));
    btns.appendChild(btn('Delete', 'no', async () => {
      if (!confirm('Delete this automation?')) return;
      await api('/api/automations/' + encodeURIComponent(rule.id) + '/delete',
                {method: 'POST'});
      refresh(true);
    }));
    card.appendChild(btns);
    side.appendChild(card);
  }

  side.appendChild(el('h4', null, 'Fire history'));
  if (!fires.length) {
    side.appendChild(el('div', 'small', 'No automation fires recorded.'));
  }
  for (const f of fires.slice(0, 40)) {
    side.appendChild(el('div', 'small',
      short(f.fired_at) + '  ' + (f.automation_name || '(default)') + '  ' +
      f.status + '  ' + (f.event_title || f.inbox_event_id) +
      (f.reason ? '  |  ' + f.reason : '')));
  }
}

/* ----------------------------------------------------------------- memory */

function memoryMatches(m, filter) {
  if (filter === 'active') return !m.archived && m.status === 'active';
  if (filter === 'archived') return !!m.archived;
  if (filter === 'superseded') return m.status === 'superseded';
  return true;
}

function renderMemory(side) {
  const counts = state.memory_counts || state.counts || {};
  const card0 = el('div', 'card');
  card0.appendChild(el('h3', null, 'Memory'));
  card0.appendChild(statGrid([
    ['active', counts.active || 0],
    ['archived', counts.archived || 0],
    ['superseded', counts.superseded || 0],
    ['pinned', counts.pinned || 0],
  ]));
  card0.appendChild(el('div', 'small',
    'Usage determines retention. Unused memories are archived; archived ' +
    'memories stay retrievable and can be restored. Pinned memories and ' +
    'protected kinds never archive. Delete retracts; history is kept.'));

  const filters = el('div', 'btns');
  for (const [label, value] of [
    ['Active', 'active'], ['Archived', 'archived'],
    ['Superseded', 'superseded'], ['All', 'all'],
  ]) {
    filters.appendChild(btn(label, memoryFilter === value ? 'ok' : null, () => {
      memoryFilter = value;
      renderSide(true);
    }));
  }
  card0.appendChild(filters);

  const searchRow = el('div', 'row');
  const query = el('input');
  query.placeholder = 'Search memories and history';
  query.style.flex = '1';
  const archivedBox = el('input');
  archivedBox.type = 'checkbox';
  const archivedLabel = el('label', 'small', ' include archived');
  archivedLabel.prepend(archivedBox);
  const go = btn('Search', null, async () => {
    if (!query.value.trim()) { memorySearch = null; renderSide(true); return; }
    const out = await api('/api/memories/search', {method: 'POST', body: {
      query: query.value.trim(), include_archived: archivedBox.checked,
    }});
    if (out) { memorySearch = out; renderSide(true); }
  });
  query.onkeydown = (e) => { if (e.key === 'Enter') go.onclick(); };
  searchRow.appendChild(query); searchRow.appendChild(archivedLabel);
  searchRow.appendChild(go);
  card0.appendChild(searchRow);
  side.appendChild(card0);

  if ((state.artifact_problems || []).length) {
    const problems = el('div', 'card');
    problems.appendChild(el('h3', null, 'Artifact problems'));
    for (const p of state.artifact_problems) {
      problems.appendChild(el('div', 'small', JSON.stringify(p).slice(0, 300)));
    }
    side.appendChild(problems);
  }

  if (memorySearch) {
    const results = memorySearch.results || [];
    const history = memorySearch.history || [];
    const card = el('div', 'card');
    card.appendChild(el('h3', null,
      'Search: ' + results.length + ' memor' +
      (results.length === 1 ? 'y' : 'ies') + ', ' + history.length +
      ' history hit(s)'));
    for (const m of results) {
      const label = m.archived ? ' [archived' +
        (m.restored ? ', restored' : '') + ']' : '';
      card.appendChild(el('div', 'small',
        '[' + m.kind + '/' + m.scope + ']' + label + ' ' + m.content));
      card.appendChild(el('div', 'small',
        m.id + ' rev ' + m.revision + ' | uses ' + (m.access_count || 0) +
        ' | ' + short(m.updated_at) +
        (m.reason ? ' | ' + m.reason : '')));
    }
    for (const h of history) {
      card.appendChild(el('div', 'small',
        'history ' + short(h.ts) + ' ' + (h.role || '') + ': ' +
        String(h.content || '').slice(0, 240)));
    }
    side.appendChild(card);
  }

  const all = state.memories_all || [];
  const list = all.filter(m => memoryMatches(m, memoryFilter));
  side.appendChild(el('h4', null,
    memoryFilter.replace(/^./, c => c.toUpperCase()) +
    ' memories (' + list.length + ')'));
  if (!list.length) {
    empty(side, 'No memories in this filter.');
    return;
  }
  for (const m of list) {
    const card = el('div', 'card');
    const flags = [];
    if (m.pinned) flags.push('pinned');
    if (m.archived) flags.push('archived');
    if (m.status && m.status !== 'active') flags.push(m.status);
    card.appendChild(el('h3', null,
      m.kind + ' | ' + m.scope + (flags.length ? ' | ' + flags.join(', ') : '')));
    card.appendChild(el('div', 'small', m.content));
    card.appendChild(el('div', 'small',
      m.id + ' rev ' + m.revision + ' | uses ' + (m.access_count || 0) +
      ' | last used ' + short(m.last_accessed_at) +
      ' | updated ' + short(m.updated_at) +
      (m.expires_at ? ' | expires ' + short(m.expires_at) : '')));
    const actions = el('div', 'btns');
    if (m.archived) {
      actions.appendChild(btn('Restore', 'ok', async () => {
        const out = await api('/api/memories/' + encodeURIComponent(m.id) +
          '/restore', {method: 'POST'});
        if (out) { toast('Memory restored.'); refresh(true); }
      }));
    } else if (m.status === 'active') {
      actions.appendChild(btn('Archive', 'no', async () => {
        if (!confirm('Archive this memory? It stays retrievable and can be ' +
                     'restored.')) return;
        const out = await api('/api/memories/' + encodeURIComponent(m.id) +
          '/archive', {method: 'POST'});
        if (out) { toast('Memory archived.'); refresh(true); }
      }));
      actions.appendChild(btn('Edit', null, async () => {
        const next = prompt('Edit memory', m.content);
        if (next == null || !next.trim() || next === m.content) return;
        const out = await api('/api/memories/' + encodeURIComponent(m.id),
          {method: 'PATCH', body: {content: next.trim(),
                                   reason: 'edited in the web UI'}});
        if (out) { toast('Memory updated.'); refresh(true); }
      }));
    }
    actions.appendChild(btn(m.pinned ? 'Unpin' : 'Pin', null, async () => {
      const out = await api('/api/memories/' + encodeURIComponent(m.id) +
        (m.pinned ? '/unpin' : '/pin'), {method: 'POST'});
      if (out) {
        toast(m.pinned ? 'Memory unpinned.' : 'Memory pinned.');
        refresh(true);
      }
    }));
    actions.appendChild(btn('Delete', 'no', async () => {
      if (!confirm('Retract this memory? Its revision history is kept.')) return;
      const out = await api('/api/memories/' + encodeURIComponent(m.id),
        {method: 'DELETE', body: {reason: 'deleted in the web UI'}});
      if (out) { toast('Memory retracted.'); refresh(true); }
    }));
    card.appendChild(actions);
    side.appendChild(card);
  }
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
  form.appendChild(field('name', name));
  form.appendChild(field('description', desc));
  const add = btn('Create domain', 'ok', async () => {
    if (!name.value.trim()) { toast('A name is required.'); return; }
    const out = await api('/api/domains', {method: 'POST', body: {
      name: name.value.trim(), description: desc.value.trim(),
    }});
    if (out) { toast('Domain created.'); refresh(true); }
  });
  const btns = el('div', 'btns'); btns.appendChild(add);
  form.appendChild(btns);
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
      const btns2 = el('div', 'btns');
      btns2.appendChild(btn('Save', 'ok', async () => {
        const out = await api('/api/domains/' + encodeURIComponent(d.id),
          {method: 'PATCH', body: {
            name: nameInput.value.trim(),
            description: descInput.value,
          }});
        if (out) { toast('Domain saved.'); refresh(true); }
      }));
      btns2.appendChild(btn(group[1] === 'active' ? 'Archive' : 'Restore',
        group[1] === 'active' ? 'no' : 'ok', async () => {
          const status = group[1] === 'active' ? 'archived' : 'active';
          if (status === 'archived' &&
              !confirm('Archive this domain? Existing tasks keep their domain; ' +
                       'new work stops routing here.')) return;
          const out = await api('/api/domains/' + encodeURIComponent(d.id),
            {method: 'PATCH', body: {status}});
          if (out) { toast('Domain ' + status + '.'); refresh(true); }
        }));
      card.appendChild(btns2);
      side.appendChild(card);
    }
  }
  if (!domains.length) {
    empty(side, 'No domains yet. General work stays in General. Domains are ' +
      'archived, never hard-deleted, so task history keeps its scope.');
  }
}

/* ----------------------------------------------------------------- skills */

function skillStatusGroup(skill) {
  return skill.status || 'draft';
}

function renderSkills(side) {
  const skills = state.skills || [];
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Skills'));
  const counts = { active: 0, draft: 0, disabled: 0, error: 0 };
  for (const s of skills) counts[skillStatusGroup(s)] = (counts[skillStatusGroup(s)] || 0) + 1;
  head.appendChild(statGrid([
    ['active', counts.active], ['drafts', counts.draft],
    ['disabled', counts.disabled], ['errors', counts.error],
  ]));
  head.appendChild(el('div', 'small',
    'Skills are instruction text only. Drafts are never disclosed or ' +
    'activated automatically; activation, rollback, and deletion are ' +
    'explicit user actions.'));
  side.appendChild(head);

  const prefill = skillPrefill || {};
  const form = el('div', 'card');
  form.appendChild(el('h3', null,
    skillPrefill && skillPrefill.id ? 'Revise skill draft' : 'New skill draft'));
  const name = el('input'); name.value = prefill.name || '';
  name.placeholder = 'short reusable procedure name';
  const desc = el('input'); desc.value = prefill.description || '';
  desc.placeholder = 'one line: when does it apply?';
  const pre = el('input');
  pre.value = (prefill.preconditions || []).join(', ');
  pre.placeholder = 'preconditions, comma separated';
  const domainPick = el('select');
  domainPick.multiple = true;
  for (const d of activeDomains()) {
    const o = el('option', null, d.name);
    o.value = d.id;
    if ((prefill.domains || []).includes(d.id)) o.selected = true;
    domainPick.appendChild(o);
  }
  const body = el('textarea'); body.rows = 8; body.value = prefill.body || '';
  body.placeholder = 'Instructions only. No code, handlers, or tool schemas.';
  for (const [label, node] of [
    ['name', name], ['description', desc], ['preconditions', pre],
  ]) form.appendChild(field(label, node));
  form.appendChild(el('div', 'small',
    'Bind to domains (leave empty for global or adaptive scope)'));
  form.appendChild(domainPick);
  form.appendChild(el('div', 'small', 'Body'));
  form.appendChild(body);
  const formBtns = el('div', 'btns');
  formBtns.appendChild(btn('Save as draft', 'ok', async () => {
    if (!name.value.trim() || !body.value.trim()) {
      toast('A skill needs a name and a body.'); return;
    }
    const bodyPayload = {
      name: name.value.trim(),
      description: desc.value.trim(),
      preconditions: pre.value.split(',').map(s => s.trim()).filter(Boolean),
      domains: Array.from(domainPick.selectedOptions).map(o => o.value),
      body: body.value,
      reason: 'edited in the web UI',
    };
    const out = await api('/api/skills', {method: 'POST', body: bodyPayload});
    if (out) {
      toast('Skill saved as a draft.');
      skillPrefill = null;
      refresh(true);
    }
  }));
  if (skillPrefill && skillPrefill.id) {
    formBtns.appendChild(btn('Cancel revise', null, () => {
      skillPrefill = null; renderSide(true);
    }));
  }
  form.appendChild(formBtns);
  side.appendChild(form);

  if (!skills.length) {
    empty(side, 'No skills yet. Import a skills directory or create a draft.');
    return;
  }
  side.appendChild(el('h4', null, 'All skills (' + skills.length + ')'));
  for (const s of skills) {
    const card = el('div', 'card');
    const row = el('div', 'row');
    row.appendChild(pill(s.status));
    row.appendChild(el('span', 'tag', s.scope));
    row.appendChild(el('span', 'small', 'v' + s.version +
      '  |  uses ' + (s.usage_count || 0)));
    card.appendChild(row);
    card.appendChild(el('h3', null, s.name));
    if (s.description) card.appendChild(el('div', 'small', s.description));
    card.appendChild(el('div', 'small',
      'id ' + s.id + '  |  origin ' + (s.origin || '?') +
      '  |  created by ' + (s.created_by || '?') +
      '  |  updated ' + short(s.updated_at)));
    if ((s.preconditions || []).length) {
      card.appendChild(el('div', 'small',
        'preconditions: ' + s.preconditions.join(', ')));
    }
    if ((s.domains || []).length) {
      card.appendChild(el('div', 'small',
        'domains: ' + s.domains.map(domainName).join(', ')));
    }
    if (s.provenance && Object.keys(s.provenance).length) {
      card.appendChild(el('div', 'small mono',
        'provenance: ' + JSON.stringify(s.provenance).slice(0, 300)));
    }
    if (s.body_preview) {
      card.appendChild(el('div', 'small',
        'body: ' + s.body_preview.slice(0, 300)));
    }
    if ((s.revisions || []).length) {
      card.appendChild(el('div', 'small',
        'revisions: ' + s.revisions.slice(-4).map(r =>
          'v' + r.version + ' ' + r.status + ' (' + (r.changed_by || '?') +
          ')').join(' | ')));
    }

    if (openSkillId === s.id && skillDetails[s.id]) {
      const detail = skillDetails[s.id];
      card.appendChild(el('h4', null, 'Body'));
      card.appendChild(el('pre', null, detail.body || '(no body file)'));
    }

    const btns = el('div', 'btns');
    btns.appendChild(btn(openSkillId === s.id ? 'Hide body' : 'View body',
      null, async () => {
        if (openSkillId === s.id) { openSkillId = null; renderSide(true); return; }
        const detail = await api('/api/skills/' + encodeURIComponent(s.id));
        if (detail) {
          skillDetails[s.id] = detail;
          openSkillId = s.id;
          renderSide(true);
        }
      }));
    if (s.status !== 'active') {
      btns.appendChild(btn('Activate', 'ok', async () => {
        const out = await api('/api/modules/' + encodeURIComponent(s.id) +
          '/activate', {method: 'POST'});
        if (out) { toast('Skill activated.'); refresh(true); }
      }));
    }
    if (s.status === 'active') {
      btns.appendChild(btn('Disable', 'no', async () => {
        const out = await api('/api/modules/' + encodeURIComponent(s.id) +
          '/disable', {method: 'POST'});
        if (out) { toast('Skill disabled.'); refresh(true); }
      }));
    }
    if (s.rollback_available) {
      btns.appendChild(btn('Rollback', null, async () => {
        if (!confirm('Restore the previous version of this skill?')) return;
        const out = await api('/api/modules/' + encodeURIComponent(s.id) +
          '/rollback', {method: 'POST'});
        if (out) { toast('Skill rolled back.'); refresh(true); }
      }));
    }
    btns.appendChild(btn('Revise', null, async () => {
      const detail = skillDetails[s.id] ||
        await api('/api/skills/' + encodeURIComponent(s.id));
      if (!detail) return;
      skillDetails[s.id] = detail;
      skillPrefill = {
        id: s.id, name: s.name, description: s.description,
        preconditions: s.preconditions || [], domains: s.domains || [],
        body: detail.body || '',
      };
      renderSide(true);
      side.scrollTop = 0;
    }));
    if (s.status !== 'active') {
      btns.appendChild(btn('Delete', 'no', async () => {
        if (!confirm('Delete this skill and its versioned body files?')) return;
        const out = await api('/api/skills/' + encodeURIComponent(s.id),
          {method: 'DELETE'});
        if (out) { toast('Skill deleted.'); refresh(true); }
      }));
    }
    card.appendChild(btns);
    side.appendChild(card);
  }
}

/* ---------------------------------------------------------- consolidation */

function renderConsolidation(side) {
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Consolidation'));
  head.appendChild(el('div', 'small',
    'One bounded model pass proposes create/update/merge/retract changes. ' +
    'The harness validates every change and records a revision and a markdown ' +
    'change log. A failed pass changes nothing.'));
  head.appendChild(el('div', 'small',
    'Scheduled every ' +
    pct(state.consolidation_schedule || 0) + 's when configured.'));
  const btns = el('div', 'btns');
  btns.appendChild(btn('Run consolidation now', 'ok', async () => {
    const out = await api('/api/consolidate', {method: 'POST'});
    if (out) {
      toast('Consolidation ' + out.status + ': applied ' +
        (out.applied || []).length + ', rejected ' +
        (out.rejected || []).length);
    }
    refresh(true);
  }));
  head.appendChild(btns);
  side.appendChild(head);

  const runs = state.consolidations || [];
  side.appendChild(el('h4', null, 'Recent runs (' + runs.length + ')'));
  if (!runs.length) {
    empty(side, 'No consolidation runs recorded.');
  }
  for (const r of runs) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null,
      (r.trigger || 'manual') + ' | ' + (r.status || '?')));
    card.appendChild(el('div', 'small',
      'applied ' + (r.applied || 0) + '  |  rejected ' +
      (r.rejected || 0) + (r.error ? '  |  ' + r.error : '')));
    if (r.log_path) card.appendChild(el('div', 'small', 'log: ' + r.log_path));
    if (r.run_id) card.appendChild(el('div', 'small', 'run ' + r.run_id));
    side.appendChild(card);
  }
}

/* ---------------------------------------------------------------- modules */

function moduleHealthPill(health) {
  const value = String(health || '');
  if (!value) return el('span', 'small', 'no health probe yet');
  return pill(value);
}

function renderModules(side) {
  const modules = state.modules || [];
  const budget = state.schema_budget || {};
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Capability modules'));
  head.appendChild(statGrid([
    ['modules', modules.length],
    ['active', modules.filter(m => m.status === 'active').length],
    ['errors', modules.filter(m => m.status === 'error').length],
    ['schema tokens', budget.tokens || 0],
  ]));
  head.appendChild(el('div', 'small',
    'One record shape for skills, MCP servers, and the web module. A draft, ' +
    'disabled, or error module is never disclosed and never callable. ' +
    'Reload changes the toolset without restarting the process.'));
  const btns = el('div', 'btns');
  btns.appendChild(btn('Reload all modules', null, async () => {
    const out = await api('/api/modules/reload', {method: 'POST'});
    if (out && out.report) {
      toast('Reloaded: ' + (out.report.loaded || []).join(', '));
    }
    refresh(true);
  }));
  head.appendChild(btns);
  side.appendChild(head);

  const budgetCard = el('div', 'card');
  budgetCard.appendChild(el('h3', null, 'Tool-schema budget'));
  budgetCard.appendChild(el('div', 'small',
    'Selected ' + (budget.tool_count || 0) + ' tool schema(s) using ' +
    (budget.tokens || 0) + ' of ' + (budget.budget_tokens || 0) +
    ' tokens. Whole tools are dropped, never half a schema.'));
  if ((budget.exclusions || []).length) {
    budgetCard.appendChild(el('h4', null, 'Exclusions'));
    for (const ex of budget.exclusions) {
      budgetCard.appendChild(el('div', 'small',
        ex.tool + ' (' + ex.module + '): ' + ex.reason +
        (ex.tokens ? '  ' + ex.tokens + ' tokens' : '')));
    }
  } else {
    budgetCard.appendChild(el('div', 'small', 'No tools excluded.'));
  }
  side.appendChild(budgetCard);

  if (!modules.length) {
    empty(side, 'No modules registered.');
    return;
  }
  const domains = activeDomains();
  for (const m of modules) {
    const card = el('div', 'card');
    const row = el('div', 'row');
    row.appendChild(pill(m.status));
    row.appendChild(el('span', 'tag', m.kind));
    row.appendChild(el('span', 'tag', m.scope));
    row.appendChild(moduleHealthPill(m.health));
    card.appendChild(row);
    card.appendChild(el('h3', null, m.name + '  (' + m.id + ')'));
    card.appendChild(el('div', 'small',
      'v' + m.version + '  |  origin ' + (m.origin || '?') +
      '  |  uses ' + (m.usage_count || 0) +
      '  |  loaded ' + short(m.last_loaded_at)));
    if (m.last_error) {
      card.appendChild(el('div', 'small', 'last error: ' + m.last_error));
    }
    if (m.content_path) {
      card.appendChild(el('div', 'small', 'content: ' + m.content_path));
    }
    if ((m.bound_domains || []).length) {
      card.appendChild(el('div', 'small',
        'bound domains: ' + m.bound_domains.map(domainName).join(', ')));
    }
    if ((m.tools || []).length) {
      card.appendChild(el('h4', null, 'Tools (' + m.tools.length + ')'));
      for (const tool of m.tools) {
        const line = el('div', 'row');
        line.appendChild(pill(tool.risk));
        line.appendChild(el('code', null, tool.name));
        line.appendChild(el('span', 'small',
          (tool.side_effects ? 'side effects' : 'no side effects') + '  |  ' +
          (tool.read_only ? 'read-only' : 'not read-only')));
        card.appendChild(line);
      }
    }
    const actions = el('div', 'btns');
    actions.appendChild(btn(m.status === 'active' ? 'Disable' : 'Enable',
      m.status === 'active' ? 'no' : 'ok', async () => {
        const action = m.status === 'active' ? 'disable' : 'enable';
        const out = await api('/api/modules/' + encodeURIComponent(m.id) +
          '/' + action, {method: 'POST'});
        if (out) { toast('Module ' + action + 'd.'); refresh(true); }
      }));
    actions.appendChild(btn('Reload', null, async () => {
      const out = await api('/api/modules/' + encodeURIComponent(m.id) +
        '/reload', {method: 'POST'});
      if (out) toast('Module reload requested.');
      refresh(true);
    }));
    if (m.kind === 'skill') {
      actions.appendChild(btn('Open skills', null, () => switchTab('skills')));
    }
    card.appendChild(actions);

    if (m.scope === 'domain' && domains.length) {
      card.appendChild(el('h4', null, 'Domain bindings'));
      const bound = new Set(m.bound_domains || []);
      for (const d of domains) {
        const label = el('label', 'small');
        const box = el('input');
        box.type = 'checkbox';
        box.checked = bound.has(d.id);
        box.onchange = async () => {
          const out = await api('/api/modules/' + encodeURIComponent(m.id) +
            '/bindings', {method: 'POST',
                          body: {domain_id: d.id, enabled: box.checked}});
          if (out) toast('Binding saved.');
          refresh(true);
        };
        label.appendChild(box);
        label.appendChild(document.createTextNode(' ' + d.name));
        card.appendChild(label);
      }
    }
    if (m.manifest && Object.keys(m.manifest).length) {
      card.appendChild(el('h4', null, 'Manifest'));
      card.appendChild(el('pre', null,
        JSON.stringify(m.manifest, null, 1).slice(0, 2400)));
    }
    side.appendChild(card);
  }
}

/* ------------------------------------------------------------------ tools */

function renderTools(side) {
  const tools = state.tools || [];
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Registered tools (' + tools.length + ')'));
  head.appendChild(el('div', 'small',
    'Risk is enforced by the harness, not the model. Read-only tools stay ' +
    'low-risk in a tainted run; side-effecting tools are escalated to ' +
    'confirm-risk after untrusted content enters the run.'));
  side.appendChild(head);

  const byModule = {};
  for (const t of tools) {
    (byModule[t.module || 'builtin'] = byModule[t.module || 'builtin'] || [])
      .push(t);
  }
  for (const moduleId of Object.keys(byModule).sort()) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, moduleId + ' (' +
      byModule[moduleId].length + ')'));
    for (const t of byModule[moduleId]) {
      const row = el('div', 'row');
      row.appendChild(pill(t.risk));
      row.appendChild(el('code', null, t.name));
      const flags = [];
      if (t.side_effects) flags.push('side effects');
      if (t.read_only) flags.push('read-only');
      if (t.untrusted_output) flags.push('untrusted output');
      row.appendChild(el('span', 'small',
        flags.length ? flags.join(' | ') : 'trusted, no side effects'));
      card.appendChild(row);
    }
    side.appendChild(card);
  }
  const status = state.mcp_status || {};
  if (Object.keys(status).length) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, 'Module transport status'));
    for (const id of Object.keys(status)) {
      card.appendChild(el('div', 'small', id + ': ' + status[id]));
    }
    side.appendChild(card);
  }
}

/* ----------------------------------------------------------------- events */

function renderEvents(side) {
  const all = state.events || [];
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Event log'));
  head.appendChild(el('div', 'small',
    'The event log is the observability contract: routing, runs, tool ' +
    'outcomes, approvals, schedules, module health, trust escalation, and ' +
    'sub-agent work are recorded here.'));
  const filters = el('div', 'btns');
  for (const level of ['all', 'info', 'warn', 'error']) {
    filters.appendChild(btn(level, eventLevel === level ? 'ok' : null, () => {
      eventLevel = level; renderSide(true);
    }));
  }
  head.appendChild(filters);
  side.appendChild(head);

  const list = all.filter(e => eventLevel === 'all' || e.level === eventLevel)
    .slice().reverse();
  side.appendChild(el('h4', null, 'Recent (' + list.length + ')'));
  if (!list.length) { empty(side, 'No events at this level.'); return; }
  const card = el('div', 'card');
  for (const e of list) {
    const line = el('div', 'ev ' + (e.level || 'info'));
    line.appendChild(el('b', null, e.type));
    const detail = e.data && Object.keys(e.data).length
      ? '  ' + JSON.stringify(e.data).slice(0, 300) : '';
    line.appendChild(document.createTextNode(
      '  ' + short(e.ts) + detail));
    if (e.task_id || e.run_id) {
      line.appendChild(el('div', 'small',
        (e.task_id ? 'task ' + e.task_id + ' ' : '') +
        (e.run_id ? 'run ' + e.run_id : '')));
    }
    card.appendChild(line);
  }
  side.appendChild(card);
}

/* ----------------------------------------------------------------- system */

function renderSystem(side) {
  const counts = state.counts || {};
  const sched = state.scheduler || {};
  const system = state.system || {};
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Status'));
  head.appendChild(statGrid([
    ['open tasks', counts.open_tasks || 0],
    ['paused', counts.paused || 0],
    ['pending approvals', counts.pending_approvals || 0],
    ['domains', counts.domains || 0],
  ]));
  head.appendChild(el('div', 'small',
    'System time ' + short(state.now) + '  |  timezone ' +
    (state.system_timezone || 'local')));
  head.appendChild(el('div', 'small',
    'Scheduler ' + (sched.enabled ? 'enabled' : 'disabled') +
    ' | last tick ' + short(sched.last_tick)));
  if (sched.last_report) {
    head.appendChild(el('pre', null,
      JSON.stringify(sched.last_report, null, 1).slice(0, 3000)));
  }
  side.appendChild(head);

  const actions = el('div', 'card');
  actions.appendChild(el('h3', null, 'Maintenance'));
  const btns = el('div', 'btns');
  btns.appendChild(btn('Run heartbeat now', null, async () => {
    const out = await api('/api/heartbeat', {method: 'POST'});
    if (out && out.report) {
      const r = out.report;
      toast('Heartbeat: ' + (r.submitted || []).length + ' submitted, ' +
        (r.resumed || []).length + ' resumed, ' +
        (r.retried || []).length + ' retried, ' +
        (r.deferred || 0) + ' deferred.');
    }
    refresh(true);
  }));
  btns.appendChild(btn('Create backup', null, async () => {
    const out = await api('/api/backup', {method: 'POST'});
    if (out) toast('Backup written: ' + out.backup);
    refresh(true);
  }));
  actions.appendChild(btns);
  side.appendChild(actions);

  const ep = el('div', 'card');
  ep.appendChild(el('h3', null, 'Model endpoint'));
  ep.appendChild(el('div', 'small',
    ((system.model || {}).base_url || (state.config.model || {}).base_url ||
     '') + '  |  ' + ((system.model || {}).name ||
     (state.config.model || {}).name || '')));
  const probe = btn('Probe endpoint', null, async () => {
    out.textContent = 'probing...';
    const data = await api('/api/endpoint-probe');
    if (data) out.textContent = JSON.stringify(data, null, 1);
  });
  const out = el('pre');
  ep.appendChild(probe); ep.appendChild(out);
  side.appendChild(ep);

  const health = el('div', 'card');
  health.appendChild(el('h3', null, 'Module health'));
  const mods = system.modules || [];
  if (!mods.length) health.appendChild(el('div', 'small', 'No modules.'));
  for (const m of mods) {
    health.appendChild(el('div', 'small',
      m.id + ': ' + m.status + (m.health ? ' / ' + m.health : '') +
      (m.last_error ? '  |  ' + m.last_error : '')));
  }
  side.appendChild(health);
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
  btns.appendChild(btn('Save persona', 'ok', async () => {
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
  }));
  card.appendChild(btns);
  side.appendChild(card);
}

/* --------------------------------------------------------------- settings */

const SETTING_GROUPS = ['limits', 'context', 'domains', 'memory', 'tools',
                        'modules', 'skills', 'subagents', 'web', 'scheduler',
                        'consolidation', 'approvals', 'inbox', 'automations'];

function renderSettings(side) {
  if (!configMeta) {
    api('/api/config').then(data => {
      if (data) { applyConfig(data); renderSide(true); }
    });
  }
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Settings'));
  head.appendChild(el('div', 'small',
    'Editable operational groups. Values are validated before being written ' +
    'to config.yaml; unknown keys are rejected.'));
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
  btns.appendChild(btn('Save ' + group, 'ok', async () => {
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
  }));
  card.appendChild(btns);
  return card;
}

function timezoneCard() {
  const card = el('div', 'card');
  card.appendChild(el('h3', null, 'timezone'));
  card.appendChild(el('div', 'small',
    'Use "system" to follow this computer\'s clock. Set an IANA name only ' +
    'to override it for schedules and the assistant\'s clock.'));
  const input = el('input');
  input.type = 'text';
  input.value = state.config.timezone || 'system';
  input.placeholder = 'system';
  card.appendChild(field('System or IANA timezone', input));
  card.appendChild(el('div', 'small',
    'Detected system timezone: ' + (state.system_timezone || 'local')));
  const btns = el('div', 'btns');
  btns.appendChild(btn('Save timezone', 'ok', async () => {
    const out = await api('/api/config',
      {method: 'POST', body: {group: 'timezone', value: input.value}});
    if (out) { toast('Timezone saved.'); refresh(true); }
  }));
  card.appendChild(btns);
  return card;
}

/* ----------------------------------------------------------------- backup */

function humanSize(bytes) {
  const n = Number(bytes || 0);
  if (n > 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

function renderBackup(side) {
  const backups = state.backups || [];
  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Backup and restore'));
  head.appendChild(el('div', 'small',
    'A backup is a consistent SQLite snapshot plus config.yaml, artifacts, ' +
    'and data/modules/. Restore replaces live state and exits so the service ' +
    'manager can restart cleanly.'));
  const btns = el('div', 'btns');
  btns.appendChild(btn('Create backup now', 'ok', async () => {
    const out = await api('/api/backup', {method: 'POST'});
    if (out) toast('Backup written: ' + out.backup);
    refresh(true);
  }));
  head.appendChild(btns);
  side.appendChild(head);

  side.appendChild(el('h4', null, 'Backups (' + backups.length + ')'));
  if (!backups.length) {
    empty(side, 'No backups under data/backups yet.');
  }
  for (const b of backups) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, b.name));
    card.appendChild(el('div', 'small',
      humanSize(b.size) + '  |  ' + short(b.mtime_ms)));
    card.appendChild(el('div', 'small mono', b.path));
    const row = el('div', 'btns');
    row.appendChild(btn('Restore this backup', 'no', async () => {
      if (!confirm('Restore replaces all live state. Continue?')) return;
      if (!confirm('This restarts the service. Really restore ' +
                   b.name + '?')) return;
      const out = await api('/api/restore',
        {method: 'POST', body: {confirm: true, path: b.path}});
      if (out) toast('Restore started; the service will restart.');
    }));
    card.appendChild(row);
    side.appendChild(card);
  }

  const manual = el('div', 'card');
  manual.appendChild(el('h3', null, 'Restore from a path'));
  manual.appendChild(el('div', 'small',
    'The server reads the archive from its own filesystem. Use the exact ' +
    'path shown above.'));
  const path = el('input'); path.placeholder = 'path to a harness .zip';
  manual.appendChild(field('archive', path));
  const confirmBox = el('input');
  confirmBox.type = 'checkbox';
  manual.appendChild(field('I understand state is replaced', confirmBox));
  const row = el('div', 'btns');
  row.appendChild(btn('Restore', 'no', async () => {
    if (!confirmBox.checked) { toast('Confirm the restore first.'); return; }
    if (!path.value.trim()) { toast('An archive path is required.'); return; }
    const out = await api('/api/restore',
      {method: 'POST', body: {confirm: true, path: path.value.trim()}});
    if (out) toast('Restore started; the service will restart.');
  }));
  manual.appendChild(row);
  side.appendChild(manual);
}

/* ------------------------------------------------------------------ guard */

function renderGuard(side) {
  const card = el('div', 'card');
  card.appendChild(el('h3', null, 'Injection guard (Phase 7)'));
  card.appendChild(el('div', 'small',
    'No classifier is installed in this build. The trust policy still applies ' +
    'today: web and MCP output is untrusted data, an untrusted result taints ' +
    'the run, and any side-effecting tool is escalated to confirm-risk with ' +
    'the source named in the approval.'));
  card.appendChild(el('div', 'small',
    'When LaYa is online, this panel will show recent verdicts, cache hit ' +
    'rate, and manual rescan. Disabling it will restore exactly the current ' +
    'behaviour.'));
  side.appendChild(card);

  const policy = el('div', 'card');
  policy.appendChild(el('h3', null, 'Current trust policy'));
  for (const line of [
    'untrusted_output=True taints the run for subsequent steps',
    'side-effects escalate to confirm-risk in a tainted run',
    'read-only tools remain low-risk',
    'side-effecting tools default on when provenance is unknown',
    'approvals name the first untrusted source (URL, source, or tool)',
    'inbox events are untrusted and derived runs start tainted',
  ]) policy.appendChild(el('div', 'bullet', '- ' + line));
  side.appendChild(policy);
}

/* ---------------------------------------------------------------- switches */

function switchTab(next) {
  tab = next;
  document.querySelectorAll('#nav button').forEach(x =>
    x.classList.toggle('active', x.dataset.tab === tab));
  $('side').scrollTop = 0;
  renderSide(true);
  refresh(true);
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

$('send').onclick = send;
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('banner').onclick = () => switchTab('approvals');
$('drawerclose').onclick = () => { $('drawer').hidden = true; };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('drawer').hidden = true;
});
document.querySelectorAll('#nav button').forEach((b) => {
  b.onclick = () => switchTab(b.dataset.tab);
});

refresh(true);
setInterval(() => { if (!busy) refresh(false); }, 3000);
