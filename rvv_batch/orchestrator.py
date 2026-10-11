"""Batch scheduling and recovery. Workers execute ELFs; only this thread uses Store."""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import shutil

from .artifacts import now, read_result
from .runner import RunSpec, cleanup_request, run_single, stage_input


def discover(root, output):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"ELF directory not found: {root}")
    excluded = {output, Path(str(output) + '.work'), Path(str(output) + '.artifacts')}
    candidates = []
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not (Path(parent) / d).is_symlink()
                                and Path(parent) / d not in excluded)
        for name in sorted(files):
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file() or path in excluded:
                continue
            with path.open('rb') as handle:
                magic = handle.read(4)
            if magic == b'\x7fELF' or path.suffix.lower() in ('.elf', '.riscv'):
                candidates.append((path.relative_to(root).as_posix(), path))
    if not candidates:
        raise ValueError('no ELF inputs found')
    return sorted(candidates, key=lambda row: row[0])


def snapshot_inputs(candidates):
    for name, path in candidates:
        yield name, path.read_bytes()


class Orchestrator:
    def __init__(self, store, docker, cpus, cancellation, output=print, executor=run_single):
        self.store, self.docker = store, docker
        self.config = store.run()
        self.cpus, self.cancellation = cpus, cancellation
        self.output, self.executor = output, executor
        self.work_root = Path(str(store.path) + '.work')

    def recover(self):
        cleanup_request(self.docker, self.config['id'])
        for attempt in self.store.attempts():
            directory = self.work_root / str(attempt['attempt_id'])
            if attempt['status'] != 'running':
                if attempt['imported'] and directory.exists():
                    shutil.rmtree(directory)
                continue
            if (directory / 'result.json').is_file():
                try:
                    result = read_result(directory)
                    if result.attempt_id != attempt['attempt_id']:
                        raise ValueError('completed result attempt ID mismatch')
                except (ValueError, TypeError, KeyError) as error:
                    self.output(f"recovery: incomplete result {directory}: {error}", flush=True)
                else:
                    self.store.import_result(directory, result)
                    shutil.rmtree(directory)
                    continue
            self.store.interrupt_attempt(attempt['attempt_id'])

    def execute(self):
        active = {}
        pool = ThreadPoolExecutor(max_workers=len(self.cpus))
        try:
            self.recover()
            jobs = self.store.jobs()
            total = len(jobs)
            finished = sum(job['status'] == 'succeeded' for job in jobs)
            started = finished
            pending = deque(self.store.jobs(pending=True))
            available = deque(self.cpus)
            manifests = self.store.manifests()
            attempt_id = max((row['attempt_id'] for row in self.store.attempts()), default=0)
            self.store.set_status('running', f"jobs={len(self.cpus)}; CPUs={self.cpus}")
            while pending or active:
                while pending and available and not self.cancellation.event.is_set():
                    job, cpu = pending.popleft(), available.popleft()
                    attempt_id += 1
                    spec = RunSpec(**{key: self.config[key] for key in
                                     ('backend', 'image_id', 'image_ref', 'seed', 'max_cycles', 'timeout', 'memory')},
                                   wave=bool(self.config['wave']), cpu=cpu, run_id=self.config['id'], attempt_id=attempt_id,
                                   directory=self.work_root / str(attempt_id), started_at=now())
                    self.store.start_attempt(job, spec)
                    stage_input(spec.directory, self.store.input_bytes(job), manifests)
                    active[pool.submit(self.executor, spec, self.docker, self.cancellation.event)] = (job, spec)
                    started += 1
                    name = json.dumps(job['name'], ensure_ascii=False)[1:-1]
                    self.output(f"start [{started}/{total}] {name}", flush=True)
                if not active:
                    break
                done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    job, spec = active.pop(future)
                    result = future.result()
                    self.store.import_result(spec.directory, result)
                    shutil.rmtree(spec.directory)
                    available.append(spec.cpu)
                    if result.status != 'interrupted':
                        finished += 1
                    name = json.dumps(job['name'], ensure_ascii=False)[1:-1]
                    self.output(f"finish [{finished}/{total}] {name} {result.summary()}", flush=True)
            failed = self.store.failed()
            status = ('interrupted' if self.cancellation.event.is_set()
                      else 'completed_with_errors' if failed else 'completed')
            self.store.set_status(status, 'scheduler finished')
            return 128 + self.cancellation.signum if self.cancellation.signum else 1 if failed else 0
        except BaseException:
            self.cancellation.event.set()
            try:
                self.store.set_status('interrupted', 'controller error; resume to recover')
            except Exception:
                pass
            raise
        finally:
            pool.shutdown(wait=True)
            if self.work_root.exists() and not any(self.work_root.iterdir()):
                self.work_root.rmdir()
