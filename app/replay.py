"""Durable one-click historical replay. Validation opens only after all forecasts lock."""
import json
from uuid import uuid4
from .db import connect,now
from pathlib import Path
from .research import json_text
from . import case_forecast,case_audit
from .case_material import read_input,CaseInputError

def migrate():
    with connect('predictions') as db:
        db.execute("""CREATE TABLE IF NOT EXISTS replay_jobs (
            id TEXT PRIMARY KEY,case_id TEXT,status TEXT,series_id TEXT,input_version INTEGER,
            round_index INTEGER,steps TEXT,problem TEXT,created_at TEXT,updated_at TEXT)""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS replay_running ON replay_jobs(status) WHERE status='running'")
        db.execute("UPDATE replay_jobs SET status='interrupted',problem='服务重启，可继续运行' WHERE status='running'")

def install(case_id):
    folder=Path(__file__).resolve().parents[1]/'cases'/case_id
    if not folder.is_dir():raise CaseInputError('没有内置材料')
    for kind,table,database,file in [('input','input_versions','cases','case.txt'),('validation','validation_versions','validation','validation.txt')]:
        with connect(database) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute(f'SELECT body FROM {table} WHERE case_id=? ORDER BY id DESC LIMIT 1',(case_id,)).fetchone()
            if not row or not row[0].strip():
                db.execute(f'INSERT INTO {table}(case_id,body,created_at) VALUES (?,?,?)',(case_id,(folder/file).read_text('utf-8'),now()))
    return json.loads((folder/'preparation.json').read_text('utf-8'))


def get(case_id):
    with connect('predictions') as db:
        row=db.execute('SELECT * FROM replay_jobs WHERE case_id=? ORDER BY rowid DESC LIMIT 1',(case_id,)).fetchone()
    if not row:return None
    return {**dict(row),'steps':json.loads(row['steps'])}

def step(jid,node):
    with connect('predictions') as db:
        row=db.execute('SELECT steps,round_index FROM replay_jobs WHERE id=?',(jid,)).fetchone()
        steps=json.loads(row['steps'])
        for s in steps:
            if s['status']=='running':s['status']='done'
        steps.append({'node':node,'round':row['round_index'],'status':'running','at':now()})
        db.execute('UPDATE replay_jobs SET steps=?,updated_at=? WHERE id=?',(json_text(steps),now(),jid))

def begin(case_id,resume=False):
    version,material=read_input(case_id)
    jid=uuid4().hex
    with connect('predictions') as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM replay_jobs WHERE status='running'").fetchone():
            raise case_forecast.CaseConflict('已有自动回放运行中')
        old=db.execute('SELECT * FROM replay_jobs WHERE case_id=? ORDER BY rowid DESC LIMIT 1',(case_id,)).fetchone()
        if resume and old and old['status'] in ('blocked','interrupted') and old['input_version']==version:
            jid=old['id']
            db.execute("UPDATE replay_jobs SET status='running',problem=NULL WHERE id=?",(jid,))
        else:
            db.execute('INSERT INTO replay_jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                (jid,case_id,'running',None,version,0,'[]',None,now(),now()))
    return jid

async def execute(jid,case_id,cfg,secret,cap):
    status,problem='blocked',None
    try:
        job=get(case_id)
        _,material=read_input(case_id,job['input_version'])
        series=job['series_id']
        start=job['round_index']
        for index in range(start,len(material['rounds'])):
            with connect('predictions') as db:
                db.execute('UPDATE replay_jobs SET round_index=? WHERE id=?',(index,jid))
            step(jid,'evidence')
            series,fid=case_forecast.begin(case_id,cfg,version=job['input_version'],index=index,series_id=series)
            with connect('predictions') as db:db.execute('UPDATE replay_jobs SET series_id=? WHERE id=?',(series,jid))
            await case_forecast.execute(case_id,fid,cfg,secret,cap,progress=lambda node:step(jid,node))
            prediction=case_forecast.get_prediction(case_id,fid)
            if prediction['status']!='locked':raise CaseInputError(prediction['problem'] or '该轮未完成锁定')
            step(jid,'saved')
            with connect('predictions') as db:db.execute('UPDATE replay_jobs SET round_index=? WHERE id=?',(index+1,jid))
        step(jid,'audit')
        runs=case_forecast.list_series(case_id)
        run=next(r for r in runs if r['id']==series)
        # The validation DB is first accessed here, after all rounds are locked.
        for row in run['attempts']:
            if row['status']=='locked':case_audit.evaluate(case_id,row['id'])
        step(jid,'display')
        status='completed'
    except Exception as error:
        problem=str(error) if isinstance(error,(CaseInputError,case_forecast.CaseConflict)) else '回放中断，可检查接口后继续'
    finally:
        with connect('predictions') as db:
            row=db.execute('SELECT steps FROM replay_jobs WHERE id=?',(jid,)).fetchone()
            steps=json.loads(row[0])
            for s in steps:
                if s['status']=='running':s['status']='done' if status=='completed' else 'blocked'
            db.execute('UPDATE replay_jobs SET status=?,problem=?,steps=?,updated_at=? WHERE id=?',
                (status,problem,json_text(steps),now(),jid))

