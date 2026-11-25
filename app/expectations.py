from collections import defaultdict
from datetime import date
import math
from statistics import median

from .collection import SourceError
from .db import now


def numeric(value):
    return type(value) in (float, int) and math.isfinite(value)


def summarize_chain(records, as_of):
    """Compare observed IV within an expiry. Keep provenance for each selected observation."""
    buckets = defaultdict(list)
    for row in records:
        if not isinstance(row, dict):
            continue
        contract = row.get('details') or {}
        underlying = row.get('underlying_asset') or {}
        greeks = row.get('greeks') or {}
        quote = row.get('last_quote') or {}
        if not all(isinstance(v,dict) for v in (contract,underlying,greeks,quote)):
            continue
        iv, spot, strike = row.get('implied_volatility'), underlying.get('price'), contract.get('strike_price')
        if not all(numeric(v) and v > 0 for v in (iv,spot,strike)):
            continue
        expiry = contract.get('expiration_date')
        try:
            valid_expiry = date.fromisoformat(expiry) >= date.fromisoformat(as_of[:10])
        except (ValueError, TypeError):
            continue
        if not valid_expiry or contract.get('shares_per_contract') != 100:
            continue
        buckets[expiry].append({'contract':contract.get('ticker'),'side':contract.get('contract_type'),
                                'distance':abs(strike/spot-1), 'iv':iv,'delta':greeks.get('delta'),
                                'quote_time_ns':quote.get('last_updated'), 'feed_status':quote.get('timeframe'),
                                'spot':spot,'strike':strike})
    terms = []
    for expiry, points in sorted(buckets.items()):
        near = [p for p in points if p['distance'] <= 0.025]
        wings = []
        for side, target in [('put',-0.25),('call',0.25)]:
            usable = [p for p in points if p['side']==side and numeric(p['delta']) and abs(p['delta']-target)<=0.05]
            if usable:
                wings.append(min(usable,key=lambda p:abs(p['delta']-target)))
        terms.append({'expiry':expiry, 'near_atm_iv':median([p['iv'] for p in near]) if near else None,
                      'put_minus_call_iv':wings[0]['iv']-wings[1]['iv'] if len(wings)==2 else None,
                      'observations':near+wings})
    return terms


async def market_view(candidate, requests):
    view = {'assessed_at':now(),'volume':[{'symbol':m['symbol'],'ratio':m.get('volume_ratio')} for m in candidate['metrics']],
            'assessment':'标的成交量为拥挤程度的有限线索。期货期限结构与资金流覆盖不足，风险计价程度未知。'}
    if requests is None or not candidate['symbols']:
        return view
    if hasattr(requests,'collect'):
        view['options_gap']='MarketData 按锁定情景在期权复核时获取有限合约；当前风险是否充分定价仍待验证'
        return view
    symbol = candidate['symbols'][0]
    try:
        body = await requests.get('/v3/snapshot/options/'+symbol,{'limit':250,'sort':'expiration_date','order':'asc'})
        if not isinstance(body.get('results'),list):
            raise SourceError('invalid_response','期权 IV 样本不可用')
        terms = summarize_chain(body['results'],view['assessed_at'])
        view.update(symbol=symbol,terms=terms,partial_chain=bool(body.get('next_url')),
                    assessment='按到期日比较样本中近平值 IV 和相近绝对 Delta 的看跌减看涨 IV。'
                               '截面和报价时效受数据权限限制，风险是否充分计价仍需历史参照。')
    except SourceError as exc:
        view['options_gap'] = str(exc)
    return view

