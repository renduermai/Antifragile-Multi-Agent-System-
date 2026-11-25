"""Execute historical case forecasts using only case inputs and frozen earlier predictions."""
import asyncio
import json
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from .case_material import RULES_VERSION, CaseInputError, opening_quote, read_input, timestamp
from .db import connect, now
from .hypotheses import PROMPT_VERSION, infer
from .options import RULES as OPTION_RULES, evaluate
from .research import digest, json_text


class CaseConflict(ValueError):
    pass


def run_identity(cfg):
    return {'case_rules':RULES_VERSION, 'research_prompt':PROMPT_VERSION,
            'option_rules':OPTION_RULES['version'], 'provider':cfg['provider'],
            'model':cfg['model'],'endpoint':cfg['endpoint']}


def list_series(case_id):
    with connect('predictions') as db:
        runs = [dict(row) for row in db.execute(
            'SELECT * FROM case_series WHERE case_id=? ORDER BY rowid DESC',(case_id,))]
        for run in runs:
            run['identity'] = json.loads(run['identity'])
            run['attempts'] = [dict(row) for row in db.execute("""
                SELECT id,round_index,cutoff,status,started_at,finished_at,problem,snapshot_hash
                FROM case_predictions WHERE series_id=? ORDER BY rowid DESC""", (run['id'],))]
    return runs


def get_prediction(case_id, forecast_id):
    with connect('predictions') as db:
        record = db.execute("""SELECT p.*,s.case_id,s.input_version,s.identity FROM case_predictions p
            JOIN case_series s ON p.series_id=s.id WHERE p.id=? AND s.case_id=?""",
                            (forecast_id, case_id)).fetchone()
    if record is None:
        raise LookupError('该案例没有这条预测')
    result = dict(record)
    result.pop('independent')
    result['identity'] = json.loads(result['identity'])
    result['snapshot'] = json.loads(result['snapshot']) if result['status'] == 'locked' else None
    return result


def begin(case_id, cfg, version=None, index=None, series_id=None):
    current = run_identity(cfg)
    with connect('predictions') as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM case_predictions WHERE status='running'").fetchone():
            raise CaseConflict('已有案例轮次运行中')
        if series_id:
            series = db.execute('SELECT * FROM case_series WHERE id=? AND case_id=?',
                                (series_id,case_id)).fetchone()
            if series is None:
                raise LookupError('该案例没有这次分轮运行')
            if json.loads(series['identity']) != current:
                raise CaseConflict('模型、端点或规则版本有变动，请创建新的运行以便比较')
            version, material = read_input(case_id,series['input_version'])
            previous = db.execute("""SELECT MAX(round_index) FROM case_predictions
                WHERE series_id=? AND status='locked'""", (series_id,)).fetchone()[0]
            index = series['first_round'] if previous is None else previous + 1
        else:
            version, material = read_input(case_id,version)
        rounds = material['rounds']
        if index is None or not 0 <= index < len(rounds):
            if series_id:
                raise CaseConflict('该运行的所有后续轮次均已锁定')
            raise CaseInputError('所选轮次不存在，请重新读取已保存的分轮')
        if not series_id:
            series_id = uuid4().hex
            db.execute('INSERT INTO case_series VALUES (?,?,?,?,?,?,?)',
                       (series_id,case_id,version,index,len(rounds),now(),json_text(current)))
        forecast_id = uuid4().hex
        db.execute("""INSERT INTO case_predictions(id,series_id,round_index,cutoff,started_at,status)
            VALUES (?,?,?,?,?,'running')""", (forecast_id,series_id,index,rounds[index]['cutoff'],now()))
    return series_id, forecast_id


def prior_analysis(series_id, index, cutoff):
    with connect('predictions') as db:
        row = db.execute("""SELECT id,cutoff,independent FROM case_predictions
            WHERE series_id=? AND round_index<? AND status='locked'
            ORDER BY round_index DESC LIMIT 1""",(series_id,index)).fetchone()
    if row is None or timestamp(row['cutoff']) >= cutoff:
        return {}
    answer = json.loads(row['independent'])['analysis']
    return {'previous_forecast':row['id'], 'previous_cutoff':row['cutoff'],
            **{k:answer[k] for k in ('catalyst','risk','next_check','claims_and_conflicts')}}


def references(finding):
    if not finding:
        return set()
    return {ref['id'] for ref in finding['refs']}


def option_review(analysis, sources, quote_records, symbol, cutoff, extra_contracts=None):
    evidence = {s['id']: s for s in sources}
    confirmation = analysis['independent_confirmation']
    publishers = {evidence[ref]['publisher'].removeprefix('www.') for ref in references(confirmation) if ref in evidence and evidence[ref].get('source_type')=='case_archive'}
    confirmed = (len(publishers) >= 2 and analysis['catalyst']['kind'] in ('fact','statement')
                 and confirmation['kind'] in ('fact','statement'))
    level = 'L2' if confirmed else 'L1'
    case = analysis.get('options_case')
    price_snapshot = {'scenarios':analysis['scenarios'], 'joint':analysis['joint']}
    archived = [opening_quote(record, symbol, cutoff) for record in quote_records] if symbol else []
    latest = {}
    for quote in sorted((q for q in archived if q), key=lambda q:q['observed_at']):
        latest[quote['ticker']] = quote
    for quote in extra_contracts or []:
        latest.setdefault(quote['ticker'],quote)
    values = [evaluate(quote,case,price_snapshot,cutoff) for quote in latest.values()]
    odds = [item for item in values if item.get('probability_hurdle') is not None and item['checks']['期限']]
    risk = analysis['risk']
    risk_ok = case and case['risk_gate'] == 'clear' and risk['kind'] in ('fact','statement','inference') and risk['refs']
    eligible = [item for item in odds if all(item['checks'].values())] if confirmed and risk_ok else []
    chosen = max(eligible, key=lambda item:item['ev_low']) if eligible else None
    if chosen:
        level = 'L3'
    elif odds and confirmed:
        level = 'L2.5'
    gap = None
    if not values:
        gap = '缺少截至模拟时点可核对的历史期权报价，期权计算与执行无法验证'
    elif not odds:
        gap = '未获得与独立结算窗口匹配的可用期权赔率'
    decision = ('我把这一张合约列为条件模拟候选，按归档 Ask 估算成本。' if chosen
                else '我维持空仓观察，等待催化、报价或风险条件满足。')
    return {'signal':level,'decision':decision,'gap':gap,'quotes':values,'selected':chosen,
            'execution':'hypothetical' if chosen else 'none', 'risk_gate':bool(risk_ok), 'rules':OPTION_RULES}


async def execute(case_id, forecast_id, cfg, secret, daily_cap, progress=None):
    status, problem, output, fingerprint = 'blocked', None, None, None
    try:
        run = get_prediction(case_id,forecast_id)
        _, material = read_input(case_id,run['input_version'])
        index = run['round_index']
        cutoff = timestamp(run['cutoff'])
        chapters = material['rounds'][:index+1]
        sources = [{**item,'publisher':urlsplit(item['url']).hostname or '', 'source_type':('research_task' if '-task-' in item['id'] else 'user_scenario' if urlsplit(item['url']).hostname=='example.invalid' else 'case_archive')}
                   for chapter in chapters for item in chapter['sources'] if timestamp(item['published']) <= cutoff]
        symbol = material['symbol']
        horizon = (cutoff+timedelta(days=30)).date().isoformat()
        candidate = {'as_of':cutoff.isoformat(),'horizon_end':horizon,'title':symbol or '历史案例',
                     'symbols':[symbol] if symbol else [],'sources':sources,'metrics':[],'review':{}}
        prior = prior_analysis(run['series_id'],index,cutoff)
        from langgraph.graph import StateGraph, START, END
        def tick(name):
            if progress:progress(name)
        def screen(state):
            tick('screen')
            return state
        async def research(state):
            tick('research')
            analysis, problem = await infer(candidate,cfg,secret,daily_cap,prior,historical=True)
            return {'analysis':analysis,'problem':problem}
        def lock(state):
            tick('lock')
            independent={'analysis':state['analysis'],'recorded_at':now(),'horizon_end':horizon}
            with connect('predictions') as db:
                db.execute("UPDATE case_predictions SET independent=? WHERE id=? AND status='running'",
                           (json_text(independent),forecast_id))
            return {**state,'independent':independent}
        async def pricing(state):
            tick('pricing')
            quotes=[q for chapter in chapters for q in chapter['quotes'] if timestamp(q['published'])<=cutoff]
            extra=[]
            gap=None
            if not quotes and state['analysis'].get('options_case'):
                from .main import config, secrets
                from .option_sources import MarketOptions
                import httpx
                credentials=secrets()
                if config('marketdata').get('enabled') and credentials.get('marketdata'):
                    async with httpx.AsyncClient(timeout=15) as client:
                        provider=MarketOptions(client,config('marketdata'),credentials['marketdata'],config('search'),credentials.get('search',{}))
                        extra,_,_,gaps=await provider.collect(symbol,state['analysis']['options_case'],None,cutoff)
                        gap='；'.join(gaps) if gaps else None
            options=option_review(state['analysis'],sources,quotes,symbol,cutoff,extra)
            if gap:options['gap']='；'.join(filter(None,[options['gap'],gap]))
            return {**state,'options':options}
        graph=StateGraph(dict)
        for name,fn in [('screen',screen),('research',research),('lock',lock),('pricing',pricing)]:
            graph.add_node(name,fn)
        graph.add_edge(START,'screen');graph.add_edge('screen','research')
        graph.add_conditional_edges('research',lambda s:'lock' if s.get('analysis') else END,{'lock':'lock',END:END})
        graph.add_edge('lock','pricing');graph.add_edge('pricing',END)
        state=await graph.compile().ainvoke({})
        analysis,problem=state.get('analysis'),state.get('problem')
        if analysis is None:return
        independent,options=state['independent'],state['options']
        output = {'cutoff':cutoff.isoformat(),'symbol':symbol,'source_version':run['input_version'],
                  'sources':sources,'prior_forecast':prior.get('previous_forecast'),'identity':run['identity'],
                  **independent,'simulation':options}
        fingerprint, status = digest(output), 'locked'
    except asyncio.CancelledError:
        status, problem = 'interrupted', '预测任务已中断'
        raise
    except Exception as error:
        problem = '案例运行中止 · ' + type(error).__name__
    finally:
        with connect('predictions') as db:
            db.execute("""UPDATE case_predictions SET status=?,finished_at=?,problem=?,snapshot=?,snapshot_hash=?
                WHERE id=? AND status='running'""",
                       (status,now(),problem,json_text(output) if output else None,fingerprint,forecast_id))

