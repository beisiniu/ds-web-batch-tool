const TOKEN = document.querySelector('meta[name="app-token"]').content;
const $ = (id) => document.getElementById(id);

const DOMAINS = {
  journal: {
    label: '期刊',
    sub: '官网 / 收录平台',
    match: 'journal_name',
    fields: [
      { key: 'journal_name', desc: '期刊名称，必须原样保留我给出的名称，不要翻译、改写、增删括号' },
      { key: 'official_url', desc: '期刊官方网站或主办单位官网 URL；没有独立官网时，填主要收录/采编平台的 URL' },
      { key: 'provider', desc: '对应的网站供应商/出版平台/收录平台，例如 自有官网、Springer、中国知网、维普、万方' },
      { key: 'source_notes', desc: '判断依据与备注；同名不同刊、更名、无独立官网等情况必须写明' },
    ],
  },
  institution: {
    label: '机构',
    sub: '官网 / 类型 / 地区',
    match: 'name',
    fields: [
      { key: 'name', desc: '机构名称，必须原样保留我给出的名称' },
      { key: 'official_url', desc: '机构官方网站 URL' },
      { key: 'country', desc: '所在国家/地区' },
      { key: 'type', desc: '机构类型，例如 高校、研究所、企业、政府机构' },
      { key: 'source_notes', desc: '判断依据与备注；同名机构必须写明如何区分' },
    ],
  },
  scholar: {
    label: '学者',
    sub: '机构 / 主页 / 方向',
    match: 'name',
    fields: [
      { key: 'name', desc: '姓名，必须原样保留我给出的写法' },
      { key: 'institution', desc: '当前任职机构，院系/实验室尽量写全' },
      { key: 'homepage', desc: '个人主页或机构师资页 URL；没有则填 Google Scholar、ORCID 等学术档案页' },
      { key: 'research_area', desc: '研究方向，用分号分隔' },
      { key: 'source_notes', desc: '判断依据与备注；同名学者必须写明如何区分' },
    ],
  },
  custom: {
    label: '自由领域',
    sub: '自己写提示词',
    match: '',
    fields: [{ key: '', desc: '' }],
  },
};

const state = {
  upload: null,
  domain: 'journal',
  fields: [],
  customPreamble: false,
  preambleText: '',
  preambleTimer: null,
  task: null,
  pollTimer: null,
  lastJob: null,
  sessionOk: false,
  busy: false,
  tasks: [],
  accounts: [],
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const headers = new Headers(options.headers || {});
  if (method !== 'GET') headers.set('X-App-Token', TOKEN);
  let body = options.body;
  if (options.json !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(options.json);
  }
  if (options.form !== undefined) body = options.form;
  const res = await fetch(path, { method, headers, body });
  let data = null;
  try {
    data = await res.json();
  } catch (error) {
    data = null;
  }
  if (!res.ok) throw new Error((data && data.error) || `请求失败（${res.status}）`);
  return data;
}

let toastTimer = null;
function toast(message, type = '') {
  const box = $('toast');
  box.textContent = message;
  box.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add('hidden'), 4000);
}

function setPill(node, text, cls) {
  node.textContent = text;
  node.className = `pill ${cls || 'muted'}`;
}

function fillSelect(select, options, selected) {
  select.innerHTML = '';
  options.forEach((option) => {
    const node = document.createElement('option');
    node.value = option.value;
    node.textContent = option.label;
    select.appendChild(node);
  });
  if (selected !== undefined && selected !== null) select.value = String(selected);
}

function displayPreamble(text) {
  const size = Number($('batchSize').value) || 50;
  return (text || '').split('{batch_size}').join(String(size));
}

function schedulePreamble() {
  if (state.customPreamble) return;
  clearTimeout(state.preambleTimer);
  state.preambleTimer = setTimeout(refreshPreamble, 250);
}

async function refreshPreamble() {
  if (state.customPreamble) return;
  const fields = state.fields
    .map((field) => ({ key: field.key.trim(), desc: field.desc.trim() }))
    .filter((field) => field.key);
  try {
    const data = await api('/api/preamble', {
      method: 'POST',
      json: {
        domain: state.domain,
        fields,
        match_field: $('matchSelect').value,
        notes: $('domainNotes').value,
        custom_prompt: $('customPrompt').value,
      },
    });
    state.preambleText = data.preamble || '';
    updatePreambleDisplay();
  } catch (error) {
    // 预览失败不打断编辑，保存时后端还会再生成一次
  }
}

function updatePreambleDisplay() {
  if (state.customPreamble) return;
  $('preamblePreview').textContent = displayPreamble(state.preambleText || '正在生成任务说明...');
}

function renderDomains() {
  const row = $('domainRow');
  row.innerHTML = '';
  Object.entries(DOMAINS).forEach(([key, item]) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `domain-card${state.domain === key ? ' active' : ''}`;
    const title = document.createElement('b');
    title.textContent = item.label;
    const sub = document.createElement('span');
    sub.textContent = item.sub;
    card.append(title, sub);
    card.addEventListener('click', () => selectDomain(key));
    row.appendChild(card);
  });
}

function selectDomain(domain) {
  state.domain = domain;
  state.fields = clone(DOMAINS[domain].fields);
  state.customPreamble = false;
  $('editPreambleCheck').checked = false;
  $('preambleEdit').classList.add('hidden');
  $('preamblePreview').classList.remove('hidden');
  $('preambleEditHint').classList.add('hidden');
  $('domainNotes').value = '';
  $('customPrompt').value = '';
  $('domainNotesField').classList.toggle('hidden', domain === 'custom');
  $('customPromptField').classList.toggle('hidden', domain !== 'custom');
  renderDomains();
  renderFields();
  if (DOMAINS[domain].match) $('matchSelect').value = DOMAINS[domain].match;
  refreshPreamble();
}

function renderFields() {
  const wrap = $('fieldRows');
  wrap.innerHTML = '';
  state.fields.forEach((field, index) => {
    const row = document.createElement('div');
    row.className = 'field-row';

    const key = document.createElement('input');
    key.type = 'text';
    key.placeholder = '字段名，如 journal_name';
    key.value = field.key;
    key.addEventListener('input', () => {
      state.fields[index].key = key.value;
      updateMatchOptions();
      schedulePreamble();
    });

    const desc = document.createElement('input');
    desc.type = 'text';
    desc.placeholder = '可选：这个字段具体指什么';
    desc.value = field.desc;
    desc.addEventListener('input', () => {
      state.fields[index].desc = desc.value;
      schedulePreamble();
    });

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'del';
    del.textContent = '×';
    del.title = '删除字段';
    del.addEventListener('click', () => {
      if (state.fields.length <= 1) {
        toast('至少保留一个字段', 'error');
        return;
      }
      state.fields.splice(index, 1);
      renderFields();
      schedulePreamble();
    });

    row.append(key, desc, del);
    wrap.appendChild(row);
  });
  updateMatchOptions();
}

function updateMatchOptions() {
  const select = $('matchSelect');
  const current = select.value;
  const keys = [];
  state.fields.forEach((field) => {
    const key = field.key.trim();
    if (key && !keys.includes(key)) keys.push(key);
  });
  fillSelect(
    select,
    keys.map((key) => ({ value: key, label: key })),
    keys.includes(current) ? current : keys[0],
  );
}

function renderPreview() {
  const table = $('previewTable');
  table.innerHTML = '';
  if (!state.upload || !state.upload.headers || !state.upload.headers.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.className = 'table-empty';
    cell.textContent = '没有预览数据';
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  state.upload.headers.forEach((header) => {
    const th = document.createElement('th');
    th.textContent = header;
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement('tbody');
  (state.upload.rows || []).forEach((row) => {
    const tr = document.createElement('tr');
    row.forEach((value) => {
      const td = document.createElement('td');
      td.textContent = value;
      td.title = value;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
}

function renderColumnOptions(selected) {
  const select = $('columnSelect');
  const headers = (state.upload && state.upload.headers) || [];
  const options = headers.map((header, index) => ({
    value: index + 1,
    label: `${index + 1}. ${header}`,
  }));
  fillSelect(select, options, selected || 1);
}

async function handleFile(file) {
  if (!file) return;
  $('dropZone').textContent = '正在读取文件...';
  const form = new FormData();
  form.append('file', file);
  try {
    const data = await api('/api/upload', { method: 'POST', form });
    state.upload = data;
    $('uploadHero').classList.add('hidden');
    $('editorBody').classList.remove('hidden');
    $('fileSummary').textContent = `${data.original} · 已读取预览`;
    if (!$('taskName').value.trim()) {
      $('taskName').value = data.original.replace(/\.[^.]+$/, '').slice(0, 80);
    }
    const sheets = data.sheets || [];
    if (sheets.length) {
      $('sheetField').classList.remove('hidden');
      fillSelect($('sheetSelect'), sheets.map((name, index) => ({ value: index, label: name })), 0);
    } else {
      $('sheetField').classList.add('hidden');
      fillSelect($('sheetSelect'), [{ value: 0, label: '默认' }], 0);
    }
    renderColumnOptions(1);
    renderPreview();
    toast('文件读取成功', 'ok');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    $('dropZone').textContent = '也可以直接把文件拖到这里';
    $('fileInput').value = '';
  }
}

async function reloadPreview() {
  if (!state.upload) return;
  const sheet = Number($('sheetSelect').value || 0);
  const hasHeader = $('headerCheck').checked;
  try {
    const data = await api(`/api/upload/${state.upload.upload_id}/preview`, {
      method: 'POST',
      json: { sheet, has_header: hasHeader },
    });
    const previous = Number($('columnSelect').value) || 1;
    state.upload = { ...state.upload, ...data };
    renderColumnOptions(Math.min(previous, data.headers.length || 1));
    renderPreview();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function resetEditor() {
  state.upload = null;
  state.domain = 'journal';
  state.fields = clone(DOMAINS.journal.fields);
  state.customPreamble = false;
  state.preambleText = '';
  clearTimeout(state.preambleTimer);
  $('taskName').value = '';
  $('batchSize').value = 50;
  $('rotateEvery').value = 8;
  $('cooldownMin').value = 8;
  $('cooldownMax').value = 25;
  $('conversationUrl').value = '';
  $('domainNotes').value = '';
  $('customPrompt').value = '';
  $('domainNotesField').classList.remove('hidden');
  $('customPromptField').classList.add('hidden');
  $('headerCheck').checked = true;
  $('editPreambleCheck').checked = false;
  $('preambleEdit').classList.add('hidden');
  $('preamblePreview').classList.remove('hidden');
  $('preambleEdit').value = '';
  $('preambleEditHint').classList.add('hidden');
  $('fileSummary').textContent = '—';
  $('uploadHero').classList.remove('hidden');
  $('editorBody').classList.add('hidden');
  $('previewTable').innerHTML = '';
  renderDomains();
  renderFields();
  refreshPreamble();
  $('pageTitle').textContent = '新建采集任务';
  $('pageSub').textContent = '上传名单，配置字段，剩下的交给脚本';
  setPill($('jobPill'), '未运行', 'muted');
  setAccountPill('');
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  $('runView').classList.add('hidden');
  $('editorView').classList.remove('hidden');
  toggleCallout();
}

function toggleCallout() {
  const editing = !$('editorView').classList.contains('hidden');
  $('sessionCallout').classList.toggle('hidden', state.sessionOk || !editing);
}

function showRunView(task) {
  state.task = task;
  $('editorView').classList.add('hidden');
  $('runView').classList.remove('hidden');
  $('sessionCallout').classList.add('hidden');
  $('pageTitle').textContent = '任务详情';
  $('pageSub').textContent = '进度、日志与结果都在这里';
  $('runTitle').textContent = task.name || '任务';
  $('runSub').textContent = `${task.total || 0} 条 · 每批 ${task.batch_size || 50} 条 · ${task.fields_count || (task.fields || []).length} 个字段`;
  updateCounts(task.counts || { ok: 0, failed: 0, pending: task.total || 0, total: task.total || 0 });
  setJobLabel(task.job || 'pending');
  setAccountPill((task.state && task.state.active_account) || task.active_account || '');
  if (task.log) setLog(task.log);
  updateDownloads(task.id, false, false);
  startPolling(task.id);
  loadResults(task.id);
  renderTaskList();
}

function setJobLabel(job) {
  const labels = {
    running: ['运行中', 'running'],
    completed: ['已完成', 'ok'],
    failed: ['异常结束', 'failed'],
    stopped: ['已停止', 'muted'],
    pending: ['未开始', 'muted'],
    idle: ['未运行', 'muted'],
  };
  const [text, cls] = labels[job] || labels.idle;
  setPill($('jobPill'), text, cls);
  setPill($('jobStateText'), text, cls);
  const running = job === 'running';
  $('runStopBtn').disabled = !running;
  $('runStartBtn').disabled = running;
  $('runTestBtn').disabled = running;
}

function updateCounts(counts) {
  $('statOk').textContent = counts.ok || 0;
  $('statFailed').textContent = counts.failed || 0;
  $('statPending').textContent = counts.pending || 0;
  const total = counts.total || 0;
  const done = (counts.ok || 0) + (counts.failed || 0);
  const pct = total ? Math.round((done / total) * 100) : 0;
  $('statPct').textContent = `${pct}%`;
  $('progressFill').style.width = `${pct}%`;
}

function updateVerifyBanner(text) {
  const box = $('verifyBanner');
  if (!box) return;
  const lastWarn = text.lastIndexOf('安全验证');
  const lastOk = text.lastIndexOf('安全验证已通过');
  box.classList.toggle('hidden', !(lastWarn >= 0 && lastWarn > lastOk));
}

function setLog(text) {
  const box = $('logBox');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.textContent = text || '等待任务开始...';
  updateVerifyBanner(text || '');
  if (stick) box.scrollTop = box.scrollHeight;
}

function setDownload(link, available, url) {
  if (available) {
    link.href = url;
    link.dataset.ok = '1';
    link.classList.remove('disabled');
  } else {
    link.href = '#';
    link.dataset.ok = '';
    link.classList.add('disabled');
  }
}

function updateDownloads(taskId, hasJson, hasCsv) {
  setDownload($('dlJson'), hasJson, `/api/tasks/${taskId}/download/json`);
  setDownload($('dlCsv'), hasCsv, `/api/tasks/${taskId}/download/csv`);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function startPolling(taskId) {
  stopPolling();
  state.lastJob = null;
  const tick = async () => {
    try {
      const data = await api(`/api/tasks/${taskId}/status`);
      if (!state.task || state.task.id !== taskId) return;
      state.task = { ...state.task, ...data.task };
      updateCounts(data.counts || {});
      setJobLabel(data.job);
      setAccountPill((data.state && data.state.active_account) || '');
      setLog(data.log || '');
      updateDownloads(taskId, Boolean(data.has_json), Boolean(data.has_csv));
      if (data.job !== 'running') {
        stopPolling();
        loadTasks();
        loadResults(taskId);
        if (state.lastJob === 'running') {
          if (data.job === 'completed') toast('任务已完成', 'ok');
          else if (data.job === 'failed') toast('任务异常结束，请查看日志', 'error');
          else toast('任务已停止，进度已保存', 'ok');
        }
        state.lastJob = data.job;
      } else {
        state.lastJob = 'running';
      }
    } catch (error) {
      // 网络抖动时继续轮询，连续失败由用户手动刷新处理
    }
  };
  tick();
  state.pollTimer = setInterval(tick, 2000);
}

async function loadResults(taskId) {
  try {
    const data = await api(`/api/tasks/${taskId}/preview`);
    renderResultTable(data);
    $('resultHint').textContent = data.total
      ? `显示前 ${data.records.length} 条，共 ${data.total} 条已完成`
      : '还没有完成的记录';
  } catch (error) {
    $('resultHint').textContent = '读取结果预览失败';
  }
}

function renderResultTable(data) {
  const table = $('resultTable');
  table.innerHTML = '';
  const fields = data.fields || [];
  const records = data.records || [];
  if (!fields.length || !records.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.className = 'table-empty';
    cell.textContent = '还没有完成的记录';
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  fields.forEach((field) => {
    const th = document.createElement('th');
    th.textContent = field.key;
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement('tbody');
  records.forEach((record) => {
    const tr = document.createElement('tr');
    fields.forEach((field) => {
      const td = document.createElement('td');
      const value = record[field.key] == null ? '' : String(record[field.key]);
      td.textContent = value;
      td.title = value;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
}

function renderTaskList() {
  const box = $('taskList');
  box.innerHTML = '';
  if (!state.tasks.length) {
    const empty = document.createElement('div');
    empty.className = 'task-empty';
    empty.textContent = '还没有任务';
    box.appendChild(empty);
    return;
  }
  const labels = { running: '运行中', completed: '已完成', failed: '异常', stopped: '已停止', pending: '未开始', idle: '未运行' };
  state.tasks.forEach((task) => {
    const item = document.createElement('div');
    item.className = 'task-item' + (state.task && state.task.id === task.id ? ' active' : '');
    const top = document.createElement('div');
    top.className = 'task-item-top';
    const dot = document.createElement('span');
    dot.className = `dot ${task.job || 'pending'}`;
    const name = document.createElement('span');
    name.className = 'task-item-name';
    name.textContent = task.name || '未命名任务';
    top.append(dot, name);
    const meta = document.createElement('div');
    meta.className = 'task-item-meta';
    meta.textContent = `${task.counts.ok}/${task.total} 完成 · ${labels[task.job] || task.job}`;
    item.append(top, meta);
    item.addEventListener('click', () => loadTask(task.id));
    box.appendChild(item);
  });
}

async function loadTasks() {
  try {
    state.tasks = await api('/api/tasks');
    renderTaskList();
  } catch (error) {
    // 忽略列表加载失败，不打断主流程
  }
}

async function loadTask(taskId) {
  try {
    const data = await api(`/api/tasks/${taskId}`);
    showRunView(data);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function createTask(mode) {
  if (state.busy) return;
  if (!state.upload) {
    toast('请先上传名单文件', 'error');
    return;
  }
  const fields = state.fields
    .filter((field) => field.key.trim())
    .map((field) => ({ key: field.key.trim(), desc: field.desc.trim() }));
  if (!fields.length) {
    toast('至少配置一个字段', 'error');
    return;
  }
  for (const field of fields) {
    if (!field.key) {
      toast('每个字段都要填写字段名', 'error');
      return;
    }
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(field.key)) {
      toast(`字段名「${field.key}」不合法：请用英文、数字、下划线，且不以数字开头`, 'error');
      return;
    }
  }
  const keys = fields.map((field) => field.key);
  if (new Set(keys).size !== keys.length) {
    toast('字段名不能重复', 'error');
    return;
  }
  if (state.domain === 'custom' && !state.customPreamble && !$('customPrompt').value.trim()) {
    toast('自由领域需要填写任务提示词', 'error');
    return;
  }
  const match = $('matchSelect').value;
  if (!match || !keys.includes(match)) {
    toast('请选择用于对齐输入名称的字段', 'error');
    return;
  }
  const column = Number($('columnSelect').value);
  if (!column) {
    toast('请选择名称所在列', 'error');
    return;
  }
  const batchSize = Number($('batchSize').value);
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 500) {
    toast('每批条数需要在 1~500 之间', 'error');
    return;
  }
  const preamble = state.customPreamble ? $('preambleEdit').value.trim() : '';
  if (state.customPreamble && !preamble) {
    toast('任务说明不能为空', 'error');
    return;
  }
  if (mode !== 'save' && !state.sessionOk) {
    const go = confirm('还没有导入登录状态，任务可能停在登录页等待。仍要开始吗？');
    if (!go) return;
  }

  state.busy = true;
  $('saveAndStartBtn').disabled = true;
  $('testRunBtn').disabled = true;
  try {
    const task = await api('/api/tasks', {
      method: 'POST',
      json: {
        name: $('taskName').value.trim(),
        upload_id: state.upload.upload_id,
        sheet: Number($('sheetSelect').value || 0),
        has_header: $('headerCheck').checked,
        column,
        fields,
        match_field: match,
        batch_size: batchSize,
        rotate_every_batches: Number($('rotateEvery').value),
        cooldown_sec: [Number($('cooldownMin').value), Number($('cooldownMax').value)],
        conversation_url: $('conversationUrl').value.trim(),
        domain: state.domain,
        notes: $('domainNotes').value.trim(),
        custom_prompt: $('customPrompt').value.trim(),
        preamble,
      },
    });
    state.task = task;
    showRunView(task);
    await loadTasks();
    if (mode !== 'save') await startJob(task.id, mode === 'test' ? 3 : null);
    else toast('任务已保存，可以稍后点“开始 / 继续”', 'ok');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    state.busy = false;
    $('saveAndStartBtn').disabled = false;
    $('testRunBtn').disabled = false;
  }
}

async function startJob(taskId, limit) {
  try {
    await api(`/api/tasks/${taskId}/start`, { method: 'POST', json: { limit } });
    toast(limit ? `已开始试跑 ${limit} 条` : '任务已启动', 'ok');
    startPolling(taskId);
    loadTasks();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function stopJob() {
  if (!state.task) return;
  if (!confirm('停止后已完成的数据会保留，确定停止吗？')) return;
  try {
    await api(`/api/tasks/${state.task.id}/stop`, { method: 'POST' });
    toast('已停止，进度已保存', 'ok');
    startPolling(state.task.id);
    loadTasks();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function refreshExport() {
  if (!state.task) return;
  try {
    const data = await api(`/api/tasks/${state.task.id}/export`, { method: 'POST' });
    if (data.ok) {
      toast('结果文件已刷新', 'ok');
    } else {
      toast('导出失败，请查看日志', 'error');
    }
    startPolling(state.task.id);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadSessionStatus() {
  try {
    const data = await api('/api/session/status');
    state.sessionOk = Boolean(data.ok);
    if (data.ok) {
      setPill($('sessionPill'), `登录正常 · ${data.time || ''}`, 'ok');
    } else {
      setPill($('sessionPill'), '登录状态未导入', 'warn');
    }
  } catch (error) {
    setPill($('sessionPill'), '登录状态未检测', 'muted');
  }
  toggleCallout();
}

async function importSession() {
  const button = $('importSessionBtn');
  const spinner = $('importSpinner');
  const output = $('importOutput');
  const screenshot = $('importScreenshot');
  button.disabled = true;
  spinner.classList.remove('hidden');
  output.classList.add('hidden');
  screenshot.classList.add('hidden');
  try {
    const data = await api('/api/session/import', { method: 'POST' });
    output.textContent = data.output || '';
    output.classList.remove('hidden');
    if (data.screenshot) {
      screenshot.href = data.screenshot + `?t=${Date.now()}`;
      screenshot.classList.remove('hidden');
    }
    if (data.ok) {
      state.sessionOk = true;
      setPill($('sessionPill'), `登录正常 · ${new Date().toLocaleString()}`, 'ok');
      toggleCallout();
      toast('登录状态导入成功', 'ok');
    } else {
      toast('导入失败，请看下方输出', 'error');
    }
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
    spinner.classList.add('hidden');
  }
}

async function loadAccounts() {
  try {
    const data = await api('/api/accounts');
    state.accounts = data.accounts || [];
  } catch (error) {
    state.accounts = [];
  }
  renderAccounts();
}

function renderAccounts() {
  const list = $('accountList');
  if (list) {
    list.innerHTML = '';
    if (!state.accounts.length) {
      const empty = document.createElement('div');
      empty.className = 'micro';
      empty.textContent = '还没有账号，先在下面添加。';
      list.appendChild(empty);
    } else {
      state.accounts.forEach((account) => {
        const item = document.createElement('div');
        item.className = 'account-item';
        const name = document.createElement('b');
        name.textContent = account.name;
        const path = document.createElement('span');
        path.textContent = account.profile_dir;
        path.title = account.profile_dir;
        item.append(name, path);
        list.appendChild(item);
      });
    }
  }
  const hint = $('accountPoolHint');
  if (hint) {
    hint.textContent = state.accounts.length
      ? `账号池：${state.accounts.map((account) => account.name).join(' / ')}（卡住或限流自动轮换）`
      : '账号池：未配置';
  }
}

function setAccountPill(name) {
  const pill = $('accountPill');
  if (!pill) return;
  pill.textContent = name ? `账号：${name}` : '账号：—';
  pill.className = name ? 'pill ok' : 'pill muted';
}

async function addAccount() {
  const name = $('newAccountName').value.trim();
  if (!name) {
    toast('先填写账号名称', 'error');
    return;
  }
  const button = $('addAccountBtn');
  button.disabled = true;
  $('addAccountSpinner').classList.remove('hidden');
  $('addAccountOutput').classList.add('hidden');
  try {
    const data = await api('/api/accounts', { method: 'POST', json: { name } });
    $('addAccountOutput').textContent = data.output || '';
    $('addAccountOutput').classList.remove('hidden');
    if (data.ok) {
      state.accounts = data.accounts || [];
      renderAccounts();
      $('newAccountName').value = '';
      toast('账号已加入账号池', 'ok');
    } else {
      toast('添加失败，请查看下方输出', 'error');
    }
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
    $('addAccountSpinner').classList.add('hidden');
  }
}

function openLogin() {
  $('loginModal').classList.remove('hidden');
  loadAccounts();
}

function closeLogin() {
  $('loginModal').classList.add('hidden');
}

function bindEvents() {
  $('newTaskBtn').addEventListener('click', () => {
    state.task = null;
    resetEditor();
  });
  $('fileInput').addEventListener('change', (event) => {
    const file = event.target.files && event.target.files[0];
    if (file) handleFile(file);
  });
  $('reuploadBtn').addEventListener('click', () => $('fileInput').click());
  ['dragenter', 'dragover'].forEach((name) => {
    $('uploadHero').addEventListener(name, (event) => {
      event.preventDefault();
    });
  });
  $('uploadHero').addEventListener('drop', (event) => {
    event.preventDefault();
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) handleFile(file);
  });
  $('sheetSelect').addEventListener('change', reloadPreview);
  $('headerCheck').addEventListener('change', reloadPreview);
  $('addFieldBtn').addEventListener('click', () => {
    state.fields.push({ key: '', desc: '' });
    renderFields();
    schedulePreamble();
  });
  $('batchSize').addEventListener('input', updatePreambleDisplay);
  $('batchChips').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-v]');
    if (!button) return;
    $('batchSize').value = button.dataset.v;
    updatePreambleDisplay();
  });
  $('domainNotes').addEventListener('input', schedulePreamble);
  $('customPrompt').addEventListener('input', schedulePreamble);
  $('matchSelect').addEventListener('change', schedulePreamble);
  $('editPreambleCheck').addEventListener('change', (event) => {
    if (event.target.checked) {
      state.customPreamble = true;
      $('preambleEdit').value = $('preamblePreview').textContent;
      $('preambleEdit').classList.remove('hidden');
      $('preamblePreview').classList.add('hidden');
      $('preambleEditHint').classList.remove('hidden');
    } else {
      state.customPreamble = false;
      $('preambleEdit').classList.add('hidden');
      $('preamblePreview').classList.remove('hidden');
      $('preambleEditHint').classList.add('hidden');
      refreshPreamble();
    }
  });
  $('regenBtn').addEventListener('click', () => {
    state.customPreamble = false;
    $('editPreambleCheck').checked = false;
    $('preambleEdit').classList.add('hidden');
    $('preamblePreview').classList.remove('hidden');
    $('preambleEditHint').classList.add('hidden');
    refreshPreamble();
  });
  $('saveAndStartBtn').addEventListener('click', () => createTask('full'));
  $('testRunBtn').addEventListener('click', () => createTask('test'));
  $('saveOnlyBtn').addEventListener('click', () => createTask('save'));
  $('runStartBtn').addEventListener('click', () => {
    if (state.task) startJob(state.task.id, null);
  });
  $('runTestBtn').addEventListener('click', () => {
    if (state.task) startJob(state.task.id, 3);
  });
  $('runStopBtn').addEventListener('click', stopJob);
  $('refreshExportBtn').addEventListener('click', refreshExport);
  [$('dlJson'), $('dlCsv')].forEach((link) => {
    link.addEventListener('click', (event) => {
      if (link.dataset.ok !== '1') {
        event.preventDefault();
        toast('结果文件还没生成，请先运行或点“刷新结果”', 'error');
      }
    });
  });
  $('openLoginBtn').addEventListener('click', openLogin);
  $('calloutLoginBtn').addEventListener('click', openLogin);
  $('closeLoginBtn').addEventListener('click', closeLogin);
  $('importSessionBtn').addEventListener('click', importSession);
  $('addAccountBtn').addEventListener('click', addAccount);
  $('loginModal').addEventListener('click', (event) => {
    if (event.target === $('loginModal')) closeLogin();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeLogin();
  });
}

document.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  resetEditor();
  loadTasks();
  loadSessionStatus();
  loadAccounts();
});
