"""Core behavior without Docker processes; only three CLI integration scenarios."""

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from test_backends import elf
from rvv_batch import __version__
from rvv_batch.artifacts import CHUNK_BYTES, read_result, write_zip
from rvv_batch.backends import PARSER_VERSION
from rvv_batch.cli import Cancellation, batch, parser, positive, seconds
from rvv_batch.make import arguments
from rvv_batch.orchestrator import Orchestrator, discover
from rvv_batch.runner import RunSpec, run_single, stage_input
from rvv_batch.store import SCHEMA_VERSION, Store, now, run_lock

ROOT = Path(__file__).resolve().parents[1]


class MemoryDocker:
    def __init__(self):
        self.items = {}
        self.created = []
        self.stopped = []
        self.cancel_on_start = None

    def preflight(self, image, backend, jobs, requested_cpus=None):
        if jobs > 2:
            raise ValueError('capacity')
        return f'sha256:{backend}', list(range(jobs)), {'config.txt': b'threads=1\n'}

    def create(self, name, run_id, attempt_id, image_id, cpu, memory, uid, gid, inputs, work, command):
        item = dict(name=name, run_id=run_id, cpu=cpu, work=work, command=command, image=image_id,
                    scenario=(inputs / 'program.elf').read_bytes().split(b'FAKE:')[-1].decode(),
                    state=dict(Running=False, ExitCode=0))
        self.items[name] = item
        self.created.append(item)

    def start(self, name):
        item = self.items[name]
        scenario = item['scenario']
        text = 'fixture 한글\n'
        if scenario != 'missing':
            text += 'RVV_KERNEL name=kernel cycles=123\n'
        if scenario == 'multiple':
            text += 'RVV_KERNEL name=second cycles=456\n'
        if scenario == 'invalid-marker':
            text += 'RVV_KERNEL name=bad cycles=-1\n'
        if scenario != 'no-total':
            text += ('SATURN simulation cycleCnt = 456\n' if 'saturn' in item['image']
                     else 'Core-0 instrCnt = 321, cycleCnt = 456, IPC = 0.7039\n')
        stderr = 'HIT GOOD TRAP\n'
        if scenario == 'fail':
            stderr = 'HIT BAD TRAP\n*** FAILED ***\n'
            item['state']['ExitCode'] = 1
        if scenario == 'limit':
            stderr = 'EXCEEDING CYCLE/INSTR LIMIT\n'
        if scenario == 'oom':
            item['state']['OOMKilled'] = True
        (item['work'] / 'stdout.log').write_text(text)
        (item['work'] / 'stderr.log').write_text(stderr)
        if '--wave' in item['command'] and scenario != 'no-wave':
            (item['work'] / 'wave.fst').write_bytes(b'FST\x00\xff')
        if scenario == 'long':
            item['state']['Running'] = True
        if self.cancel_on_start:
            self.cancel_on_start.set()

    def states(self, names):
        return {name: dict(self.items[name]['state']) for name in names}

    def stop(self, name):
        self.stopped.append(name)
        self.items[name]['state'].update(Running=False, ExitCode=143)

    def remove(self, name):
        del self.items[name]

    def containers(self, run_id):
        return [name for name, item in self.items.items() if item['run_id'] == run_id]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rvv core, ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docker = MemoryDocker()
        self.count = 0

    def spec(self, scenario='ok', backend='xiangshan-v3', **changes):
        self.count += 1
        spec = RunSpec(backend, f'sha256:{backend}', 'test:image', 1, 10000000, 3600,
                       False, 0, None, 'request', self.count, self.root / str(self.count), now())
        spec = replace(spec, **changes)
        stage_input(spec.directory, elf(scenario), {'config.txt': b'threads=1\n'})
        return spec

    def store(self):
        store = Store(self.root / 'run.sqlite', create=True)
        self.addCleanup(store.close)
        stamp = now()
        store.initialize(dict(id='request', schema_version=SCHEMA_VERSION, backend='xiangshan-v3',
                              image_id='sha256:xiangshan-v3', image_ref='test:image', input_dir='inputs',
                              created_at=stamp, updated_at=stamp, status='pending', jobs=2, seed=1,
                              max_cycles=10000000, timeout=3600, wave=0, cpu_set=None, memory=None,
                              parser_version=PARSER_VERSION, tool_version=__version__),
                         [('a.elf', elf()), ('b.elf', elf('fail'))], {'config.txt': b'threads=1\n'})
        return store

    def test_executor_classifies_core_outcomes_on_both_backends(self):
        cases = [('ok', 'succeeded', 'complete'), ('missing', 'succeeded', 'missing_kernel'),
                 ('multiple', 'succeeded', 'complete'), ('invalid-marker', 'succeeded', 'invalid'),
                 ('fail', 'failed', 'partial'), ('oom', 'failed', 'partial'),
                 ('no-wave', 'failed', 'partial')]
        for backend in ('xiangshan-v3', 'saturn'):
            for scenario, status, measurement in cases:
                with self.subTest(backend=backend, scenario=scenario):
                    spec = self.spec(scenario, backend, wave=True)
                    result = run_single(spec, self.docker, threading.Event())
                    self.assertEqual((result.status, result.measurement_status), (status, measurement))
                    self.assertEqual(result.total_cycle, 456)
                    self.assertEqual(read_result(spec.directory), result)
                    self.assertEqual(result.failed, status != 'succeeded' or measurement == 'invalid')
                    if scenario == 'multiple':
                        self.assertIn('kernel_cycle=multiple(2)', result.summary())
        for scenario, status in [('no-total', 'succeeded'), ('limit', 'cycle_limit')]:
            result = run_single(self.spec(scenario), self.docker, threading.Event())
            self.assertEqual(result.status, status)
            self.assertTrue(result.failed)
        self.assertFalse(self.docker.items)

    def test_timeout_cancel_and_invalid_input(self):
        spec = self.spec('long', timeout=1)
        result = run_single(spec, self.docker, threading.Event(), clock=iter([0, 2]).__next__)
        self.assertEqual(result.status, 'timeout')
        event = threading.Event()
        self.docker.cancel_on_start = event
        result = run_single(self.spec('long'), self.docker, event)
        self.assertEqual(result.status, 'interrupted')
        spec = self.spec()
        path = spec.directory / 'input/program.elf'
        path.chmod(0o644)
        path.write_bytes(b'invalid')
        count = len(self.docker.created)
        result = run_single(spec, self.docker, threading.Event())
        self.assertEqual(result.status, 'invalid_input')
        self.assertEqual(len(self.docker.created), count)
        self.assertFalse(self.docker.items)

    def test_controller_failure_preserves_work_without_completion_marker(self):
        spec = self.spec()
        with patch.object(self.docker, 'start', side_effect=RuntimeError('start failed')):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                run_single(spec, self.docker, threading.Event())
        self.assertEqual(self.docker.stopped, [spec.container])
        self.assertFalse((spec.directory / 'result.json').exists())
        self.assertTrue((spec.directory / 'input/program.elf').exists())

    def test_zip_is_complete_and_never_overwrites(self):
        spec = self.spec(wave=True)
        result = run_single(spec, self.docker, threading.Event())
        output = self.root / 'result.zip'
        write_zip(spec.directory, output, result)
        original = output.read_bytes()
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('input/program.elf'), elf())
            self.assertEqual(archive.read('output/wave.fst'), b'FST\x00\xff')
            self.assertEqual(archive.getinfo('output/wave.fst').compress_type, zipfile.ZIP_STORED)
            self.assertEqual(json.loads(archive.read('result.json'))['total_cycle'], 456)
        with self.assertRaises(FileExistsError):
            write_zip(spec.directory, output, result)
        self.assertEqual(output.read_bytes(), original)
        temporary = output.with_name(output.name + '.tmp')
        temporary.write_bytes(b'other invocation')
        with self.assertRaises(FileExistsError):
            write_zip(spec.directory, output, result)
        self.assertEqual(temporary.read_bytes(), b'other invocation')
        temporary.unlink()
        unpublished = self.root / 'unpublished.zip'
        with patch('rvv_batch.artifacts.os.link', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                write_zip(spec.directory, unpublished, result)
        self.assertFalse(unpublished.exists())
        self.assertTrue((spec.directory / 'result.json').exists())
        (spec.directory / 'output/wave.fst').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
            read_result(spec.directory)

    def test_store_import_rolls_back_and_can_be_repeated(self):
        store = self.store()
        spec = self.spec(wave=True, directory=Path(str(store.path) + '.work') / '1')
        store.start_attempt(store.jobs()[0], spec)
        result = run_single(spec, self.docker, threading.Event())
        with patch.object(store, 'event', side_effect=RuntimeError('commit failed')):
            with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                store.import_result(spec.directory, result)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 0)
        self.assertEqual(store.attempts()[0]['status'], 'running')
        self.assertTrue((spec.directory / 'output/wave.fst').exists())
        store.import_result(spec.directory, result)
        store.import_result(spec.directory, result)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0], len(result.measurements))
        wave = store.db.execute("SELECT relative_path FROM artifacts WHERE kind='wave'").fetchone()[0]
        self.assertEqual((self.root / wave).read_bytes(), b'FST\x00\xff')
        self.assertEqual(store.jobs()[0]['status'], 'succeeded')
        row = store.db.execute('SELECT total_cycle,kernel_cycle FROM job_results WHERE job_id=1').fetchone()
        self.assertEqual(tuple(row), (result.total_cycle, result.kernel_cycle))
        # A crash during post-commit cleanup may already have removed the marker.
        (spec.directory / 'result.json').unlink()
        Orchestrator(store, self.docker, [0], Cancellation()).recover()
        self.assertFalse(spec.directory.exists())

    def test_log_import_preserves_utf8_and_invalid_bytes(self):
        store = self.store()
        spec = self.spec()
        store.start_attempt(store.jobs()[0], spec)
        original_start = self.docker.start
        data = ('a' * (CHUNK_BYTES - 1) + '한글\n').encode() + b'\xff\n'
        def start(name):
            original_start(name)
            with (spec.directory / 'output/stdout.log').open('ab') as handle:
                handle.write(data)
        with patch.object(self.docker, 'start', side_effect=start):
            result = run_single(spec, self.docker, threading.Event())
        store.import_result(spec.directory, result)
        rows = store.db.execute("SELECT text,raw_bytes FROM logs WHERE stream='stdout' ORDER BY sequence")
        actual = b''.join(row[1] if row[1] is not None else row[0].encode() for row in rows)
        self.assertEqual(actual, (spec.directory / 'output/stdout.log').read_bytes())

    def test_orchestration_uses_distinct_cpus_and_resume_keeps_successes(self):
        store = self.store()
        barrier = threading.Barrier(2)
        def execute(spec, docker, event):
            barrier.wait(timeout=2)
            return run_single(spec, docker, event)
        orchestrator = Orchestrator(store, self.docker, [0, 1], Cancellation(), output=lambda *a, **k: None,
                                    executor=execute)
        self.assertEqual(orchestrator.execute(), 1)
        self.assertEqual({item['cpu'] for item in self.docker.created}, {0, 1})
        store.resume_settings(1, 7200, 20000000)
        self.assertEqual(Orchestrator(store, self.docker, [0], Cancellation(), output=lambda *a, **k: None).execute(), 1)
        self.assertEqual([a['job_id'] for a in store.attempts()], [1, 2, 2])
        self.assertEqual(json.loads(store.attempts()[-1]['settings_json'])['timeout'], 7200)
        self.assertEqual(self.docker.created[-1]['command'][4], '20000000')

    def test_recovery_imports_completed_and_retries_only_unfinished(self):
        store = self.store()
        work = Path(str(store.path) + '.work')
        spec = self.spec(directory=work / '1')
        store.start_attempt(store.jobs()[0], spec)
        run_single(spec, self.docker, threading.Event())
        spec2 = self.spec('fail', directory=work / '2')
        store.start_attempt(store.jobs()[1], spec2)
        # No result.json: even an exited container must be retried.
        self.docker.create(spec2.container, 'request', 2, spec2.image_id, 1, None, 0, 0,
                           spec2.directory / 'input', spec2.directory, [])
        self.docker.items[spec2.container]['state']['Running'] = True
        code = Orchestrator(store, self.docker, [0], Cancellation(), output=lambda *a, **k: None).execute()
        self.assertEqual(code, 1)
        self.assertEqual([(a['job_id'], a['status']) for a in store.attempts()],
                         [(1, 'succeeded'), (2, 'interrupted'), (2, 'failed')])
        self.assertIn(spec2.container, self.docker.stopped)
        self.assertFalse(spec.directory.exists())
        self.assertTrue(spec2.directory.exists())

    def test_discovery_lock_and_old_schema_rejection(self):
        inputs = self.root / 'inputs'
        for name in ('a', 'b'):
            (inputs / name).mkdir(parents=True)
            (inputs / name / 'same').write_bytes(elf())
        (inputs / 'link').symlink_to(inputs / 'a', target_is_directory=True)
        self.assertEqual([name for name, _ in discover(inputs, self.root / 'out')], ['a/same', 'b/same'])
        path = self.root / 'old.sqlite'
        db = sqlite3.connect(path)
        db.execute('PRAGMA user_version=1')
        db.close()
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'original tool'):
            Store(path)
        self.assertEqual(path.read_bytes(), original)
        with run_lock(path):
            with self.assertRaisesRegex(ValueError, 'another runner'):
                with run_lock(path):
                    pass

    def test_cli_and_make_validation_and_failed_preflight_do_not_save_overrides(self):
        args = arguments('run-single', {'ELF': 'a b.elf', 'BACKEND': 'saturn', 'ARTIFACT': 'a b.zip', 'WAVE': '1'})
        parsed = parser().parse_args(args)
        self.assertEqual(parsed.elf, Path('a b.elf'))
        self.assertTrue(parsed.wave)
        with self.assertRaises(ValueError):
            arguments('run-single', {'JOBS': '2'})
        for converter, values in ((positive, ['0', '-1', str(2**63)]), (seconds, ['0', 'nan', 'inf'])):
            for value in values:
                with self.assertRaises(Exception):
                    converter(value)
        store = self.store()
        before = store.run()
        args = parser().parse_args(['resume', str(store.path), '--jobs', '3', '--timeout', '5'])
        with self.assertRaisesRegex(ValueError, 'capacity'):
            batch(args, self.docker, Cancellation())
        self.assertEqual(store.run(), before)


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rvv CLI, spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.database = self.root / 'result.sqlite'
        self.fake_root = self.root / 'fake'
        self.env = dict(os.environ, RVV_FAKE_DOCKER_ROOT=str(self.fake_root))
        self.docker = self.root / 'fake docker'
        self.docker.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests/fake_docker.py"}" "$@"\n')
        self.docker.chmod(0o755)
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for path in self.fake_root.glob('rvv-*.json'):
            item = json.loads(path.read_text())
            if item['State']['Running'] and item.get('pid'):
                try:
                    os.kill(item['pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'rvv_batch', *map(str, args), '--docker', str(self.docker)],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=15)

    def query(self, sql):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_standalone_zip_and_make_entrypoint(self):
        path = self.inputs / 'one.elf'
        path.write_bytes(elf())
        output = self.root / 'result.zip'
        result = subprocess.run(['make', 'run-single', f'ELF={path}', 'BACKEND=saturn',
                                 f'ARTIFACT={output}', 'WAVE=1', f'DOCKER={self.docker}'],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('total_cycle=456 kernel_cycle=123 status=succeeded', result.stdout)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('input/program.elf'), path.read_bytes())
            self.assertTrue(archive.read('output/wave.fst'))
        self.assertFalse(Path(str(output) + '.work').exists())

    def test_parallel_batch_and_resume_with_saved_inputs_and_overrides(self):
        (self.inputs / 'a.elf').write_bytes(elf())
        (self.inputs / 'b.elf').write_bytes(elf('fail'))
        result = self.cli('run', self.inputs, '--backend', 'xiangshan-v3', '--output', self.database, '--jobs', '2')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('finish [2/2]', result.stdout)
        for path in self.inputs.iterdir():
            path.unlink()
        result = self.cli('resume', self.database, '--jobs', '1', '--timeout', '7200', '--max-cycles', '20000000')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.query('SELECT status,attempt_count FROM job_results ORDER BY job_id'),
                         [('succeeded', 1), ('failed', 2)])
        self.assertEqual(self.query('SELECT jobs,timeout,max_cycles FROM run'), [(1, 7200, 20000000)])
        self.assertEqual(self.query('PRAGMA user_version'), [(2,)])
        self.assertFalse(Path(str(self.database) + '.work').exists())

    def test_sigint_stops_and_saves_interrupted_result(self):
        (self.inputs / 'long.elf').write_bytes(elf('long'))
        process = subprocess.Popen([sys.executable, '-m', 'rvv_batch', 'run', str(self.inputs),
                                    '--backend', 'xiangshan-v3', '--output', str(self.database),
                                    '--docker', str(self.docker)], cwd=ROOT, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                logs = list(Path(str(self.database) + '.work').glob('*/output/stdout.log'))
                if logs and b'RVV_KERNEL' in logs[0].read_bytes():
                    break
                time.sleep(0.01)
            else:
                self.fail('simulator did not start')
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertIn('status=interrupted', stdout)
            self.assertEqual(self.query('SELECT status,kernel_cycle FROM job_results'), [('interrupted', 123)])
            self.assertTrue(self.query('SELECT text FROM logs'))
            self.assertFalse(list(self.fake_root.glob('rvv-*.json')))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


if __name__ == '__main__':
    unittest.main()
