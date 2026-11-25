import asyncio
import json
import re
import httpx
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from . import options as option_decisions
from .collection import CollectionRunning, Requests, run_collection
from .expectations import market_view
from .db import connect, now, root
from .hypotheses import PROMPT_VERSION, infer
from .relative_value import RULES, features, refresh_history
from .research import UNIVERSE, digest, event_detail, json_text, latest_market, normalized_text


def begin(mode, query):
    run_id = uuid4().hex
    with connect('research') as db:
        db.execute("INSERT INTO research_runs(id,mode,query,status,started_at) VALUES (?,?,?,'running',?)",
                   (run_id, mode, query, now()))
    return run_id


def resume(run_id):
    with connect('research') as db:
        return db.execute("""UPDATE research_runs SET status='running',finished_at=NULL
            WHERE id=? AND status='interrupted'""",(run_id,)).rowcount == 1


def run_record(run_id=None):
    with connect('research') as db:
        row = (db.execute('SELECT * FROM research_runs WHERE id=?', (run_id,)) if run_id else
               db.execute('SELECT * FROM research_runs ORDER BY rowid DESC LIMIT 1')).fetchone()
    return {**dict(row), 'result':json.loads(row['result'] or '{}')} if row else None


def latest_report(subject):
    with connect('research') as db:
        row = db.execute('SELECT payload FROM reports WHERE subject=? ORDER BY rowid DESC LIMIT 1',
                         (subject,)).fetchone()
    return json.loads(row[0]) if row else None


def report_by_id(report_id):
    with connect('research') as db:
        row = db.execute('SELECT payload FROM reports WHERE id=?', (report_id,)).fetchone()
    return json.loads(row[0]) if row else None


def opportunities_list():
    prices = {r['symbol']:r for r in latest_market()}
    today = datetime.now(timezone.utc).date().isoformat()
    with connect('research') as db:
        rows = db.execute('''SELECT payload FROM reports r WHERE rowid=(
            SELECT MAX(rowid) FROM reports WHERE subject=r.subject)
            ORDER BY created_at DESC''')
        items = []
        for row in rows:
            report = json.loads(row[0])
            decision = option_decisions.latest(report['id'])
            signal = decision['signal'] if decision else report['level']
            if not signal.startswith('R') and (signal == 'L0' or report['horizon_end'] < today
                                               or option_decisions.archived_subject(report['subject'])):
                continue
            items.append({k: report[k] for k in ('id','subject','title','symbols','level','path','created_at',
                                                'first_seen','status','reasons','risk','market','catalyst')})
            items[-1].update(signal=signal, quote_valid_through=decision.get('quote_valid_through') if decision else None,
                             odds_checked=bool(decision and any(
                quote.get('probability_hurdle') is not None for quote in decision['quotes'])))
            items[-1]['prices'] = [prices[s] for s in report['symbols'] if s in prices]
        return items


def ids_for(mode, query, cutoff):
    clause = "state!='archived'"
    args = []
    if mode == 'asset':
        clause += ' AND event_id IN (SELECT event_id FROM event_assets WHERE symbol=?)'
        args.append(query)
    elif mode == 'keyword':
        clause += ''' AND (instr(lower(title),lower(?))>0 OR event_id IN (
             SELECT event_id FROM evidence WHERE instr(lower(headline),lower(?))>0 OR instr(lower(body),lower(?))>0))'''
        args += [query]*3
    else:
        clause += ' AND COALESCE(updated_at,created_at)>=?'
        args.append(cutoff)
    with connect('research') as db:
        return [r[0] for r in db.execute(f'SELECT event_id FROM events WHERE {clause} ORDER BY updated_at DESC', args)]


def make_candidate(subject, event, symbols, metrics, cutoff, as_of, horizon):
    review = next((v['result'] for v in event.get('versions', []) if v['result'].get('type')=='manual_review'), {})
    rows = [r for r in reversed(event.get('evidence', [])) if not r['duplicate_of']]
    title = event.get('title') or symbols[0]
    active_news = [r for r in rows if not (r['source_type']=='search_result' and re.search(r'\b(?:page not found|404 not found)\b', r['headline'], re.I)) and
                   (r['version_public_at'] or r['acquired_at'] or '') >= cutoff and
                   (r['version_public_at'] or r['acquired_at']) <= as_of and r['source_url']]
    words = sorted({word for r in active_news if r['source_type']=='news_article' or r['provider']=='tavily_search'
                    for word in RULES['catalysts'] if word in normalized_text((r['headline'] or '') + ' ' + (r['body'] or '')[:1200])})
    reasons = [f'新闻催化线索 · {word}' for word in words]
    if review.get('state')=='active' and active_news:
        reasons.append('人工标记跟踪')
    for feature in metrics:
        reasons.extend(feature['symbol'] + ' · ' + reason for reason in feature['triggers'])
    priority_ids = set(review.get('evidence_ids', []))
    for r in active_news:
        if r['supersedes_id']:
            priority_ids.update([r['id'], r['supersedes_id']])
    ordered = sorted(rows, key=lambda r:r['id'] not in priority_ids)
    selected = ordered[:RULES['max_sources']]
    sources = [{'id':r['id'],'url':r['source_url'],'publisher':r['publisher'],'source_type':r['source_type'],
                'published':r['version_public_at'] or r['first_public_at'], 'revision_of':r['supersedes_id'],
                'text': (r['headline']+'\n'+re.sub('<[^>]+>', ' ', r['body'] or ''))[:1500]} for r in selected]
    for metric in metrics:
        if metric.get('as_of'):
            sources.append({'id':'market:' + metric['symbol'], 'url':metric['source_url'],
                            'publisher':'Alpaca', 'source_type':'derived_daily_bars',
                            'published':metric['as_of'], 'revision_of':None,'text':json_text(metric)})
    origins = {r['publisher'].strip().casefold() for r in active_news if r['source_type']=='news_article' and r['publisher']}
    return {'subject':subject,'title':title,'symbols':symbols,'sources':sources,'metrics':metrics,
            'reasons':reasons,'path':'B' if words or event else 'A','review':review,
            'independent_publishers':len(origins), 'first_seen':event.get('created_at') or as_of,
            'as_of':as_of,'horizon_end':horizon,'evidence_count':len(rows),
            'omitted_priority':len(priority_ids - {r['id'] for r in selected}),
            'score':(10 if words else 0)+min(len(origins),3)+len(reasons)}


def candidates(mode, query, feed):
    dt = datetime.now(timezone.utc)
    as_of, horizon = dt.isoformat(), (dt + timedelta(days=30)).date().isoformat()
    cutoff = (dt - timedelta(days=RULES['news_age_days'])).isoformat()
    symbols = set(UNIVERSE) | ({query} if mode=='asset' else set())
    feature_map = {s: features(s,feed) for s in symbols}
    result, covered = [], set()
    for event_id in ids_for(mode,query,cutoff):
        e = event_detail(event_id)
        assets = [a['symbol'] for a in e['assets']]
        ordered_assets = sorted(assets,key=lambda s:(s != query,s not in UNIVERSE,s))
        for symbol in ordered_assets[:4]:
            if symbol not in feature_map:
                feature_map[symbol] = features(symbol,feed)
        metrics = [feature_map[s] for s in ordered_assets[:4]]
        candidate = make_candidate(event_id,e,assets,metrics,cutoff,as_of,horizon)
        if candidate['reasons'] or mode != 'all':
            result.append(candidate)
            covered.update(assets)
    for s in ([query] if mode=='asset' else sorted(UNIVERSE) if mode=='all' else []):
        metric = feature_map[s]
        if s not in covered and (metric['triggers'] or (mode=='asset' and metric.get('as_of'))):
            result.append(make_candidate('asset:'+s,{},[s],[metric],cutoff,as_of,horizon))
    return sorted(result,key=lambda c:c['score'],reverse=True)


class Flow(TypedDict, total=False):
    item: dict
    level: str
    analysis: dict | None
    problem: str | None
    snapshot_id: str | None
    locked_at: str | None
    market_view: dict


def make_graph(saver, run_id, cfg, secret, daily_cap, previous, allow_call, options):
    def screen(state):
        return {'level':'L1' if state['item']['reasons'] else 'L0'}

    async def analyze(state):
        c = state['item']
        if not allow_call:
            return {'analysis':None,'problem':'达到单轮模型调用上限'}
        with connect('research') as db:
            saved = db.execute('SELECT outcome FROM model_steps WHERE run_id=? AND subject=?',
                               (run_id,c['subject'])).fetchone()
            if saved:
                return json.loads(saved[0]) if saved[0] else {'analysis':None,'problem':'中断时模型请求结果未知，可新建扫描重试'}
            db.execute('INSERT INTO model_steps VALUES (?,?,NULL)', (run_id,c['subject']))
        prior = {}
        context = c
        if previous and previous.get('analysis'):
            prior = {k:previous['analysis'][k] for k in ('catalyst','risk','next_check','claims_and_conflicts')}
            needed = {r['id'] for finding in prior.values() for r in finding['refs']}
            needed.update(c['review'].get('evidence_ids', []))
            needed.update(s['revision_of'] for s in c['sources'] if s['revision_of'])
            known = {s['id']:s['text'] for s in previous['sources']}
            increment = [s for s in c['sources'] if s['id'] in needed or s['revision_of'] or known.get(s['id']) != s['text']]
            context = {**c,'sources':increment or c['sources'][:1]}
            prior['previous_report_id'] = previous['id']
        answer, problem = await infer(context,cfg,secret,daily_cap,prior)
        outcome = {'analysis':answer,'problem':problem}
        with connect('research') as db:
            db.execute('UPDATE model_steps SET outcome=? WHERE run_id=? AND subject=?',
                       (json_text(outcome),run_id,c['subject']))
        return outcome

    def lock(state):
        c = state['item']
        answer = state.get('analysis')
        if not answer:
            return {'snapshot_id':None, 'locked_at':None}
        sid = digest([run_id,c['subject'],'A7'])
        data = {'as_of':c['as_of'],'horizon_end':c['horizon_end'],'scenarios':answer['scenarios'],
                'joint':answer['joint'],'options_case':answer.get('options_case'),'risk':answer['risk'],'evidence_ids':[s['id'] for s in c['sources']],
                'rules':RULES['version'],'prompt':PROMPT_VERSION,'model':cfg['model']}
        with connect('research') as db:
            db.execute('INSERT OR IGNORE INTO probability_snapshots VALUES (?,?,?,?,?)',
                       (sid,run_id,c['subject'],now(),json_text(data)))
            locked_at = db.execute('SELECT locked_at FROM probability_snapshots WHERE id=?',(sid,)).fetchone()[0]
        return {'snapshot_id':sid,'locked_at':locked_at}

    async def pricing(state):
        c = state['item']
        answer = state.get('analysis')
        level = state['level']
        required = RULES['confirming_publishers']
        if answer and c['independent_publishers'] >= required and answer['catalyst']['kind'] in ('fact','statement') and not c['omitted_priority']:
            confirmation = answer['independent_confirmation']
            cited = {ref['id'] for ref in confirmation['refs']} if confirmation['kind'] in ('fact','statement') else set()
            if len({s['publisher'].lower() for s in c['sources'] if s['id'] in cited and s['publisher']
                    and s['source_type']=='news_article'}) >= required:
                level = 'L2'
        return {'level':level, 'market_view':await market_view(c,options if state.get('snapshot_id') else None)}
    graph = StateGraph(Flow)
    for name,fn in [('screen_A1_A2_A4',screen),('research_A3_A4_A5_A6_A7_A8',analyze),
                    ('lock_A7',lock),('expectations_A9',pricing)]:
        graph.add_node(name,fn)
    graph.add_edge(START,'screen_A1_A2_A4')
    graph.add_conditional_edges('screen_A1_A2_A4', lambda s:'deep' if s['level']=='L1' else 'observe',
                                {'deep':'research_A3_A4_A5_A6_A7_A8','observe':'expectations_A9'})
    graph.add_edge('research_A3_A4_A5_A6_A7_A8','lock_A7')
    graph.add_edge('lock_A7','expectations_A9')
    graph.add_edge('expectations_A9',END)
    return graph.compile(checkpointer=saver)


async def study(saver, run_id, c, signature, cfg, secret, limits, prior, allowance, options):
    graph = make_graph(saver,run_id,cfg,secret,limits['daily_llm_calls'],prior,allowance,options)
    thread = run_id + '/' + c['subject']
    graph_config = {'configurable':{'thread_id':thread}}
    checkpoint = await graph.aget_state(graph_config)
    if checkpoint.values and not checkpoint.next:
        state = checkpoint.values
    else:
        state = await graph.ainvoke(None if checkpoint.values else {'item':c},graph_config)
    analysis = state.get('analysis')
    status = 'blocked' if state.get('problem') else 'analyzed' if analysis else 'observed'
    report = {**c,'id':digest([run_id,c['subject']]),'signature':signature,'run_id':run_id,'created_at':now(),
              'level':state['level'],'status':status,'problem':state.get('problem'),'analysis':analysis,
              'snapshot_id':state.get('snapshot_id'),'locked_at':state.get('locked_at'),'market':state['market_view'],
              'model':cfg.get('model'), 'rules':RULES['version'],'prompt':PROMPT_VERSION,'thread':thread,
              'risk':analysis['risk']['text'] if analysis else '证据或研究条件不足',
              'catalyst':analysis['catalyst']['text'] if analysis else '待验证'}
    with connect('research') as db:
        db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?,?)',
                   (report['id'],run_id,c['subject'],signature,status,report['created_at'],json_text(report)))
    return report


async def execute(run_id, mode, query, configs, secrets, limits, force=False, target=None, cost=False):
    result = {'reports': [], 'reused':0, 'deferred':0, 'blocked':0}
    status = 'failed'
    def progress(message):
        result['phase']=message
        with connect('research') as db:
            db.execute('UPDATE research_runs SET result=? WHERE id=?',(json_text(result),run_id))
    try:
        progress('正在补充最新事实与读取既有档案')
        with connect('research') as db:
            row = db.execute('SELECT body FROM scan_plans WHERE run_id=?',(run_id,)).fetchone()
        identity = digest([RULES,PROMPT_VERSION,configs['llm']['endpoint'],configs['llm']['model']])
        if row:
            plan = json.loads(row[0])
            target=plan.get('target',target)
            cost=plan.get('cost',cost)
            if plan['identity'] != identity:
                result['error'] = '模型或规则版本已变化，请新建扫描以保留原快照'
                return
        else:
            settings = {k:configs[k] for k in ('news','market','search')}
            collection = await run_collection(query,settings,secrets,query if mode=='asset' else None)
            symbols = set(UNIVERSE) | ({query} if mode=='asset' else set())
            history = await refresh_history(symbols,configs['market'],secrets.get('market',{}),
                                            collection['result']['market'].get('requests',0))
            queue=candidates(mode,query,history['feed'])
            if target:
                queue=[c for c in queue if c['subject']==target]
            plan = {'collection':collection,'history':history,'queue':queue,
                    'identity':identity, 'deep_cap':limits['deep_candidates'],'target':target,'cost':cost}
            with connect('research') as db:
                db.execute('INSERT INTO scan_plans VALUES (?,?)',(run_id,json_text(plan)))
        queue, collection, history = plan['queue'], plan['collection'], plan['history']
        result.update(collection=collection,history=history)
        result['candidates'] = len(queue)
        progress('正在比较新增证据、历史判断与独立情景')
        deep = 0
        reviewed = set()
        cfg = configs['llm']
        async with (AsyncSqliteSaver.from_conn_string(str(root()/'research_checkpoints.sqlite3')) as saver,
                    httpx.AsyncClient(timeout=20) as client):
            from .option_sources import select_source
            options = select_source(client,configs,secrets)

            async def review_options(report):
                reviewed.add(report['subject'])
                if report['level'] == 'L2' or option_decisions.prior_candidate(report['subject']) or (cost and report['subject']==target):
                    progress('正在检查市场定价、有限合约与赔率条件')
                    await option_decisions.assess(report,options,
                        archived=option_decisions.archived_subject(report['subject']))

            for c in queue:
                saved = report_by_id(digest([run_id,c['subject']]))
                if saved:
                    deep += int(bool(c['reasons']))
                    result['reports'].append(saved['id'])
                    result['blocked'] += int(saved['status']=='blocked')
                    if not option_decisions.latest(saved['id']):
                        await review_options(saved)
                    continue
                old = latest_report(c['subject'])
                if old:
                    c['first_seen'] = old['first_seen']
                signature = digest([c['sources'],c['metrics'],c['review'],c['reasons'],c['symbols'],
                                    c['independent_publishers'],c['omitted_priority'],cfg['endpoint'],cfg['model'],
                                    RULES,PROMPT_VERSION])
                if not force and old and old['signature']==signature and old['status']!='blocked' and old['horizon_end']>=c['as_of'][:10]:
                    result['reports'].append(old['id'])
                    result['reused'] += 1
                    await review_options(old)
                    continue
                if c['reasons'] and deep >= min(plan['deep_cap'],limits['deep_candidates']):
                    result['deferred'] += 1
                    continue
                report = await study(saver,run_id,c,signature,cfg,secrets.get('llm',{}),limits,old,
                                     deep < cfg['per_run_limit'],options)
                deep += int(bool(c['reasons']))
                result['reports'].append(report['id'])
                result['blocked'] += int(report['status']=='blocked')
                await review_options(report)
            for report_id in ([] if target else option_decisions.active_decisions()):
                previous = report_by_id(report_id)
                if previous['subject'] not in reviewed:
                    await review_options(latest_report(previous['subject']))
        result['deep_candidates'] = deep
        if target and result['reports']:
            from .presentation import translate
            progress('正在生成中文新闻摘要与研究变化记录')
            try:
                await translate(report_by_id(result['reports'][-1]),cfg,secrets.get('llm',{}),limits['daily_llm_calls'])
            except Exception:
                result['presentation_gap']='中文摘要暂不可用，可查看原文或稍后重试'
        result['phase']='本轮完成，已保存判断与数据缺口'
        if not queue:
            status = 'no_candidates' if collection['state']=='succeeded' or history['bars'] else 'insufficient_data'
        else:
            status = 'partial' if result['blocked'] or result['deferred'] or collection['state']!='succeeded' else 'completed'
    except CollectionRunning:
        result['error'] = '已有数据采集运行，请待其结束后重试'
    except asyncio.CancelledError:
        status = 'interrupted'
        raise
    except Exception as exc:
        result['error'] = '研究中断，已提交报告保留。错误类型 ' + type(exc).__name__
    finally:
        with connect('research') as db:
            db.execute('UPDATE research_runs SET status=?,finished_at=?,result=? WHERE id=?',
                       (status,now(),json_text(result),run_id))

