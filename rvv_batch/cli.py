"""Public run/resume entry points. No project-specific analysis or extraction step."""

import argparse
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from . import __version__
from .backends import BACKENDS, MAX_INTEGER, PARSER_VERSION, image_name
from .docker import Docker
from .runner import Runner, discover, snapshot_inputs
from .store import SCHEMA_VERSION, Store, now, run_lock


def positive(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if not 1 <= number <= MAX_INTEGER:
        raise argparse.ArgumentTypeError("expected a positive SQLite INTEGER")
    return number


def seconds(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected positive seconds") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected finite positive seconds")
    return number


def parser():
    root = argparse.ArgumentParser(prog="rvv-batch", description="Run RTL ELF batches into plain SQLite.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="create a new SQLite request and run its ELF inputs")
    run.add_argument("directory", type=Path)
    run.add_argument("--backend", choices=BACKENDS, required=True)
    run.add_argument("--output", type=Path, required=True, help="new SQLite file; never overwritten")
    run.add_argument("--image", help="existing simulator image (default: backend runtime image)")
    run.add_argument("--jobs", type=positive, default=1)
    run.add_argument("--seed", type=positive, default=1)
    run.add_argument("--max-cycles", type=positive, default=10000000)
    run.add_argument("--timeout", type=seconds, default=3600)
    run.add_argument("--wave", action="store_true", help="enable FST dumping (off by default)")
    run.add_argument("--cpu-set", help="Docker CPU IDs to allocate, e.g. 0-7 or 0,2,4,6")
    run.add_argument("--memory", help="per-container Docker memory limit, e.g. 8g (unset by default)")
    run.add_argument("--docker", default=os.environ.get("DOCKER", "docker"), help="Docker executable")
    resume = commands.add_parser("resume", help="retry all jobs that have not succeeded in the same SQLite file")
    resume.add_argument("database", type=Path)
    resume.add_argument("--jobs", type=positive, help="override previous scheduler concurrency")
    resume.add_argument("--timeout", type=seconds, help="override and save the per-attempt wall timeout in seconds")
    resume.add_argument("--max-cycles", type=positive, help="override and save the per-attempt simulator cycle limit")
    resume.add_argument("--docker", default=os.environ.get("DOCKER", "docker"), help="Docker executable")
    return root


def run(args, docker=None):
    docker = docker or Docker(args.docker)
    path = (args.output if args.action == "run" else args.database).absolute()
    # Resolve the parent only: a result symlink must not silently redirect writes.
    if path.is_symlink():
        raise ValueError("result database must not be a symlink")
    path = path.parent.resolve() / path.name
    if args.action == "resume" and not path.is_file():
        raise ValueError(f"database not found: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with run_lock(path):
        store = None
        try:
            if args.action == "run":
                if path.exists():
                    raise ValueError(f"database already exists; use resume: {path}")
                for suffix in (".work", ".artifacts", "-wal", "-shm"):
                    if Path(str(path) + suffix).exists():
                        raise ValueError(f"existing request sidecar: {path}{suffix}")
                if args.memory and not re.fullmatch(r"[1-9][0-9]*(?:[bBkKmMgG])?", args.memory):
                    raise ValueError("--memory must be a positive Docker size, e.g. 8g")
                if args.seed > 2**31 - 1:
                    raise ValueError("--seed must fit the simulators' signed 32-bit seed")
                workloads = discover(args.directory, path)
                image = args.image or image_name(args.backend)
                image_id, cpus, manifests = docker.preflight(image, args.backend, args.jobs, args.cpu_set)
                store = Store(path, create=True)
                stamp = now()
                config = dict(id=uuid.uuid4().hex, schema_version=SCHEMA_VERSION, backend=args.backend,
                              image_id=image_id, image_ref=image, input_dir=str(args.directory.resolve()),
                              created_at=stamp, updated_at=stamp, status="pending", jobs=args.jobs,
                              seed=args.seed, max_cycles=args.max_cycles, timeout=args.timeout,
                              wave=int(args.wave), cpu_set=args.cpu_set, memory=args.memory,
                              parser_version=PARSER_VERSION, tool_version=__version__)
                store.initialize(config, snapshot_inputs(workloads), manifests)
                jobs = args.jobs
            else:
                store = Store(path)
                config = store.run()
                if config["parser_version"] != PARSER_VERSION:
                    raise ValueError("parser version changed; resume with the original tool version")
                jobs = args.jobs if args.jobs is not None else config["jobs"]
                image_id, cpus, _ = docker.preflight(config["image_id"], config["backend"], jobs, config["cpu_set"])
                if image_id != config["image_id"]:
                    raise ValueError("resume requires the original immutable image ID")
                timeout = args.timeout if args.timeout is not None else config["timeout"]
                max_cycles = args.max_cycles if args.max_cycles is not None else config["max_cycles"]
                with store.db:
                    if args.timeout is not None or args.max_cycles is not None:
                        store.db.execute("UPDATE run SET timeout=?,max_cycles=?,updated_at=?", (timeout, max_cycles, now()))
                    store.event("resume", f"jobs={jobs}; timeout={timeout}; previous_timeout={config['timeout']}; "
                                f"max_cycles={max_cycles}; previous_max_cycles={config['max_cycles']}")
            result = Runner(store, docker, cpus, jobs).execute()
        finally:
            if store is not None:
                store.close()
    print(f"sqlite {path}", flush=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("rvv-batch: interrupted; use resume with the same database", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f"rvv-batch: {error}", file=sys.stderr)
        return 2
