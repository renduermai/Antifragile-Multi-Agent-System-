async function loadSchedule() {
  const data=await api('/api/schedule');
  const form=$('schedule-form');
  for(const [key,value] of Object.entries(data.config)) {
    const field=form.elements.namedItem(key);
    if(key==='enabled')field.checked=value;
    else field.value=Array.isArray(value)?value.join('\n'):value;
  }
  const logs=$('schedule-jobs');
  logs.replaceChildren(node('h3','定时任务日志'));
  if(!data.jobs.length)logs.append(node('p','暂无定时运行记录。'));
  const names={daily:'每日增量',weekly:'每周完整',priority:'重点事件'};
  const states={pending:'等待',running:'运行中',completed:'已完成',retry:'等待重试',
    failed:'失败',interrupted:'已中断',superseded:'已由新一期替代'};
  for(const job of data.jobs) {
    const item=node('details');
    item.append(node('summary',names[job.kind]+' '+job.query+' · '+states[job.state]+' · '+displayTime(job.due_at)),
      node('p','尝试 '+job.attempts+' 次'+(job.retry_at?' · 重试 '+displayTime(job.retry_at):'')));
    if(job.result)item.append(node('pre',JSON.stringify(job.result,null,2)));
    if(job.run_id)item.append(caseAction('查看研究进度',async()=>{
      location.hash='hunt';
      await followScan(job.run_id);
    }));
    logs.append(item);
  }
}
function initSchedule() {
  $('schedule-form').onsubmit=async event=>{
    event.preventDefault();
    const form=event.currentTarget;
    const data=Object.fromEntries(new FormData(form));
    data.enabled=form.elements.namedItem('enabled').checked;
    data.priority_queries=data.priority_queries.split('\n').map(x=>x.trim()).filter(Boolean);
    for(const key of ['daily_hour','daily_minute','weekly_day','weekly_hour','priority_minutes','max_retries'])
      data[key]=Number(data[key]);
    try {await api('/api/schedule','PUT',data);await loadSchedule();notice('调度已保存');}
    catch(error){notice(error.message);}
  };
}

