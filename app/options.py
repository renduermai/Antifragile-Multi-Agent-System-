import json
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .collection import SourceError
from .db import connect
from .expectations import numeric
from .research import SYMBOL, json_text

RULES = tomllib.loads((Path(__file__).resolve().parents[1] / 'config/options.toml').read_text('utf-8'))
REVIEW_NAMES = {'normal': '常规复核', 'overheated': '过热或新增风险',
                'realized': '催化兑现或驱动力减弱', 'invalidated': '核心假设失效'}


def positive(value):
    return numeric(value) and value > 0


def contract_from(row, symbol):
    if not isinstance(row, dict) or not isinstance(row.get('details'), dict):
        return None
    d = row['details']
    side, strike, multiplier = d.get('contract_type'), d.get('strike_price'), d.get('shares_per_contract')
    if side not in ('call', 'put') or not positive(strike) or multiplier != 100:
        return None
    if d.get('exercise_style') != 'american' or d.get('additional_underlyings'):
        return None
    try:
        expiry = date.fromisoformat(d['expiration_date'])
    except (KeyError, TypeError, ValueError):
        return None
    ticker = d.get('ticker')
    strike_mills = round(strike * 1000)
    if not 0 < strike_mills <= 99999999 or abs(strike_mills/1000 - strike) > 1e-8:
        return None
    expected = f'O:{symbol}{expiry:%y%m%d}{side[0].upper()}{strike_mills:08d}'
    if ticker != expected:
        return None
    underlying = row.get('underlying_asset')
    if isinstance(underlying, dict) and underlying.get('ticker') not in (None, symbol):
        return None
    q = row.get('last_quote')
    q = q if isinstance(q, dict) else {}
    trade = row.get('last_trade')
    trade = trade if isinstance(trade, dict) else {}
    return {'ticker': ticker, 'side': side, 'strike': strike, 'expiry': expiry.isoformat(),
            'multiplier': multiplier, 'open_interest': row.get('open_interest'),
            'spot': underlying.get('price') if isinstance(underlying, dict) else None,
            'quote': {key: q.get(key) for key in ('bid','ask','bid_size','ask_size','last_updated','timeframe')},
            'last_trade': {key: trade.get(key) for key in ('price','sip_timestamp','timeframe')},
            'contract_verified': False}


def probability_ev(bands, values, minimize=True):
    """Exact linear bound on the probability simplex intersected with three intervals."""
    if any(p is None for p in bands.values()):
        return None
    weights = {branch: band['low'] for branch, band in bands.items()}
    remaining = 1.0 - sum(weights.values())
    if remaining < -1e-7 or sum(b['high'] for b in bands.values()) < 1 - 1e-7:
        return None
    for branch in sorted(values, key=values.get, reverse=not minimize):
        allocated = min(max(remaining, 0), bands[branch]['high'] - weights[branch])
        weights[branch] += allocated
        remaining -= allocated
    return sum(weights[branch] * value for branch, value in values.items())


def breakeven_probability(cost, success, failure):
    if success <= failure:
        return None
    return max(0, (cost-failure)/(success-failure))


def evaluate(contract, case, snapshot, at):
    q = contract['quote']
    bid, ask = q['bid'], q['ask']
    bid_ask = positive(bid) and positive(ask) and bid <= ask
    expiry = date.fromisoformat(contract['expiry'])
    dte = (expiry - at.date()).days
    duration_ok = (RULES.get('event_min_days',1) if case else RULES['min_days']) <= dte <= RULES['max_days']
    stamp = q['last_updated']
    age = at.timestamp() - stamp / 1e9 if type(stamp) is int and 0 < stamp < 10**20 else None
    realtime = (age is not None and 0 <= age <= RULES['max_quote_age_seconds']
                and q['timeframe'] == 'REAL-TIME')
    spread = (ask-bid)/ask if bid_ask else None
    size_ok = all(numeric(q[k]) and q[k] >= RULES['min_size'] for k in ('bid_size','ask_size'))
    oil = contract['open_interest']
    liquid = (spread is not None and spread <= RULES['max_relative_spread'] and size_ok
              and numeric(oil) and oil >= RULES['min_open_interest'])
    compatible = (case is not None and contract['side'] == case['side']
                  and case['from_date'] <= contract['expiry'] <= case['through_date'])
    available = contract.get('available_at')
    visible = not available or datetime.fromisoformat(available) <= at
    reference_only = contract.get('reference_only',False)
    usable = (visible and bid_ask and age is not None and 0 <= age <= (RULES.get('max_reference_quote_age_seconds',604800) if reference_only else RULES['max_research_quote_age_seconds'])
              and spread <= RULES['max_research_spread'])
    checks = {'近期实时双边报价': realtime and bid_ask and not reference_only and visible, '期限': duration_ok,
              '点差、报价数量与未平仓量': liquid, '标准交割已核验': contract['contract_verified'],
              '结算情景覆盖该合约': compatible}
    values = {**contract, 'quote_age_seconds': age, 'relative_spread': spread, 'days': dte, 'checks': checks}
    if not usable:
        return values
    strike, multiplier = contract['strike'], contract['multiplier']
    fees = RULES['round_trip_fee_usd']
    debit = ask * multiplier
    total = debit + fees
    sign = 1 if contract['side'] == 'call' else -1
    stress_cost = total + (ask - bid) * RULES['entry_spread_fraction'] * multiplier
    values.update(ask_debit=debit, fees=fees, total_cost=total, stressed_cost=stress_cost,
                  breakeven=strike + sign * total / multiplier, max_loss=total,
                  reference_only=reference_only, calculation_basis='假设标准100份合约' if contract.get('multiplier_assumed') else '合约记录乘数')
    checks['单张风险预算'] = stress_cost <= RULES['max_contract_loss_usd']
    if not compatible:
        return values

    payoffs = {}
    bands = {s['branch']: s['probability'] for s in snapshot['scenarios']}
    for branch, band in case['prices'].items():
        gross = sorted(max(sign * (p - strike), 0) * multiplier for p in (band['low'], band['high']))
        payoffs[branch] = {'price': band, 'gross_low': gross[0], 'gross_high': gross[1],
                           'net_low': gross[0] - total, 'net_high': gross[1] - total}
    settle = RULES['settlement_value_fraction']
    worst = {k: p['gross_low'] * settle for k, p in payoffs.items()}
    floor = probability_ev(bands, worst)
    ceiling = probability_ev(bands, {k: p['gross_high'] for k, p in payoffs.items()}, minimize=False)
    tail = 'up' if sign == 1 else 'down'
    # A zero failure recovery is the explicit conservative binary assumption.
    hurdle = breakeven_probability(stress_cost, worst[tail], 0)
    lower = bands[tail]['low'] if bands[tail] else None
    values.update(payoffs=payoffs, ev_low=floor-stress_cost if floor is not None else None,
                  ev_high=ceiling-total if ceiling is not None else None,
                  tail=tail, success_gross=worst[tail], failure_gross=0,
                  probability_hurdle=hurdle, locked_success_lower=lower,
                  exit_probability=snapshot.get('joint', {}).get('exit'))
    margin=RULES['minimum_probability_margin']
    limits=[RULES['max_contract_loss_usd']]
    if floor is not None:limits.append(floor-RULES['minimum_ev_usd'])
    if lower is not None:limits.append(max(0,lower-margin)*worst[tail])
    ceiling_cost=min(limits)
    max_ask=max(0,(ceiling_cost-fees+bid*RULES['entry_spread_fraction']*multiplier)/
                (multiplier*(1+RULES['entry_spread_fraction']))) if floor is not None and lower is not None else None
    values.update(max_acceptable_ask=max_ask,
        conditions=['待更新报价、交割乘数和流动性确认','仅适用于已锁定的价格区间及事件时间窗口','权利金上限按当前Bid及费用不变反推，报价变化后须重算'],
        sensitivity={'event_misses_expiry_net':-total,
                     'insufficient_move_net':max(sign*(case['prices']['base']['low' if sign==1 else 'high']-strike),0)*multiplier-total,
                     'extra_fee_1usd_ev_low':floor-stress_cost-1 if floor is not None else None})
    for payoff in payoffs.values():
        payoff['return_multiple_low']=payoff['net_low']/total
        payoff['return_multiple_high']=payoff['net_high']/total
    checks.update({'保守净期望': floor is not None and floor-stress_cost >= RULES['minimum_ev_usd'],
                   '独立尾部概率覆盖门槛': hurdle is not None and lower is not None
                    and lower >= hurdle + RULES['minimum_probability_margin']})
    return values


def prior_candidate(subject):
    with connect('research') as db:
        row = db.execute("""SELECT payload FROM option_decisions WHERE subject=?
            AND signal IN ('L3','R3') ORDER BY rowid DESC LIMIT 1""", (subject,)).fetchone()
    if row:
        decision = json.loads(row[0])
        if decision['signal'] == 'L3':
            return decision['selected']


def latest(report_id):
    with connect('research') as db:
        row = db.execute('SELECT payload FROM option_decisions WHERE report_id=? ORDER BY rowid DESC LIMIT 1',
                         (report_id,)).fetchone()
    return json.loads(row[0]) if row else None


def archived_subject(subject):
    with connect('research') as db:
        row = db.execute('SELECT state FROM events WHERE event_id=?', (subject,)).fetchone()
    return bool(row and row[0] == 'archived')


def active_decisions():
    """Bring existing L3 plans back into review even when scan filters omit the event."""
    with connect('research') as db:
        rows = db.execute("""SELECT report_id FROM option_decisions d WHERE signal='L3' AND rowid=(
            SELECT MAX(rowid) FROM option_decisions WHERE subject=d.subject AND signal IN ('L3','R3'))
            ORDER BY rowid ASC""")
        return [r[0] for r in rows]


def history(report_id):
    with connect('research') as db:
        rows = db.execute('SELECT payload FROM option_decisions WHERE report_id=? ORDER BY rowid DESC', (report_id,))
        return [json.loads(row[0]) for row in rows]


def snapshot_for(report):
    with connect('research') as db:
        row = db.execute('SELECT payload FROM probability_snapshots WHERE id=? AND subject=?',
                         (report.get('snapshot_id'),report['subject'])).fetchone()
    return json.loads(row[0]) if row else {}


async def reference_ok(requests, contract, symbol):
    result = (await requests.get('/v3/reference/options/contracts/' + contract['ticker'], {})).get('results')
    if not isinstance(result, dict):
        return False
    keys = {'ticker': 'ticker', 'expiration_date': 'expiry', 'shares_per_contract': 'multiplier',
            'contract_type': 'side', 'strike_price': 'strike'}
    return (result.get('underlying_ticker') == symbol and result.get('exercise_style') == 'american'
            and not result.get('additional_underlyings')
            and all(result.get(k) == contract[v] for k,v in keys.items()))


async def collect_contracts(requests, symbol, case, previous, at):
    if hasattr(requests,'collect'):
        return await requests.collect(symbol,case,previous,at)
    begin = at.date() + timedelta(days=RULES['min_days'])
    end = at.date() + timedelta(days=RULES['max_days'])
    params = {'limit': 250, 'expiration_date.gte': begin.isoformat(),
              'expiration_date.lte': end.isoformat(), 'sort':'expiration_date','order':'asc'}
    if case and case['through_date'] >= begin.isoformat() and case['from_date'] <= end.isoformat():
        params.update({'expiration_date.gte': max(begin,date.fromisoformat(case['from_date'])-timedelta(days=7)).isoformat(),
                       'expiration_date.lte': min(end,date.fromisoformat(case['through_date'])+timedelta(days=7)).isoformat(),
                       'contract_type':case['side']})
        center = case['prices']['base']
        params.update({'strike_price.gte':round(center['low']*0.8, 2),
                       'strike_price.lte':round(center['high']*1.2, 2)})
    body = await requests.get('/v3/snapshot/options/' + symbol, params)
    if not isinstance(body.get('results'), list):
        raise SourceError('invalid_response', '期权链响应缺少合约列表')
    normalized = {}
    for row in body['results']:
        item = contract_from(row, symbol)
        if item:
            normalized.setdefault(item['ticker'], item)
    previous_ticker = previous['ticker'] if previous else None
    gaps = []
    if previous_ticker and previous_ticker not in normalized:
        try:
            single = await requests.get(f'/v3/snapshot/options/{symbol}/{previous_ticker}', {})
            item = contract_from(single.get('results'), symbol)
            if item and item['ticker'] == previous_ticker:
                normalized[previous_ticker] = item
        except SourceError as error:
            gaps.append(str(error))
    return list(normalized.values()), bool(body.get('next_url')), body.get('request_id'), gaps


def soft_rank(row):
    return (row.get('probability_hurdle') is not None, all(row['checks'].values()),
            row['checks'].get('近期实时双边报价', False), row.get('ev_low', float('-inf'))
            if row.get('ev_low') is not None else float('-inf'))


def sample_rows(rows, selected, previous):
    """Keep an expiry/strike grid around the primary contract plus a bounded sample."""
    if not rows:
        return []
    anchor = selected or rows[0]
    grid = [r for r in rows if r['side'] == anchor['side'] and
            (r['expiry'] == anchor['expiry'] or r['strike'] == anchor['strike'])]
    keep = [r for r in rows if (previous and r['ticker'] == previous['ticker']) or r is anchor]
    unique = {r['ticker']:r for r in [*keep,*grid,*rows]}
    return sorted(list(unique.values())[:RULES['max_display']], key=lambda r:(r['expiry'],r['side'],r['strike']))


def exit_signal(previous, current, at, disposition, best):
    if not previous:
        return None, None
    if disposition == 'invalidated':
        return 'R3', '核心假设已失效'
    if (date.fromisoformat(previous['expiry'])-at.date()).days <= RULES['exit_days']:
        return 'R3', '进入原计划的到期退出窗口'
    if disposition == 'realized':
        return 'R2', '催化兑现或核心驱动力减弱'
    if disposition == 'overheated':
        return 'R1', '出现过热或新增风险'
    if not current or not all(current['checks'].get(k, False) for k in ('近期实时双边报价','点差、报价数量与未平仓量')):
        return 'R1', '原合约报价或流动性未通过复核'
    if current['stressed_cost'] > RULES['max_contract_loss_usd']:
        return 'R3', '原合约按当前报价超过模拟风险上限'
    if current.get('ev_low') is not None and current['ev_low'] <= 0:
        return 'R2', '原合约的保守净期望已降至零或以下'
    if (not best or current['quote']['ask'] >= previous['quote']['ask'] * (1+RULES['ask_increase_fraction'])
            or not current['checks'].get('独立尾部概率覆盖门槛')):
        return 'R1', '原合约的赔率、价格或证据条件恶化'
    return None, None


async def assess(report, requests, review='normal', note='', at=None, archived=False):
    at = at or datetime.now(timezone.utc)
    snapshot = snapshot_for(report)
    case = snapshot.get('options_case')
    previous = prior_candidate(report['subject'])
    last = latest(report['id'])
    symbol = case['symbol'] if case else next(iter(report['symbols']), None)
    if previous and (not case or symbol != previous.get('underlying')):
        symbol = previous.get('underlying')
        case = None
    if not symbol or not SYMBOL.fullmatch(symbol):
        symbol = None
    disposition = review
    if review == 'normal' and last and last.get('disposition') in ('overheated','realized','invalidated'):
        disposition = last['disposition']
    if archived:
        disposition = 'invalidated'
    elif disposition == 'normal' and case:
        risk = snapshot.get('risk', {})
        disposition = case['risk_gate'] if risk.get('refs') and risk['kind'] not in ('unknown','rumor') else 'watch'
    gaps = []
    partial, request_id = False, None
    rows = []
    active = date.fromisoformat(report['horizon_end']) >= at.date()
    if symbol and snapshot and (active or previous) and disposition != 'invalidated':
        try:
            contracts, partial, request_id, gaps = await collect_contracts(requests,symbol,case,previous,at)
        except SourceError as error:
            contracts = []
            gaps.append(str(error))
        prelim = [evaluate(c,case,snapshot,at) for c in contracts]
        ranked = sorted(prelim,key=soft_rank,reverse=True)
        eligible = [r['ticker'] for r in ranked if r['checks'].get('保守净期望') and
                    all(ok for name,ok in r['checks'].items() if name != '标准交割已核验')]
        for contract in contracts:
            contract['underlying'] = symbol
        by_ticker = {c['ticker']:c for c in contracts}
        for ticker in eligible[:RULES['reference_checks']]:
            try:
                by_ticker[ticker]['contract_verified'] = await reference_ok(requests,by_ticker[ticker],symbol)
            except SourceError as error:
                gaps.append(str(error))
                break
        rows = sorted((evaluate(c,case,snapshot,at) for c in contracts),key=soft_rank,reverse=True)
    if not snapshot:
        gaps.append('需要先保存独立概率快照')
    elif not case:
        gaps.append('独立研究未给出与到期日匹配的结算价区间')
    if not active:
        gaps.append('原研究窗口已过期')
    if not rows and not gaps:
        gaps.append('未返回可识别的美股或 ETF 标准候选合约')
    if rows and not any(r.get('total_cost') is not None for r in rows):
        gaps.append('没有符合时效和点差限制的有效 Bid/Ask，可能缺报价权限或处于非交易时段')

    best = next((r for r in rows if r.get('probability_hurdle') is not None), None)
    risk_ok = (disposition == 'clear' and snapshot.get('risk',{}).get('kind') in ('fact','statement','inference')
               and bool(snapshot['risk'].get('refs')) and not report['omitted_priority']
               and not report.get('review', {}).get('unresolved_conflicts'))
    current = next((r for r in rows if previous and r['ticker'] == previous['ticker']), None)
    if previous and current:
        best = current
    l3 = bool(best and all(best['checks'].values()) and risk_ok and report['level'] == 'L2' and active)
    signal, explanation = exit_signal(previous,current,at,disposition,best if l3 else None)
    if last and last['signal'] == 'R3':
        signal, explanation = 'R3', last['explanation']
    if not signal:
        if l3:
            signal, explanation = 'L3', '我将此合约列为单张模拟买入候选，按当前 Ask、费用和保守到期估值复核。'
        elif best and best['checks']['期限'] and active and report['level'] == 'L2':
            signal, explanation = 'L2.5', '我先跟踪赔率门槛，等待报价、反证和保守收益条件通过。'
        else:
            signal, explanation = report['level'], '我继续观察证据和可用报价。'
        if not l3 and rows:
            reference=next((r for r in rows if r.get('reference_only') and r.get('total_cost') is not None),None)
            if reference:
                explanation='我仅计算历史或延迟报价的参考赔率，等待更新报价、交割及执行条件确认。'
                if reference.get('probability_hurdle') is None:
                    explanation+='独立价格情景或概率证据不足，暂时无法判断是否值得承担成本。'
                elif reference.get('ev_low') is not None and reference['ev_low']<0:
                    explanation+='该参考价格下的保守净期望未通过。'
    else:
        action = {'R1':'我停止追仓。','R2':'我研究减仓或兑现。','R3':'我退出原模拟计划。'}
        explanation = action[signal] + explanation + '。'
    state = {'id':uuid4().hex,'report_id':report['id'],'subject':report['subject'],
             'created_at':at.isoformat(),'signal':signal,'explanation':explanation,
             'probability_snapshot':report.get('snapshot_id'),'data_provider':'MarketData / 公开网页' if hasattr(requests,'collect') else 'Massive',
             'chain_request_id':request_id,'partial_chain':partial,'symbol':symbol,
             'quote_valid_through': (datetime.fromtimestamp(best['quote']['last_updated']/1e9,timezone.utc)
                 + timedelta(seconds=RULES['max_quote_age_seconds'])).isoformat() if signal == 'L3' else None,
             'rules':RULES, 'review':review,'disposition':disposition,'note':note,'risk_gate':risk_ok,
             'search_leads':getattr(requests,'leads',[]),
             'gaps':list(dict.fromkeys(gaps)),'selected':best if signal == 'L3' else None,
             'previous_contract':previous['ticker'] if previous else None,
             'quotes':sample_rows(rows, best, previous)}
    with connect('research') as db:
        db.execute('INSERT INTO option_decisions VALUES (?,?,?,?,?,?)',
                   (state['id'],report['id'],report['subject'],state['created_at'],signal,json_text(state)))
    return state

