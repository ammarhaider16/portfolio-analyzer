"""Small persistent application store; research itself does not depend on it."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import AnalysisResult, PortfolioSnapshot


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                  result TEXT, error TEXT, schedule_key TEXT UNIQUE, delivery TEXT,
                  email_requested INTEGER NOT NULL DEFAULT 0);
            """)
            if "summary" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN summary TEXT")

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            yield db

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def snapshot(self):
        data = self.get("snapshot")
        return PortfolioSnapshot.model_validate(data) if data else None

    def save_snapshot(self, snapshot):
        self.set("snapshot", snapshot.model_dump(mode="json"))

    def create_run(self, run_id, schedule_key=None, email_requested=False):
        with self.db() as db:
            # SQLite transaction prevents competing processes from admitting overlapping runs.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running')").fetchone():
                return False
            try:
                db.execute("INSERT INTO runs(id,created_at,status,schedule_key,email_requested) VALUES (?,?,?,?,?)",
                           (run_id, datetime.now(timezone.utc).isoformat(), "queued", schedule_key, int(email_requested)))
            except sqlite3.IntegrityError:
                return False
        return True

    def update_run(self, run_id, **changes):
        allowed = {"status", "result", "error", "delivery", "summary"}
        if not changes or set(changes) - allowed:
            raise ValueError("Invalid run update")
        with self.db() as db:
            db.execute("UPDATE runs SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?", (*changes.values(), run_id))

    def finish(self, result: AnalysisResult):
        self.update_run(result.run_id, status=result.status, result=result.model_dump_json(), summary=result.coverage.get("summary"))

    def claim_delivery(self, run_id):
        with self.db() as db:
            return db.execute("UPDATE runs SET delivery='sending' WHERE id=? AND (delivery IS NULL OR delivery='failed')", (run_id,)).rowcount == 1

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def delete_run(self, run_id):
        """Remove one finished report; active runs are never deleted here."""
        with self.db() as db:
            return db.execute("DELETE FROM runs WHERE id=? AND status NOT IN ('queued','running')", (run_id,)).rowcount == 1

    def history(self, limit=30):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT id,created_at,status,error,delivery,schedule_key,summary FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def has_schedule(self, key):
        with self.db() as db:
            return db.execute("SELECT 1 FROM runs WHERE schedule_key=?", (key,)).fetchone() is not None

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE runs SET status='failed',error='Application stopped during this run. Run it again manually.' WHERE status IN ('queued','running')")
            db.execute("UPDATE runs SET delivery='uncertain' WHERE delivery='sending'")

    def prune(self, retention_days):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self.db() as db:
            db.execute("DELETE FROM runs WHERE created_at < ? AND status NOT IN ('queued','running')", (cutoff,))
