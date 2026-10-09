"""A single-writer scheduler for independent, detached Docker simulations."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time

from .backends import command, parse_lines, validate_elf
from .store import now


def parse_docker_timestamp(value):
    """Parse Docker's RFC3339Nano timestamps on Python 3.10 and newer."""
    # Python 3.10 only accepts three or six fractional digits. Docker can emit
    # up to nine; truncate to datetime's microseconds and pad shorter fractions.
    value = re.sub(r"\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def discover(root, output):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"ELF directory not found: {root}")
    excluded = {output, Path(str(output) + ".work"), Path(str(output) + ".artifacts")}
    candidates = []
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories
                                if not (Path(parent) / d).is_symlink()
                                and Path(parent) / d not in excluded)
        for name in sorted(files):
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file() or path in excluded:
                continue
            with path.open("rb") as handle:
                magic = handle.read(4)
            if magic != b"\x7fELF" and path.suffix.lower() not in (".elf", ".riscv"):
                continue
            candidates.append((path.relative_to(root).as_posix(), path))
    if not candidates:
        raise ValueError("no ELF inputs found")
    return sorted(candidates, key=lambda row: row[0])


def snapshot_inputs(candidates):
    # Only one ELF is held in memory while initializing even a very large batch.
    for name, path in candidates:
        data = path.read_bytes()
        error = None
        try:
            validate_elf(data)
        except ValueError as exc:
            error = str(exc)
        yield name, data, error


@dataclass
class Active:
    attempt_id: int
    job_id: int
    name: str
    container: str
    cpu: int
    started: float


class Runner:
    def __init__(self, store, docker, cpus, jobs, output=print, poll_seconds=0.25):
        self.store, self.db, self.docker = store, store.db, docker
        self.config = store.run()
        self.cpus, self.jobs = cpus, jobs
        self.output, self.poll_seconds = output, poll_seconds
        self.work_root = Path(str(store.path) + ".work")
        self.artifact_root = Path(str(store.path) + ".artifacts")
        self.active = {}
        self.stop_signal = None
        self.total = self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def work(self, attempt_id):
        return self.work_root / str(attempt_id)

    def signal(self, signum, frame):
        self.stop_signal = signum

    def ingest(self, attempt_id, final=False):
        with self.db:
            self.store.ingest(attempt_id, self.work(attempt_id) / "output", final)

    def collect_files(self, attempt_id):
        row = self.db.execute("SELECT job_id,attempt_no FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        source = self.work(attempt_id) / "output"
        destination = self.artifact_root / str(row[0]) / f"attempt-{row[1]}"
        # A crash can happen after a move but before its DB insert. Re-index both roots.
        if source.exists():
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"refusing artifact symlink: {path}")
                if not path.is_file() or path.relative_to(source).as_posix() in ("stdout.log", "stderr.log"):
                    continue
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
        if destination.exists():
            for path in sorted(destination.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"refusing artifact symlink: {path}")
                if not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                relative = path.relative_to(self.store.path.parent).as_posix()
                self.db.execute("DELETE FROM artifacts WHERE attempt_id=? AND relative_path=?", (attempt_id, relative))
                self.db.execute(
                    "INSERT INTO artifacts(attempt_id,kind,name,storage,sha256,size_bytes,relative_path) "
                    "VALUES(?,?,?,'external',?,?,?)",
                    (attempt_id, "wave" if path.name == "wave.fst" else "auxiliary",
                     path.relative_to(destination).as_posix(), digest.hexdigest(), path.stat().st_size, relative))

    def finalize(self, attempt_id, state, forced_status=None, reason=None):
        self.ingest(attempt_id, final=True)
        parsed = parse_lines(self.config["backend"], self.store.lines(attempt_id))
        exit_code = state.get("ExitCode")
        if forced_status:
            status = forced_status
        elif state.get("OOMKilled"):
            status, reason = "failed", "container exceeded its memory limit"
        elif parsed.limit:
            status, reason = "cycle_limit", "simulator cycle/instruction limit reached"
        elif exit_code == 0 and parsed.good and not parsed.bad:
            status = "succeeded"
        else:
            status = "failed"
            reason = reason or state.get("Error") or "simulator failed or did not report normal completion"
        core_name = "simulation" if self.config["backend"] == "saturn" else "core-0"
        totals = [m[3] for m in parsed.measurements if m[0] == "cycle" and m[1] == "simulation" and m[2] == core_name]
        total_cycle = totals[-1] if totals else None
        kernel_count = sum(m[0] == "kernel_cycle" for m in parsed.measurements)
        kernel_status = "invalid" if any("RVV_KERNEL" in e or "kernel_cycle" in e for e in parsed.errors) else ("available" if kernel_count else "missing")
        attempt = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        ended = now()
        if state.get("FinishedAt"):
            candidate = parse_docker_timestamp(state["FinishedAt"])
            if candidate >= datetime.fromisoformat(attempt["started_at"]):
                ended = candidate.isoformat(timespec="milliseconds")
        wall = max(0, (datetime.fromisoformat(ended) - datetime.fromisoformat(attempt["started_at"])).total_seconds())
        with self.db:
            self.collect_files(attempt_id)
            if self.config["wave"] and status == "succeeded":
                wave = self.db.execute("SELECT size_bytes FROM artifacts WHERE attempt_id=? AND kind='wave'",
                                       (attempt_id,)).fetchone()
                if not wave or wave[0] == 0:
                    status, reason = "failed", "requested waveform is missing or empty"
            measurement_status = ("invalid" if parsed.errors else "missing_total" if total_cycle is None
                                  else "partial" if status != "succeeded" else "missing_kernel" if not kernel_count else "complete")
            self.db.execute("DELETE FROM measurements WHERE attempt_id=?", (attempt_id,))
            counters = {}
            for metric, scope, name, value, stream, line_no, source in parsed.measurements:
                key = metric, scope, name
                index = counters.get(key, 0)
                counters[key] = index + 1
                self.db.execute(
                    "INSERT INTO measurements(attempt_id,metric,scope,name,sample_index,value,source_stream,"
                    "source_line,source,validity) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, metric, scope, name, index, value, stream, line_no, source,
                     "complete" if status == "succeeded" else "partial"))
            self.db.execute(
                "UPDATE attempts SET status=?,finished_at=?,wall_seconds=?,exit_code=?,reason=?,total_cycle=?,"
                "measurement_status=?,kernel_status=? WHERE attempt_id=?",
                (status, ended, wall, exit_code, reason, total_cycle, measurement_status, kernel_status, attempt_id))
            for error in parsed.errors:
                self.store.event("measurement_error", error, attempt_id)
            self.store.event("finish", status, attempt_id)
        name = self.db.execute("SELECT name FROM jobs WHERE job_id=?", (attempt["job_id"],)).fetchone()[0]
        kernels = [m[3] for m in parsed.measurements if m[0] == "kernel_cycle"]
        kernel_text = str(kernels[0]) if len(kernels) == 1 else f"multiple({len(kernels)})" if kernels else "null"
        # Escape control characters without obscuring ordinary spaces and Unicode names.
        name = json.dumps(name, ensure_ascii=False)[1:-1]
        self.output(f"finish [{attempt['job_id']}/{self.total}] {name} "
                    f"total_cycle={total_cycle if total_cycle is not None else 'null'} "
                    f"kernel_cycle={kernel_text} status={status}", flush=True)

    def cleanup(self, attempt_id, container=None):
        if container:
            self.docker.remove(container)
        if self.work(attempt_id).exists():
            shutil.rmtree(self.work(attempt_id))

    def recover(self):
        names = self.docker.containers(self.config["id"])
        states = self.docker.states(names)
        known = {row["container_name"]: row for row in self.db.execute("SELECT * FROM attempts") if row["container_name"]}
        previously_running = {name for name, state in states.items() if state.get("Running")}
        for name, state in states.items():
            if name not in known:
                raise ValueError(f"unrecognized container for this request: {name}")
            if state.get("Running"):
                self.docker.stop(name)
        if names:
            states = self.docker.states(names)
        for row in self.db.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall():
            name = row["container_name"]
            if row["status"] in ("starting", "running", "stopping"):
                state = states.get(name, {})
                # A simulator that finished before controller death is still a completed job.
                finished = name in states and state.get("Status") == "exited" and not states[name].get("Running")
                was_running = name in previously_running
                forced = None if finished and not was_running and row["status"] != "stopping" else "interrupted"
                self.finalize(row["attempt_id"], state, forced, "controller interrupted" if forced else None)
            if name in names:
                self.cleanup(row["attempt_id"], name)
            elif row["status"] not in ("starting", "running", "stopping") or self.work(row["attempt_id"]).exists():
                self.cleanup(row["attempt_id"])

    def launch(self, job, cpu):
        number = self.db.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE job_id=?", (job["job_id"],)).fetchone()[0]
        args = command(self.config["backend"], self.config["max_cycles"], self.config["seed"], self.config["wave"])
        with self.db:
            attempt_id = self.db.execute(
                "INSERT INTO attempts(job_id,attempt_no,status,started_at,cpu,command_json) VALUES(?,?,'starting',?,?,?)",
                (job["job_id"], number, now(), cpu, json.dumps(args))).lastrowid
            name = f"rvv-{self.config['id']}-{attempt_id}"
            self.db.execute("UPDATE attempts SET container_name=? WHERE attempt_id=?", (name, attempt_id))
            self.store.event("start", job["name"], attempt_id)
        escaped = json.dumps(job["name"], ensure_ascii=False)[1:-1]
        self.output(f"start [{job['job_id']}/{self.total}] {escaped}", flush=True)
        work = self.work(attempt_id)
        (work / "input").mkdir(parents=True)
        (work / "output").mkdir()
        if job["validation_error"]:
            self.finalize(attempt_id, {}, "invalid_input", job["validation_error"])
            self.cleanup(attempt_id)
            return
        data = self.db.execute("SELECT data,sha256 FROM artifacts WHERE artifact_id=?", (job["elf_artifact_id"],)).fetchone()
        if hashlib.sha256(data[0]).hexdigest() != data[1]:
            raise ValueError(f"stored ELF hash mismatch: {job['name']}")
        elf = work / "input" / "program.elf"
        elf.write_bytes(data[0])
        elf.chmod(0o444)
        self.active[name] = Active(attempt_id, job["job_id"], job["name"], name, cpu, time.monotonic())
        self.docker.create(name, self.config["id"], attempt_id, self.config["image_id"], cpu,
                           self.config["memory"], os.getuid(), os.getgid(), work / "input", work / "output", args)
        self.docker.start(name)
        with self.db:
            self.db.execute("UPDATE attempts SET status='running' WHERE attempt_id=?", (attempt_id,))

    def execute(self):
        previous_handlers = {sig: signal.signal(sig, self.signal) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.recover()
            pending = self.db.execute(
                "SELECT j.* FROM jobs j JOIN job_results r USING(job_id) WHERE r.status IN ('pending','interrupted') ORDER BY j.job_id"
            ).fetchall()
            with self.db:
                self.db.execute("UPDATE run SET status='running',jobs=?,updated_at=?", (self.jobs, now()))
                self.store.event("scheduler", f"jobs={self.jobs}; CPUs={self.cpus}")
            while pending or self.active:
                while pending and len(self.active) < self.jobs and not self.stop_signal:
                    used = {active.cpu for active in self.active.values()}
                    cpu = next(c for c in self.cpus if c not in used)
                    self.launch(pending.pop(0), cpu)
                if self.stop_signal:
                    with self.db:
                        self.db.execute("UPDATE attempts SET status='stopping' WHERE status IN ('starting','running')")
                    for name in list(self.active):
                        self.docker.stop(name)
                    states = self.docker.states(list(self.active))
                    for name, active in list(self.active.items()):
                        self.finalize(active.attempt_id, states[name], "interrupted", "user interrupted")
                        self.cleanup(active.attempt_id, name)
                        del self.active[name]
                    break
                states = self.docker.states(list(self.active))
                for name, active in list(self.active.items()):
                    state = states[name]
                    if state.get("Running") and time.monotonic() - active.started >= self.config["timeout"]:
                        self.docker.stop(name)
                        state = self.docker.states([name])[name]
                        self.finalize(active.attempt_id, state, "timeout", "wall timeout exceeded")
                    elif not state.get("Running"):
                        self.finalize(active.attempt_id, state)
                    else:
                        self.ingest(active.attempt_id)
                        continue
                    self.cleanup(active.attempt_id, name)
                    del self.active[name]
                if self.active:
                    time.sleep(self.poll_seconds)
            failed = self.db.execute(
                "SELECT COUNT(*) FROM job_results WHERE status NOT IN ('succeeded','pending','interrupted') "
                "OR (status='succeeded' AND measurement_status IN ('invalid','missing_total'))"
            ).fetchone()[0]
            status = "interrupted" if self.stop_signal else "completed_with_errors" if failed else "completed"
            with self.db:
                self.db.execute("UPDATE run SET status=?,updated_at=?", (status, now()))
                self.store.event(status, "scheduler finished")
            return 128 + self.stop_signal if self.stop_signal else 1 if failed else 0
        except BaseException:
            # Keep spooled files and nonterminal attempts for resume, even if SQLite is full.
            try:
                with self.db:
                    self.db.execute("UPDATE attempts SET status='stopping' WHERE status IN ('starting','running')")
            except Exception:
                pass
            for name in self.active:
                try:
                    self.docker.stop(name)
                except Exception:
                    pass
            try:
                with self.db:
                    self.db.execute("UPDATE run SET status='interrupted',updated_at=?", (now(),))
                    self.store.event("interrupted", "controller error; resume to recover")
            except Exception:
                pass
            raise
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            if self.work_root.exists() and not any(self.work_root.iterdir()):
                self.work_root.rmdir()
