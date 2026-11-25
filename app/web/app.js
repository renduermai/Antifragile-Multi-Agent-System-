const $ = id => document.getElementById(id);
const labels = {marketdata:'MarketData 期权数据',llm:'大语言模型', news:'新闻 API', search:'独立搜索 API', market:'金融行情 API', options:'期权数据 API'};
function notice(message){ $('notice').textContent=message; }
async function api(url, method='GET', body){
  const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'输入字段不合法，请检查范围');
  return result;
}
function route(){const page=location.hash.slice(1)||'hunt';for(const name of ['hunt','cases','settings'])$(name).hidden=name!==page;document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('selected',b.dataset.page===page));}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{location.hash=b.dataset.page;});
window.onhashchange=route;route();
function field(form,label,name,type,value){const wrapper=document.createElement('label');wrapper.textContent=label;const input=document.createElement('input');input.name=name;input.type=type;input.value=value??'';if(type==='number'){input.min='0';input.step='any';}wrapper.append(input);form.append(wrapper);return input;}
async function loadProviders(){
  const providers=await api('/api/providers');$('providers').replaceChildren();
  for(const cfg of providers){
    const card=document.createElement('article');const title=document.createElement('h2');title.textContent=labels[cfg.kind]+' · '+cfg.provider;card.append(title);
    const form=document.createElement('form');form.className='provider';
    const endpoint=field(form,'Endpoint','endpoint','url',cfg.endpoint);
    endpoint.required=true;
    if(cfg.kind==='search'){
      const label=document.createElement('label');label.textContent='检索服务';
      const select=document.createElement('select');select.name='provider';
      const endpoints={tavily:'https://api.tavily.com',brave:'https://api.search.brave.com'};
      for(const name of Object.keys(endpoints))select.add(new Option(name,name));
      select.value=cfg.provider;
      select.onchange=()=>{endpoint.value=endpoints[select.value];form.elements.api_key.value='';};
      label.append(select);form.prepend(label);
    }
    const key=field(form,'API Key（留空保留）','api_key','password','');key.autocomplete='new-password';key.placeholder=cfg.key_mask||'未配置';
    if(['news','market'].includes(cfg.kind))field(form,'Alpaca Secret Key（留空保留）','secret_key','password','').placeholder=cfg.secret_mask||'未配置';
    if(cfg.kind==='llm'){field(form,'Model Name','model','text',cfg.model);field(form,'输入价格 USD / 百万 Token','input_price','number',cfg.input_price);field(form,'输出价格 USD / 百万 Token','output_price','number',cfg.output_price);}
    if(cfg.kind==='market')field(form,'Feed · iex / sip / delayed_sip','feed','text',cfg.feed);
    if(cfg.kind==='marketdata'){
      const enabled=field(form,'启用 MarketData','enabled','checkbox','');enabled.checked=cfg.enabled!==false;
      field(form,'每日积分预算','credit_limit','number',cfg.credit_limit??80);
      card.append(document.createTextNode('试用数据至少延迟24小时。查询结果按实际报价时间标记，余额以响应为准。'));
    }
    field(form,'单次运行调用上限','per_run_limit','number',cfg.per_run_limit);
    field(form,'每日调用上限','daily_limit','number',cfg.daily_limit);
    const actions=document.createElement('div');actions.className='row wide';const save=document.createElement('button');save.type='submit';save.textContent='保存配置';const test=document.createElement('button');test.type='button';test.textContent='测试已保存配置';const clear=document.createElement('button');clear.type='button';clear.textContent='清除密钥';actions.append(save,test,clear);form.append(actions);card.append(form);
    const output=document.createElement('pre');const describe=result=>cfg.kind==='marketdata'?(result.message+'。'+(result.capabilities?.notice??'')+' 最近探针 '+(result.tested_at??'')):JSON.stringify(result,null,2);output.textContent=cfg.last_result?describe(cfg.last_result):'尚未测试。权限、数据延迟、历史覆盖和实际账单费用待核验。';card.append(output);
    if(cfg.kind==='marketdata'){
      const tools=document.createElement('details');const summary=document.createElement('summary');summary.textContent='查询期权数据';tools.append(summary);
      const queryForm=document.createElement('form');queryForm.className='provider';
      const op=document.createElement('select');op.name='operation';for(const [v,t] of [['expirations','可用到期日'],['chain','限定期权链'],['quotes','单合约及历史日终']])op.add(new Option(t,v));queryForm.append(op);
      for(const [label,name,value] of [['标的代码','symbol','SPY'],['OCC 合约代码（单合约必填）','contract',''],['到期日 YYYY-MM-DD（期权链必填）','expiration',''],['方向 call / put','side','call'],['行权价下限','strike_low',''],['行权价上限','strike_high',''],['历史单日 YYYY-MM-DD','date',''],['历史起日 YYYY-MM-DD','from_date',''],['历史止日 YYYY-MM-DD','to_date','']])field(queryForm,label,name,'text',value);
      const go=document.createElement('button');go.textContent='查询并保存';queryForm.append(go);const result=document.createElement('div');
      queryForm.onsubmit=async event=>{event.preventDefault();go.disabled=true;try{const body={};for(const [k,v]of new FormData(queryForm))if(v)body[k]=k.startsWith('strike_')?Number(v):v;const r=await api('/api/marketdata/query','POST',body);result.replaceChildren();const p=document.createElement('p');p.textContent=(r.cached?'已读取缓存。':'已保存查询结果。')+r.notice+' 本周期已用或预留 '+r.budget.used_or_reserved+' 积分。';result.append(p);if(r.expirations.length){const p=document.createElement('p');p.textContent=r.expirations.join('、');result.append(p);}for(const row of r.rows){const p=document.createElement('p');p.textContent=row.contract+' · Bid '+(row.bid??'未知')+' · Ask '+(row.ask??'未知')+' · 报价时间 '+(row.observed_at??'未知')+' · '+(row.data_type==='historical_eod'?'历史日终':'延迟或时效待核验');result.append(p);}}catch(error){result.textContent=error.message;}finally{go.disabled=false;}};
      tools.append(queryForm,result);card.append(tools);
      api('/api/marketdata/budget').then(b=>{const p=document.createElement('p');p.textContent='本地预算 '+b.limit+' 积分，本周期已用或预留 '+b.used_or_reserved+'；最近响应账户剩余 '+(b.account_headers.remaining??'未知')+'，额度上限 '+(b.account_headers.limit??'未知')+'。按美东09:30划分周期。';card.append(p);}).catch(()=>{});
    }
    async function persist(clearKey=false){const body={...cfg,clear_key:clearKey};delete body.last_result;for(const [name,value] of new FormData(form)){body[name]=['per_run_limit','daily_limit','credit_limit','input_price','output_price'].includes(name)?(value===''?null:Number(value)):value;}if(cfg.kind==='marketdata')body.enabled=form.elements.enabled.checked;await api('/api/providers/'+cfg.kind,'PUT',body);notice('配置已保存');await loadProviders();}
    form.onsubmit=async e=>{e.preventDefault();try{await persist();}catch(e){notice(e.message);}};
    clear.onclick=async()=>{try{form.querySelectorAll('input[type=password]').forEach(i=>i.value='');await persist(true);}catch(e){notice(e.message);}};
    test.onclick=async()=>{test.disabled=true;output.textContent='正在发送真实连接测试请求…';try{const result=await api('/api/providers/'+cfg.kind+'/test','POST');output.textContent=describe(result);await loadUsage();}catch(e){output.textContent=e.message;}finally{test.disabled=false;}};
    $('providers').append(card);
  }
}
async function loadUsage(){const rows=await api('/api/usage');$('usage').replaceChildren();for(const r of rows){const tr=document.createElement('tr');for(const value of [r.created_at,labels[r.kind],r.status,`${r.input_tokens??'未知'} / ${r.output_tokens??'未知'}`,r.estimated_cost_usd??'未知']){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('usage').append(tr);}}
let loadedCase=null;
let caseLoadGeneration=0;
async function loadCase(){
  const id=$('caseSelect').value;
  const generation=++caseLoadGeneration;
  loadedCase=null;
  resetCaseWorkflow();
  const values=await Promise.all(['input','validation'].map(kind=>api('/api/cases/'+id+'/'+kind)));
  if(generation!==caseLoadGeneration)return;
  loadedCase=id;
  ['input','validation'].forEach((kind,i)=>{
    $(kind+'Text').value=values[i].text;
    $(kind+'Version').textContent=values[i].version?'版本 '+values[i].version:'空档案';
    $(kind+'Version').dataset.version=values[i].version??'';
  });
  await renderCaseRuns(id);
  await followReplay(id);
}
$('caseSelect').onchange=()=>loadCase().catch(e=>notice(e.message));
for(const kind of ['input','validation']){
  $('save'+(kind==='input'?'Input':'Validation')).onclick=async()=>{
    const id=loadedCase;
    if(!id)return;
    try{
      const value=await api('/api/cases/'+id+'/'+kind,'PUT',{text:$(kind+'Text').value});
      if(loadedCase!==id)return;
      $(kind+'Version').textContent='版本 '+value.version;
      $(kind+'Version').dataset.version=value.version;
      if(kind==='input') $('case-timeline').replaceChildren();
      notice('已保存独立内容版本');
    }catch(e){notice(e.message);}
  };
  $(kind+'File').onchange=async event=>{
    const file=event.target.files[0];
    const generation=caseLoadGeneration;
    event.target.value='';
    if(!file||!loadedCase)return;
    if(file.size>1500000){notice('TXT 文件过大');return;}
    try{
      const bytes=await file.arrayBuffer();
      if(generation!==caseLoadGeneration)return;
      const editor=$(kind+'Text');
      editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
      editor.dispatchEvent(new Event('input'));
    }catch{notice('无法读取 TXT，请检查文件及 UTF-8 编码');}
  };
}
$('limits').onsubmit=async e=>{e.preventDefault();try{await api('/api/limits','PUT',{daily_llm_calls:Number($('dailyLimit').value),deep_candidates:Number($('candidateLimit').value)});notice('预算已保存');}catch(e){notice(e.message);}};
async function init() {
  initCaseWorkflow();
  initSchedule();
  initScanning().catch(error => notice(error.message));
  $('collection-form').onsubmit = submitCollection;
  const status = await api('/api/status');
  $('dailyLimit').value = status.limits.daily_llm_calls;
  $('candidateLimit').value = status.limits.deep_candidates;
  for (const c of await api('/api/cases')) {
    const option = document.createElement('option');
    option.value = c.id;
    option.textContent = c.title;
    $('caseSelect').append(option);
  }
  await Promise.all([loadCase(), loadProviders(), loadUsage(), loadResearch(status), loadSchedule()]);
}
init().catch(error => notice(error.message));
