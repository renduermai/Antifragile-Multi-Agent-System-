"""Option source adapters. Public search results remain leads unless structured quotes exist."""
import json
import httpx
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from .collection import Requests, SourceError
from .db import connect, now
from .marketdata import Query, query
from .research import digest, json_text


def normalize(row, symbol):
    match=re.fullmatch(r'([A-Z]{1,6})(\d{6})([CP])(\d{8})',row.get('contract',''))
    if not match or match[1]!=symbol or not row.get('observed_at'):
        return None
    expiry=datetime.strptime(match[2],'%y%m%d').date().isoformat()
    stamp=datetime.fromisoformat(row['observed_at'])
    available=stamp
    if row.get('data_type')=='historical_eod':
        day=max(stamp.date(),stamp.astimezone(ZoneInfo('America/New_York')).date())+timedelta(days=1)
        available=max(stamp,datetime.combine(day,datetime.min.time(),ZoneInfo('America/New_York')))
    return {'ticker':'O:'+row['contract'],'side':'call' if match[3]=='C' else 'put',
            'strike':int(match[4])/1000,'expiry':expiry,'multiplier':row.get('multiplier') or 100,
            'multiplier_assumed':row.get('multiplier') is None,'contract_verified':False,
            'open_interest':row.get('openInterest'),'spot':row.get('underlyingPrice'),
            'quote':{'bid':row.get('bid'),'ask':row.get('ask'),'bid_size':row.get('bidSize'),
                     'ask_size':row.get('askSize'),'last_updated':int(stamp.timestamp()*1e9),
                     'timeframe':'HISTORICAL EOD' if row.get('data_type')=='historical_eod' else 'DELAYED / UNVERIFIED'},
            'last_trade':{'price':row.get('last'),'sip_timestamp':None,'timeframe':row.get('data_type')},
            'source':row.get('source'),'source_url':row.get('source_url'),'raw':row.get('raw'),
            'observed_at':stamp.isoformat(),'available_at':available.isoformat(),
            'published_at':available.isoformat(),'evidence_id':digest([row.get('source'),row.get('contract'),row.get('observed_at'),row.get('source_url')]),
            'reference_only':True,'iv':row.get('iv'),'delta':row.get('delta')}


class MarketOptions:
    def __init__(self,client,cfg,secret,search_cfg,search_secret):
        self.client=client
        self.config=cfg
        self.secret=secret
        self.search=Requests(client,'search',{**search_cfg,'search_topic':'general'},search_secret)
        self.attempts=0
        self.leads=[]
        self.cache_only=False

    async def request(self,value):
        if self.attempts>=self.config.get('per_run_limit',6):
            raise ValueError('达到本轮 MarketData 请求上限')
        self.attempts+=1
        return await query(value,self.config,self.secret)

    async def collect(self,symbol,case,previous,at):
        gaps=[]
        contracts=[]
        if case:
            try:
                # Historical callers always request dated EOD data; availability is checked below.
                historical=at.date()<datetime.now(timezone.utc).date()
                prior_day=at.astimezone(ZoneInfo('America/New_York')).date()-timedelta(days=1)
                while prior_day.weekday()>4:prior_day-=timedelta(days=1)
                stamp=prior_day.isoformat() if historical else None
                data=await self.request(Query(operation='expirations',symbol=symbol,date=stamp))
                expiries=sorted({d for d in data['expirations'] if case['from_date']<=d<=case['through_date'] and d>=at.date().isoformat()})
                target=(datetime.fromisoformat(case['from_date'])+
                        (datetime.fromisoformat(case['through_date'])-datetime.fromisoformat(case['from_date']))/2).date()
                chosen=sorted(expiries,key=lambda d:abs((datetime.fromisoformat(d).date()-target).days))[:3]
                if not chosen:gaps.append('独立情景窗口内没有可用到期日')
                center=case['prices']['base']
                for expiry in sorted(chosen):
                    data=await self.request(Query(operation='chain',symbol=symbol,expiration=expiry,side=case['side'],
                        strike_low=max(0.01,round(center['low']*0.9,2)),strike_high=round(center['high']*1.1,2),
                        strike_limit=3,date=stamp))
                    contracts.extend(filter(None,(normalize(r,symbol) for r in data['rows'])))
            except ValueError as error:gaps.append(str(error))
        else:
            gaps.append('缺少锁定的定量价格情景，暂不批量查询期权链')
        if previous:
            try:
                data=await self.request(Query(operation='quotes',symbol=symbol,contract=previous['ticker'].removeprefix('O:'),date=at.date().isoformat() if at.date()<datetime.now(timezone.utc).date() else None))
                contracts.extend(filter(None,(normalize(r,symbol) for r in data['rows'])))
            except ValueError as error:gaps.append(str(error))
        if not contracts or any(c['reference_only'] for c in contracts):
            more,reason=await self.fallback(symbol,case,at)
            contracts.extend(more)
            gaps.extend(reason)
        visible=[c for c in contracts if datetime.fromisoformat(c['available_at'])<=at]
        if len(visible)!=len(contracts):gaps.append('已排除模拟时点尚不可见的报价，包括当天日终数据')
        return list({c['ticker']:c for c in visible}.values()),False,None,gaps

    async def fallback(self,symbol,case,at):
        query_text=f'{symbol} options bid ask OCC quote timestamp'
        if case:query_text+=f" {case['from_date']} {case['side']}"
        historical=at.date()<datetime.now(timezone.utc).date()
        if historical:
            return [],['历史模拟需要可核实的当时网页归档，当前搜索结果未释放给历史研究']
        try:
            body=await self.search.get('/search' if self.search.config['provider']=='tavily' else '/res/v1/web/search',
                                       {'q':query_text,'count':3,'result_filter':'web'})
            results=body.get('results') if self.search.config['provider']=='tavily' else body.get('web',{}).get('results')
            if not isinstance(results,list):raise ValueError('搜索结果结构无效')
            quotes=[]
            for result in results[:3]:
                url=result.get('url','')
                parts=urlsplit(url)
                if parts.scheme!='https' or parts.username:continue
                self.leads.append({'url':url,'title':result.get('title'),'excerpt':result.get('content') or result.get('description'),
                                   'acquired_at':now(),'kind':'search_lead'})
                # Only public, explicitly allowed quote pages. Never extract prices from snippets.
                if parts.hostname not in ('chartexchange.com','www.cboe.com','finance.yahoo.com','www.nasdaq.com'):continue
                response=await self.client.get(url,follow_redirects=False,timeout=12)
                if response.status_code!=200:continue
                html=response.text[:500000]
                for script in re.findall(r'<script[^>]*type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>',html,re.S|re.I):
                    try:record=json.loads(script)
                    except ValueError:continue
                    records=record if isinstance(record,list) else [record]
                    for record in records:
                        if not isinstance(record,dict) or record.get('priceCurrency')!='USD':continue
                        if not all(k in record for k in ('identifier','bid','ask','dateModified')):continue
                        try:
                            stamp=datetime.fromisoformat(record['dateModified'].replace('Z','+00:00'))
                            if stamp.tzinfo is None:continue
                            bid,ask=float(record['bid']),float(record['ask'])
                            if not 0<bid<=ask:continue
                        except (ValueError,TypeError,AttributeError):continue
                        quote=normalize({'contract':record['identifier'],'bid':bid,'ask':ask,'observed_at':stamp.isoformat(),
                                         'source':'public_web','source_url':url,'raw':record},symbol)
                        if quote:quotes.append(quote)
            with connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS option_search_evidence (id TEXT PRIMARY KEY,created_at TEXT,body TEXT)')
                packet={'query':query_text,'leads':self.leads,'quotes':quotes}
                db.execute('INSERT OR IGNORE INTO option_search_evidence VALUES (?,?,?)',(digest(packet),now(),json_text(packet)))
            return quotes,['已补充公开网页搜索；搜索摘要仅作线索，未核实完整合约、价格类型及时间的内容不作为报价']
        except (SourceError,ValueError,TypeError,AttributeError,httpx.HTTPError):
            return [],['备用搜索不可用或缺少可核验的公开报价']


def select_source(client,configs,secrets):
    if (configs.get('marketdata',{}).get('enabled') and secrets.get('marketdata',{}).get('api_key')) or not configs['options'].get('enabled',True):
        return MarketOptions(client,configs.get('marketdata',{}),secrets.get('marketdata',{}),
                             configs['search'],secrets.get('search',{}))
    return Requests(client,'options',configs['options'],secrets.get('options',{}))

