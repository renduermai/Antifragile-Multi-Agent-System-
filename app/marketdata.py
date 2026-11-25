"""Bounded MarketData requests, durable cache and credit reservations."""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, model_validator
from typing import Literal

from .db import connect, now
from .providers import HttpProvider
from .research import digest, json_text


class Query(BaseModel):
    operation: Literal['expirations','chain','quotes']
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.]{0,10}$')
    contract: str | None = Field(default=None,pattern=r'^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$')
    expiration: str | None = None
    side: Literal['call','put'] = 'call'
    strike_low: float | None = Field(default=None,gt=0)
    strike_high: float | None = Field(default=None,gt=0)
    strike_limit: int = Field(default=3,ge=1,le=10)
    date: str | None = None
    from_date: str | None = None
    to_date: str | None = None

    @model_validator(mode='after')
    def bounded(self):
        for value in (self.expiration,self.date,self.from_date,self.to_date):
            if value:
                datetime.strptime(value,'%Y-%m-%d')
        if self.date and (self.from_date or self.to_date):
            raise ValueError('历史单日与区间不能同时指定')
        if bool(self.from_date) != bool(self.to_date):
            raise ValueError('历史区间需同时填写起止日期')
        if self.from_date and (datetime.fromisoformat(self.to_date)-datetime.fromisoformat(self.from_date)).days not in range(31):
            raise ValueError('单次历史区间限 0—30 天')
        if self.operation=='quotes' and (not self.contract or re.match(r'^([A-Z]+)[0-9]',self.contract).group(1)!=self.symbol):
            raise ValueError('请填写属于该标的的 OCC 合约代码')
        if self.operation=='chain' and (not self.expiration or self.strike_low is None or self.strike_high is None or self.strike_low>self.strike_high):
            raise ValueError('期权链需指定单一到期日及有效行权价范围')
        if self.operation!='quotes' and self.from_date:
            raise ValueError('历史日期区间只用于单合约日终报价')
        return self


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS marketdata_requests (
            id INTEGER PRIMARY KEY, period TEXT NOT NULL, created_at TEXT NOT NULL,
            reserved INTEGER NOT NULL, consumed REAL, estimated INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, headers TEXT, cache_key TEXT);
        CREATE TABLE IF NOT EXISTS marketdata_cache (
            key TEXT PRIMARY KEY, acquired_at TEXT NOT NULL, body TEXT NOT NULL);
    """)


def period():
    dt=datetime.now(ZoneInfo('America/New_York'))
    if (dt.hour,dt.minute)<(9,30):
        dt-=timedelta(days=1)
    return dt.date().isoformat()


def budget():
    with connect() as db:
        used=db.execute('SELECT COALESCE(SUM(COALESCE(consumed,reserved)),0) FROM marketdata_requests WHERE period=?',(period(),)).fetchone()[0]
        row=db.execute("SELECT headers FROM marketdata_requests WHERE headers IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return {'used_or_reserved':used,'period':period(),'reset_rule':'美东 09:30',
            'account_headers':json.loads(row[0]) if row else {},'balance_note':'仅以最近响应报告账户额度；缺失时未知'}


def normalize(body,query,acquired,url):
    symbols=body.get('optionSymbol',[])
    if not isinstance(symbols,list):
        raise ValueError('合约代码数组无效')
    for key,value in body.items():
        if isinstance(value,list) and len(value)!=len(symbols):
            raise ValueError('期权响应数组长度不一致')
    rows=[]
    for i,symbol in enumerate(symbols):
        fields={key:value[i] for key,value in body.items() if isinstance(value,list)}
        if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}',symbol):
            raise ValueError('上游 OCC 合约代码无效')
        stamp=fields.get('updated')
        observed=datetime.fromtimestamp(stamp,timezone.utc).isoformat() if type(stamp) in (int,float) and math.isfinite(stamp) and 0<stamp<32503680000 else None
        age=(datetime.fromisoformat(acquired)-datetime.fromisoformat(observed)).total_seconds() if observed else None
        historical=bool(query.date or query.from_date)
        rows.append({'source':'marketdata','underlying':query.symbol,'contract':symbol,
                     **{k:fields.get(k) for k in ('side','expiration','strike','bid','ask','last','volume','openInterest','iv','delta','gamma','theta','vega','underlyingPrice','bidSize','askSize')},
                     'currency':'USD','price_unit':'USD/份','multiplier':None,'observed_at':observed,'acquired_at':acquired,
                     'delay_seconds':None,'age_seconds':age,'data_type':'historical_eod' if historical else 'delayed_or_unverified',
                     'execution_eligible':False,'source_url':url,'raw':fields})
    return rows


async def query(value,cfg,secret):
    if not cfg.get('enabled',True):
        raise ValueError('MarketData 已停用')
    base,headers=HttpProvider('marketdata').connection(cfg,secret)
    if base.rstrip('/').endswith('/v1/v1'):
        raise ValueError('Base URL 重复包含 v1')
    params={}
    if value.date: params['date']=value.date
    if value.from_date:params.update({'from':value.from_date,'to':value.to_date})
    path=f"/options/{value.operation}/{value.contract if value.operation=='quotes' else value.symbol}/"
    if value.operation=='chain':
        params.update(expiration=value.expiration,side=value.side,strike=f'{value.strike_low}-{value.strike_high}',strikeLimit=value.strike_limit)
    # Reserve a conservative per-symbol bound even where historical billing is cheaper.
    reserve=value.strike_limit if value.operation=='chain' else 31 if value.from_date else 1
    key=digest([base,secret.get('api_key'),path,params])
    with connect() as db:
        cached=db.execute('SELECT acquired_at,body FROM marketdata_cache WHERE key=?',(key,)).fetchone()
        if cached and (value.date or value.from_date or (datetime.now(timezone.utc)-datetime.fromisoformat(cached[0])).total_seconds()<900):
            return {**json.loads(cached[1]),'cached':True,'budget':budget()}
        db.execute('BEGIN IMMEDIATE')
        used=db.execute('SELECT COALESCE(SUM(COALESCE(consumed,reserved)),0) FROM marketdata_requests WHERE period=?',(period(),)).fetchone()[0]
        count=db.execute('SELECT COUNT(*) FROM marketdata_requests WHERE period=?',(period(),)).fetchone()[0]
        if count>=cfg.get('daily_limit',20):
            raise ValueError('MarketData 每日请求次数上限已达到')
        if used+reserve>cfg.get('credit_limit',80):
            raise ValueError('MarketData 每日积分预算不足，已保存的资料仍可查看')
        rid=db.execute('INSERT INTO marketdata_requests(period,created_at,reserved,status,cache_key) VALUES (?,?,?, ?,?)',
                       (period(),now(),reserve,'pending',key)).lastrowid
    consumed=None
    rate={}
    status='failed'
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            response=await client.get(base+path,headers=headers,params=params)
        for name in ('limit','remaining','reset','consumed'):
            text=response.headers.get('X-Api-Ratelimit-'+name)
            if text is not None:
                try:
                    number=float(text)
                    if math.isfinite(number) and number>=0:rate[name]=number
                except ValueError:pass
        consumed=rate.get('consumed')
        if consumed is None and response.status_code not in (200,203):
            consumed=0
        if response.status_code not in (200,203,204):
            raise ValueError({401:'MarketData 认证失败',403:'MarketData 账户权限不足',429:'MarketData 额度或并发限制'}.get(response.status_code,f'MarketData HTTP {response.status_code}'))
        body=response.json() if response.status_code!=204 else {'s':'no_data'}
        if not isinstance(body,dict):
            raise ValueError('MarketData 响应必须是对象')
        if body.get('s') not in ('ok','no_data'):
            raise ValueError('MarketData 未返回有效数据')
        acquired=now()
        result={'source':'marketdata','acquired_at':acquired,'query':value.model_dump(),
                'raw':body,'headers':rate,'cached':False,
                'rows':normalize(body,value,acquired,base+path) if value.operation!='expirations' and body.get('s')=='ok' else [],
                'expirations':body.get('expirations',[]),
                'notice':'历史或延迟报价仅供研究，标准交割和当前可成交性尚待核验'}
        if not isinstance(result['expirations'],list):raise ValueError('到期日列表无效')
        status='success'
        with connect() as db:
            db.execute('INSERT OR REPLACE INTO marketdata_cache VALUES (?,?,?)',(key,acquired,json_text(result)))
    except httpx.HTTPError:
        raise ValueError('MarketData 网络失败或超时') from None
    finally:
        with connect() as db:
            db.execute('UPDATE marketdata_requests SET status=?, consumed=?, estimated=?,headers=? WHERE id=?',
                       (status,consumed,int(consumed is None),json_text(rate),rid))
    return {**result,'budget':budget()}

