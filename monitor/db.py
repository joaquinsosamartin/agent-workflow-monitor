import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), parent_id TEXT REFERENCES nodes(id),
 title TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', position INTEGER NOT NULL DEFAULT 0,
 started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_run ON nodes(run_id,parent_id,position);
CREATE TABLE IF NOT EXISTS usage (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT REFERENCES nodes(id),
 session_id TEXT NOT NULL, usage_key TEXT NOT NULL,
 agent TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 cached_input_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
 cost_usd TEXT, cost_source TEXT NOT NULL DEFAULT 'unavailable',
 recorded_at TEXT NOT NULL, UNIQUE(run_id, session_id, usage_key)
);
CREATE INDEX IF NOT EXISTS idx_usage_node ON usage(run_id,node_id);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 external_id TEXT, UNIQUE(run_id,external_id)
);
"""

def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def add_run(self, title, project='', run_id=None):
        rid = run_id or str(uuid4())
        with self.lock, self.conn:
            self.conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)', (rid,title,project,'pending',utc(),utc()))
        return rid

    def add_node(self, run_id, title, parent_id=None, agent='', provider='', position=0, node_id=None):
        nid = node_id or str(uuid4())
        with self.lock, self.conn:
            if not self.conn.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise ValueError('Unknown run')
            if parent_id and not self.conn.execute('SELECT 1 FROM nodes WHERE id=? AND run_id=?',(parent_id,run_id)).fetchone():
                raise ValueError('Invalid parent node')
            self.conn.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)', (nid,run_id,parent_id,title,agent,provider,'pending',position,None,None,utc()))
        return nid

    def save_event(self, event):
        kind=event['type']; rid=event['run_id']; nid=event.get('node_id')
        with self.lock, self.conn:
            if not self.conn.execute('SELECT 1 FROM runs WHERE id=?',(rid,)).fetchone():
                raise ValueError('Unknown run')
            if nid and not self.conn.execute('SELECT 1 FROM nodes WHERE id=? AND run_id=?',(nid,rid)).fetchone():
                raise ValueError('Invalid node')
            external_id=event.get('external_id')
            if external_id and self.conn.execute('SELECT 1 FROM events WHERE run_id=? AND external_id=?',(rid,external_id)).fetchone():
                return None
            if kind=='node.status':
                status=event['status']
                self.conn.execute('''UPDATE nodes SET status=?, agent=COALESCE(?,agent), provider=COALESCE(?,provider),
                started_at=CASE WHEN ?='running' THEN COALESCE(started_at,?) ELSE started_at END,
                finished_at=CASE WHEN ? IN ('completed','failed','cancelled') THEN ? ELSE finished_at END,
                updated_at=? WHERE id=?''', (status,event.get('agent'),event.get('provider'),status,utc(),status,utc(),utc(),nid))
            elif kind=='run.status':
                self.conn.execute('UPDATE runs SET status=?, updated_at=? WHERE id=?',(event['status'],utc(),rid))
            elif kind=='usage':
                u=event['usage']; key=u['usage_key']; session=u['session_id']
                self.conn.execute('''INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(run_id,session_id,usage_key) DO UPDATE SET
                   input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                   cached_input_tokens=excluded.cached_input_tokens, reasoning_tokens=excluded.reasoning_tokens,
                   cost_usd=excluded.cost_usd, cost_source=excluded.cost_source,
                   recorded_at=excluded.recorded_at''',
                  (str(uuid4()),rid,nid,session,key,event.get('agent',''),event.get('provider',''),u.get('model',''),
                   u.get('input_tokens',0),u.get('output_tokens',0),u.get('cached_input_tokens',0),
                   u.get('reasoning_tokens',0),u.get('cost_usd'),u.get('cost_source','unavailable'),utc()))
            self.conn.execute('UPDATE runs SET updated_at=? WHERE id=?',(utc(),rid))
            cursor=self.conn.execute('INSERT INTO events (run_id,kind,payload,created_at,external_id) VALUES (?,?,?,?,?)',
                                     (rid,kind,json.dumps(event,ensure_ascii=False),utc(),external_id))
            return cursor.lastrowid

    def run_list(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute('SELECT * FROM runs ORDER BY updated_at DESC LIMIT 100')]

    def event_list(self, run_id, after=0, limit=100):
        with self.lock:
            return [dict(x) for x in self.conn.execute('SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?', (run_id,after,limit))]

    def snapshot(self, run_id):
        with self.lock:
            row=self.conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            if not row: return None
            nodes=[dict(n) for n in self.conn.execute('SELECT * FROM nodes WHERE run_id=? ORDER BY position,id',(run_id,))]
            usage=[dict(u) for u in self.conn.execute('SELECT * FROM usage WHERE run_id=?',(run_id,))]
        return {'run':dict(row),'nodes':nodes,'usage':usage}
