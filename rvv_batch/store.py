"""One writer, uncompressed standard SQLite types, and an analysis view."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil

from .artifacts import file_hash, log_chunks, now, regular_file

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA user_version = 2;
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
 elf_artifact_id INTEGER NOT NULL REFERENCES artifacts(artifact_id)
);
CREATE TABLE attempts (
 attempt_id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 attempt_no INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, wall_seconds REAL, exit_code INTEGER, reason TEXT,
 container_name TEXT UNIQUE, cpu INTEGER, command_json TEXT NOT NULL, settings_json TEXT NOT NULL,
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
        path = Path(path).absolute()
        if path.is_symlink():
            raise ValueError("result database must not be a symlink")
        self.path = path.parent.resolve() / path.name
        if create:
            # Exclusive creation, including protection against dangling symlinks.
            with self.path.open("xb"):
                pass
        elif not self.path.is_file():
            raise ValueError(f"database not found: {path}")
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=5000")
            if create:
                self.db.executescript(SCHEMA)
            elif self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("unsupported SQLite schema version; resume this database with the original tool version")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            self.db.close()
            raise

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
            for i, (name, data) in enumerate(workloads, 1):
                artifact = self.blob("elf", name, data)
                self.db.execute("INSERT INTO jobs VALUES(?,?,?)", (i, name, artifact))
                count = i
            for name, data in manifests.items():
                self.blob("manifest", name, data)
            self.event("created", json.dumps({"jobs": count}))

    def resume_settings(self, jobs, timeout, max_cycles):
        previous = self.run()
        with self.db:
            self.db.execute("UPDATE run SET jobs=?,timeout=?,max_cycles=?,updated_at=?",
                            (jobs, timeout, max_cycles, now()))
            self.event("resume", f"jobs={jobs}; timeout={timeout}; previous_timeout={previous['timeout']}; "
                       f"max_cycles={max_cycles}; previous_max_cycles={previous['max_cycles']}")

    def set_status(self, status, message):
        with self.db:
            self.db.execute("UPDATE run SET status=?,updated_at=?", (status, now()))
            self.event(status, message)

    def jobs(self, pending=False):
        where = "WHERE r.status != 'succeeded'" if pending else ""
        return [dict(row) for row in self.db.execute(
            f"SELECT j.*,r.status FROM jobs j JOIN job_results r USING(job_id) {where} ORDER BY j.job_id")]

    def attempts(self):
        return [dict(row) for row in self.db.execute(
            "SELECT a.*,EXISTS(SELECT 1 FROM artifacts ar WHERE ar.attempt_id=a.attempt_id "
            "AND ar.kind='result') AS imported FROM attempts a ORDER BY attempt_id")]

    def manifests(self):
        return {row[0]: row[1] for row in self.db.execute(
            "SELECT name,data FROM artifacts WHERE kind='manifest'")}

    def input_bytes(self, job):
        row = self.db.execute("SELECT data,sha256 FROM artifacts WHERE artifact_id=?",
                              (job['elf_artifact_id'],)).fetchone()
        if sha256(row[0]) != row[1]:
            raise ValueError(f"stored ELF hash mismatch: {job['name']}")
        return row[0]

    def start_attempt(self, job, spec):
        with self.db:
            number = self.db.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE job_id=?",
                                     (job['job_id'],)).fetchone()[0]
            self.db.execute(
                "INSERT INTO attempts(attempt_id,job_id,attempt_no,status,started_at,container_name,cpu,command_json,settings_json) "
                "VALUES(?,?,?,'running',?,?,?,?,?)",
                (spec.attempt_id, job['job_id'], number, spec.started_at, spec.container, spec.cpu,
                 json.dumps(spec.command), json.dumps(spec.settings())))
            self.event("start", job['name'], spec.attempt_id)

    def interrupt_attempt(self, attempt_id):
        with self.db:
            self.db.execute("UPDATE attempts SET status='interrupted',finished_at=?,reason=? WHERE attempt_id=?",
                            (now(), "controller interrupted before result publication", attempt_id))
            self.event("interrupted", "unfinished execution will be retried", attempt_id)

    def import_result(self, directory, result):
        """Copy files first; commit the complete result once; caller then removes staging."""
        attempt_id = result.attempt_id
        attempt = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if attempt is None:
            raise ValueError("completed result has no matching attempt")
        expected = self.db.execute("SELECT ar.sha256 FROM jobs j JOIN artifacts ar "
                                   "ON ar.artifact_id=j.elf_artifact_id WHERE j.job_id=?",
                                   (attempt['job_id'],)).fetchone()[0]
        if (result.run_id != self.run()['id'] or result.input_sha256 != expected
                or result.settings != json.loads(attempt['settings_json'])
                or result.started_at != attempt['started_at']):
            raise ValueError("completed result does not match its attempt")
        if attempt['status'] != 'running':
            return  # Already committed; only cleanup remains after a controller crash.
        destination = Path(str(self.path) + '.artifacts') / str(attempt['job_id']) / f"attempt-{attempt['attempt_no']}"
        external = []
        for item in result.files:
            relative = Path(item['path'])
            if relative.parts[0] != 'output' or relative.as_posix() in ('output/stdout.log', 'output/stderr.log'):
                continue
            source = regular_file(directory, relative)
            name = relative.relative_to('output')
            target = destination / name
            for parent in (target, *target.parents):
                if parent.is_symlink():
                    raise ValueError(f"refusing artifact symlink: {parent}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if file_hash(target) != item['sha256']:
                raise ValueError(f"artifact copy hash mismatch: {target}")
            external.append((name.as_posix(), target, item))
        with self.db:
            for stream in ('stdout', 'stderr'):
                offset = 0
                for sequence, (data, text, raw) in enumerate(log_chunks(regular_file(directory, f'output/{stream}.log'))):
                    self.db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)",
                                    (attempt_id, stream, sequence, offset, len(data), text, raw))
                    offset += len(data)
            counts = {}
            for measurement in result.measurements:
                key = measurement['metric'], measurement['scope'], measurement['name']
                index = counts.get(key, 0)
                counts[key] = index + 1
                self.db.execute(
                    "INSERT INTO measurements(attempt_id,metric,scope,name,sample_index,value,source_stream,"
                    "source_line,source,validity) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, *key, index, measurement['value'], measurement['source_stream'],
                     measurement['source_line'], measurement['source'],
                     'complete' if result.status == 'succeeded' else 'partial'))
            for name, target, item in external:
                self.db.execute(
                    "INSERT INTO artifacts(attempt_id,kind,name,storage,sha256,size_bytes,relative_path) "
                    "VALUES(?,?,?,'external',?,?,?)",
                    (attempt_id, 'wave' if name == 'wave.fst' else 'auxiliary', name,
                     item['sha256'], item['size_bytes'], target.relative_to(self.path.parent).as_posix()))
            self.blob('result', 'result.json', (Path(directory) / 'result.json').read_bytes(), attempt_id)
            self.db.execute(
                "UPDATE attempts SET status=?,finished_at=?,wall_seconds=?,exit_code=?,reason=?,total_cycle=?,"
                "measurement_status=?,kernel_status=? WHERE attempt_id=?",
                (result.status, result.finished_at, result.wall_seconds, result.exit_code, result.reason,
                 result.total_cycle, result.measurement_status, result.kernel_status, attempt_id))
            for error in result.errors:
                self.event('measurement_error', error['message'], attempt_id)
            self.event('finish', result.status, attempt_id)

    def failed(self):
        return bool(self.db.execute(
            "SELECT COUNT(*) FROM job_results WHERE status != 'succeeded' "
            "OR measurement_status IN ('invalid','missing_total')").fetchone()[0])
