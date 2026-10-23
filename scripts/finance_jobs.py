"""Durable video-ID deduplication and resumable processing checkpoints."""
import json
import pathlib
import re
import sqlite3
import time


class Jobs:
    def __init__(self, path):
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            video_id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
            checkpoint TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
            updated REAL NOT NULL, error TEXT)''')
        self.db.commit()

    def add(self, entry, status='queued', checkpoint=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', entry['video_id']):
            raise ValueError('Invalid video ID')
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?)',
                (entry['video_id'], json.dumps(entry, ensure_ascii=False), status,
                 json.dumps(checkpoint or {}), 0, time.time(), None))
        return cursor.rowcount == 1

    def get(self, vid):
        row = self.db.execute('SELECT * FROM jobs WHERE video_id=?', (vid,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['source'] = json.loads(value['source'])
        value['checkpoint'] = json.loads(value['checkpoint'])
        return value

    def pending(self, limit=3):
        return [r[0] for r in self.db.execute('''SELECT video_id FROM jobs
            WHERE status IN ('queued','retry','processing') AND attempts < 5
            ORDER BY updated LIMIT ?''', (limit,))]

    def save(self, vid, status=None, error=None, **checkpoint):
        value = self.get(vid)
        value['checkpoint'].update(checkpoint)
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,checkpoint=?,updated=?,error=? WHERE video_id=?',
                (status or value['status'], json.dumps(value['checkpoint'], ensure_ascii=False),
                 time.time(), error, vid))

    def start(self, vid):
        with self.db:
            self.db.execute("UPDATE jobs SET status='processing',attempts=attempts+1,updated=? WHERE video_id=?",
                            (time.time(), vid))

    def counts(self):
        return dict(self.db.execute('SELECT status,count(*) FROM jobs GROUP BY status'))

    def request_historic(self, vid):
        """Explicit user-requested backfill; never resets completed or uncertain work."""
        job = self.get(vid)
        if job is None:
            raise ValueError('Video must already be discovered')
        if job['status'] == 'complete':
            return False
        if job['status'] == 'needs_review' or job['attempts'] >= 5:
            raise ValueError('Resolve held/exhausted work before requesting it again')
        if job['status'] == 'baseline':
            self.save(vid, status='queued', explicitly_requested_backfill=True)
        return True
