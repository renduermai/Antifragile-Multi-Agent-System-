"""A12 can read a validation version only after a complete forecast was locked."""
import json
import math
from datetime import date
from itertools import product
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .case_forecast import CaseConflict, get_prediction, references
from .case_material import CaseInputError, timestamp, valid_url
from .db import connect, now
from .research import digest, json_text


VERSION = 'A12-5.0'
AGENTS = {
    'evidence':'A3 新闻证据', 'conflicts':'A4 矛盾识别', 'facts':'A3/A12 事实校验',
    'scenarios':'A6 情景推演', 'probability':'A7 概率估计', 'timing':'A7 时间窗口',
    'temporal':'A12 历史隔离', 'options':'A10 期权计算', 'decision':'A11 风险决策',
    'market':'A7/A12 实际行情', 'returns':'A10/A12 收益审计',
}


SUGGESTIONS = {
    'evidence':'补齐可回溯的公开证据或检查证据检索与取舍。',
    'conflicts':'逐条比对互相冲突的来源及各自的公开时间。',
    'facts':'核对纠错来源、引文和事实结论之间的对应关系。',
    'scenarios':'为缺失分支补充触发条件、时间窗口、价格传导和改判条件。',
    'probability':'补充独立概率依据，跨案例比较 Brier 分数。',
    'timing':'标注事件发生、公开的准确时点及时间窗口依据。',
    'temporal':'核查每条材料的原始公开时间，移除超出截断时间的材料。',
    'options':'补齐同一时点可核对的 Bid/Ask 或核查计算差异。',
    'decision':'复核报价、反证、风险预算与信号等级的对应关系。',
    'market':'补充同一标的与结算窗口可对照的归档行情。',
    'returns':'补齐同一合约的退出报价及可核对的独立成交记录。',
}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Correction(Strict):
    forecast_text: str = Field(min_length=4, max_length=1200)
    actual_fact: str = Field(min_length=1, max_length=1000)
    url: str

    @field_validator('url')
    @classmethod
    def link(cls, value):
        if not valid_url(value):
            raise ValueError('请提供可复查的事实来源')
        return value


class Reference(Strict):
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    conflict_ids: list[str] = Field(default_factory=list, max_length=100)
    corrections: list[Correction] = Field(default_factory=list, max_length=50)
    notes: str = Field(default='', max_length=5000)


class Outcome(Strict):
    summary: str = Field(default='', max_length=5000)
    branch: str | None = Field(default=None, pattern='^(base|up|down)$')
    event: bool | None = None
    occurred_at: str | None = None
    published_at: str | None = None
    terminal_price: float | None = Field(default=None, ge=0)
    terminal_at: str | None = None
    symbol: str | None = None
    price_url: str | None = None

    @model_validator(mode='after')
    def check_times(self):
        for value in (self.occurred_at,self.published_at,self.terminal_at):
            if value is not None:
                timestamp(value)
        if self.occurred_at and self.published_at and timestamp(self.occurred_at) > timestamp(self.published_at):
            raise ValueError('事件公开时间早于发生时间')
        if self.terminal_price is not None and not (self.symbol and self.terminal_at and self.price_url and valid_url(self.price_url)):
            raise ValueError('行情需要标的、价格、时间及可核对来源')
        return self


class ExitEvidence(Strict):
    ticker: str
    observed_at: str
    bid: float = Field(ge=0)
    bid_size: int = Field(ge=1)
    url: str

    @model_validator(mode='after')
    def check(self):
        timestamp(self.observed_at)
        if not valid_url(self.url):
            raise ValueError('请提供退出报价的归档来源')
        return self


class ValidationRound(Strict):
    cutoff: str
    outcome: Outcome = Field(default_factory=Outcome)
    reference: Reference = Field(default_factory=Reference)
    exit_quote: ExitEvidence | None = None
    actual_trade_pnl_usd: float | None = None

    @field_validator('cutoff')
    @classmethod
    def canonical(cls, value):
        return timestamp(value).isoformat()


def manifest(text, cutoff):
    try:
        raw = json.loads(text)
        if not isinstance(raw,dict) or set(raw) != {'rounds'}:
            raise ValueError()
        rounds = raw['rounds']
        if not isinstance(rounds,list) or any(not isinstance(r,dict) for r in rounds):
            raise ValueError()
        rows = [r for r in rounds if timestamp(r.get('cutoff')).isoformat() == cutoff]
        if len(rows) != 1:
            raise CaseInputError('验证 TXT 需要且只能包含一个与预测截断时间匹配的条目')
        return ValidationRound.model_validate(rows[0])
    except CaseInputError:
        raise
    except (ValueError, ValidationError, TypeError):
        raise CaseInputError('验证 TXT 格式无效，请按说明填写 rounds、cutoff 和已有的结果或参考字段') from None


def expected_at_vertices(intervals, payouts):
    """Recompute the probability-constrained range by enumerating simplex vertices."""
    keys = list(payouts)
    if any(intervals[k] is None for k in keys):
        return []
    results = []
    for free in keys:
        fixed = [k for k in keys if k != free]
        edges = [[intervals[k]['low'],intervals[k]['high']] for k in fixed]
        for point in product(*edges):
            slack = 1-sum(point)
            if intervals[free]['low']-1e-7 <= slack <= intervals[free]['high']+1e-7:
                results.append(sum(payouts[k]*p for k,p in zip(fixed,point)) + payouts[free]*slack)
    return results


def compare_options(selected, intervals, rules):
    expected = {}
    if not selected:
        return expected
    q, strike, count = selected['quote'], selected['strike'], selected['multiplier']
    ask = q['ask']
    fees = selected['fees']
    sign = 1 if selected['side'] == 'call' else -1
    total = ask * count + fees
    expected.update(ask_debit=ask*count, total_cost=total, max_loss=total, breakeven=strike+sign*total/count)
    if 'payoffs' in selected:
        discount = rules['settlement_value_fraction']
        stress = total+(ask-q['bid'])*rules['entry_spread_fraction']*count
        expected['stressed_cost'] = stress
        lows, highs = {}, {}
        for branch, values in selected['payoffs'].items():
            intrinsic = sorted(max(sign*(end-strike),0)*count for end in values['price'].values())
            expected.update({branch+'_net_low':intrinsic[0]-total,branch+'_net_high':intrinsic[1]-total})
            lows[branch], highs[branch] = intrinsic[0]*discount, intrinsic[1]
        success = lows['up' if sign==1 else 'down']
        expected.update(success_gross=success,failure_gross=0)
        if success > 0:
            expected['probability_hurdle'] = stress/success
        low_ev, high_ev = expected_at_vertices(intervals,lows), expected_at_vertices(intervals,highs)
        if low_ev and high_ev:
            expected.update(ev_low=min(low_ev)-stress,ev_high=max(high_ev)-total)
    actual = {**selected, **{branch+'_'+key:value for branch,row in selected.get('payoffs',{}).items()
                            for key,value in row.items() if key in ('net_low','net_high')}}
    return {key:{'recorded':actual.get(key),'recomputed':value,'match':actual.get(key) is not None
                 and math.isclose(actual[key],value,rel_tol=1e-7,abs_tol=1e-7)} for key,value in expected.items()}


def coverage(expected, available, cited):
    keys = set(expected)
    visible = keys & available
    found, missed = sorted(visible & cited), sorted(visible - cited)
    return len(found)/len(visible) if visible else None, {'found':found,'missed':missed,
           'outside_visible_material':sorted(keys-available)}


def score(snapshot, validation):
    analysis = snapshot['analysis']
    cutoff = timestamp(snapshot['cutoff'])
    horizon = date.fromisoformat(snapshot['horizon_end'])
    source_index = {s['id']:s for s in snapshot['sources']}
    findings = [v for v in analysis.values() if isinstance(v,dict) and 'refs' in v]
    findings += [*analysis['action_tree'], *(s['basis'] for s in analysis['scenarios']), analysis['joint']['basis']]
    refs = [ref for finding in findings for ref in finding['refs']]
    verified = {ref['id'] for ref in refs if ref['id'] in source_index and ref['quote'] in source_index[ref['id']]['text']}
    unverified = [ref for ref in refs if ref['id'] not in verified or ref['quote'] not in source_index[ref['id']]['text']]
    late_evidence = [s['id'] for s in source_index.values() if timestamp(s['published']) > cutoff]
    simulation = snapshot['simulation']
    late_quotes = [q['evidence_id'] for q in simulation['quotes']
                   if timestamp(q['observed_at']) > cutoff or timestamp(q['published_at']) > cutoff]
    forecast_text = '\n'.join(f['text'] for f in findings)
    forecast_text += '\n' + '\n'.join(s['condition']+' '+s['price_response'] for s in analysis['scenarios'])
    reference = validation.reference
    outcome = validation.outcome
    rows = {}

    def entry(key, result, summary, **details):
        rows[key] = {'score':round(result*100,2) if result is not None else None,
                     'summary':summary,'agent':AGENTS[key],**details}

    mark, data = coverage(reference.evidence_ids,set(source_index),verified)
    entry('evidence',mark,'通过逐字引文核对关键证据',**data, cited=sorted(verified))
    mark, data = coverage(reference.conflict_ids,set(source_index),references(analysis['claims_and_conflicts']) & verified)
    entry('conflicts',mark,'核对矛盾分析里实际引用的材料',**data)
    corrections = [c.model_dump() for c in reference.corrections if c.forecast_text in forecast_text]
    entry('facts',0 if corrections or unverified else None,
          '以可复查的明确纠错和引文校验为准', corrections=corrections,
          unmatched_reference_count=len(reference.corrections)-len(corrections), unverified_quotes=unverified)
    branches = {s['branch']:s for s in analysis['scenarios']}
    complete = [k for k,row in branches.items() if all(row[f].strip() for f in ('condition','occurrence_window',
                'public_window','price_response','change_if')) and row['basis']['kind'] != 'unknown']
    entry('scenarios',len(complete)/3,'核对情景的条件、窗口、传导、改判项和依据',complete=complete)
    branch = branches.get(outcome.branch)
    band = branch['probability'] if branch else None
    joint = analysis['joint']
    interval = joint['event']
    brier = None
    if interval and outcome.event is not None:
        midpoint = (interval['low']+interval['high'])/2
        brier = (midpoint-int(outcome.event))**2
    entry('probability',1-brier if brier is not None else None,
          '实际事件的单次 Brier 分数使用事前区间中点，校准需要更多案例',
          brier=brier,event_interval=interval,outcome_branch=outcome.branch,outcome_branch_probability=band)
    timely = None
    if outcome.occurred_at and outcome.published_at:
        occurred, public = timestamp(outcome.occurred_at), timestamp(outcome.published_at)
        timely = cutoff < occurred <= public and public.date() <= horizon
    timely_interval = joint['timely']
    if outcome.event is False:
        timely = False
    timing_brier = None
    if timely_interval is not None and timely is not None:
        midpoint = (timely_interval['low']+timely_interval['high'])/2
        timing_brier = (midpoint-int(timely))**2
    entry('timing',1-timing_brier if timing_brier is not None else None,
          '对照实际发生和公开时间，依据事前联合概率计算 Brier 分数', brier=timing_brier,
          within_locked_window=timely,horizon=snapshot['horizon_end'],
          forecast_windows={k:{f:s[f] for f in ('occurrence_window','public_window')} for k,s in branches.items()})
    entry('temporal',0 if late_evidence or late_quotes else 1,'核对快照所载新闻公开时间及报价时间',
          late_evidence=late_evidence,late_quotes=late_quotes)
    intervals = {k: v['probability'] for k,v in branches.items()}
    money = [compare_options(row,intervals,simulation['rules'])
             for row in simulation['quotes'] if row.get('total_cost') is not None]
    checks = [item['match'] for group in money for item in group.values()]
    entry('options',sum(checks)/len(checks) if checks else None,'复算归档报价的每张成本、保本、情景损益与赔率',
          calculation=money, gap=simulation['gap'])
    selected = simulation['selected']
    compliant = selected is None or (simulation['signal']=='L3' and simulation['risk_gate']
                and all(selected['checks'].values()))
    entry('decision',int(compliant),'仅按事前的证据、报价和风控检查流程一致性',
          signal=simulation['signal'], execution=simulation['execution'],decision=simulation['decision'],
          observed_event=outcome.event)
    case = analysis.get('options_case')
    matches = []
    assessed_price = False
    if case and outcome.terminal_price is not None and case['symbol'] == outcome.symbol:
        observed = timestamp(outcome.terminal_at)
        if cutoff < observed and case['from_date'] <= observed.date().isoformat() <= case['through_date']:
            assessed_price = True
            matches = [k for k,b in case['prices'].items() if b['low'] <= outcome.terminal_price <= b['high']]
    entry('market',(int(outcome.branch in matches) if outcome.branch else int(bool(matches))) if assessed_price else None,
          '比较同一标的的实际到期窗口行情与事前价带', matching_bands=matches,
          provided_price=outcome.terminal_price,price_source=outcome.price_url)
    exit_quote = validation.exit_quote
    executable = None
    if selected and exit_quote and exit_quote.ticker == selected['ticker']:
        exit_at = timestamp(exit_quote.observed_at)
        if cutoff < exit_at and exit_at.date().isoformat() <= selected['expiry']:
            executable = exit_quote.bid * selected['multiplier']-selected['total_cost']
    entry('returns',None,'分别记录条件模拟、归档双边报价估算与用户提供的真实交易结果',
          hypothetical_range=[selected['ev_low'],selected['ev_high']] if selected else None,
          quote_based_pnl_usd=executable, reported_actual_trade_pnl_usd=validation.actual_trade_pnl_usd)
    scored = [v['score'] for v in rows.values() if v['score'] is not None]
    overall = round(sum(scored)/len(scored),1) if scored else None
    improvements = [{'agent':v['agent'],'metric':k,'suggestion':SUGGESTIONS[k]} for k,v in rows.items()
                    if v['score'] is None or v['score'] < 100]
    return {'scores':rows,'overall':overall,'scored_count':len(scored),'actual':outcome.model_dump(),
            'reference_notes':reference.notes,'improvements':improvements,
            'evaluation':'按有可核对数据的分项等权汇总。实际盈亏独立记录，空仓依事前条件复核。'}


def public(row):
    return {**dict(row),'result':json.loads(row['result'])}


def evaluate(case_id, forecast_id, version=None):
    prediction = get_prediction(case_id,forecast_id)
    if prediction['status'] != 'locked' or digest(prediction['snapshot']) != prediction['snapshot_hash']:
        raise CaseConflict('预测尚未完整锁定或校验失败')
    with connect('validation') as db:
        if version is None:
            row = db.execute('SELECT id,body FROM validation_versions WHERE case_id=? ORDER BY id DESC LIMIT 1',
                             (case_id,)).fetchone()
        else:
            row = db.execute('SELECT id,body FROM validation_versions WHERE case_id=? AND id=?',
                             (case_id,version)).fetchone()
        if not row or not row['body'].strip():
            raise CaseInputError('验证 TXT 为空，请先保存与该轮对应的验证材料')
        evaluation = manifest(row['body'],prediction['cutoff'])
        result = score(prediction['snapshot'],evaluation)
        db.execute("""INSERT OR IGNORE INTO case_evaluations VALUES (?,?,?,?,?,?,?,?)""",
            (uuid4().hex,case_id,forecast_id,row['id'],prediction['snapshot_hash'],VERSION,now(),json_text(result)))
        stored = db.execute("""SELECT * FROM case_evaluations
            WHERE forecast_id=? AND validation_version=? AND evaluator_version=?""",
            (forecast_id,row['id'],VERSION)).fetchone()
        return public(stored)


def summaries(case_id):
    with connect('validation') as db:
        rows = db.execute("""SELECT forecast_id,validation_version,evaluator_version,result
            FROM case_evaluations e WHERE case_id=? AND rowid=(SELECT MAX(rowid) FROM case_evaluations
              WHERE forecast_id=e.forecast_id)""",(case_id,))
        return {row['forecast_id']:{'validation_version':row['validation_version'],
            'evaluator_version':row['evaluator_version'],'overall':json.loads(row['result'])['overall']}
            for row in rows}


def history(case_id, forecast_id):
    prediction = get_prediction(case_id,forecast_id)
    if prediction['status'] != 'locked':
        return []
    with connect('validation') as db:
        return [public(row) for row in db.execute('''SELECT * FROM case_evaluations
            WHERE case_id=? AND forecast_id=? ORDER BY rowid DESC''',(case_id,forecast_id))]

