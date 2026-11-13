'use strict';
let state = null;
let tab = 'chat';
let busy = false;
let lastChatKey = '';
let openTaskId = null;
let taskDetail = {};
let memorySearch = null;
let memoryFilter = 'active';
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
    schedules: renderSchedules, inbox: renderInbox, memory: renderMemory,
    system: renderSystem,
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
  if (detail.task && detail.task.parent_task_id) {
    box.appendChild(el('div', 'small',
      'Sub-agent of task ' + detail.task.parent_task_id));
  }
  box.appendChild(el('h4', null, 'Sub-agents'));
  if (!(detail.children || []).length) {
    box.appendChild(el('div', 'small', 'No delegated work.'));
  }
  for (const child of detail.children || []) {
    const row = el('div', 'small');
    row.appendChild(el('span', null,
      child.status + '  ' + (child.objective || '').slice(0, 90) +
      '  |  ' + child.id));
    if (child.result) {
      row.appendChild(el('div', 'small',
        'result: ' + String(child.result).slice(0, 220)));
    }
    if (child.blocked_reason) {
      row.appendChild(el('div', 'small',
        'blocked: ' + String(child.blocked_reason).slice(0, 220)));
    }
    if (child.result_path) {
      row.appendChild(el('div', 'small', 'artifact: ' + child.result_path));
    }
    box.appendChild(row);
  }
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
    const line = el('div', 'small',
      x.tool + '  ' + x.status + '  ' + short(x.started_at) +
      (x.error ? '  ' + x.error : ''));
    if (x.tool === 'delegate_task' && x.result) {
      line.appendChild(el('div', 'small',
        'delegated: ' + String(x.result).slice(0, 260)));
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
                        'scheduler', 'consolidation', 'approvals', 'inbox',
                        'automations'];

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
  card.appendChild(el('div', 'small',
    'Use "system" to follow this computer\'s clock. Set an IANA name only ' +
    'to override it for schedules and the assistant\'s clock.'));
  const row = el('div', 'field');
  row.appendChild(el('label', null, 'System or IANA timezone'));
  const input = el('input');
  input.type = 'text';
  input.value = state.config.timezone || 'system';
  input.placeholder = 'system';
  row.appendChild(input);
  card.appendChild(row);
  card.appendChild(el('div', 'small',
    'Detected system timezone: ' + (state.system_timezone || 'local')));
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
  form.appendChild(el('div', 'small',
    'Times without an explicit offset use the schedule timezone; "system" ' +
    'means this computer\'s local time.'));
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
    const tzShown = (s.tz === 'system' || !s.tz)
      ? 'system (' + (state.system_timezone || 'local') + ')' : s.tz;
    card.appendChild(el('div', 'small',
      (s.enabled ? 'enabled' : 'disabled') + '  |  next ' +
      short(s.next_due_at) + '  |  tz ' + tzShown +
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

/* ------------------------------------------------------------------ inbox */

function renderInbox(side) {
  const events = state.inbox_events || [];
  const rules = state.automations || [];
  const fires = state.automation_fires || [];
  const counts = state.counts || {};

  const head = el('div', 'card');
  head.appendChild(el('h3', null, 'Inbox'));
  const grid = el('div', 'split');
  for (const [k, v] of [['new', counts.inbox_new || 0],
                        ['events', counts.inbox_events || 0],
                        ['automations', counts.automations || 0]]) {
    const stat = el('div', 'stat');
    stat.appendChild(el('div', 'n', pct(v)));
    stat.appendChild(el('div', 'k', k));
    grid.appendChild(stat);
  }
  head.appendChild(grid);
  head.appendChild(el('div', 'small',
    'External events are untrusted data. A matching rule may create a task; ' +
    'otherwise the default action applies. Quiet hours postpone triage and ' +
    'keep events queued.'));
  const headBtns = el('div', 'btns');
  const triage = el('button', null, 'Triage now');
  triage.onclick = async () => {
    triage.disabled = true;
    const out = await api('/api/inbox/triage', {method: 'POST'});
    if (out && out.report) {
      const r = out.report;
      toast('Inbox: ' + (r.promoted || []).length + ' promoted, ' +
        (r.notified || 0) + ' notified, ' + (r.deduped || 0) + ' deduped, ' +
        (r.runs_started || 0) + ' run(s).');
    }
    refresh(true);
  };
  headBtns.appendChild(triage);
  head.appendChild(headBtns);
  side.appendChild(head);

  const form = el('div', 'card');
  form.appendChild(el('h3', null, 'New automation'));
  const name = el('input'); name.placeholder = 'name';
  const source = el('input'); source.placeholder = 'source (blank = any)';
  const kind = el('input'); kind.placeholder = 'kind (blank = any)';
  const keywords = el('input');
  keywords.placeholder = 'match keywords, comma separated';
  const action = el('select');
  for (const a of ['notify', 'task', 'ignore']) {
    const o = el('option', null, a); o.value = a; action.appendChild(o);
  }
  const cooldown = el('input'); cooldown.type = 'number'; cooldown.value = '0';
  cooldown.placeholder = 'cooldown seconds';
  const cap = el('input'); cap.type = 'number';
  cap.value = String((state.config.automations || {}).max_per_hour || 6);
  cap.placeholder = 'max per hour';
  const template = el('textarea'); template.rows = 2;
  template.placeholder = 'task prompt template ({title}, {body}, {source})';
  for (const [label, node] of [
    ['name', name], ['source', source], ['kind', kind],
    ['keywords', keywords], ['action', action],
    ['cooldown s', cooldown], ['max/hour', cap],
  ]) {
    const row = el('div', 'field');
    row.appendChild(el('label', null, label));
    row.appendChild(node);
    form.appendChild(row);
  }
  form.appendChild(el('div', 'small', 'Task prompt template'));
  form.appendChild(template);
  const add = el('button', 'ok', 'Add automation');
  add.onclick = async () => {
    const body = {
      name: name.value.trim(),
      source: source.value.trim() || null,
      kind: kind.value.trim() || null,
      match_keywords: keywords.value.trim() || null,
      action: action.value,
      cooldown_s: Number(cooldown.value || 0),
      max_per_hour: Number(cap.value || 0),
      prompt_template: template.value,
    };
    if (!body.name) { toast('An automation needs a name.'); return; }
    const out = await api('/api/automations', {method: 'POST', body});
    if (out) { toast('Automation added.'); refresh(true); }
  };
  const fbtns = el('div', 'btns'); fbtns.appendChild(add);
  form.appendChild(fbtns);
  side.appendChild(form);

  if (!rules.length) {
    side.appendChild(el('h4', null, 'Automations'));
    empty(side, 'No automation rules. The default action is ' +
      ((state.config.automations || {}).default_action || 'notify') + '.');
  }
  for (const rule of rules) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null,
      rule.name + ' [' + rule.action + ']' + (rule.enabled ? '' : ' (off)')));
    const keywordsText = (rule.match_keywords || []).join(', ') || '*';
    card.appendChild(el('div', 'small',
      'source ' + (rule.source || '*') + '  |  kind ' + (rule.kind || '*') +
      '  |  keywords ' + keywordsText + '  |  cooldown ' +
      rule.cooldown_s + 's  |  max/hour ' + rule.max_per_hour));
    if (rule.prompt_template) {
      card.appendChild(el('div', 'small',
        'prompt: ' + rule.prompt_template.slice(0, 220)));
    }
    if (rule.disabled_reason) {
      card.appendChild(el('div', 'small',
        'disabled: ' + rule.disabled_reason));
    }
    const btns = el('div', 'btns');
    const toggle = el('button', null, rule.enabled ? 'Disable' : 'Enable');
    toggle.onclick = async () => {
      await api('/api/automations/' + encodeURIComponent(rule.id) + '/' +
        (rule.enabled ? 'disable' : 'enable'), {method: 'POST'});
      refresh(true);
    };
    const del = el('button', 'no', 'Delete');
    del.onclick = async () => {
      if (!confirm('Delete this automation?')) return;
      await api('/api/automations/' + encodeURIComponent(rule.id) + '/delete',
                {method: 'POST'});
      refresh(true);
    };
    btns.appendChild(toggle); btns.appendChild(del);
    card.appendChild(btns);
    side.appendChild(card);
  }

  side.appendChild(el('h4', null, 'Events (' + events.length + ')'));
  if (!events.length) { empty(side, 'No inbox events.'); }
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
    card.appendChild(el('div', 'small', e.id +
      '  |  external ' + e.external_id));
    if (e.status !== 'promoted' && e.status !== 'ignored') {
      const btns = el('div', 'btns');
      const ignore = el('button', 'no', 'Ignore');
      ignore.onclick = async () => {
        const out = await api('/api/inbox/' + encodeURIComponent(e.id) +
          '/ignore', {method: 'POST', body: {reason: 'ignored in UI'}});
        if (out) { toast('Event ignored.'); refresh(true); }
      };
      btns.appendChild(ignore);
      card.appendChild(btns);
    }
    side.appendChild(card);
  }

  if (fires.length) {
    const card = el('div', 'card');
    card.appendChild(el('h3', null, 'Fire history'));
    for (const f of fires.slice(0, 20)) {
      card.appendChild(el('div', 'small',
        short(f.fired_at) + '  ' + (f.automation_name || '(default)') + '  ' +
        f.status + '  ' + (f.event_title || f.inbox_event_id) +
        (f.reason ? '  |  ' + f.reason : '')));
    }
    side.appendChild(card);
  }
}

/* ----------------------------------------------------------------- memory */

function renderMemory(side) {
  const counts = state.counts || {};
  const card0 = el('div', 'card');
  card0.appendChild(el('h3', null, 'Memory'));
  const grid = el('div', 'split');
  for (const [k, v] of [['active', counts.memories || 0],
                        ['archived', counts.archived_memories || 0]]) {
    const stat = el('div', 'stat');
    stat.appendChild(el('div', 'n', pct(v)));
    stat.appendChild(el('div', 'k', k));
    grid.appendChild(stat);
  }
  card0.appendChild(grid);
  card0.appendChild(el('div', 'small',
    'Usage determines retention. Unused memories are archived; archived ' +
    'memories stay retrievable and can be restored. Pinned memories and ' +
    'protected kinds never archive.'));

  const filters = el('div', 'btns');
  for (const [label, value] of [['Active', 'active'], ['Archived', 'archived']]) {
    const b = el('button', memoryFilter === value ? 'ok' : null, label);
    b.onclick = () => { memoryFilter = value; renderSide(true); };
    filters.appendChild(b);
  }
  card0.appendChild(filters);

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
  const archivedBox = el('input');
  archivedBox.type = 'checkbox';
  const archivedLabel = el('label', 'small', ' include archived');
  archivedLabel.prepend(archivedBox);
  const go = el('button', null, 'Search');
  go.onclick = async () => {
    if (!query.value.trim()) { memorySearch = null; renderSide(true); return; }
    const out = await api('/api/memories/search',
      {method: 'POST', body: {
        query: query.value.trim(),
        include_archived: archivedBox.checked,
      }});
    if (out) { memorySearch = out; renderSide(true); }
  };
  query.onkeydown = (e) => { if (e.key === 'Enter') go.onclick(); };
  searchRow.appendChild(query); searchRow.appendChild(archivedLabel);
  searchRow.appendChild(go);
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

  const list = memoryFilter === 'archived'
    ? (state.memories_archived || []) : (state.memories || []);
  if (!list.length) {
    empty(side, memoryFilter === 'archived'
      ? 'No archived memories.' : 'No active memories.');
    return;
  }
  for (const m of list) {
    const card = el('div', 'card');
    const flags = [];
    if (m.pinned) flags.push('pinned');
    if (m.archived) flags.push('archived');
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
      const restore = el('button', 'ok', 'Restore');
      restore.onclick = async () => {
        const out = await api('/api/memories/' +
          encodeURIComponent(m.id) + '/restore', {method: 'POST'});
        if (out) { toast('Memory restored.'); refresh(true); }
      };
      actions.appendChild(restore);
    } else {
      const archive = el('button', 'no', 'Archive');
      archive.onclick = async () => {
        if (!confirm('Archive this memory? It stays retrievable and can be ' +
                     'restored.')) return;
        const out = await api('/api/memories/' +
          encodeURIComponent(m.id) + '/archive', {method: 'POST'});
        if (out) { toast('Memory archived.'); refresh(true); }
      };
      actions.appendChild(archive);
    }
    const pin = el('button', null, m.pinned ? 'Unpin' : 'Pin');
    pin.onclick = async () => {
      const out = await api('/api/memories/' + encodeURIComponent(m.id) +
        (m.pinned ? '/unpin' : '/pin'), {method: 'POST'});
      if (out) { toast(m.pinned ? 'Memory unpinned.' : 'Memory pinned.'); refresh(true); }
    };
    actions.appendChild(pin);
    card.appendChild(actions);
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
    'System time ' + short(state.now) + '  |  timezone ' +
    (state.system_timezone || 'local')));
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
