const money = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:2});
const percent = value => value == null ? '—' : (value * 100).toFixed(1) + '%';

function optionTable(quotes) {
  const container = node('div');
  container.className = 'table';
  const table = node('table');
  const headings = ['到期日','方向','行权价','Bid / Ask','报价时间与来源状态','最近成交价','费用后成本 / 最大亏损',
    '到期保本价','最低成功率','锁定尾部概率下界','保守至乐观净期望'];
  const head = node('tr');
  for (const label of headings) head.append(node('th',label));
  const header = node('thead');
  header.append(head);
  table.append(header);
  const body = node('tbody');
  for (const c of quotes) {
    const q = c.quote;
    const quotedAt = new Date(q.last_updated / 1e6);
    const stamp = q.last_updated && Number.isFinite(quotedAt.getTime()) ? displayTime(quotedAt.toISOString()) : '时间未知';
    const cells = [c.expiry,c.side.toUpperCase(),money(c.strike),money(q.bid)+' / '+money(q.ask),
      stamp+' · '+(q.timeframe||'状态未知'),money(c.last_trade.price),
      money(c.total_cost)+' / '+money(c.max_loss),money(c.breakeven),percent(c.probability_hurdle),
      percent(c.locked_success_lower),money(c.ev_low)+' 至 '+money(c.ev_high)];
    const line = node('tr');
    for (const text of cells) line.append(node('td',text));
    body.append(line);
  }
  table.append(body);
  container.append(table);
  return container;
}

function renderOptionDecision(d) {
  const section = node('div');
  section.append(node('p',d.signal+' · '+d.explanation),node('small',
    displayTime(d.created_at)+' · '+d.data_provider+' · '+d.rules.version+' · 快照 '+(d.probability_snapshot||'未知')));
  if (d.quote_valid_through && Date.now() > Date.parse(d.quote_valid_through)) {
    section.append(node('p','该模拟候选的报价已过时，复核后再使用。'));
  }
  if (d.note) section.append(node('p','复核依据 · '+d.note));
  if (d.previous_contract) section.append(node('p','原模拟计划合约 · '+d.previous_contract));
  if (d.partial_chain) section.append(node('p','仅展示期权链样本，数据源还有后续分页。'));
  for (const lead of d.search_leads||[]) { const p=node('p','备用检索线索 · ');const link=node('a',lead.title||'查看原始来源');link.href=lead.url;link.target='_blank';link.rel='noopener noreferrer';p.append(link);section.append(p); }
  for (const gap of d.gaps) section.append(node('p',gap));
  if (d.quotes.length) {
    section.append(node('p',(d.symbol||'')+' · 美元报价按每股，损益按每张。最近成交价单列。'),
      optionTable(d.quotes));
  }
  for(const row of d.quotes.filter(r=>r.reference_only && r.total_cost!=null))section.append(node('p',row.ticker+' · '+row.calculation_basis+' · 历史或延迟研究参考，待更新报价确认。'));
  const context = d.selected || d.quotes.find(row => row.probability_hurdle != null);
  if (context) {
    section.append(node('h5',(d.selected?'模拟候选':'赔率观察')+' · '+context.ticker),
      node('p','Ask 权利金 '+money(context.ask_debit)+' USD · 往返费用 '+money(context.fees)+
        ' USD · 压力成本 '+money(context.stressed_cost)+' USD · 目标退出概率 '+intervalView(context.exit_probability)),
      node('p','二元估值成功回收 '+money(context.success_gross)+' USD · 失败回收 '+money(context.failure_gross)+' USD'),
      node('p','风险检查 · '+Object.entries(context.checks).map(([name,ok])=>name+' '+(ok?'通过':'待核验')).join('、')+
        '、反证门槛 '+(d.risk_gate?'通过':'待核验')));
    if(context.max_acceptable_ask!=null)section.append(node('p','按锁定情景和风险预算反推，最高可接受 Ask '+money(context.max_acceptable_ask)+' USD/份，需更新报价及执行条件确认。'));
    if(context.sensitivity)section.append(node('p','事件错过到期窗口的归零压力损益 '+money(context.sensitivity.event_misses_expiry_net)+' USD；价格反应不足的基准压力损益 '+money(context.sensitivity.insufficient_move_net)+' USD。'));
    const labels = {base:'基准',up:'上行尾部',down:'下行尾部'};
    for (const [branch, s] of Object.entries(context.payoffs)) {
      section.append(node('p',labels[branch]+' · 到期标的价 '+money(s.price.low)+' 至 '+money(s.price.high)+
        ' USD · 每张净损益 '+money(s.net_low)+' 至 '+money(s.net_high)+' USD'));
    }
  }
  return section;
}

function optionsView(report) {
  const area = node('section');
  area.append(node('h4','期权赔率与模拟决策'));
  const [current,...history] = report.decisions || [];
  if (current) area.append(renderOptionDecision(current));
  else area.append(node('p',report.locked_snapshot?'尚未复核期权报价。':'独立概率快照缺失。'));
  if (history.length) {
    const group = node('details');
    group.append(node('summary','期权复核历史（'+history.length+'）'));
    for (const d of history) {
      const item = node('details');
      item.append(node('summary', d.signal+' · '+displayTime(d.created_at)),renderOptionDecision(d));
      group.append(item);
    }
    area.append(group);
  }
  if (!report.current || (!report.locked_snapshot && !current?.previous_contract)) return area;
  const form = node('form');
  form.className = 'row';
  const label = node('label','复核类型');
  const select = node('select');
  const active = current && ['L3','R1','R2'].includes(current.signal);
  const types = active ? {normal:'刷新期权报价',overheated:'过热或新增风险',
    realized:'催化兑现或驱动力减弱',invalidated:'核心假设失效'} : {normal:'刷新期权报价'};
  for (const [value,text] of Object.entries(types)) {
    const option = node('option',text);
    option.value = value;
    select.append(option);
  }
  label.append(select);
  const noteLabel = node('label','复核依据');
  const note = node('input');
  note.maxLength = 2000;
  noteLabel.append(note);
  noteLabel.hidden = true;
  select.onchange = () => {
    noteLabel.hidden = select.value === 'normal';
    note.required = !noteLabel.hidden;
  };
  const submit = node('button','判断机会是否值得承担成本');
  const status = node('span');
  status.setAttribute('role','status');
  form.append(label,noteLabel,submit,status);
  form.onsubmit = async event => {
    event.preventDefault();
    submit.disabled = true;
    status.textContent = '复核中';
    try {
      if(select.value==='normal'){await refreshOpportunity(report.id,true);return;}
      await api('/api/reports/'+report.id+'/options','POST',{
        review:select.value,note:note.value.trim()});
      await Promise.all([openReport(report.id),showOpportunities(),loadUsage()]);
    } catch (error) {
      status.textContent = error.message;
      submit.disabled = false;
    }
  };
  area.append(form);
  return area;
}

