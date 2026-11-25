const collectionStates = {
  idle: '尚未采集', running: '采集中', succeeded: '采集完成', partial: '部分完成',
  failed: '采集失败', interrupted: '上次采集中断', unconfigured: '等待配置数据接口',
};
const eventStates = {watch: '观察', active: '跟踪', archived: '归档'};
const evidenceTypes = {full_text: '原文', summary: '摘要', search_snippet: '搜索摘要', headline: '标题'};
const versionTypes = {
  new_evidence: '新增证据', source_revision: '原来源修订，待复核',
  older_source_version: '补录旧版证据', manual_review: '人工判断',
};

function node(tag, text) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  return element;
}

function displayTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '未知';
}

function renderCollection(status) {
  $('universe').textContent = status.universe.join(' · ');
  const last = status.last_successful_scan ? displayTime(status.last_successful_scan) : '暂无';
  $('scanstatus').textContent = `${collectionStates[status.scan_status]} · 最近完整采集 ${last} · 定时扫描${status.schedule_enabled?'开启':'关闭'}`;
  const output = $('collection-results');
  output.replaceChildren();
  for (const [kind, result] of Object.entries(status.last_collection?.result || {})) {
    const summary = [];
    if (result.message) summary.push(result.message);
    if (result.requests != null) summary.push(`${result.requests} 次请求`);
    if (result.added != null) summary.push(`新增 ${result.added}，未变化 ${result.unchanged}`);
    if (result.revisions != null) summary.push(`修订 ${result.revisions}，重复来源 ${result.duplicates}，无效 ${result.invalid}`);
    if (result.continuation) summary.push('下次从保存的新闻分页继续');
    if (result.covered_until) summary.push(`新闻窗口已覆盖至 ${displayTime(result.covered_until)}`);
    if (result.missing_symbols?.length) summary.push(`本轮缺少 ${result.missing_symbols.join('、')}`);
    output.append(node('p', `${labels[kind]} · ${summary.join(' · ')}`));
  }
}

function renderMarkets(observations) {
  const output = $('market-observations');
  output.replaceChildren();
  if (!observations.length) {
    output.append(node('p', '暂无已采集的有效成交。'));
    return;
  }
  const table = node('table');
  const head = table.createTHead().insertRow();
  for (const title of ['标的', '品种', '成交价', '行情源', '成交时间']) head.append(node('th', title));
  const body = table.createTBody();
  for (const item of observations) {
    const kind = item.proxy_for ? `商品 ETP · ${item.proxy_for}代理` : item.instrument_type === 'etf' ? 'ETF' : '股票';
    const row = body.insertRow();
    for (const value of [item.symbol, kind, `${item.price} ${item.currency}/份`, item.feed, displayTime(item.observed_at)]) {
      row.append(node('td', value));
    }
  }
  output.append(table);
}

function renderEvents(events) {
  const output = $('event-list');
  output.replaceChildren();
  if (!events.length) {
    output.append(node('p', '暂无事件。可在灵石区配置数据源后运行手动采集。'));
    return;
  }
  const list = node('ul');
  for (const event of events) {
    const link = node('button', (event.assets.map(a=>a.symbol).join('、')||'待确认资产')+' · 事件档案');
    link.type = 'button';
    link.onclick = () => showEvent(event.event_id).catch(error => notice(error.message));
    const symbols = event.assets.map(asset => `${asset.symbol}${asset.in_universe ? '' : '（池外）'}`);
    const entry = node('li');
    entry.append(link, node('small', `${eventStates[event.state] || event.state} · ${event.evidence_count} 条证据 · ${symbols.join('、') || '待确认标的'}`));
    list.append(entry);
  }
  output.append(list);
}

function evidenceCard(evidence) {
  const details = node('details');
  const labels = [evidenceTypes[evidence.body_kind], evidence.duplicate_of ? '重复来源' : '',
                  evidence.supersedes_id ? '修订' : ''].filter(Boolean);
  details.append(node('summary', `新闻原始材料 · ${labels.join(' · ')}`));
  const times = [
    `事件发生 ${displayTime(evidence.occurred_at)}`, `首次公开 ${displayTime(evidence.first_public_at)}`,
    `版本公开 ${displayTime(evidence.version_public_at)}`, `系统获取 ${displayTime(evidence.acquired_at)}`,
  ];
  details.append(node('p', `${evidence.publisher || evidence.provider} · ${times.join(' · ')}`));
  if (evidence.source_url && /^https?:\/\//i.test(evidence.source_url)) {
    const source = node('a', '打开原来源');
    source.href = evidence.source_url;
    source.rel = 'noopener noreferrer';
    source.target = '_blank';
    details.append(source);
  }
  details.append(node('p',evidence.headline),node('pre', evidence.body || '来源仅提供标题'));
  details.append(node('small', `证据 ID ${evidence.id}`));
  return details;
}

function reviewForm(event) {
  const form = node('form');
  const previous = event.versions.find(version => version.result.type === 'manual_review')?.result;
  const stateField = node('label', '事件状态');
  const state = node('select');
  for (const [key, label] of Object.entries(eventStates)) {
    const option = node('option', label);
    option.value = key;
    state.append(option);
  }
  state.value = event.state;
  stateField.append(state);
  const judgmentField = node('label', '当前判断');
  const judgment = node('textarea');
  judgment.maxLength = 5000;
  judgment.rows = 3;
  judgment.value = previous?.judgment || '';
  judgmentField.append(judgment);
  const conflictsField = node('label', '未解决的矛盾（每行一条）');
  const conflicts = node('textarea');
  conflicts.rows = 3;
  conflicts.value = previous?.unresolved_conflicts?.join('\n') || '';
  conflictsField.append(conflicts);
  const idsField = node('label', '关联证据 ID（可选，每行一个，可填其他档案中的证据）');
  const ids = node('textarea');
  ids.rows = 3;
  ids.value = previous?.evidence_ids?.join('\n') || '';
  idsField.append(ids);
  const save = node('button', '保存判断版本');
  save.type = 'submit';
  form.append(stateField, judgmentField, conflictsField, idsField, save);
  form.onsubmit = async change => {
    change.preventDefault();
    save.disabled = true;
    try {
      const lines = value => value.split('\n').map(line => line.trim()).filter(Boolean);
      await api(`/api/events/${event.event_id}/reviews`, 'POST', {
        state: state.value, judgment: judgment.value,
        unresolved_conflicts: lines(conflicts.value), evidence_ids: lines(ids.value),
      });
      renderEvents(await api('/api/events'));
      await showEvent(event.event_id);
      notice('判断和矛盾已保存为新版本');
    } catch (error) {
      notice(error.message);
    } finally {
      save.disabled = false;
    }
  };
  return form;
}

async function showEvent(id) {
  const panel = $('event-detail');
  const event = await api('/api/events/' + id);
  const close = node('button', '关闭档案');
  close.onclick = () => {panel.hidden = true;};
  panel.replaceChildren(node('h3', '事件档案'), close,
                        node('h4', '证据时间线'), ...event.evidence.map(evidenceCard), node('h4', '变化与判断历史'));
  for (const version of event.versions) {
    const record = node('details');
    const result = version.result;
    record.append(node('summary', `${displayTime(version.created_at)} · ${versionTypes[result.type] || result.type}`));
    if (result.type === 'manual_review') {
      record.append(node('p', eventStates[result.state]), node('pre', result.judgment || '未填写判断'));
      for (const conflict of result.unresolved_conflicts) record.append(node('p', `未解决 · ${conflict}`));
    } else {
      record.append(node('small', `证据 ${result.evidence_id}`));
    }
    panel.append(record);
  }
  panel.append(node('h4', '追加人工判断'), reviewForm(event));
  panel.hidden = false;
  panel.scrollIntoView({block: 'nearest'});
}

async function loadResearch(status) {
  const [events, markets, current] = await Promise.all([
    api('/api/events'), api('/api/market/observations/latest'), status || api('/api/status'),
  ]);
  renderCollection(current);
  renderEvents(events);
  renderMarkets(markets);
}

async function submitCollection(event) {
  event.preventDefault();
  const button = $('collect-button');
  button.disabled = true;
  $('scanstatus').textContent = '采集中，正在检查已配置的数据源。';
  try {
    await api('/api/collections', 'POST', {query: $('collection-query').value});
    await Promise.all([loadResearch(), loadUsage()]);
  } catch (error) {
    notice(error.message);
    await loadResearch().catch(() => {});
  } finally {
    button.disabled = false;
  }
}
