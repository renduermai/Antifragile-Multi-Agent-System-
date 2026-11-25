import asyncio
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collection import Requests, CollectionRunning, run_collection, run_summary
from .db import DailyLimitReached, connect, migrate, now, reserve_usage, root, save_setting, setting
from .providers import CATALOG, HttpProvider
from .research import UNIVERSE, event_detail, event_list, latest_market, save_review
from . import scheduling, case_audit, case_forecast, options as option_decisions, scanning
from .case_material import CaseInputError, read_input
from .research import SYMBOL


LOCK = threading.Lock()
ACTIVE = set()
OPTION_REVIEW = 'option_review'
WEB = Path(__file__).parent / 'web'


@asynccontextmanager
async def lifespan(app):
    migrate()
    from . import replay
    replay.migrate()
    from . import marketdata
    with connect() as db:
        marketdata.migrate(db)
    timer=asyncio.create_task(scheduling.serve(dispatch_schedule))
    try:
        yield
    finally:
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass


app = FastAPI(title='反脆弱投资研究 · 阶段 7', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=WEB), name='static')


@app.middleware('http')
async def local_write_guard(request: Request, call_next):
    # Browsers must make writes from the same origin. Local CLI clients may omit Origin.
    origin = request.headers.get('origin')
    if request.method in ('POST', 'PUT', 'DELETE') and origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


def valid_kind(kind):
    if kind not in CATALOG:
        raise HTTPException(404, '接口类别不存在')


def secrets():
    path = root() / 'secrets.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else {}


def update_secrets(kind, values):
    root().mkdir(parents=True, exist_ok=True)
    target = root() / 'secrets.json'
    temporary = root() / 'secrets.tmp'
    temporary.write_text(json.dumps(values), encoding='utf-8')
    os.replace(temporary, target)


def config(kind):
    return setting('provider.' + kind, {**CATALOG[kind], 'model': '', 'feed': 'iex',
                   'per_run_limit': 6, 'daily_limit': 20, 'input_price': None, 'output_price': None, 'enabled': kind != 'options', 'credit_limit':80})


def public_config(kind):
    value = config(kind)
    secret = secrets().get(kind, {})
    with connect() as db:
        probe = db.execute('SELECT result FROM probes WHERE kind=?', (kind,)).fetchone()
    return {**value, 'kind': kind, 'key_mask': '••••••••' if secret.get('api_key') else '',
            'secret_mask': '••••••••' if secret.get('secret_key') else '',
            'last_result': json.loads(probe['result']) if probe else None}


class ProviderConfig(BaseModel):
    enabled: bool = True
    credit_limit: int = Field(default=80,ge=1,le=10000)
    provider: str
    endpoint: str = Field(max_length=500)
    model: str = Field(default='', max_length=150)
    feed: Literal['iex', 'sip', 'delayed_sip'] = 'iex'
    api_key: str | None = Field(default=None, max_length=2000)
    secret_key: str | None = Field(default=None, max_length=2000)
    clear_key: bool = False
    per_run_limit: int = Field(default=6, ge=1, le=10000)
    daily_limit: int = Field(default=20, ge=1, le=100000)
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)


class Limits(BaseModel):
    daily_llm_calls: int = Field(default=20, ge=1, le=10000)
    deep_candidates: int = Field(default=3, ge=1, le=100)


class TextContent(BaseModel):
    text: str = Field(max_length=500000)


class CollectionInput(BaseModel):
    query: str = Field(default='', max_length=200)


class EventReview(BaseModel):
    state: Literal['watch', 'active', 'archived']
    judgment: str = Field(default='', max_length=5000)
    unresolved_conflicts: list[str] = Field(default_factory=list, max_length=40)
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)


@app.get('/')
def home():
    return FileResponse(WEB / 'index.html')


@app.get('/api/status')
def status():
    collection, last_success = run_summary()
    return {'stage': 7, 'schedule_enabled': scheduling.config().enabled, 'last_successful_scan': last_success,
            'scan_status': collection['state'] if collection else 'idle', 'last_collection': collection,
            'universe': UNIVERSE,
            'limits': setting('limits', Limits().model_dump())}


@app.get('/api/opportunities')
def opportunities():
    return {'items': scanning.opportunities_list(), 'reason': '研究等级基于已采集的证据和行情。'}


async def dispatch_schedule(job):
    with LOCK:
        if OPTION_REVIEW in ACTIVE:
            return None
        with connect('research') as db:
            busy=db.execute("SELECT 1 FROM research_runs WHERE status='running'").fetchone()
            collecting=db.execute("SELECT 1 FROM collection_runs WHERE state='running'").fetchone()
        if busy or collecting:
            return None
        configs={k:config(k) for k in ('news','market','search','llm','options','marketdata')}
        credentials=secrets()
        limits=setting('limits',Limits().model_dump())
        mode='keyword' if job['kind']=='priority' else 'all'
        try:
            run_id=scanning.begin(mode,job['query'])
        except sqlite3.IntegrityError:
            return None
    await scanning.execute(run_id,mode,job['query'],configs,credentials,limits,force=job['kind']=='weekly')
    record=scanning.run_record(run_id)
    result=record['result']
    sources=result.get('collection',{}).get('result',{}).values()
    retryable=record['status'] in ('failed','interrupted') or any(
        v.get('status') in ('connection_error','failed') or v.get('http_status') in (429,500,502,503,504)
        for v in sources)
    return {'run_id':run_id,'scan_status':record['status'],'retryable':retryable,
            'reused':result.get('reused',0),'deferred':result.get('deferred',0),
            'blocked':result.get('blocked',0),'error':result.get('error')}


@app.get('/api/schedule')
def schedule_status():
    return scheduling.status()


@app.put('/api/schedule')
def save_schedule(value: scheduling.Schedule):
    return scheduling.update(value)


class ScanInput(BaseModel):
    mode: Literal['all', 'asset', 'keyword'] = 'all'
    query: str = Field(default='', max_length=160)


@app.post('/api/scans', status_code=202)
async def start_scan(value: ScanInput, tasks: BackgroundTasks):
    query = value.query.strip()
    if value.mode == 'all':
        query = ''
    elif value.mode == 'asset':
        query = query.upper()
        if not SYMBOL.fullmatch(query):
            raise HTTPException(422, '请填写有效资产代码')
    elif not query:
        raise HTTPException(422, '请填写事件关键词')
    with LOCK:
        if OPTION_REVIEW in ACTIVE:
            raise HTTPException(409, '期权复核正在运行')
        configs = {k:config(k) for k in ('news','market','search','llm','options','marketdata')}
        credentials = secrets()
        limits = setting('limits', Limits().model_dump())
        try:
            run_id = scanning.begin(value.mode, query)
        except sqlite3.IntegrityError:
            raise HTTPException(409, '已有研究任务运行中') from None
    tasks.add_task(scanning.execute,run_id,value.mode,query,configs,credentials,limits)
    return {'run_id':run_id}


@app.post('/api/reports/{report_id}/refresh',status_code=202)
def refresh_opportunity(report_id: str, tasks: BackgroundTasks, cost: bool=False):
    report=scanning.report_by_id(report_id)
    if not report:raise HTTPException(404,'研究报告不存在')
    with LOCK:
        if OPTION_REVIEW in ACTIVE:raise HTTPException(409,'期权复核正在运行')
        configs={k:config(k) for k in ('news','market','search','llm','options','marketdata')}
        try:rid=scanning.begin('all','')
        except sqlite3.IntegrityError:raise HTTPException(409,'已有研究任务运行中') from None
    tasks.add_task(scanning.execute,rid,'all','',configs,secrets(),
        setting('limits',Limits().model_dump()),target=report['subject'],cost=cost)
    return {'run_id':rid}


@app.post('/api/reports/{report_id}/presentation')
async def translate_report(report_id: str):
    from .presentation import translate
    report=scanning.report_by_id(report_id)
    if not report:raise HTTPException(404,'研究报告不存在')
    try:return await translate(report,config('llm'),secrets().get('llm',{}),setting('limits',Limits().model_dump())['daily_llm_calls'])
    except Exception:raise HTTPException(400,'中文摘要未完成，请检查模型预算或稍后重试') from None


@app.get('/api/scans/latest')
def latest_scan():
    return scanning.run_record()


@app.post('/api/runs/{run_id}/resume',status_code=202)
async def resume_scan(run_id: str, tasks: BackgroundTasks):
    record = scanning.run_record(run_id)
    if not record:
        raise HTTPException(404, '无此研究任务')
    with LOCK:
        if OPTION_REVIEW in ACTIVE:
            raise HTTPException(409, '期权复核正在运行')
        try:
            resumed = scanning.resume(run_id)
        except sqlite3.IntegrityError:
            resumed = False
        if not resumed:
            raise HTTPException(409, '当前任务不可恢复或另有研究正在运行')
        configs = {k:config(k) for k in ('news','market','search','llm','options')}
        tasks.add_task(scanning.execute,run_id,record['mode'],record['query'],configs,secrets(),
                       setting('limits',Limits().model_dump()))
    return {'run_id':run_id}


@app.get('/api/runs/{run_id}')
def scan_result(run_id: str):
    record = scanning.run_record(run_id)
    if record is None:
        raise HTTPException(404, '无此研究任务')
    return record


@app.get('/api/reports/{report_id}')
def research_report(report_id: str):
    record = scanning.report_by_id(report_id)
    if record is None:
        raise HTTPException(404, '无此报告')
    with connect('research') as db:
        record['history'] = [dict(row) for row in db.execute(
            'SELECT id,created_at,status FROM reports WHERE subject=? ORDER BY rowid DESC',(record['subject'],))]
        row = db.execute('SELECT payload FROM probability_snapshots WHERE id=?',(record['snapshot_id'],)).fetchone()
        record['locked_snapshot'] = json.loads(row[0]) if row else None
    from .presentation import cached
    record['presentation']=cached(record)
    record['timeline']=[]
    for row in reversed(record['history']):
        old=scanning.report_by_id(row['id'])
        record['timeline'].append({'id':old['id'],'at':old.get('created_at',row['created_at']),'judgment':old.get('catalyst'),
            'risk':old.get('risk'),'level':old['level'],'evidence_ids':[s['id'] for s in old.get('sources',[])],
            'decisions':option_decisions.history(old['id'])})
    record['decisions'] = option_decisions.history(report_id)
    record['current'] = scanning.latest_report(record['subject'])['id'] == report_id
    return record


class OptionsReview(BaseModel):
    review: Literal['normal','overheated','realized','invalidated'] = 'normal'
    note: str = Field(default='', max_length=2000)


@app.post('/api/reports/{report_id}/options')
async def review_options(report_id: str, value: OptionsReview):
    note = value.note.strip()
    if value.review != 'normal' and not note:
        raise HTTPException(422, '请写出复核依据')
    with LOCK:
        with connect('research') as db:
            running = db.execute("SELECT 1 FROM research_runs WHERE status='running'").fetchone()
        if running or OPTION_REVIEW in ACTIVE:
            raise HTTPException(409, '已有研究或期权复核运行中')
        report = scanning.report_by_id(report_id)
        if not report:
            raise HTTPException(404, '无此报告')
        if scanning.latest_report(report['subject'])['id'] != report_id:
            raise HTTPException(409, '请在该事件最新报告上复核')
        if value.review != 'normal' and not option_decisions.prior_candidate(report['subject']):
            raise HTTPException(409, '该机会尚无可复核的 L3 模拟计划')
        source_configs={k:config(k) for k in ('options','marketdata','search')}
        credentials=secrets()
        ACTIVE.add(OPTION_REVIEW)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            from .option_sources import select_source
            return await option_decisions.assess(report,select_source(client,source_configs,credentials),
                review=value.review,note=note,archived=option_decisions.archived_subject(report['subject']))
    finally:
        with LOCK:
            ACTIVE.discard(OPTION_REVIEW)


@app.post('/api/collections')
async def collect(value: CollectionInput):
    with LOCK:
        settings = {kind: config(kind) for kind in ('news', 'search', 'market')}
        credentials = secrets()
    try:
        return await run_collection(value.query.strip(), settings, credentials)
    except CollectionRunning:
        raise HTTPException(409, '已有采集正在运行') from None


@app.get('/api/events')
def events():
    return event_list()


@app.get('/api/events/{event_id}')
def event(event_id: str):
    detail = event_detail(event_id)
    if detail is None:
        raise HTTPException(404, '事件不存在')
    return detail


@app.post('/api/events/{event_id}/reviews', status_code=201)
def review_event(event_id: str, review: EventReview):
    if any(len(conflict) > 1000 for conflict in review.unresolved_conflicts):
        raise HTTPException(422, '单条矛盾最多 1000 字符')
    try:
        saved = save_review(event_id, review.state, review.judgment.strip(),
                            review.unresolved_conflicts, review.evidence_ids)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if not saved:
        raise HTTPException(404, '事件不存在')
    return event_detail(event_id)


@app.get('/api/market/observations/latest')
def market_observations():
    return latest_market()


@app.get('/api/providers')
def providers():
    return [public_config(kind) for kind in CATALOG]


@app.put('/api/providers/{kind}')
def save_provider(kind: str, value: ProviderConfig):
    valid_kind(kind)
    allowed = {'brave','tavily'} if kind == 'search' else {CATALOG[kind]['provider']}
    if value.provider not in allowed:
        raise HTTPException(422, '当前只实现页面所列的 Provider 适配器')
    with LOCK:
        if kind in ACTIVE:
            raise HTTPException(409, '连接测试进行中，请完成后保存')
        all_secrets = secrets()
        current = all_secrets.setdefault(kind, {})
        if value.clear_key or value.provider != config(kind)['provider']:
            current.clear()
        for key in ('api_key', 'secret_key'):
            supplied = getattr(value, key)
            if supplied:
                current[key] = supplied
        update_secrets(kind, all_secrets)
        save_setting('provider.' + kind, {**value.model_dump(exclude={'api_key', 'secret_key', 'clear_key'}),
                                        'label': CATALOG[kind]['label']})
        with connect() as db:
            db.execute('DELETE FROM probes WHERE kind=?', (kind,))
    return public_config(kind)


@app.put('/api/limits')
def limits(value: Limits):
    save_setting('limits', value.model_dump())
    return value


@app.post('/api/providers/{kind}/test')
async def test_provider(kind: str):
    valid_kind(kind)
    with LOCK:
        if kind in ACTIVE:
            raise HTTPException(409, '该接口正在测试')
        cfg = config(kind)
        cap = cfg['daily_limit']
        if kind == 'llm':
            cap = min(cap, setting('limits', Limits().model_dump())['daily_llm_calls'])
        try:
            usage_id = reserve_usage(kind, cfg['provider'], cap)
        except DailyLimitReached:
            raise HTTPException(429, '每日调用上限已达到（UTC 日界）') from None
        ACTIVE.add(kind)
    result = {'ok': False, 'tested_at': now()}
    probe = None
    try:
        probe = await HttpProvider(kind).test(cfg, secrets().get(kind, {}))
        result.update(ok=True, message='连接及样本格式校验通过', capabilities=probe.capabilities)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        result['message'] = {401: '认证失败', 403: '权限不足', 429: 'Provider 限流或额度不足'}.get(code, 'Provider HTTP 请求失败')
        result['http_status'] = code
    except (httpx.HTTPError, TimeoutError):
        result['message'] = '网络、TLS 或连接超时，请检查 Endpoint 与网络'
    except ValueError:
        result['message'] = '配置缺失、Endpoint 无效或响应格式校验失败，请核对字段与 Provider 能力'
    except (KeyError, IndexError, TypeError):
        result['message'] = 'Provider 响应结构不符合适配器要求'
    finally:
        with LOCK:
            with connect() as db:
                db.execute('UPDATE usage SET status=?,detail=?,input_tokens=?,output_tokens=? WHERE id=?',
                           ('success' if result['ok'] else 'failed', json.dumps(result),
                            probe.input_tokens if probe else None, probe.output_tokens if probe else None, usage_id))
                db.execute('INSERT INTO probes VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET result=excluded.result',
                           (kind, json.dumps(result)))
            ACTIVE.discard(kind)
    return result


@app.get('/api/marketdata/budget')
def marketdata_budget():
    from .marketdata import budget
    return {**budget(), 'limit':config('marketdata').get('credit_limit',80)}


@app.post('/api/marketdata/query')
async def marketdata_query(value: dict):
    from .marketdata import Query, query
    from pydantic import ValidationError
    try:
        request = Query.model_validate(value)
    except ValidationError:
        raise HTTPException(422,'请核对查询参数、日期及合约范围') from None
    try:
        return await query(request,config('marketdata'),secrets().get('marketdata',{}))
    except ValueError as error:
        raise HTTPException(400,str(error)) from None


@app.get('/api/usage')
def usage():
    with connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM usage ORDER BY id DESC LIMIT 200')]
    for row in rows:
        cfg = config(row['kind'])
        rates = (cfg.get('input_price'), cfg.get('output_price'))
        row['estimated_cost_usd'] = ((row['input_tokens'] * rates[0] + row['output_tokens'] * rates[1]) / 1000000
                                     if None not in (*rates, row['input_tokens'], row['output_tokens']) else None)
    return rows


@app.get('/api/cases/capacity')
def case_capacity():
    cap=min(config('llm')['daily_limit'],setting('limits',Limits().model_dump())['daily_llm_calls'])
    with connect() as db:
        used=db.execute("SELECT COUNT(*) FROM usage WHERE kind='llm' AND created_at LIKE ?",(now()[:10]+'%',)).fetchone()[0]
    remaining=max(0,cap-used)
    return {'daily_calls':cap,'used':used,'remaining':remaining,'standard_max_rounds':5,
            'estimated_full_runs_remaining':remaining//5,'note':'按每轮一次请求估算，失败重试和其他研究共享预算'}


@app.get('/api/cases')
def cases():
    with connect('cases') as db:
        return [dict(row) for row in db.execute('SELECT * FROM cases ORDER BY rowid')]


def case_exists(case_id):
    with connect('cases') as db:
        if not db.execute('SELECT 1 FROM cases WHERE id=?', (case_id,)).fetchone():
            raise HTTPException(404, '案例不存在')


class CaseStart(BaseModel):
    input_version: int = Field(gt=0)
    round_index: int = Field(ge=0, lt=40)
    confirmed: bool = False


class CaseNext(BaseModel):
    confirmed: bool = False


class CaseValidation(BaseModel):
    validation_version: int | None = Field(default=None,gt=0)


@app.exception_handler(CaseInputError)
async def case_input_error(request, error):
    from fastapi.responses import JSONResponse

    return JSONResponse({'detail':str(error)}, status_code=422)


@app.exception_handler(case_forecast.CaseConflict)
async def case_conflict(request, error):
    from fastapi.responses import JSONResponse

    return JSONResponse({'detail':str(error)}, status_code=409)


@app.get('/api/cases/{case_id}/replay')
def replay_status(case_id: str):
    from . import replay
    case_exists(case_id)
    try:
        version,material=read_input(case_id)
        ready={'ready':True,'rounds':len(material['rounds']),'start':material['rounds'][0]['cutoff'],'end':material['rounds'][-1]['cutoff']}
    except CaseInputError:
        ready={'ready':False,'message':'案例材料尚未准备，请展开案例管理保存资料'}
    folder=Path(__file__).resolve().parents[1]/'cases'/case_id/'preparation.json'
    return {'job':replay.get(case_id),'material':ready,'preparation':json.loads(folder.read_text('utf-8')) if folder.exists() else None}


@app.post('/api/cases/{case_id}/replay',status_code=202)
def start_replay(case_id: str,tasks: BackgroundTasks,resume: bool=False):
    from . import replay
    case_exists(case_id)
    with LOCK:
        replay.install(case_id)
        jid=replay.begin(case_id,resume)
        cfg=config('llm');secret=secrets().get('llm',{})
        cap=setting('limits',Limits().model_dump())['daily_llm_calls']
    tasks.add_task(replay.execute,jid,case_id,cfg,secret,cap)
    return {'id':jid}


@app.get('/api/cases/{case_id}/workflow/preview')
def case_preview(case_id: str, version: int | None = None):
    case_exists(case_id)
    version, material = read_input(case_id,version)
    return {'input_version':version,'symbol':material['symbol'],
            'rounds':[{'cutoff':r['cutoff'], 'sources':r['sources'],
                       'quote_ids':[q['id'] for q in r['quotes']]} for r in material['rounds']]}


@app.get('/api/cases/{case_id}/workflow/runs')
def case_runs(case_id: str):
    case_exists(case_id)
    runs = case_forecast.list_series(case_id)
    summaries = case_audit.summaries(case_id)
    for run in runs:
        for attempt in run['attempts']:
            attempt['evaluation'] = summaries.get(attempt['id'])
    return runs


def schedule_case(case_id, tasks, confirmed, **run_args):
    case_exists(case_id)
    if not confirmed:
        raise CaseInputError('运行前请确认所选版本的归档材料和时间边界')
    with LOCK:
        cfg = config('llm')
        secret = secrets().get('llm',{})
        cap = setting('limits',Limits().model_dump())['daily_llm_calls']
        try:
            series_id, forecast_id = case_forecast.begin(case_id,cfg,**run_args)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
    tasks.add_task(case_forecast.execute,case_id,forecast_id,cfg,secret,cap)
    return {'series_id':series_id,'forecast_id':forecast_id}


@app.post('/api/cases/{case_id}/workflow/runs', status_code=202)
def start_case(case_id: str, payload: CaseStart, tasks: BackgroundTasks):
    return schedule_case(case_id,tasks,payload.confirmed,version=payload.input_version,index=payload.round_index)


@app.post('/api/cases/{case_id}/workflow/runs/{series_id}/next', status_code=202)
def next_case(case_id: str, series_id: str, payload: CaseNext, tasks: BackgroundTasks):
    return schedule_case(case_id,tasks,payload.confirmed,series_id=series_id)


@app.get('/api/cases/{case_id}/workflow/predictions/{forecast_id}')
def case_prediction(case_id: str, forecast_id: str):
    try:
        return case_forecast.get_prediction(case_id,forecast_id)
    except LookupError as error:
        raise HTTPException(404,str(error)) from None


@app.get('/api/cases/{case_id}/workflow/predictions/{forecast_id}/evaluations')
def case_evaluations(case_id: str, forecast_id: str):
    try:
        return case_audit.history(case_id,forecast_id)
    except LookupError as error:
        raise HTTPException(404,str(error)) from None


@app.post('/api/cases/{case_id}/workflow/predictions/{forecast_id}/evaluations')
def validate_case(case_id: str, forecast_id: str, payload: CaseValidation):
    try:
        return case_audit.evaluate(case_id,forecast_id,payload.validation_version)
    except LookupError as error:
        raise HTTPException(404,str(error)) from None


def text_table(kind):
    if kind == 'input':
        return 'cases', 'input_versions'
    if kind == 'validation':
        return 'validation', 'validation_versions'
    raise HTTPException(404, '内容类别不存在')


@app.get('/api/cases/{case_id}/{kind}')
def read_text(case_id: str, kind: str):
    case_exists(case_id)
    database, table = text_table(kind)
    with connect(database) as db:
        row = db.execute(f'SELECT id,body,created_at FROM {table} WHERE case_id=? ORDER BY id DESC LIMIT 1', (case_id,)).fetchone()
    return {'version': row['id'], 'text': row['body'], 'created_at': row['created_at']} if row else {'version': None, 'text': ''}


@app.put('/api/cases/{case_id}/{kind}')
def save_text(case_id: str, kind: str, content: TextContent):
    case_exists(case_id)
    database, table = text_table(kind)
    with connect(database) as db:
        db.execute(f'INSERT INTO {table}(case_id,body,created_at) VALUES (?,?,?)', (case_id, content.text, now()))
    return read_text(case_id, kind)
