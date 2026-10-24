"""Durable JSON records and immutable events, with atomic cross-record updates."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading

from .common import utc_now, parse_time


def _json(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False, default=lambda v: str(v) if isinstance(v, Decimal) else _bad(v))


def _bad(value):
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS invest_records (
                kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                immutable INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS invest_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                event_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(kind,event_id));
            CREATE TRIGGER IF NOT EXISTS invest_immutable_update BEFORE UPDATE ON invest_records
                WHEN OLD.immutable=1 BEGIN SELECT RAISE(ABORT,'immutable record'); END;
            CREATE TRIGGER IF NOT EXISTS invest_immutable_delete BEFORE DELETE ON invest_records
                WHEN OLD.immutable=1 BEGIN SELECT RAISE(ABORT,'immutable record'); END;
            CREATE TRIGGER IF NOT EXISTS invest_event_update BEFORE UPDATE ON invest_events
                BEGIN SELECT RAISE(ABORT,'immutable event'); END;
            CREATE TRIGGER IF NOT EXISTS invest_event_delete BEFORE DELETE ON invest_events
                BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        """)

    @contextmanager
    def transaction(self):
        """Serialize a read/check/write operation, including across processes."""
        with self._lock:
            outer = self._depth == 0
            savepoint = f"invest_nested_{self._depth}"
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            else:
                self.db.execute(f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.db.rollback()
                else:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.db.commit()
                else:
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")

    def put(self, kind, key, payload, *, immutable=False):
        encoded = _json(payload)
        if not kind or not key:
            raise ValueError("kind and key required")
        with self.transaction():
            existing = self.db.execute("SELECT payload,immutable FROM invest_records WHERE kind=? AND key=?", (kind, key)).fetchone()
            if existing:
                if existing[0] == encoded:
                    if immutable and not existing[1]:
                        self.db.execute("UPDATE invest_records SET immutable=1 WHERE kind=? AND key=?", (kind, key))
                    return False
                if existing[1]:
                    raise ValueError(f"immutable conflict: {kind}/{key}")
                self.db.execute("UPDATE invest_records SET payload=?,immutable=? WHERE kind=? AND key=?", (encoded, int(immutable), kind, key))
            else:
                self.db.execute("INSERT INTO invest_records VALUES(?,?,?,?)", (kind, key, encoded, int(immutable)))
        return True

    def get(self, kind, key):
        with self._lock:
            row = self.db.execute("SELECT payload FROM invest_records WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def items(self, kind):
        with self._lock:
            rows = self.db.execute("SELECT key,payload FROM invest_records WHERE kind=? ORDER BY key", (kind,)).fetchall()
        return [(key, json.loads(payload)) for key, payload in rows]

    def append_event(self, kind, event_id, payload):
        with self.transaction():
            old = self.db.execute("SELECT payload FROM invest_events WHERE kind=? AND event_id=?", (kind, event_id)).fetchone()
            event = dict(payload)
            if event.get("id", event_id) != event_id:
                raise ValueError("event id mismatch")
            event.setdefault("id", event_id)
            event.setdefault("timestamp", json.loads(old[0])["timestamp"] if old else utc_now())
            event["timestamp"] = parse_time(event["timestamp"]).isoformat()
            encoded = _json(event)
            if old:
                if old[0] != encoded:
                    raise ValueError(f"immutable event conflict: {kind}/{event_id}")
                return False
            self.db.execute("INSERT INTO invest_events(kind,event_id,payload) VALUES(?,?,?)", (kind, event_id, encoded))
        return True

    def events(self, kind):
        with self._lock:
            rows = self.db.execute("SELECT payload FROM invest_events WHERE kind=? ORDER BY seq", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
