import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import scheduling
from app.db import connect, save_setting
from app.main import app


@pytest.fixture(autouse=True)
def controlled_timer(monkeypatch):
    async def idle(dispatch):
        await asyncio.Event().wait()
    monkeypatch.setattr(scheduling,'serve',idle)


def test_schedule_disabled_catchup_retry_and_idempotency(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    calls=[]
    at=datetime(2025,2,10,10,tzinfo=timezone.utc)

    async def transient(job):
        calls.append(job['id'])
        return {'retryable':True,'run_id':'TEST_RUN'}

    with TestClient(app) as client:
        assert client.get('/api/schedule').json()['config']['enabled'] is False
        asyncio.run(scheduling.tick(transient,at))
        assert calls==[]
        cfg=scheduling.Schedule(enabled=True,timezone='UTC',daily_hour=9,weekly_day=0,weekly_hour=9)
        save_setting('schedule',{**cfg.model_dump(),'enabled_since':(at-timedelta(days=1)).isoformat()})
        asyncio.run(scheduling.tick(transient,at))
        jobs=scheduling.status()['jobs']
        assert sum(j['attempts'] for j in jobs)==1
        assert sum(j['state']=='retry' for j in jobs)==1
        asyncio.run(scheduling.tick(transient,at))
        assert len(calls)==2
        asyncio.run(scheduling.tick(transient,at))
        assert len(calls)==2
        asyncio.run(scheduling.tick(transient,at+timedelta(minutes=2)))
        asyncio.run(scheduling.tick(transient,at+timedelta(minutes=2)))
        assert len(calls)==4
        asyncio.run(scheduling.tick(transient,at+timedelta(minutes=7)))
        asyncio.run(scheduling.tick(transient,at+timedelta(minutes=7)))
        assert len(calls)==6 and all(j['state']=='failed' for j in scheduling.status()['jobs'])
        asyncio.run(scheduling.tick(transient,at+timedelta(minutes=8)))
        assert len(calls)==6
        assert client.put('/api/schedule',json={'timezone':'bad/timezone'}).status_code==422
        scheduling.update(scheduling.Schedule())
        asyncio.run(scheduling.tick(transient,at+timedelta(days=8)))
        assert len(calls)==6


def test_busy_does_not_use_attempt_and_restart_persistence(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    at=datetime.now(timezone.utc)
    with TestClient(app):
        cfg=scheduling.Schedule(enabled=True,timezone='UTC',daily_hour=0,weekly_day=0,weekly_hour=0)
        save_setting('schedule',{**cfg.model_dump(),'enabled_since':(at-timedelta(days=8)).isoformat()})
        async def busy(job):
            return None
        asyncio.run(scheduling.tick(busy,at))
        assert all(j['attempts']==0 for j in scheduling.status()['jobs'])
        with connect('runtime') as db:
            db.execute("UPDATE scheduled_jobs SET state='running' WHERE kind='daily'")
    with TestClient(app):
        assert any(j['state']=='interrupted' for j in scheduling.status()['jobs'])
        async def success(job):
            return {'retryable':False,'scan_status':'TEST_completed'}
        asyncio.run(scheduling.tick(success,at+timedelta(days=9)))
        jobs=scheduling.status()['jobs']
        assert any(j['state']=='superseded' for j in jobs)
        assert any(j['state']=='completed' for j in jobs)

