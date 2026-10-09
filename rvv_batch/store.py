"""One writer, uncompressed standard SQLite types, and an analysis view."""

import codecs
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 1
CHUNK_BYTES = 64 * 1024

SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE run (
 id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, backend TEXT NOT NULL,
 image_id TEXT NOT NULL, image_ref TEXT NOT NULL, input_dir TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
 jobs INTEGER NOT NULL, seed INTEGER NOT NULL, max_cycles INTEGER NOT NULL,
 timeout REAL NOT NULL, wave INTEGER NOT NULL, cpu_set TEXT, memory TEXT,
 parser_version INTEGER NOT NULL, tool_version TEXT NOT NULL
);
CREATE TABLE artifacts (
 artifact_id INTEGER PRIMARY KEY, attempt_id INTEGER REFERENCES attempts(attempt_id),
 kind TEXT NOT NULL, name TEXT NOT NULL, storage TEXT NOT NULL CHECK(storage IN ('sqlite','external')),
 sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, data BLOB, relative_path TEXT,
 CHECK((storage='sqlite' AND data IS NOT NULL AND relative_path IS NULL)
    OR (storage='external' AND data IS NULL AND relative_path IS NOT NULL))
);
CREATE UNIQUE INDEX elf_hash ON artifacts(sha256) WHERE kind='elf';
CREATE TABLE jobs (
 job_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
 elf_artifact_id INTEGER NOT NULL REFERENCES artifacts(artifact_id),
 validation_error TEXT
);
CREATE TABLE attempts (
 attempt_id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 attempt_no INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, wall_seconds REAL, exit_code INTEGER, reason TEXT,
 container_name TEXT UNIQUE, cpu INTEGER, command_json TEXT NOT NULL,
 total_cycle INTEGER, measurement_status TEXT NOT NULL DEFAULT 'pending',
 kernel_status TEXT NOT NULL DEFAULT 'pending', UNIQUE(job_id, attempt_no)
);
CREATE TABLE logs (
 attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id),
 stream TEXT NOT NULL CHECK(stream IN ('stdout','stderr')),
 sequence INTEGER NOT NULL, byte_offset INTEGER NOT NULL, byte_length INTEGER NOT NULL,
 text TEXT NOT NULL, raw_bytes BLOB,
 PRIMARY KEY(attempt_id, stream, sequence)
);
CREATE TABLE measurements (
 measurement_id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id),
 metric TEXT NOT NULL, scope TEXT NOT NULL, name TEXT NOT NULL, sample_index INTEGER NOT NULL,
 value NUMERIC NOT NULL, source_stream TEXT NOT NULL, source_line INTEGER NOT NULL,
 source TEXT NOT NULL, validity TEXT NOT NULL CHECK(validity IN ('complete','partial'))
);
CREATE INDEX measurements_attempt ON measurements(attempt_id, metric);
CREATE TABLE events (
 event_id INTEGER PRIMARY KEY, time TEXT NOT NULL, attempt_id INTEGER REFERENCES attempts(attempt_id),
 kind TEXT NOT NULL, message TEXT NOT NULL
);
CREATE VIEW job_results AS
 SELECT j.job_id, j.name, a.attempt_id, a.attempt_no,
        COALESCE(a.status,'pending') AS status, a.exit_code, a.reason,
        a.total_cycle,
        CASE WHEN k.samples=1 THEN k.value END AS kernel_cycle,
        COALESCE(k.samples,0) AS kernel_sample_count,
        COALESCE(a.measurement_status,'pending') AS measurement_status,
        COALESCE(a.kernel_status,'pending') AS kernel_status,
        a.wall_seconds, a.started_at, a.finished_at,
        (SELECT COUNT(*) FROM attempts x WHERE x.job_id=j.job_id) AS attempt_count
 FROM jobs j
 LEFT JOIN attempts a ON a.attempt_id=(
   SELECT x.attempt_id FROM attempts x WHERE x.job_id=j.job_id ORDER BY x.attempt_no DESC LIMIT 1)
 LEFT JOIN (SELECT attempt_id, COUNT(*) samples, MAX(value) value FROM measurements
            WHERE metric='kernel_cycle' GROUP BY attempt_id) k ON k.attempt_id=a.attempt_id;
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def run_lock(path):
    # Keep this inode in place: unlinking a lock file permits overlapping locks.
    with Path(str(path) + ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"another runner owns {path}") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, path, create=False):
        self.path = Path(path)
        if create:
            # Exclusive creation, including protection against dangling symlinks.
            with self.path.open("xb"):
                pass
        elif not self.path.is_file():
            raise ValueError(f"database not found: {path}")
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if create:
            self.db.executescript(SCHEMA)
        elif self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.db.close()
            raise ValueError("unsupported SQLite schema version")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")

    def close(self):
        try:
            self.db.commit()
            result = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result[0]:
                raise RuntimeError("SQLite reader prevents checkpoint; close readers before copying the DB")
        finally:
            self.db.close()

    def event(self, kind, message, attempt_id=None):
        self.db.execute("INSERT INTO events(time,attempt_id,kind,message) VALUES(?,?,?,?)",
                        (now(), attempt_id, kind, message))

    def blob(self, kind, name, data, attempt_id=None):
        digest = sha256(data)
        if kind == "elf":
            row = self.db.execute("SELECT artifact_id FROM artifacts WHERE kind='elf' AND sha256=?",
                                  (digest,)).fetchone()
            if row:
                return row[0]
        return self.db.execute(
            "INSERT INTO artifacts(attempt_id,kind,name,storage,sha256,size_bytes,data) "
            "VALUES(?,?,?,'sqlite',?,?,?)", (attempt_id, kind, name, digest, len(data), data)
        ).lastrowid

    def ingest(self, attempt_id, directory, final=False):
        """Durable offsets make ingestion idempotent after an unclean controller exit."""
        for stream in ("stdout", "stderr"):
            path = Path(directory) / f"{stream}.log"
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"unexpected log file: {path}")
            last = self.db.execute(
                "SELECT sequence,byte_offset+byte_length FROM logs WHERE attempt_id=? AND stream=? "
                "ORDER BY sequence DESC LIMIT 1", (attempt_id, stream)).fetchone()
            sequence, offset = (last[0] + 1, last[1]) if last else (0, 0)
            if path.stat().st_size < offset:
                raise ValueError(f"log was truncated: {path}")
            with path.open("rb") as handle:
                handle.seek(offset)
                # Bound each polling pass; on completion drain the entire file.
                remaining = None if final else 32
                while remaining is None or remaining > 0:
                    data = handle.read(CHUNK_BYTES)
                    if not data:
                        break
                    try:
                        text, raw = data.decode("utf-8"), None
                    except UnicodeDecodeError as error:
                        if error.reason == "unexpected end of data" and error.start > 0:
                            # Keep valid multibyte characters intact across chunk/poll boundaries.
                            handle.seek(error.start - len(data), os.SEEK_CUR)
                            data = data[:error.start]
                            text, raw = data.decode("utf-8"), None
                        elif error.reason == "unexpected end of data" and not final:
                            break
                        else:
                            text, raw = data.decode("utf-8", "replace"), data
                    self.db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)",
                                    (attempt_id, stream, sequence, offset, len(data), text, raw))
                    offset += len(data)
                    sequence += 1
                    if remaining is not None:
                        remaining -= 1

    def lines(self, attempt_id):
        for stream in ("stdout", "stderr"):
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            pending, line_no = "", 0
            rows = self.db.execute(
                "SELECT text,raw_bytes FROM logs WHERE attempt_id=? AND stream=? ORDER BY sequence",
                (attempt_id, stream))
            for row in rows:
                pending += decoder.decode(row[1] if row[1] is not None else row[0].encode("utf-8"))
                parts = pending.split("\n")
                pending = parts.pop()
                for line in parts:
                    line_no += 1
                    yield stream, line_no, line
                # Simulator output is line-oriented; bound malformed unbroken output.
                if len(pending) > 1024 * 1024:
                    line_no += 1
                    yield stream, line_no, pending
                    pending = ""
            pending += decoder.decode(b"", final=True)
            if pending:
                yield stream, line_no + 1, pending

    def run(self):
        row = self.db.execute("SELECT * FROM run").fetchone()
        if row is None:
            raise ValueError("database contains no initialized request")
        return dict(row)

    def initialize(self, config, workloads, manifests):
        keys = list(config)
        with self.db:
            self.db.execute(f"INSERT INTO run({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                            tuple(config.values()))
            count = 0
            for i, (name, data, error) in enumerate(workloads, 1):
                artifact = self.blob("elf", name, data)
                self.db.execute("INSERT INTO jobs VALUES(?,?,?,?)", (i, name, artifact, error))
                count = i
            for name, data in manifests.items():
                self.blob("manifest", name, data)
            self.event("created", json.dumps({"jobs": count}))
