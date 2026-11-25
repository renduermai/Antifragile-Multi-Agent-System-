const graphSteps=[
 ['screen','异常与证据检查'],['research','因果与情景研究'],['lock','锁定独立概率'],['pricing','市场预期与报价复核']
];
const outerSteps=[['evidence','读取当轮证据与历史时钟'],['screen','检查已有判断与研究条件'],['research','运行研究图'],['lock','锁定事前判断'],['pricing','期权资料与共同赔率计算'],['saved','保存本轮决策并推进'],['audit','全部锁定后独立验证'],['display','展示结果与限制']];
function replayDiagram(title,steps,job){
 const area=node('section');area.append(node('h3',title));const graph=node('div');graph.className='workflow-graph';
 const round=(job?.steps||[]).filter(s=>s.node==='evidence').at(-1)?.round??0;
 for(const [i,[key,label]] of steps.entries()){
  const records=(job?.steps||[]).filter(s=>s.node===key && (['audit','display'].includes(key)||s.round===round||job.status==='completed'));
  const last=records.at(-1);const state=last?.status||'waiting';
  const box=node('div',label);box.className='workflow-node '+state;
  box.append(node('small',({waiting:'等待',running:'运行中',done:'已完成',blocked:'受限 / 待继续'})[state]));graph.append(box);
  if(i<steps.length-1)graph.append(node('span','↓'));
 }
 area.append(graph);if(title==='研究图')area.append(node('p','现实扫描条件分支：未触发异常时从筛选直接进入市场观察。历史测试按指定事件进入研究；模型校验失败则停止本轮。'));return area;
}
function renderReplay(status){
 const area=$('replay-overview');const job=status.job;const caseId=loadedCase;area.replaceChildren();
 if(status.preparation)area.append(node('h2',status.preparation.title),node('p',status.preparation.status),node('p','待补充 · '+status.preparation.missing.join('、')));
 area.append(node('p',status.material.ready?'已载入分轮材料 · '+status.material.rounds+' 轮 · '+status.material.start+' 至 '+status.material.end:status.material.message));
 const start=caseAction(job&&['blocked','interrupted'].includes(job.status)?'继续历史模拟':'开始历史模拟',async()=>{
  const id=loadedCase;await api('/api/cases/'+id+'/replay?resume='+Boolean(job&&['blocked','interrupted'].includes(job.status)),'POST');
  await followReplay(id);
 });
 start.disabled=(!status.material.ready&&!status.preparation)||job?.status==='running';area.append(start);
 api('/api/cases/capacity').then(cap=>{if(loadedCase!==caseId)return;area.querySelector('.replay-capacity')?.remove();const text=node('p','每日模型预算 '+cap.daily_calls+' 次，今日已用 '+cap.used+' 次；按最长5轮估算，还可运行 '+cap.estimated_full_runs_remaining+' 次完整案例。'+cap.note);text.className='replay-capacity';area.prepend(text);}).catch(()=>{});
 area.append(node('p','灰色等待 · 蓝色运行 · 绿色完成 · 橙色受限。每轮重置步骤，颜色来自后台记录。'));
 if(job)area.append(node('p','第 '+Math.min(job.round_index+1,status.material.rounds||1)+' 轮 · '+({running:'运行中',completed:'全部完成',blocked:'已保存进度，等待补充',interrupted:'已中断'})[job.status]+(job.problem?' · '+job.problem:'')));
 area.append(replayDiagram('研究图',graphSteps,job),node('p','历史回放读取当轮材料后进入研究；模型输出不通过校验时停止，保留原因。'),replayDiagram('完整回放流程',outerSteps,job));
}
async function followReplay(id){
 let status;
 do{
  status=await api('/api/cases/'+id+'/replay');if(loadedCase!==id)return;
  renderReplay(status);await renderCaseRuns(id);
  if(status.job?.status==='running')await new Promise(r=>setTimeout(r,900));
 }while(status.job?.status==='running');
 if(status.job)await renderReplayResults(id,status.job);
}
async function renderReplayResults(id,job){
 if(loadedCase!==id)return;
 const area=$('replay-results');area.replaceChildren();
 const runs=await api(casePath(id)+'/runs');const run=runs.find(r=>r.id===job.series_id);if(!run)return;
 for(const round of [...run.attempts].reverse().filter(r=>r.status==='locked')){
  const p=await api(casePath(id)+'/predictions/'+round.id);if(loadedCase!==id)return;
  const item=node('section');item.className='timeline-entry';item.append(node('h3','第 '+(p.round_index+1)+' 轮 · '+p.cutoff));
  const s=p.snapshot;
  for(const [k,label] of [['catalyst','当时的判断'],['claims_and_conflicts','矛盾与因果'],['next_check','后续观察']])item.append(node('h4',label),findingView(s.analysis[k]));
  for(const scenario of s.analysis.scenarios)item.append(node('p',({base:'正常情况',up:'向上尾部',down:'向下尾部'})[scenario.branch]+' · '+scenario.condition+' · '+scenario.price_response+' · 概率 '+intervalView(scenario.probability)));
  item.append(node('p',s.simulation.decision),node('p',s.simulation.gap||''));
  if(s.simulation.quotes.length)item.append(optionTable(s.simulation.quotes));
  const sources=node('details');sources.append(node('summary','当轮可见原始证据'));for(const source of s.sources)sources.append(caseEvidence(source));item.append(sources);
  if(job.status==='completed'){
   const evaluations=await api(casePath(id)+'/predictions/'+round.id+'/evaluations');if(loadedCase!==id)return;
   for(const e of evaluations)item.append(showEvaluation(e));
  }
  area.append(item);
 }
}

