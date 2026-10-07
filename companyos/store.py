"""Filesystem state store (runtime only, never committed; see docs/replication.md).

Layout under the state dir:
  company.json                      shared company state (company/state.schema.json)
  <collection>/<id>.json            tasks, runs, approvals, decisions, artifacts, directives
  artifacts/<id>.md                 artifact bodies
  events.jsonl audit.jsonl usage.jsonl ledger.jsonl board/inbox.jsonl memory/<dept>.jsonl   (append-only)

Writes are atomic (tmp + rename) under one lock (thread + fcntl), so a crash never leaves a half-written record
and a CLI process can safely act while the runner runs. Everything persisted passes through redact().
"""
import contextlib
import fcntl
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import validate as v

SECRET_PATTERNS = [r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9_-]{20,}", r"AKIA[0-9A-Z]{16}",
                   r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"xox[bp]-[A-Za-z0-9-]{10,}", r"AIza[0-9A-Za-z_-]{30,}"]
_SECRET_RE = re.compile("|".join(SECRET_PATTERNS))
SCHEMAS = {"tasks": "task", "approvals": "approval", "decisions": "decision", "artifacts": "artifact", "runs": "run"}


def redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class InvalidRecord(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._fh = None

    @contextlib.contextmanager
    def tx(self):
        """Exclusive section across threads and processes. Re-entrant within a thread."""
        with self._lock:
            if self._depth == 0:
                self._fh = open(self.path / ".lock", "w")
                fcntl.flock(self._fh, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
                if self._depth == 0:
                    fcntl.flock(self._fh, fcntl.LOCK_UN)
                    self._fh.close()

    def _write(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(redact(text))
        os.replace(tmp, path)

    # documents
    def put(self, coll: str, obj: dict, key: str | None = None) -> dict:
        kind = SCHEMAS.get(coll)
        if kind:
            errs = v.validate_instance(kind, obj)
            if errs:
                raise InvalidRecord(f"{coll}: {errs[:3]}")
        rid = key or obj.get(f"{coll[:-1]}_id") or obj["id"]
        with self.tx():
            self._write(self.path / coll / f"{rid}.json", json.dumps(obj, indent=1, default=str))
        return obj

    def get(self, coll: str, rid: str) -> dict | None:
        p = self.path / coll / f"{rid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def all(self, coll: str) -> list[dict]:
        d = self.path / coll
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []

    def put_text(self, rel: str, text: str) -> Path:
        p = self.path / rel
        with self.tx():
            self._write(p, text)
        return p

    # append-only logs
    def append(self, log: str, rec: dict):
        p = self.path / log
        p.parent.mkdir(parents=True, exist_ok=True)
        with self.tx(), open(p, "a") as f:
            f.write(redact(json.dumps(rec, default=str)) + "\n")

    def read_log(self, log: str, limit: int | None = None) -> list[dict]:
        p = self.path / log
        if not p.exists():
            return []
        lines = p.read_text().splitlines()
        return [json.loads(x) for x in (lines[-limit:] if limit else lines) if x.strip()]

    # idempotency ledger: a key is recorded once an effect has happened; recovery never repeats it
    def done(self, key: str) -> dict | None:
        return next((r for r in self.read_log("ledger.jsonl") if r["key"] == key), None)

    def mark(self, key: str, **data):
        self.append("ledger.jsonl", {"key": key, "at": now(), **data})

    # company state
    def company(self) -> dict:
        p = self.path / "company.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def save_company(self, state: dict):
        state["as_of"] = now()
        errs = v.validate_instance("company-state", state)
        if errs:
            raise InvalidRecord(f"company state: {errs[:3]}")
        with self.tx():
            self._write(self.path / "company.json", json.dumps(state, indent=1, default=str))
