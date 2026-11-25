"""Persistent schedules with bounded catch-up and retry, driven by the local service."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .db import connect, now, save_setting, setting
from .research import digest, json_text


class Schedule(BaseModel):
    enabled: bool = False
    timezone: str = 'Asia/Shanghai'
    daily_hour: int = Field(default=8,ge=0,le=23)
    daily_minute: int = Field(default=0,ge=0,le=59)
    weekly_day: int = Field(default=5,ge=0,le=6)
    weekly_hour: int = Field(default=9,ge=0,le=23)
    priority_minutes: int = Field(default=120,ge=15,le=1440)
    priority_queries: list[str] = Field(default_factory=list,max_length=5)
    max_retries: int = Field(default=2,ge=0,le=3)

    @field_validator('timezone')
    @classmethod
    def zone(cls,value):
        try:
            ZoneInfo(value)
        except (KeyError,ValueError):
            raise ValueError('时区不可用') from None
        return value

    @field_validator('priority_queries')
    @classmethod
    def queries(cls,values):
        result=[]
        for value in values:
            query=value.strip()
            if not query or len(query)>160:
                raise ValueError('重点事件关键词长度为 1–160')
            if query not in result:
                result.append(query)
        return result


def config():
    return Schedule.model_validate(setting('schedule',Schedule().model_dump()))


def slots(cfg,at):
    local=at.astimezone(ZoneInfo(cfg.timezone))
    day=local.replace(hour=cfg.daily_hour,minute=cfg.daily_minute,second=0,microsecond=0)
    if day>local:
        day-=timedelta(days=1)
    week=local.replace(hour=cfg.weekly_hour,minute=0,second=0,microsecond=0)
    week-=timedelta(days=(local.weekday()-cfg.weekly_day)%7)
    if week>local:
        week-=timedelta(days=7)
    period=cfg.priority_minutes*60
    priority=datetime.fromtimestamp(int(at.timestamp())//period*period,timezone.utc)
    return [('daily','',day.astimezone(timezone.utc)),('weekly','',week.astimezone(timezone.utc)),
            *[('priority',q,priority) for q in cfg.priority_queries]]


def update(cfg):
    # Existing running tasks finish. Pending jobs from the old configuration are superseded.
    at=now()
    with connect('runtime') as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE scheduled_jobs SET state='superseded',updated_at=? WHERE state IN ('pending','retry','interrupted')",(at,))
        value={**cfg.model_dump(),'enabled_since':at}
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                   ('schedule',json_text(value)))
    return status()


def status():
    cfg=config()
    with connect('runtime') as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM scheduled_jobs ORDER BY due_at DESC, id LIMIT 60')]
    for row in rows:
        row['result']=json.loads(row['result']) if row['result'] else None
    return {'config':cfg.model_dump(),'jobs':rows,'service_required':True}


def enqueue(cfg,at):
    since=setting('schedule',{}).get('enabled_since')
    if not since:
        return
    with connect('runtime') as db:
        for kind,query,due in slots(cfg,at):
            # On restart only the latest due slot of each job is caught up.
            if due < datetime.fromisoformat(since):
                continue
            identity=digest([kind,query,due.isoformat(),since])
            db.execute('INSERT OR IGNORE INTO scheduled_jobs VALUES (?,?,?,?,?,0,NULL,NULL,NULL,?)',
                       (identity,kind,query,due.isoformat(),'pending',at.isoformat()))
        newest={(k,q):d.isoformat() for k,q,d in slots(cfg,at)}
        for (kind,query),due in newest.items():
            db.execute("""UPDATE scheduled_jobs SET state='superseded',updated_at=? WHERE kind=? AND query=?
                AND due_at<? AND state IN ('pending','retry','interrupted')""",(at.isoformat(),kind,query,due))


async def tick(dispatch,at=None):
    at=at or datetime.now(timezone.utc)
    cfg=config()
    if not cfg.enabled:
        return
    enqueue(cfg,at)
    with connect('runtime') as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM scheduled_jobs WHERE state='running'").fetchone():
            return
        row=db.execute("""SELECT * FROM scheduled_jobs WHERE state IN ('pending','retry','interrupted')
            AND (retry_at IS NULL OR retry_at<=?) ORDER BY due_at,id LIMIT 1""",(at.isoformat(),)).fetchone()
        if not row:
            return
        job=dict(row)
        db.execute("UPDATE scheduled_jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",
                   (at.isoformat(),job['id']))
    try:
        result=await dispatch(job)
        if result is None:
            state,retry='pending',None
            attempts=job['attempts']
        else:
            attempts=job['attempts']+1
            failed=result.get('retryable',False)
            state='retry' if failed and attempts<=cfg.max_retries else 'failed' if failed else 'completed'
            retry=(at+timedelta(seconds=60*2**(attempts-1))).isoformat() if state=='retry' else None
    except asyncio.CancelledError:
        state,retry,attempts,result='interrupted',None,job['attempts']+1,None
        raise
    except Exception as error:
        attempts=job['attempts']+1
        state='retry' if attempts<=cfg.max_retries else 'failed'
        retry=(at+timedelta(seconds=60*2**(attempts-1))).isoformat() if state=='retry' else None
        result={'error':type(error).__name__}
    finally:
        with connect('runtime') as db:
            db.execute('''UPDATE scheduled_jobs SET state=?,attempts=?,retry_at=?,run_id=?,result=?,updated_at=?
                WHERE id=?''',(state,attempts,retry,(result or {}).get('run_id'),
                json_text(result) if result else None,now(),job['id']))


async def serve(dispatch):
    while True:
        try:
            await tick(dispatch)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Durable job rows retain failures. Keep the timer available after transient SQLite errors.
        await asyncio.sleep(15)

