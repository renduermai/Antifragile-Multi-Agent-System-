import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .db import connect
from .research import SYMBOL


RULES_VERSION = 'case-5.0'
ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
STAMPS = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})')


class CaseInputError(ValueError):
    pass


def timestamp(value):
    if not isinstance(value, str) or not STAMPS.fullmatch(value):
        raise CaseInputError('请填写带时区的 ISO 8601 时间，例如 YYYY-MM-DDTHH:MM:SS+08:00')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise CaseInputError('时间无效') from None


def valid_url(value):
    try:
        parts = urlsplit(value)
        return parts.scheme in ('https','http') and bool(parts.hostname) and not parts.username
    except ValueError:
        return False


def parse(text):
    """Parse only explicit boundaries. Unassigned prose is never part of a model input."""
    symbol = None
    rounds = []
    evidence = None
    seen = set()

    def flush():
        nonlocal evidence
        if evidence is None:
            return
        kind, identifier, published, url, content = evidence
        body = '\n'.join(content).strip()
        if not body:
            raise CaseInputError(f'证据 {identifier} 没有正文')
        rounds[-1][kind].append({'id':identifier,'published':published,'url':url,'text':body})
        evidence = None

    for number, raw in enumerate(text.lstrip('\ufeff').splitlines(), 1):
        line = raw.strip()
        if not line.startswith('@'):
            if evidence is not None:
                evidence[-1].append(raw)
            elif line:
                raise CaseInputError(f'第 {number} 行未分配到任何证据。请指定各轮截断时间并标注当时已公开的内容')
            continue
        flush()
        parts = line.split()
        command = parts[0]
        if command == '@symbol' and len(parts) == 2 and not rounds and symbol is None and SYMBOL.fullmatch(parts[1]):
            symbol = parts[1]
        elif command == '@round' and len(parts) == 2:
            cutoff = timestamp(parts[1])
            if cutoff >= datetime.now(timezone.utc):
                raise CaseInputError(f'第 {number} 行的截断时间必须为历史时点')
            if rounds and cutoff <= timestamp(rounds[-1]['cutoff']):
                raise CaseInputError('各轮截断时间必须严格递增')
            rounds.append({'cutoff':cutoff.isoformat(),'sources':[],'quotes':[]})
        elif command in ('@evidence','@quote') and len(parts) == 4 and rounds:
            _, identifier, time, url = parts
            if not ID.fullmatch(identifier) or identifier in seen:
                raise CaseInputError(f'第 {number} 行的证据 ID 无效或重复')
            published = timestamp(time)
            if published > timestamp(rounds[-1]['cutoff']):
                raise CaseInputError(f'证据 {identifier} 的公开时间晚于所在轮次截断时间，请移到以后轮次')
            if not valid_url(url):
                raise CaseInputError(f'证据 {identifier} 需要 HTTP(S) 原始来源地址')
            seen.add(identifier)
            evidence = ('sources' if command == '@evidence' else 'quotes',identifier,published.isoformat(),url,[])
        else:
            raise CaseInputError(f'第 {number} 行标记无效。请按说明指定截断时间、证据 ID、公开时间与来源')
    flush()
    if not rounds or not rounds[0]['sources']:
        raise CaseInputError('请指定首轮历史截断时间，并至少标注一条可用证据')
    if len(rounds) > 40:
        raise CaseInputError('单份案例最多 40 轮')
    return {'symbol':symbol,'rounds':rounds}


def read_input(case_id, version=None):
    with connect('cases') as db:
        if version is None:
            row = db.execute('SELECT id,body FROM input_versions WHERE case_id=? ORDER BY id DESC LIMIT 1',
                             (case_id,)).fetchone()
        else:
            row = db.execute('SELECT id,body FROM input_versions WHERE id=? AND case_id=?',(version,case_id)).fetchone()
    if not row or not row['body'].strip():
        raise CaseInputError('案例 TXT 为空，请先保存已分轮材料')
    return row['id'], parse(row['body'])


def opening_quote(record, symbol, cutoff):
    """Historical quote lines use archived NBBO and explicit settlement evidence."""
    from .options import contract_from

    try:
        body = json.loads(record['text'])
        if not isinstance(body, dict) or body.get('underlying') != symbol:
            return None
        observation = timestamp(body.get('observed_at'))
        if observation > timestamp(record['published']) or observation > cutoff:
            return None
        row = body.get('snapshot')
        if not isinstance(row, dict) or not isinstance(row.get('last_quote'), dict):
            return None
        raw = {**row, 'last_quote':{**row['last_quote'],
               'last_updated':int(observation.timestamp()*1e9),
               'timeframe':'REAL-TIME' if body.get('quote_kind') == 'NBBO' else 'UNVERIFIED'}}
        contract = contract_from(raw,symbol)
        if not contract:
            return None
        contract['contract_verified'] = body.get('standard_deliverable') is True
        return {**contract, 'evidence_id':record['id'], 'source_url':record['url'],
                'observed_at':observation.isoformat(),'published_at':record['published'],'supplied_by_case':True}
    except (ValueError, TypeError, OverflowError):
        return None

