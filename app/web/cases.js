const caseStates = {running:'预测运行中',locked:'已锁定',blocked:'条件不足',interrupted:'已中断'};
const caseMetrics = {evidence:'关键证据',conflicts:'新闻矛盾',facts:'事实校验',scenarios:'情景完整性',
  probability:'概率',timing:'时间窗口',temporal:'未来信息检查',options:'期权计算',decision:'模拟决策',
  market:'实际行情',returns:'收益口径'};

function casePath(id) {return '/api/cases/'+id+'/workflow';}
function resetCaseWorkflow() {
  for(const key of ['case-timeline','case-runs','case-prediction']) $(key).replaceChildren();
  $('case-workflow-status').textContent='';
}
function caseAction(label, callback) {
  const button=node('button',label);
  button.type='button';
  button.onclick=async()=>{
    button.disabled=true;
    try {await callback();}
    catch(error) {$('case-workflow-status').textContent=error.message;}
    finally {button.disabled=false;}
  };
  return button;
}
function caseEvidence(source) {
  const detail=node('details');
  const anchor=node('a','打开归档来源');
  anchor.href=source.url;anchor.target='_blank';anchor.rel='noopener noreferrer';
  detail.append(node('summary',source.id+' · '+source.published),node('pre',source.text),anchor);
  return detail;
}
async function previewCase(id, version, run, nextIndex) {
  const data=await api(casePath(id)+'/preview?version='+version);
  if(loadedCase!==id)return;
  const area=$('case-timeline');
  area.replaceChildren(node('h3','已保存分轮 · 版本 '+data.input_version),
    node('p','时间以 UTC 显示。每轮读取该时点及之前的已标注材料。'));
  const rounds=node('div');
  const label=node('label','开始轮次');
  const choice=node('select');
  for(const [index,round] of data.rounds.entries()) {
    const option=node('option','第 '+(index+1)+' 轮 · '+round.cutoff);
    option.value=index;
    choice.append(option);
    const chapter=node('details');
    chapter.append(node('summary','第 '+(index+1)+' 轮 · 截断 '+round.cutoff));
    for(const source of round.sources)chapter.append(caseEvidence(source));
    if(round.quote_ids.length)chapter.append(node('p','归档报价 ID '+round.quote_ids.join('、')));
    rounds.append(chapter);
  }
  label.append(choice);
  if(run) {choice.value=nextIndex;choice.disabled=true;}
  const form=node('form');
  form.className='row';
  const confirm=node('input');confirm.type='checkbox';confirm.required=true;
  const confirmed=node('label','已核对原文、公开时间和截断边界');
  confirmed.append(confirm);
  const start=node('button',run?'运行下一轮':'运行选定轮次');
  form.append(label,confirmed,start);
  area.append(rounds,form);
  area.scrollIntoView({block:'nearest'});
  form.onsubmit=async event=>{
    event.preventDefault();
    start.disabled=true;
    try {
      const path=casePath(id)+'/runs'+(run?'/'+run.id+'/next':'');
      const body=run?{confirmed:confirm.checked}:{confirmed:confirm.checked,input_version:data.input_version,
        round_index:Number(choice.value)};
      const job=await api(path,'POST',body);
      if(loadedCase===id) {
        area.replaceChildren();
        await followCase(id,job.forecast_id);
      }
    } catch(error) {$('case-workflow-status').textContent=error.message;}
    finally {start.disabled=false;}
  };
}
function initCaseWorkflow() {
$('case-preview').onclick=async()=>{
  try{
    const version=$('inputVersion').dataset.version;
    if(!loadedCase||!version)throw new Error('请先保存带历史时间标注的案例 TXT');
    await previewCase(loadedCase,version);
    $('case-workflow-status').textContent='';
  }catch(error){$('case-workflow-status').textContent=error.message;}
};
$('inputText').addEventListener('input',()=>$('case-timeline').replaceChildren());
}

async function renderCaseRuns(id) {
  const runs=await api(casePath(id)+'/runs');
  if(loadedCase!==id)return;
  const list=$('case-runs');
  list.replaceChildren();
  if(!runs.length)list.append(node('p','暂无预测记录。保存案例 TXT 后可预览分轮。'));
  for(const run of runs) {
    const group=node('details');
    group.open=true;
    const locked=run.attempts.filter(result=>result.status==='locked');
    const next=locked.length?Math.max(...locked.map(row=>row.round_index))+1:run.first_round;
    group.append(node('summary',(run.identity.model||'模型未配置')+' · TXT '+run.input_version+' · '+run.created_at),
      node('p','运行 '+run.id+' · 规则 '+run.identity.case_rules+' · Prompt '+run.identity.research_prompt));
    for(const row of run.attempts) {
      const detail=node('div');
      const caption='第 '+(row.round_index+1)+' 轮 · '+caseStates[row.status]+' · '+row.cutoff;
      detail.append(caseAction(caption,()=>row.status==='running'?followCase(id,row.id):showCasePrediction(id,row.id)));
      if(row.evaluation)detail.append(node('small','评分 '+(row.evaluation.overall??'—')+
        ' · 验证版本 '+row.evaluation.validation_version+' · '+row.evaluation.evaluator_version));
      group.append(detail);
    }
    if(next<run.round_count&&!run.attempts.some(row=>row.status==='running'))
      group.append(caseAction(locked.length?'准备下一轮':'准备重试本轮',()=>previewCase(id,run.input_version,run,next)));
    list.append(group);
  }
}
async function followCase(id, forecast) {
  const path=casePath(id)+'/predictions/'+forecast;
  let record;
  do {
    record=await api(path);
    if(loadedCase!==id)return;
    $('case-workflow-status').textContent=caseStates[record.status]+' · '+record.cutoff;
    if(record.status==='running')await new Promise(resolve=>setTimeout(resolve,900));
  } while(record.status==='running');
  await Promise.all([renderCaseRuns(id),showCasePrediction(id,forecast),loadUsage()]);
}
function showEvaluation(entry) {
  const block=node('details');
  block.open=true;
  const result=entry.result;
  block.append(node('summary','可评分项目均分 '+(result.overall??'未评分')+' · 可评分 '+result.scored_count+
    ' / 11 · 验证 TXT '+entry.validation_version+' · '+entry.evaluator_version),node('p',result.evaluation),node('p','本轮有 '+result.scored_count+' / 11 项具备评分条件，缺少资料的项目保持未评分。'),
    node('p','实际结果 · '+(result.actual.summary||'未填写')),node('p','参考答案 · '+(result.reference_notes||'未填写')));
  for(const [key,row] of Object.entries(result.scores)) {
    const part=node('details');
    part.append(node('summary',caseMetrics[key]+' · '+(row.score??'未评分')+' · '+row.agent),
      node('p',row.summary));
    const {score,agent,summary,...detail}=row;
    part.append(node('pre',JSON.stringify(detail,null,2)));
    block.append(part);
  }
  if(result.improvements.length) {
    const suggestions=node('ul');
    for(const s of result.improvements)suggestions.append(node('li',s.agent+' · '+s.suggestion));
    block.append(node('h4','改进建议'),suggestions);
  }
  return block;
}
async function showCasePrediction(id,forecast) {
  const path=casePath(id)+'/predictions/'+forecast;
  const data=await api(path);
  if(loadedCase!==id)return;
  const panel=$('case-prediction');
  panel.replaceChildren(node('h3','第 '+(data.round_index+1)+' 轮 · '+caseStates[data.status]),
    node('p','模拟截断 '+data.cutoff+' · 预测 '+data.id+' · 案例 TXT '+data.input_version));
  if(data.problem)panel.append(node('p',data.problem));
  const s=data.snapshot;
  if(!s)return;
  panel.append(node('small','快照校验值 '+data.snapshot_hash),node('h4','模拟决策'),
    node('p',s.simulation.signal+' · '+s.simulation.decision));
  if(s.simulation.gap)panel.append(node('p',s.simulation.gap));
  if(s.simulation.quotes.length)panel.append(optionTable(s.simulation.quotes));
  const analysis=node('details');
  analysis.append(node('summary','锁定的证据与情景推演'));
  for(const [key,label] of Object.entries(sectionNames)) {
    analysis.append(node('h4',label));
    for(const f of [].concat(s.analysis[key]))analysis.append(findingView(f));
  }
  for(const scenario of s.analysis.scenarios) {
    const labels={base:'基准',up:'上行',down:'下行'};
    analysis.append(node('h4',labels[scenario.branch]+' · '+intervalView(scenario.probability)),
      node('p',scenario.condition),node('p','发生 '+scenario.occurrence_window+' · 公开 '+scenario.public_window),
      node('p',scenario.price_response),node('p','改判 · '+scenario.change_if),findingView(scenario.basis));
  }
  const joint=s.analysis.joint;
  analysis.append(node('p','发生 '+intervalView(joint.event)+' · 窗口内 '+intervalView(joint.timely)+
    ' · 足够变动 '+intervalView(joint.move)+' · 退出 '+intervalView(joint.exit)),findingView(joint.basis));
  for(const source of s.sources)analysis.append(caseEvidence(source));
  panel.append(analysis);
  panel.append(caseAction('开始验证',async()=>{
    const version=Number($('validationVersion').dataset.version);
    if(!version)throw new Error('请先保存对应截断时间的验证 TXT');
    await api(path+'/evaluations','POST',{validation_version:version});
    if(loadedCase===id)await Promise.all([showCasePrediction(id,forecast),renderCaseRuns(id)]);
  }));
  const evaluations=await api(path+'/evaluations');
  if(loadedCase!==id||!panel.contains(analysis))return;
  for(const item of evaluations)panel.append(showEvaluation(item));
  panel.scrollIntoView({block:'nearest'});
}

