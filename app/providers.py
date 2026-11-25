import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx


CATALOG = {
    'llm': {'provider': 'openai_compatible', 'endpoint': 'https://api.openai.com/v1', 'label': '大语言模型'},
    'news': {'provider': 'alpaca', 'endpoint': 'https://data.alpaca.markets', 'label': '新闻'},
    'search': {'provider': 'tavily', 'endpoint': 'https://api.tavily.com', 'label': '独立网页搜索'},
    'market': {'provider': 'alpaca', 'endpoint': 'https://data.alpaca.markets', 'label': '金融行情'},
    'options': {'provider': 'massive', 'endpoint': 'https://api.massive.com', 'label': '期权数据'},
    'marketdata': {'provider':'marketdata','endpoint':'https://api.marketdata.app/v1','label':'MarketData 期权数据'},
}


def dashscope(base):
    host = urlsplit(base).hostname or ''
    return host.endswith('.maas.aliyuncs.com') or host == 'dashscope.aliyuncs.com'


def chat_fields(base):
    if dashscope(base):
        return {'enable_thinking': False, 'response_format': {'type': 'json_object'}}
    return {}


async def post_chat(base, headers, payload, timeout):
    url = base + '/chat/completions'
    curl = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/curl.exe'
    if os.name != 'nt' or not dashscope(base) or not curl.is_file():
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await client.post(url, headers=headers, json=payload)

    # Use the Windows TLS stack for this provider. Pass credentials through stdin.
    config = ['url = ' + json.dumps(url), 'header = "Content-Type: application/json"']
    config.extend('header = ' + json.dumps(f'{k}: {v}') for k,v in headers.items())
    config.append('data-binary = ' + json.dumps(json.dumps(payload)))
    request = httpx.Request('POST', url)
    try:
        process = await asyncio.create_subprocess_exec(
            str(curl), '--disable', '--silent', '--config', '-', '--max-time', str(timeout),
            '--write-out', '\\n%{http_code}', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            stdout, _ = await process.communicate(('\n'.join(config) + '\n').encode())
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
    except OSError as error:
        raise httpx.ConnectError('Windows TLS 客户端无法启动', request=request) from error
    body, _, status = stdout.rpartition(b'\n')
    if process.returncode or not status.isdigit() or int(status) < 100:
        raise httpx.ConnectError('DashScope 网络或 TLS 连接失败', request=request)
    return httpx.Response(int(status), content=body, request=request)


@dataclass
class Probe:
    capabilities: dict
    input_tokens: int | None = None
    output_tokens: int | None = None


class Provider(Protocol):
    async def test(self, config: dict, secrets: dict) -> Probe: ...


class HttpProvider:
    def __init__(self, kind):
        self.kind = kind

    def connection(self, config, secrets):
        if not config.get('enabled',True):
            raise ValueError('该数据接口已停用')
        base = config['endpoint'].rstrip('/')
        parts = urlsplit(base)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise ValueError('Endpoint 必须为无凭据、无查询参数的 HTTP(S) 地址')
        key = secrets.get('api_key')
        if not key:
            raise ValueError('API Key 未配置')
        headers = {}
        if self.kind in ('news', 'market'):
            if not secrets.get('secret_key'):
                raise ValueError('Alpaca Secret Key 未配置')
            headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secrets['secret_key']}
        elif self.kind == 'search' and config['provider'] == 'brave':
            headers = {'X-Subscription-Token': key}
        else:
            headers = {'Authorization': f'Bearer {key}'}
        return base, headers

    async def search(self, client, config, secrets, query, count):
        base, headers = self.connection(config, secrets)
        payload = {'query':query, 'topic':config.get('search_topic','news'), 'search_depth':'basic', 'time_range':'week',
                   'max_results':count, 'include_answer':False, 'include_raw_content':False}
        return await client.post(base + '/search', headers=headers, json=payload)

    async def get(self, client, config, secrets, path, params):
        base, headers = self.connection(config, secrets)
        if self.kind == 'search' and config['provider'] == 'tavily':
            response = await self.search(client,config,secrets,params['q'],params['count'])
        else:
            response = await client.get(base + path, headers=headers, params=params)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError('Provider 响应不是 JSON 对象')
        return body

    async def test(self, config, secrets):
        base, headers = self.connection(config, secrets)
        if self.kind == 'marketdata':
            from .marketdata import Query, query
            result = await query(Query(operation='expirations',symbol='SPY'),config,secrets)
            return Probe({'expirations':result['expirations'],'budget':result['budget'],'notice':result['notice']})
        if self.kind == 'llm':
            if not config.get('model'):
                raise ValueError('Model Name 未填写')
            response = await post_chat(base, headers, {
                'model': config['model'], 'messages': [{'role': 'user', 'content': 'Return only JSON: {"ok":true}'}],
                'max_tokens': 64, **chat_fields(base)}, timeout=20)
        else:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                paths = {'news': '/v1beta1/news', 'market': '/v2/stocks/SPY/snapshot',
                         'options': '/v3/snapshot/options/SPY', 'search': '/res/v1/web/search'}
                params = {'limit': 1} if self.kind in ('news', 'options') else {}
                if self.kind == 'market':
                    params['feed'] = config['feed']
                if self.kind == 'search':
                    params = {'q': 'major world news', 'count': 1}
                if self.kind == 'search' and config['provider'] == 'tavily':
                    response = await self.search(client,config,secrets,params['q'],1)
                else:
                    response = await client.get(base + paths[self.kind], headers=headers, params=params)
        response.raise_for_status()
        body = response.json()
        if self.kind == 'llm':
            content = body['choices'][0]['message']['content']
            if json.loads(content).get('ok') is not True:
                raise ValueError('模型结构化响应校验失败')
            usage = body.get('usage', {})
            return Probe({'structured_json_probe': True, 'history': '不适用', 'cost': '待配置费率估算'},
                         usage.get('prompt_tokens'), usage.get('completion_tokens'))
        field = {'news': 'news', 'search': 'web', 'options': 'results', 'market': 'latestTrade'}[self.kind]
        if self.kind == 'search' and config['provider'] == 'tavily':
            field = 'results'
            if not isinstance(body.get(field), list):
                raise ValueError('Tavily 搜索样本格式无效')
        if field not in body:
            raise ValueError('响应缺少预期字段，Provider 格式或权限待核验')
        records = body[field]
        capabilities = {'endpoint_access': True, 'sample_available': bool(records),
                        'history': '尚未实测', 'delay': '尚未核验', 'cost': '账单费用未知'}
        if self.kind == 'market':
            capabilities['requested_feed'] = config['feed']
            capabilities['sample_timestamp'] = (body.get('latestTrade') or {}).get('t')
        if self.kind == 'options':
            sample = records[0] if records else {}
            quote = sample.get('last_quote') or {}
            capabilities['bid_ask_present'] = 'bid' in quote and 'ask' in quote
            capabilities['sample_timestamp'] = quote.get('last_updated')
            capabilities['realtime_and_historical_entitlement'] = '尚未核验'
        return Probe(capabilities)
