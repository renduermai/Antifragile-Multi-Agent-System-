"""Back up the application databases together and verify restore into a new directory."""
import argparse
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory

from .db import now, root


REQUIRED = {'runtime.sqlite3','research.sqlite3','cases.sqlite3','predictions.sqlite3','validation.sqlite3'}
OPTIONAL = {'research_checkpoints.sqlite3'}


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def check_database(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('数据库完整性检查失败 '+path.name)


def create(source, stage):
    filenames = {p.name for p in source.glob('*.sqlite3')}
    if not REQUIRED <= filenames or filenames - (REQUIRED | OPTIONAL):
        raise ValueError('源数据库集合不完整或存在未知数据库')
    with ExitStack() as stack:
        # Take writer reservations in a fixed order. Use separate readers for SQLite backup,
        # including committed pages still in WAL. All locks stay held until all copies finish.
        for name in sorted(filenames):
            db=stack.enter_context(closing(sqlite3.connect((source/name).as_uri()+'?mode=rw',uri=True,timeout=5)))
            db.execute('BEGIN IMMEDIATE')
        for name in sorted(filenames):
            with closing(sqlite3.connect((source/name).as_uri()+'?mode=ro',uri=True)) as src:
                with closing(sqlite3.connect(stage/name)) as dest:
                    src.backup(dest)
    manifest={'format':1,'created_at':now(),'sha256':{}}
    for name in sorted(filenames):
        check_database(stage/name)
        manifest['sha256'][name]=checksum(stage/name)
    (stage/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')


def restore(source, stage):
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    hashes=manifest['sha256']
    if manifest['format']!=1 or not REQUIRED <= set(hashes) <= REQUIRED|OPTIONAL:
        raise ValueError('备份清单无效')
    for name,expected in hashes.items():
        dest=stage/name
        shutil.copyfile(source/name,dest)
        if checksum(dest)!=expected:
            raise ValueError('备份校验值不一致 '+name)
        check_database(dest)


def run(operation, source, dest):
    source, dest=source.resolve(), dest.resolve()
    if not source.is_dir() or dest.exists() or dest.is_relative_to(source):
        raise ValueError('源目录必须存在，目标必须是源目录之外的新目录')
    dest.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(dir=dest.parent) as temporary:
        stage=Path(temporary)/'snapshot'
        stage.mkdir()
        operation(source,stage)
        stage.rename(dest)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','restore'])
    parser.add_argument('destination',type=Path)
    parser.add_argument('--source',type=Path)
    args=parser.parse_args()
    if args.action=='restore' and args.source is None:
        parser.error('restore requires --source')
    run(create if args.action=='create' else restore,args.source or root(),args.destination)
    print(args.destination.resolve())

