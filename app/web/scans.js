const scanStates = {
  completed:'已完成', running:'研究中', partial:'部分完成', failed:'失败', interrupted:'已中断',
  no_candidates:'本轮无候选', insufficient_data:'可用数据不足',
};
const sectionNames = {
  independent_confirmation:'独立确认',
  catalyst:'催化因素', claims_and_conflicts:'主体声明与证据冲突', attractor:'长期价值锚', network:'产业瓶颈与替代',
  power:'决定权与否决权', action_tree:'行动与反制', feedback:'反馈与系统状态', risk:'风险与反证', next_check:'下次核查',
};
const kindNames = {fact:'事实',statement:'主体声明',inference:'推断',rumor:'传闻',unknown:'未知'};
function reportButton(id, label) {
  const button = node('button',label);
  button.onclick = () => openReport(id).catch(error => notice(error.message));
  return button;
}
function findingView(finding) {
  const block = node('div');
  block.append(node('p',(kindNames[finding.kind] || '') + ' · ' + finding.text));
  const refs=node('details');refs.append(node('summary','核对引用原文'));for(const ref of finding.refs)refs.append(node('p',ref.quote));if(finding.refs.length)block.append(refs);
  return block;
}
function intervalView(value) {
  return value ? (100*value.low).toFixed(1)+'% – '+(100*value.high).toFixed(1)+'%' : '未知';
}
async function openReport(id) {
  const report = await api('/api/reports/'+id);
  const panel = $('scan-report');
  panel.replaceChildren(node('h3','发现了什么机会'),node('p',report.analysis?.catalyst?.text || '当前证据不足，暂时无法形成机会判断。'),node('small','研究更新于 '+displayTime(report.created_at)));
  const original=node('details');original.append(node('summary','查看原始标题'),node('p',report.title));panel.append(original);
  panel.append(node('h4','为什么值得关注'));
  const timeline=node('div');timeline.className='research-timeline';
  const news=report.sources.filter(s=>s.source_type!=='derived_daily_bars').sort((a,b)=>(a.published||'').localeCompare(b.published||''));
  if(!news.length)timeline.append(node('p','当前没有可展示的新闻证据。'));
  for(const source of news){
    const entry=node('div');entry.className='timeline-entry';
    const cn=report.presentation?.[source.id];entry.append(node('small','首次公开 '+displayTime(source.published)),node('h5',cn?.title||'新闻线索待翻译'),node('p',cn?.summary||'可生成中文摘要，或展开核对原文。'));
    if(source.url && /^https?:\/\//i.test(source.url)){const link=node('a','查看来源');link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';entry.append(link);}
    const related=[report.analysis?.claims_and_conflicts,report.analysis?.catalyst].filter(f=>f?.refs?.some(r=>r.id===source.id));entry.append(node('p',related.length?'对当前判断的影响 · '+related.map(f=>f.text).join(' '):'对判断的影响尚待研究，当前保留为证据线索。'));const raw=node('details');raw.append(node('summary','查看原文'),node('pre',source.text));entry.append(raw);timeline.append(entry);
  }
  panel.append(timeline);
  if(news.some(s=>!report.presentation?.[s.id])){const translate=node('button','生成中文新闻摘要');translate.onclick=async()=>{translate.disabled=true;try{await api('/api/reports/'+id+'/presentation','POST');await openReport(id);}catch(error){notice(error.message);translate.disabled=false;}};panel.append(translate);}
  const update=node('button','更新这个机会');update.onclick=()=>refreshOpportunity(id,false);panel.append(update);
  const changes=node('div');changes.className='research-timeline';let previousIds=new Set();let previousJudgment=null;
  for(const moment of report.timeline||[]){const row=node('div');row.className='timeline-entry';const added=moment.evidence_ids.filter(id=>!previousIds.has(id)).length;row.append(node('small',displayTime(moment.at)),node('p',moment.judgment||'本轮证据不足'),node('p','本轮新增或修订证据 '+added+' 条。'+(previousJudgment===null?'首次记录判断。':moment.judgment===previousJudgment?'核心判断延续，继续核对风险。':'核心判断发生变化，请对照前后证据。')));for(const decision of moment.decisions||[])row.append(node('p',decision.explanation));changes.append(row);previousIds=new Set(moment.evidence_ids);previousJudgment=moment.judgment;}
  panel.append(node('h4','判断如何随时间变化'),changes);
  original.append(node('p',report.reasons.join('、') || '未触发研究阈值'));
  if(report.problem) panel.append(node('p','深研受限 · '+report.problem));
  const technical=node('details');technical.append(node('summary','查看行情与研究细节'));panel.append(technical);
  for(const m of report.metrics) {
    technical.append(node('h4',m.symbol+' · 相对价值'));
    if(m.as_of) technical.append(node('p',m.as_of+' 完整日线，复权收盘 '+m.adjusted_close+' USD，日收益 '+(100*m.daily_return).toFixed(2)+'%'));
    if(m.relative) {
      const r=m.relative;
      technical.append(node('p',m.benchmark+' · 同步样本 '+r.sessions+' · 近期/前期相关 '+r.correlation+' / '+r.prior_correlation+
        ' · 残差 '+(100*r.residual_return).toFixed(2)+'% · 分位 '+(100*r.percentile).toFixed(1)+'% · '+(r.stable?'关系较稳定':'关系待复核')));
      technical.append(node('p',r.rationale));
    }
    if(m.gaps.length) technical.append(node('p',m.gaps.join('；')));
  }
  if(report.analysis) {
    for(const [key,label] of Object.entries(sectionNames)) {
      panel.append(node('h4',label));
      for(const finding of [].concat(report.analysis[key])) panel.append(findingView(finding));
    }
    panel.append(node('h4','推演尚未发生的情景'),node('p','研究窗口至 '+report.horizon_end));
    const names={base:'基准',up:'正向尾部',down:'负向尾部'};
    for(const s of report.analysis.scenarios) {
      panel.append(node('strong',names[s.branch]+' · '+intervalView(s.probability)),node('p',s.condition),
        node('p','发生窗口 '+s.occurrence_window+' · 公开窗口 '+s.public_window+' · 价格反应 '+s.price_response),
        findingView(s.basis),node('p','改判条件 · '+s.change_if));
    }
    const j=report.analysis.joint;
    technical.append(node('h4','联合概率'),node('p',j.catalyst+' · 足够变动条件 '+j.sufficient_move),
      node('p','发生 '+intervalView(j.event)+' · 窗口内发生并公开 '+intervalView(j.timely)+' · 足够变动 '+intervalView(j.move)+' · 目标退出 '+intervalView(j.exit)),
      findingView(j.basis),node('small','概率快照 '+report.snapshot_id+' · '+displayTime(report.locked_at)));
  }
  technical.append(node('h4','市场预期'),node('p',report.market.assessment));
  if(report.market.options_gap) panel.append(node('p','期权样本 · '+report.market.options_gap));
  for(const term of report.market.terms || []) panel.append(node('p',term.expiry+' · 近平值 IV '+
    (term.near_atm_iv==null?'未知':(term.near_atm_iv*100).toFixed(1)+'%')+' · 看跌减看涨 IV '+
    (term.put_minus_call_iv==null?'未知':(term.put_minus_call_iv*100).toFixed(1)+' 个百分点')));
  for(const v of report.market.volume) panel.append(node('small',v.symbol+' · 成交量 / 近窗中位数 '+(v.ratio??'未知')+' '));
  panel.append(optionsView(report));
  technical.append(node('h4','可追溯来源'));
  for(const s of report.sources) {
    const details=node('details');
    details.append(node('summary',(s.publisher || s.source_type)+' · '+(s.published || '公开时间未知')),node('pre',s.text),node('small',s.id+' '));
    if(s.url && /^https?:\/\//i.test(s.url)) {
      const link=node('a','查看来源');link.href=s.url;link.target='_blank';link.rel='noopener noreferrer';details.append(link);
    }
    technical.append(details);
  }
  technical.append(node('p','版本 '+report.rules+' / '+report.prompt+' · 模型 '+(report.model||'未配置')));
  if(report.history.length>1) {
    const history=node('details');history.append(node('summary','历史报告'));
    for(const row of report.history) history.append(reportButton(row.id,displayTime(row.created_at)));
    panel.append(history);
  }
  panel.hidden=false;
  panel.scrollIntoView({block:'nearest'});
}
async function showOpportunities() {
  const data=await api('/api/opportunities');
  const list=$('scan-list');list.replaceChildren();
  if(!data.items.length) list.append(node('p','暂无已触发的候选。'));
  for(const item of data.items) {
    const section=node('div');
    const stale = item.quote_valid_through && Date.now() > Date.parse(item.quote_valid_through);
    const signal = stale ? '曾为 L3 · 报价待刷新' : item.signal || item.level;
    section.className='opportunity';
    section.append(reportButton(item.id,(item.symbols.join('、')||'待确认资产')+' · 查看研究'),node('p',item.catalyst || '证据不足，等待进一步研究。'),
      node('small','风险 '+item.risk+' · '+(item.odds_checked?'已计算赔率门槛':'期权赔率待验证')+' · 首次 '+displayTime(item.first_seen)+' · 更新 '+displayTime(item.created_at)));
    section.append(node('p',item.prices.length ? item.prices.map(p=>p.symbol+' '+p.price+' USD · '+displayTime(p.observed_at)+' '+p.feed).join('　') : '最近成交价未知'));
    list.append(section);
  }
}
async function refreshOpportunity(id,cost){try{const run=await api('/api/reports/'+id+'/refresh?cost='+cost,'POST');await followScan(run.run_id);const latest=await api('/api/runs/'+run.run_id);if(latest.result.reports?.length)await openReport(latest.result.reports.at(-1));}catch(error){notice(error.message);}}
async function followScan(id) {
  const button=$('scan-submit');button.disabled=true;
  try {
    let run;
    do {
      run=await api('/api/runs/'+id);
      $('scan-progress').textContent=scanStates[run.status]+' · '+(run.result.phase||'')+' · '+displayTime(run.started_at);
      if(run.status==='running') await new Promise(resolve=>setTimeout(resolve,900));
    } while(run.status==='running');
    const result=run.result;
    const info=$('scan-coverage');info.replaceChildren();
    if(run.status==='interrupted') {
      const resume=node('button','恢复本次研究');
      resume.onclick=async()=>{try{await api('/api/runs/'+id+'/resume','POST');await followScan(id);}catch(error){notice(error.message);}};
      info.append(resume);
    }
    if(result.error) info.append(node('p',result.error));
    if(result.collection) {
      for(const [kind,source] of Object.entries(result.collection.result))
        info.append(node('small',labels[kind]+' · '+(source.message || '已采集')+'　'));
    }
    if(result.history) info.append(node('p','可比日线 · '+(result.history.message || result.history.bars+' 条记录')));
    info.append(node('p','候选 '+(result.candidates??0)+' · 复用 '+(result.reused??0)+' · 暂缓 '+(result.deferred??0)+' · 受限 '+(result.blocked??0)));
    for(const [i,rid] of (result.reports||[]).entries()) info.append(reportButton(rid,'本轮报告 '+(i+1)));
    await Promise.all([showOpportunities(),loadResearch(),loadUsage()]);
  } finally {button.disabled=false;}
}
async function initScanning() {
  const mode=$('scan-mode');
  const switchMode=()=>{const targeted=mode.value!=='all';$('scan-query-label').hidden=!targeted;
    $('scan-query').required=targeted;$('scan-submit').textContent=targeted?'开始定向研究':'开始扫描';};
  mode.onchange=switchMode;switchMode();
  $('update-opportunities').onclick=async()=>{const items=(await api('/api/opportunities')).items;const select=$('update-choice');select.replaceChildren();for(const item of items){const option=node('option',item.symbols.join('、')||'待确认资产');option.value=item.id;select.append(option);}select.hidden=false;const go=$('update-confirm');go.hidden=false;go.onclick=()=>refreshOpportunity(select.value,false);};
  $('scan-form').onsubmit=async event=>{
    event.preventDefault();$('scan-submit').disabled=true;
    try {
      const run=await api('/api/scans','POST',{mode:mode.value,query:$('scan-query').value.trim()});
      await followScan(run.run_id);
    } catch(error) {notice(error.message);$('scan-submit').disabled=false;}
  };
  await showOpportunities();
  const latest=await api('/api/scans/latest');
  if(latest) await followScan(latest.id); else $('scan-progress').textContent='尚无研究任务';
}

