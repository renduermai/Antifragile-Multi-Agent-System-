import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def root():
    return Path(os.environ.get('AF_DATA_DIR', Path(__file__).resolve().parents[1] / 'data'))


@contextmanager
def connect(name='runtime'):
    root().mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root() / f'{name}.sqlite3', timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def migrate():
    scripts = {
        'runtime': '''
            CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, kind TEXT, provider TEXT,
                created_at TEXT, status TEXT, detail TEXT, input_tokens INTEGER,
                output_tokens INTEGER, estimated INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS probes (kind TEXT PRIMARY KEY, result TEXT NOT NULL);
        ''',
        'research': '''
            CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, state TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, event_id TEXT, body TEXT,
                source_url TEXT, source_type TEXT, occurred_at TEXT, first_public_at TEXT,
                version_public_at TEXT, acquired_at TEXT, content_hash TEXT);
            CREATE TABLE IF NOT EXISTS event_versions (id INTEGER PRIMARY KEY,
                event_id TEXT, created_at TEXT, result TEXT, rules_version TEXT, prompt_version TEXT);
        ''',
        'cases': '''
            CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS input_versions (id INTEGER PRIMARY KEY, case_id TEXT,
                body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS predictions (run_id TEXT PRIMARY KEY, case_id TEXT,
                cutoff TEXT, snapshot TEXT, locked_at TEXT);
        ''',
        'predictions': '''
            CREATE TABLE case_series (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, input_version INTEGER NOT NULL,
                first_round INTEGER NOT NULL, round_count INTEGER NOT NULL,
                created_at TEXT NOT NULL, identity TEXT NOT NULL);
            CREATE INDEX series_case ON case_series(case_id, created_at);
            CREATE TABLE case_predictions (
                id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES case_series(id),
                round_index INTEGER NOT NULL, cutoff TEXT NOT NULL, started_at TEXT NOT NULL,
                status TEXT NOT NULL, finished_at TEXT, problem TEXT,
                independent TEXT, snapshot TEXT, snapshot_hash TEXT);
            CREATE UNIQUE INDEX case_prediction_inflight ON case_predictions(status) WHERE status='running';
            CREATE UNIQUE INDEX case_prediction_lock ON case_predictions(series_id,round_index) WHERE status='locked';
            CREATE TRIGGER case_prediction_no_change BEFORE UPDATE ON case_predictions
                WHEN OLD.status='locked' BEGIN SELECT RAISE(ABORT,'locked prediction'); END;
            CREATE TRIGGER case_prediction_no_delete BEFORE DELETE ON case_predictions
                WHEN OLD.status='locked' BEGIN SELECT RAISE(ABORT,'locked prediction'); END;
        ''',
        'validation': '''
            CREATE TABLE IF NOT EXISTS validation_versions (id INTEGER PRIMARY KEY, case_id TEXT,
                body TEXT NOT NULL, created_at TEXT NOT NULL);
        ''',
    }
    for name, script in scripts.items():
        with connect(name) as db:
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)')
            if not db.execute('SELECT 1 FROM schema_migrations WHERE version=1').fetchone():
                db.executescript(script)
                db.execute('INSERT INTO schema_migrations VALUES (1,?)', (now(),))
    with connect('runtime') as db:
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE scheduled_jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, query TEXT NOT NULL,
                    due_at TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at TEXT, run_id TEXT, result TEXT, updated_at TEXT NOT NULL);
                CREATE INDEX schedule_due ON scheduled_jobs(state,retry_at);
                INSERT INTO schema_migrations VALUES (2,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                COMMIT;
            ''')
        db.execute("UPDATE scheduled_jobs SET state='interrupted',updated_at=? WHERE state='running'",(now(),))
    with connect('cases') as db:
        db.executemany('INSERT OR IGNORE INTO cases VALUES (?,?)', [
            ('gold', '黄金案例'), ('oil', '原油与霍尔木兹海峡案例'), ('nvda', '英伟达 NVDA 案例')])
    with connect('predictions') as db:
        db.execute("""UPDATE case_predictions SET status='interrupted', finished_at=?, problem=?
            WHERE status='running'""", (now(), '服务中断，已锁定的预测可继续验证'))
    with connect('validation') as db:
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE case_evaluations (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, forecast_id TEXT NOT NULL,
                    validation_version INTEGER NOT NULL REFERENCES validation_versions(id),
                    forecast_hash TEXT NOT NULL, evaluator_version TEXT NOT NULL,
                    created_at TEXT NOT NULL, result TEXT NOT NULL,
                    UNIQUE(forecast_id,validation_version,evaluator_version));
                INSERT INTO schema_migrations VALUES (2,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                COMMIT;
            ''')
    with connect('research') as db:
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
            db.executescript('''
                BEGIN IMMEDIATE;
                ALTER TABLE events ADD COLUMN title TEXT;
                ALTER TABLE events ADD COLUMN updated_at TEXT;
                ALTER TABLE evidence ADD COLUMN provider TEXT;
                ALTER TABLE evidence ADD COLUMN upstream_id TEXT;
                ALTER TABLE evidence ADD COLUMN canonical_url TEXT;
                ALTER TABLE evidence ADD COLUMN publisher TEXT;
                ALTER TABLE evidence ADD COLUMN headline TEXT;
                ALTER TABLE evidence ADD COLUMN headline_key TEXT;
                ALTER TABLE evidence ADD COLUMN body_kind TEXT;
                ALTER TABLE evidence ADD COLUMN raw_json TEXT;
                ALTER TABLE evidence ADD COLUMN duplicate_of TEXT;
                ALTER TABLE evidence ADD COLUMN fingerprint TEXT;
                ALTER TABLE evidence ADD COLUMN supersedes_id TEXT;
                CREATE INDEX evidence_story ON evidence(provider, upstream_id);
                CREATE INDEX evidence_url ON evidence(canonical_url);
                CREATE INDEX evidence_fingerprint ON evidence(fingerprint);
                CREATE INDEX evidence_headline ON evidence(headline_key, first_public_at);
                CREATE INDEX evidence_event ON evidence(event_id);
                CREATE INDEX event_versions_event ON event_versions(event_id, id);
                CREATE TABLE event_assets (
                    event_id TEXT REFERENCES events(event_id), symbol TEXT NOT NULL,
                    source TEXT NOT NULL, PRIMARY KEY(event_id, symbol)
                );
                CREATE INDEX event_assets_symbol ON event_assets(symbol);
                CREATE TABLE event_evidence_links (
                    event_id TEXT REFERENCES events(event_id), evidence_id TEXT REFERENCES evidence(id),
                    linked_at TEXT NOT NULL, PRIMARY KEY(event_id, evidence_id)
                );
                CREATE TABLE market_observations (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, instrument_type TEXT NOT NULL,
                    proxy_for TEXT, currency TEXT NOT NULL, unit TEXT NOT NULL, feed TEXT NOT NULL,
                    price TEXT NOT NULL, observed_at TEXT NOT NULL, acquired_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX market_latest ON market_observations(symbol, observed_at DESC);
                CREATE TABLE provider_cursors (
                    stream TEXT PRIMARY KEY, covered_until TEXT, window_start TEXT,
                    window_end TEXT, page_token TEXT
                );
                CREATE TABLE collection_runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    state TEXT NOT NULL, query TEXT, result TEXT
                );
                CREATE UNIQUE INDEX active_collection ON collection_runs(state) WHERE state='running';
                INSERT INTO schema_migrations VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                COMMIT;
            ''')
        db.execute("UPDATE collection_runs SET state='interrupted', finished_at=? WHERE state='running'", (now(),))
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE research_runs (
                    id TEXT PRIMARY KEY, mode TEXT, query TEXT, status TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, result TEXT
                );
                CREATE UNIQUE INDEX one_research_run ON research_runs(status) WHERE status='running';
                CREATE TABLE scan_plans (run_id TEXT PRIMARY KEY REFERENCES research_runs(id), body TEXT NOT NULL);
                CREATE TABLE model_steps (run_id TEXT, subject TEXT, outcome TEXT, PRIMARY KEY(run_id,subject));
                CREATE TABLE reports (
                    id TEXT PRIMARY KEY, run_id TEXT REFERENCES research_runs(id), subject TEXT,
                    signature TEXT, status TEXT, created_at TEXT, payload TEXT NOT NULL
                );
                CREATE INDEX report_subject ON reports(subject,created_at DESC);
                CREATE TABLE probability_snapshots (
                    id TEXT PRIMARY KEY, run_id TEXT REFERENCES research_runs(id), subject TEXT,
                    locked_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE daily_bars (
                    feed TEXT, symbol TEXT, day TEXT, close REAL, volume REAL,
                    PRIMARY KEY(feed,symbol,day)
                );
                INSERT INTO schema_migrations VALUES (3,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                COMMIT;
            ''')
        if not db.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE option_decisions (
                    id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id),
                    subject TEXT NOT NULL, created_at TEXT NOT NULL, signal TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX decision_report ON option_decisions(report_id);
                CREATE INDEX decision_subject ON option_decisions(subject,signal);
                INSERT INTO schema_migrations VALUES (4,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                COMMIT;
            ''')
        db.execute("UPDATE research_runs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))


def setting(key, default=None):
    with connect() as db:
        row = db.execute('SELECT value FROM settings WHERE id=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def save_setting(key, value):
    with connect() as db:
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(value)))


class DailyLimitReached(Exception):
    pass


def reserve_usage(kind, provider, cap):
    created_at = now()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        count = db.execute('SELECT COUNT(*) FROM usage WHERE kind=? AND created_at LIKE ?',
                           (kind, created_at[:10] + '%')).fetchone()[0]
        if count >= cap:
            raise DailyLimitReached
        cursor = db.execute('INSERT INTO usage(kind,provider,created_at,status) VALUES (?,?,?,?)',
                            (kind, provider, created_at, 'running'))
        return cursor.lastrowid
