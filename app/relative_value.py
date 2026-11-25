import math
import statistics as st
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .collection import Requests, SourceError
from .db import connect
from .research import UNIVERSE, timestamp


with (Path(__file__).resolve().parents[1] / 'config/research.toml').open('rb') as file:
    RULES = tomllib.load(file)


async def refresh_history(symbols, config, secrets, spent):
    feed = 'sip' if config['feed'] == 'delayed_sip' else config['feed']
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    params = {'symbols': ','.join(sorted(symbols)), 'timeframe': '1Day', 'adjustment': 'all',
              'feed': feed, 'currency': 'USD', 'limit': 10000, 'end': end.isoformat(),
              'start': (end - timedelta(days=130)).isoformat()}
    received = 0
    async with httpx.AsyncClient(timeout=20) as client:
        requests = Requests(client, 'market', config, secrets)
        requests.attempts = spent
        try:
            while True:
                payload = await requests.get('/v2/stocks/bars', params)
                bars = payload.get('bars')
                if not isinstance(bars, dict):
                    raise SourceError('invalid_response', '日线响应格式无效')
                rows = []
                for symbol, records in bars.items():
                    if symbol not in symbols or not isinstance(records, list):
                        continue
                    for bar in records:
                        if not isinstance(bar, dict):
                            continue
                        t = timestamp(bar.get('t'))
                        close, volume = bar.get('c'), bar.get('v')
                        if not t or t[:10] > end.date().isoformat():
                            continue
                        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (close, volume)):
                            continue
                        if close > 0 and volume >= 0:
                            rows.append((feed, symbol, t[:10], close, volume))
                with connect('research') as db:
                    db.executemany('INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?)', rows)
                received += len(rows)
                token = payload.get('next_page_token')
                if not token:
                    return {'status': 'ok', 'bars': received, 'feed': feed}
                if token == params.get('page_token'):
                    raise SourceError('invalid_response', '日线分页异常')
                params['page_token'] = token
        except SourceError as exc:
            return {'status': exc.status, 'message': str(exc), 'bars': received, 'feed': feed}


def correlation(x, y):
    if len(x) < 2 or not st.pstdev(x) or not st.pstdev(y):
        return None
    return round(st.correlation(x, y), 5)


def features(symbol, feed):
    benchmark = RULES['benchmarks'].get(symbol)
    result = {'symbol': symbol, 'benchmark': benchmark, 'feed': feed, 'adjustment': 'all',
              'currency': 'USD', 'source_url': 'https://docs.alpaca.markets/us/reference/stockbars',
              'triggers': [], 'gaps': [], 'relative': None}
    with connect('research') as db:
        def series(ticker):
            rows = db.execute('SELECT day,close,volume FROM daily_bars WHERE symbol=? AND feed=? ORDER BY day DESC LIMIT 90',
                              (ticker, feed))
            return {r['day']: (r['close'], r['volume']) for r in rows}
        asset = series(symbol)
        ref = series(benchmark)
    days = sorted(asset)
    if len(days) < 2:
        result['gaps'].append('可比完整日线不足')
        return result
    last = days[-1]
    fresh = 0 <= (datetime.now(timezone.utc).date() - date.fromisoformat(last)).days <= RULES['max_age_days']
    change = asset[last][0] / asset[days[-2]][0] - 1
    window = RULES['rolling_sessions']
    volumes = [asset[d][1] for d in days[-window-1:-1]]
    typical = st.median(volumes) if len(volumes) == window else 0
    volume_ratio = asset[last][1] / typical if typical else None
    result.update(as_of=last, adjusted_close=asset[last][0], daily_return=round(change, 6),
                  volume_ratio=round(volume_ratio, 4) if volume_ratio is not None else None, fresh=fresh)
    if fresh and abs(change) >= RULES['return_threshold']:
        result['triggers'].append('日收益异常')
    if fresh and volume_ratio and volume_ratio >= RULES['volume_multiple']:
        result['triggers'].append('成交量异常')
    if not fresh:
        result['gaps'].append('行情已过研究时效')
    aligned = sorted(asset.keys() & ref.keys())
    if len(aligned) < RULES['min_sessions'] or aligned[-1] != last:
        result['gaps'].append('缺少经济基准或同步历史样本')
        return result
    x = [ref[b][0]/ref[a][0]-1 for a,b in zip(aligned, aligned[1:])]
    y = [asset[b][0]/asset[a][0]-1 for a,b in zip(aligned, aligned[1:])]
    if not st.variance(x[:-1]):
        result['gaps'].append('基准收益方差为零')
        return result
    slope = st.covariance(x[:-1], y[:-1]) / st.variance(x[:-1])
    intercept = st.mean(y[:-1]) - slope * st.mean(x[:-1])
    residuals = [b - intercept - slope*a for a,b in zip(x,y)]
    residual = residuals[-1]
    sd = st.pstdev(residuals[:-1])
    z = residual/sd if sd > 1e-10 else None
    current = correlation(x[-window:], y[-window:])
    prior = correlation(x[-2*window:-window], y[-2*window:-window])
    stable = (current is not None and prior is not None and min(current,prior) >= RULES['minimum_correlation']
              and abs(current-prior) <= RULES['maximum_correlation_change'])
    result['relative'] = {'sessions': len(aligned), 'correlation': current, 'prior_correlation': prior,
                          'stable': stable, 'residual_return': round(residual,6), 'z': round(z,4) if z is not None else None,
                          'percentile': round(sum(r <= residual for r in residuals[:-1]) / (len(residuals)-1),4),
                          'rationale': '比较共同美股市场敞口，残差衡量当日特异收益；回归条件需独立催化证据。'}
    if fresh and stable and z is not None and abs(z) >= RULES['residual_z']:
        result['triggers'].append('基准调整后收益背离')
    return result

