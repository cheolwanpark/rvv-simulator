"""Thin public entry points for standalone execution and SQLite batches."""

import argparse
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import sys
import threading
import uuid

from . import __version__
from .artifacts import now, write_zip
from .backends import BACKENDS, MAX_INTEGER, PARSER_VERSION, image_name
from .docker import Docker
from .orchestrator import Orchestrator, discover, snapshot_inputs
from .runner import RunSpec, run_single, stage_input
from .store import SCHEMA_VERSION, Store, run_lock


def positive(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('expected a positive integer') from error
    if not 1 <= number <= MAX_INTEGER:
        raise argparse.ArgumentTypeError('expected a positive signed 64-bit integer')
    return number


def seconds(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('expected positive seconds') from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('expected finite positive seconds')
    return number


def limits(parser, *, resume=False):
    parser.add_argument('--max-cycles', type=positive, default=None if resume else 10000000)
    parser.add_argument('--timeout', type=seconds, default=None if resume else 3600)


def simulation_options(parser):
    parser.add_argument('--backend', choices=BACKENDS, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new output file; never overwritten')
    parser.add_argument('--image', help='existing simulator image')
    parser.add_argument('--seed', type=positive, default=1)
    limits(parser)
    parser.add_argument('--wave', action='store_true', help='enable FST dumping (off by default)')
    parser.add_argument('--cpu-set', help='Docker CPU IDs, e.g. 0-7 or 0,2,4,6')
    parser.add_argument('--memory', help='per-container Docker memory limit, e.g. 8g')


def parser():
    root = argparse.ArgumentParser(prog='rvv-batch', description='Execute RTL ELFs into a ZIP or SQLite batch.')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='action', required=True)
    single = commands.add_parser('run-single', help='execute one ELF into a ZIP artifact')
    single.add_argument('elf', type=Path)
    simulation_options(single)
    run = commands.add_parser('run', help='run an ELF directory into a new SQLite request')
    run.add_argument('directory', type=Path)
    run.add_argument('--jobs', type=positive, default=1)
    simulation_options(run)
    resume = commands.add_parser('resume', help='retry unsuccessful jobs in the same SQLite request')
    resume.add_argument('database', type=Path)
    resume.add_argument('--jobs', type=positive)
    limits(resume, resume=True)
    for command in (single, run, resume):
        command.add_argument('--docker', default=os.environ.get('DOCKER', 'docker'), help='Docker executable')
    return root


class Cancellation:
    """Signal handlers live only on the calling/main thread; workers share an Event."""
    def __init__(self):
        self.event = threading.Event()
        self.signum = None

    def signal(self, signum, frame):
        self.signum = signum
        self.event.set()

    def __enter__(self):
        self.previous = {sig: signal.signal(sig, self.signal) for sig in (signal.SIGINT, signal.SIGTERM)}
        return self

    def __exit__(self, *exc):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)


def output_path(path):
    path = path.absolute()
    if path.is_symlink():
        raise ValueError('output must not be a symlink')
    return path.parent.resolve() / path.name


def validate_settings(args):
    if args.memory and not re.fullmatch(r'[1-9][0-9]*(?:[bBkKmMgG])?', args.memory):
        raise ValueError('--memory must be a positive Docker size, e.g. 8g')
    if args.seed > 2**31 - 1:
        raise ValueError('--seed must fit the simulators\' signed 32-bit seed')


def standalone(args, docker, cancellation):
    path = output_path(args.output)
    work = Path(str(path) + '.work')
    for existing in (path, work, Path(str(path) + '.tmp')):
        if existing.exists() or existing.is_symlink():
            raise ValueError(f'output or work already exists: {existing}')
    validate_settings(args)
    data = args.elf.read_bytes()
    image = args.image or image_name(args.backend)
    image_id, cpus, manifests = docker.preflight(image, args.backend, 1, args.cpu_set)
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = RunSpec(args.backend, image_id, image, args.seed, args.max_cycles, args.timeout,
                   args.wave, cpus[0], args.memory, uuid.uuid4().hex, 1, work, now())
    stage_input(work, data, manifests)
    result = run_single(spec, docker, cancellation.event)
    write_zip(work, path, result)
    shutil.rmtree(work)
    print(result.summary(), flush=True)
    print(f'artifact {path}', flush=True)
    return 128 + cancellation.signum if cancellation.signum else int(result.failed)


def batch(args, docker, cancellation):
    path = output_path(args.output if args.action == 'run' else args.database)
    if args.action == 'resume' and not path.is_file():
        raise ValueError(f'database not found: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    with run_lock(path):
        store = None
        try:
            if args.action == 'run':
                if path.exists():
                    raise ValueError(f'database already exists; use resume: {path}')
                for suffix in ('.work', '.artifacts', '-wal', '-shm'):
                    sidecar = Path(str(path) + suffix)
                    if sidecar.exists() or sidecar.is_symlink():
                        raise ValueError(f'existing request sidecar: {sidecar}')
                validate_settings(args)
                workloads = discover(args.directory, path)
                image = args.image or image_name(args.backend)
                image_id, cpus, manifests = docker.preflight(image, args.backend, args.jobs, args.cpu_set)
                store = Store(path, create=True)
                stamp = now()
                config = dict(id=uuid.uuid4().hex, schema_version=SCHEMA_VERSION, backend=args.backend,
                              image_id=image_id, image_ref=image, input_dir=str(args.directory.resolve()),
                              created_at=stamp, updated_at=stamp, status='pending', jobs=args.jobs,
                              seed=args.seed, max_cycles=args.max_cycles, timeout=args.timeout,
                              wave=int(args.wave), cpu_set=args.cpu_set, memory=args.memory,
                              parser_version=PARSER_VERSION, tool_version=__version__)
                store.initialize(config, snapshot_inputs(workloads), manifests)
            else:
                store = Store(path)
                config = store.run()
                if config['parser_version'] != PARSER_VERSION:
                    raise ValueError('parser version changed; resume with the original tool version')
                jobs = args.jobs if args.jobs is not None else config['jobs']
                image_id, cpus, _ = docker.preflight(config['image_id'], config['backend'], jobs, config['cpu_set'])
                if image_id != config['image_id']:
                    raise ValueError('resume requires the original immutable image ID')
                store.resume_settings(jobs, args.timeout if args.timeout is not None else config['timeout'],
                                      args.max_cycles if args.max_cycles is not None else config['max_cycles'])
            code = Orchestrator(store, docker, cpus, cancellation).execute()
        finally:
            if store is not None:
                store.close()
    print(f'sqlite {path}', flush=True)
    return code


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with Cancellation() as cancellation:
            action = standalone if args.action == 'run-single' else batch
            return action(args, Docker(args.docker), cancellation)
    except KeyboardInterrupt:
        print('rvv-batch: interrupted', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f'rvv-batch: {error}', file=sys.stderr)
        return 2
