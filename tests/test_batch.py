import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from rvv_batch.backends import PARSER_VERSION, command, parse_lines, validate_elf
from rvv_batch.docker import bind_mount, cpu_list
from rvv_batch.make import arguments
from rvv_batch.runner import discover, parse_docker_timestamp
from rvv_batch.store import CHUNK_BYTES, Store, run_lock

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_docker.py"


def loop_record(**changes):
    record = dict(schema_version=2, mode="kernel", seed=0, repetitions=1,
                  warmups=0, metric="cycles", value=47424, numerical_validation="not_run")
    record.update(changes)
    return json.dumps(record)


def elf(scenario="ok"):
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    payload = b"\x13\0\0\0FAKE:" + scenario.encode()
    header = struct.pack("<16sHHIQQQIHHHHHH", ident, 2, 243, 1, 0x80000000,
                         64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 120, 0x80000000, 0x80000000, len(payload), len(payload) + 16, 1)
    return header + segment + payload


class BackendTests(unittest.TestCase):
    def test_static_elf_validation(self):
        validate_elf(elf())
        for data in (b"invalid", elf()[:90], elf()[:18] + b"\x3e\0" + elf()[20:]):
            with self.assertRaises(ValueError):
                validate_elf(data)
        dynamic = bytearray(elf())
        struct.pack_into("<I", dynamic, 64, 3)
        with self.assertRaisesRegex(ValueError, "dynamic"):
            validate_elf(dynamic)

    def test_xs_counts_do_not_mix_warmup_guest_or_host_time(self):
        parsed = parse_lines("xiangshan-v3", [
            ("stdout", 1, "\x1b[32mCore-0 instrCnt = 1,234, cycleCnt = 5,678, IPC = 0.2173\x1b[0m"),
            ("stdout", 2, "Core-0(Soft Warmup) instrCnt = 10, cycleCnt = 20, IPC = 0.5"),
            ("stdout", 3, "Guest cycle spent: 5,680"),
            ("stdout", 4, "Host time spent: 3000ms"),
            ("stdout", 5, "RVV_KERNEL name=a cycles=11"),
            ("stdout", 6, "RVV_KERNEL name=a cycles=12"),
            ("stderr", 1, "HIT GOOD TRAP at pc = 0x80000000"),
        ])
        self.assertTrue(parsed.good)
        self.assertEqual([m[3] for m in parsed.measurements if m[0] == "kernel_cycle"], [11, 12])
        self.assertEqual([m[3] for m in parsed.measurements if m[0] == "cycle" and m[1] == "simulation"], [5678])
        self.assertEqual(len(parsed.measurements), 9)

    def test_saturn_failure_and_invalid_markers(self):
        parsed = parse_lines("saturn", [
            ("stdout", 1, "SATURN simulation cycleCnt = 900"),
            ("stderr", 1, "*** FAILED *** (timeout) after 900 simulation cycles"),
            ("stdout", 2, "RVV_KERNEL name=bad cycles=-1"),
            ("stdout", 3, f"RVV_KERNEL name=overflow cycles={2**64}"),
        ])
        self.assertTrue(parsed.bad and parsed.limit)
        self.assertEqual(len(parsed.errors), 2)

    def test_loop_benchmark_cycles_on_all_backends(self):
        for backend in ("xiangshan-v2", "xiangshan-v3", "saturn"):
            with self.subTest(backend=backend):
                parsed = parse_lines(backend, [
                    ("stdout", 7, "\x1b[0m" + loop_record() + "\r"),
                    ("stderr", 9, loop_record(mode="full", repetitions=32, warmups=1, value=98765)),
                    ("stdout", 10, loop_record(value=0)),
                ])
                self.assertEqual(parsed.errors, [])
                self.assertEqual(parsed.measurements, [
                    ("kernel_cycle", "kernel", "bench_kernel", value, stream, line, "loop-benchmarks.v2")
                    for value, stream, line in [(47424, "stdout", 7), (98765, "stderr", 9), (0, "stdout", 10)]
                ])
                self.assertFalse(parsed.good)  # Measurements alone do not prove completion.

    def test_loop_benchmark_rejects_invalid_records(self):
        invalid = [loop_record(**change) for change in [
            {"schema_version": 1}, {"schema_version": 2.0}, {"mode": "unknown"},
            {"repetitions": 0}, {"repetitions": True}, {"warmups": -1}, {"warmups": 0.5},
            {"value": True}, {"value": "123"}, {"value": 1.5}, {"value": None},
            {"value": -1}, {"value": 2**63}, {"value": float("nan")},
        ]]
        invalid += [loop_record()[:-5], loop_record().replace("47424", "9" * 5000)]
        for line in invalid:
            with self.subTest(line=line[:180]):
                parsed = parse_lines("xiangshan-v2", [("stdout", 1, line)])
                self.assertEqual(parsed.measurements, [])
                self.assertEqual(len(parsed.errors), 1)
                self.assertIn("kernel_cycle", parsed.errors[0])

    def test_loop_benchmark_ignores_hosted_time_and_unrelated_json(self):
        parsed = parse_lines("saturn", [
            ("stdout", 1, loop_record(metric="elapsed_ns")),
            ("stdout", 2, '{"status":"ready"}'),
            ("stdout", 3, '{"schema_version":2,"status":"ready"}'),
        ])
        self.assertEqual(parsed.measurements, [])
        self.assertEqual(parsed.errors, [])

    def test_default_wave_disabled_and_cpuset_validation(self):
        for backend in ("xiangshan-v2", "xiangshan-v3", "saturn"):
            self.assertNotIn("--wave", command(backend, 100, 1, False))
            self.assertNotIn("--wave-path", command(backend, 100, 1, False))
            self.assertIn("--wave", command(backend, 100, 1, True))
        self.assertEqual(cpu_list("1,3-5,3"), [1, 3, 4, 5])
        for value in ("", "2-1", "-1", "1--3"):
            with self.assertRaises(ValueError):
                cpu_list(value)

    def test_mount_and_make_arguments_preserve_special_paths(self):
        path = '/tmp/a, b "quotes" $literal'
        fields = next(csv.reader([bind_mount(path, "/input", True)]))
        self.assertIn("src=" + path, fields)
        args = arguments("run", {"ELF_DIR": path, "DB": path + ".sqlite", "BACKEND": "saturn", "WAVE": "0"})
        self.assertEqual(args[1], path)
        self.assertNotIn("--wave", args)
        with self.assertRaises(ValueError):
            arguments("run", {"ELF_DIR": path, "DB": "x", "BACKEND": "saturn", "WAVE": "false"})
        with self.assertRaises(ValueError):
            arguments("resume", {"DB": "x", "WAVE": "1"})
        self.assertEqual(arguments("resume", {"DB": "x"}), ["resume", "x"])


class TimestampTests(unittest.TestCase):
    def test_docker_fractional_precision_and_timezones(self):
        fractions = [("", 0), (".8", 800000), (".86", 860000), (".867", 867000),
                     (".8670", 867000), (".86702", 867020), (".867021", 867021),
                     (".8670213", 867021), (".86702137", 867021), (".867021375", 867021),
                     (".999999999", 999999)]
        offsets = [("Z", timedelta()), ("+00:00", timedelta()),
                   ("+09:00", timedelta(hours=9)), ("-04:30", -timedelta(hours=4, minutes=30))]
        for fraction, microsecond in fractions:
            for suffix, offset in offsets:
                stamp = "2026-10-09T09:30:53" + fraction + suffix
                with self.subTest(timestamp=stamp):
                    expected = datetime(2026, 10, 9, 9, 30, 53, microsecond, tzinfo=timezone(offset))
                    self.assertEqual(parse_docker_timestamp(stamp), expected)


class BatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rvv batch, spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "ELF inputs"
        self.inputs.mkdir()
        self.database = self.root / "result.sqlite"
        self.fake_root = self.root / "fake"
        self.env = dict(os.environ, RVV_FAKE_DOCKER_ROOT=str(self.fake_root))
        # A portable executable shim avoids requiring the checkout's executable bit.
        self.docker = self.root / "fake docker"
        self.docker.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        self.docker.chmod(0o755)

    def tearDown(self):
        for path in self.fake_root.glob("rvv-*.json"):
            item = json.loads(path.read_text())
            if item["State"]["Running"] and item.get("pid"):
                try:
                    os.kill(item["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def add(self, name, scenario="ok"):
        path = self.inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(elf(scenario))
        return path

    def argv(self, *extra, resume=False):
        if resume:
            return [sys.executable, "-m", "rvv_batch", "resume", str(self.database), "--docker", str(self.docker), *extra]
        return [sys.executable, "-m", "rvv_batch", "run", str(self.inputs), "--backend", "xiangshan-v3",
                "--output", str(self.database), "--docker", str(self.docker), *extra]

    def invoke(self, *extra, resume=False):
        return subprocess.run(self.argv(*extra, resume=resume), cwd=ROOT, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def connection(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def calls(self):
        return [json.loads(line) for line in (self.fake_root / "calls.jsonl").read_text().splitlines()]

    def test_parallel_batch_plain_sqlite_and_same_db_resume(self):
        original = self.add("a/no-extension")
        self.add("b/no-extension", "multiple")
        self.add("missing.elf", "missing")
        self.add("failed.elf", "fail")
        (self.inputs / "ignore.txt").write_text("not ELF")
        result = self.invoke("--jobs", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("start [1/4] a/no-extension", result.stdout)
        self.assertIn("total_cycle=456 kernel_cycle=123 status=succeeded", result.stdout)
        self.assertIn("kernel_cycle=multiple(2)", result.stdout)
        db = self.connection()
        rows = db.execute("SELECT * FROM job_results ORDER BY job_id").fetchall()
        self.assertEqual([r["status"] for r in rows], ["succeeded", "succeeded", "failed", "succeeded"])
        self.assertEqual(rows[-1]["measurement_status"], "missing_kernel")
        stored = db.execute("SELECT data FROM artifacts WHERE name='a/no-extension'").fetchone()[0]
        self.assertEqual(stored, original.read_bytes())
        self.assertIn("fixture start 한글", db.execute("SELECT text FROM logs LIMIT 1").fetchone()[0])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts WHERE storage='external'").fetchone()[0], 0)
        self.assertFalse(Path(str(self.database) + ".work").exists())
        creates = [call for call in self.calls() if call[0] == "create"]
        self.assertEqual(len(creates), 4)
        self.assertEqual({c[c.index("--cpuset-cpus") + 1] for c in creates[:2]}, {"0", "1"})
        self.assertTrue(all(c[c.index("--cpus") + 1] == "1" and "--wave" not in c for c in creates))
        original.unlink()
        before = db.execute("SELECT id FROM run").fetchone()[0]
        db.close()
        resumed = self.invoke("--jobs", "1", resume=True)
        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        self.assertIn("start [4/4] failed.elf", resumed.stdout)
        self.assertIn("finish [4/4] failed.elf", resumed.stdout)
        db = self.connection()
        self.assertEqual(db.execute("SELECT id FROM run").fetchone()[0], before)
        self.assertEqual([r[0] for r in db.execute("SELECT attempt_count FROM job_results ORDER BY job_id")],
                         [1, 1, 2, 1])
        self.assertEqual(len([c for c in self.calls() if c[0] == "create"]), 5)
        overwrite = self.invoke()
        self.assertEqual(overwrite.returncode, 2)
        self.assertIn("already exists", overwrite.stderr)

    def test_wave_original_external_file_and_saturn(self):
        self.add("wave.elf")
        result = self.invoke("--backend", "saturn", "--wave")
        self.assertEqual(result.returncode, 0, result.stderr)
        db = self.connection()
        wave = db.execute("SELECT * FROM artifacts WHERE kind='wave'").fetchone()
        self.assertIsNone(wave["data"])
        self.assertEqual((self.database.parent / wave["relative_path"]).read_bytes(), b"fixture FST\x00\xff")
        self.assertEqual(db.execute("SELECT total_cycle FROM job_results").fetchone()[0], 456)
        resumed = self.invoke(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertNotIn("start [", resumed.stdout)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.assertEqual(len([c for c in self.calls() if c[0] == "create"]), 1)

    def test_loop_benchmark_json_is_stored_and_invalid_is_reported(self):
        self.add("a-loop.elf", "loop-json")
        self.add("b-invalid.elf", "loop-json-invalid")
        result = self.invoke("--jobs", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("total_cycle=456 kernel_cycle=123 status=succeeded", result.stdout)
        db = self.connection()
        rows = db.execute("SELECT * FROM job_results ORDER BY job_id").fetchall()
        self.assertEqual((rows[0]["kernel_cycle"], rows[0]["kernel_sample_count"],
                          rows[0]["kernel_status"], rows[0]["measurement_status"]),
                         (123, 1, "available", "complete"))
        self.assertEqual((rows[1]["kernel_status"], rows[1]["measurement_status"]), ("invalid", "invalid"))
        sample = db.execute("SELECT name,source,source_stream FROM measurements WHERE metric='kernel_cycle'").fetchone()
        self.assertEqual(tuple(sample), ("bench_kernel", "loop-benchmarks.v2", "stdout"))
        self.assertEqual(db.execute("SELECT parser_version FROM run").fetchone()[0], PARSER_VERSION)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='measurement_error'").fetchone()[0], 1)

    def test_resume_retries_timeout_and_invalid_input_once_per_invocation(self):
        self.add("a-long.elf", "long")
        self.add("b-ok.elf")
        (self.inputs / "c-invalid.elf").write_bytes(b"bad")
        result = self.invoke("--timeout", "0.4")
        self.assertEqual(result.returncode, 1, result.stderr)
        db = self.connection()
        rows = db.execute("SELECT * FROM job_results ORDER BY job_id").fetchall()
        self.assertEqual([r["status"] for r in rows], ["timeout", "succeeded", "invalid_input"])
        self.assertEqual(rows[0]["kernel_cycle"], 123)
        original_attempts = db.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall()
        original_logs = db.execute("SELECT * FROM logs ORDER BY attempt_id,stream,sequence").fetchall()
        for attempt_count in (2, 3):
            resumed = self.invoke(resume=True)
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            self.assertIn("start [2/3] a-long.elf", resumed.stdout)
            self.assertIn("finish [2/3] a-long.elf", resumed.stdout)
            self.assertIn("finish [3/3] c-invalid.elf", resumed.stdout)
            rows = db.execute("SELECT * FROM job_results ORDER BY job_id").fetchall()
            self.assertEqual([r["status"] for r in rows], ["timeout", "succeeded", "invalid_input"])
            self.assertEqual([r["attempt_count"] for r in rows], [attempt_count, 1, attempt_count])
            self.assertEqual(db.execute("SELECT * FROM attempts WHERE attempt_no=1 ORDER BY attempt_id").fetchall(),
                             original_attempts)
            self.assertEqual(db.execute("SELECT * FROM logs WHERE attempt_id<=3 ORDER BY attempt_id,stream,sequence").fetchall(),
                             original_logs)
            self.assertEqual(len([c for c in self.calls() if c[0] == "create"]), attempt_count + 1)

    def test_resume_retries_cycle_limit(self):
        self.add("limit.elf", "cycle-limit")
        result = self.invoke()
        self.assertEqual(result.returncode, 1, result.stderr)
        db = self.connection()
        self.assertEqual(db.execute("SELECT status FROM job_results").fetchone()[0], "cycle_limit")
        resumed = self.invoke(resume=True)
        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        self.assertIn("finish [1/1] limit.elf", resumed.stdout)
        self.assertEqual([tuple(r) for r in db.execute("SELECT attempt_no,status FROM attempts ORDER BY attempt_no")],
                         [(1, "cycle_limit"), (2, "cycle_limit")])

    def test_progress_counts_completions_independently_of_job_ids(self):
        self.add("a-long.elf", "long")
        self.add("b-ok.elf")
        (self.inputs / "c-invalid.elf").write_bytes(b"bad")
        result = self.invoke("--jobs", "2", "--timeout", "2")
        self.assertEqual(result.returncode, 1, result.stderr)
        starts = [line.split(" ", 2)[:2] for line in result.stdout.splitlines() if line.startswith("start ")]
        self.assertEqual(starts, [["start", f"[{i}/3]"] for i in range(1, 4)])
        finishes = [line.split(" ", 3)[:3] for line in result.stdout.splitlines() if line.startswith("finish ")]
        self.assertEqual(finishes, [["finish", "[1/3]", "b-ok.elf"],
                                    ["finish", "[2/3]", "c-invalid.elf"],
                                    ["finish", "[3/3]", "a-long.elf"]])

    def test_preflight_rejects_threads_and_oversubscription_without_db(self):
        self.add("one.elf")
        result = self.invoke("--jobs", "5")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.database.exists())
        self.env["RVV_FAKE_THREADS"] = "2"
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("emu_threads=1", result.stderr)
        self.assertFalse(self.database.exists())

    def test_missing_and_invalid_measurements_and_missing_wave(self):
        self.add("a-missing.elf", "missing")
        self.add("b-invalid.elf", "invalid-marker")
        self.add("c-total.elf", "no-total")
        self.add("d-wave.elf", "no-wave")
        result = self.invoke("--jobs", "2", "--wave")
        self.assertEqual(result.returncode, 1, result.stderr)
        rows = self.connection().execute("SELECT * FROM job_results ORDER BY job_id").fetchall()
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(rows[0]["kernel_status"], "missing")
        self.assertEqual(rows[1]["measurement_status"], "invalid")
        self.assertEqual(rows[1]["kernel_status"], "invalid")
        self.assertEqual(rows[2]["measurement_status"], "missing_total")
        self.assertIsNone(rows[2]["total_cycle"])
        self.assertEqual(rows[3]["status"], "failed")
        self.assertEqual(rows[3]["measurement_status"], "partial")
        resumed = self.invoke(resume=True)
        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        rows = self.connection().execute("SELECT attempt_count FROM job_results ORDER BY job_id").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 1, 1, 2])

    def resume_exited_simulator(self, scenario):
        self.add("one.elf", scenario)
        env = dict(self.env, RVV_FAKE_INSPECT_DELAY="2")
        process = subprocess.Popen(self.argv(), cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.addCleanup(process.stderr.close)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            files = list(self.fake_root.glob("rvv-*.json"))
            if files and json.loads(files[0].read_text())["State"]["Status"] == "exited":
                break
            time.sleep(0.025)
        else:
            self.fail("simulator did not exit")
        finished_at = json.loads(files[0].read_text())["State"]["FinishedAt"]
        process.kill()
        process.wait(timeout=5)
        return self.invoke(resume=True), finished_at

    def test_resume_finalizes_already_exited_simulator_without_rerun(self):
        result, finished_at = self.resume_exited_simulator("ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("finish [1/1] one.elf", result.stdout)
        row = self.connection().execute("SELECT * FROM job_results").fetchone()
        self.assertEqual(row["attempt_count"], 1)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["total_cycle"], 456)
        self.assertEqual(row["finished_at"], finished_at[:23] + "+00:00")

    def test_resume_retries_recovered_failure_without_double_counting(self):
        result, _ = self.resume_exited_simulator("fail")
        self.assertEqual(result.returncode, 1, result.stderr)
        progress = [line.split(" ", 2)[:2] for line in result.stdout.splitlines()
                    if line.startswith(("start ", "finish "))]
        self.assertEqual(progress, [["finish", "[0/1]"], ["start", "[1/1]"], ["finish", "[1/1]"]])
        rows = self.connection().execute("SELECT attempt_no,status FROM attempts ORDER BY attempt_no").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "failed"), (2, "failed")])

    def test_killed_controller_recovers_live_container_and_preserves_input(self):
        self.add("a-ok.elf")
        original = self.add("b-long.elf", "long")
        process = subprocess.Popen(self.argv("--timeout", "1.5"), cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.addCleanup(process.stderr.close)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.database.exists():
                try:
                    with sqlite3.connect(self.database) as db:
                        rows = db.execute("SELECT status FROM job_results ORDER BY job_id").fetchall()
                        logged = db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
                    if rows == [("succeeded",), ("running",)] and logged >= 3:
                        break
                except sqlite3.Error:
                    pass
            time.sleep(0.05)
        else:
            self.fail("controller did not start both jobs")
        process.kill()
        process.wait(timeout=5)
        original.unlink()
        result = self.invoke(resume=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("finish [1/2] b-long.elf", result.stdout)
        self.assertIn("start [2/2] b-long.elf", result.stdout)
        self.assertIn("finish [2/2] b-long.elf", result.stdout)
        db = self.connection()
        attempts = db.execute("SELECT job_id,attempt_no,status FROM attempts ORDER BY attempt_id").fetchall()
        self.assertEqual([tuple(r) for r in attempts], [(1, 1, "succeeded"), (2, 1, "interrupted"), (2, 2, "timeout")])
        counts = db.execute("SELECT attempt_id,COUNT(*) FROM logs GROUP BY attempt_id").fetchall()
        self.assertEqual(len(counts), 3)
        self.assertEqual(len([c for c in self.calls() if c[0] == "create"]), 3)

    def test_sigint_commits_interrupted_attempt(self):
        self.add("long.elf", "long")
        process = subprocess.Popen(self.argv(), cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            containers = list(self.fake_root.glob("rvv-*.json"))
            if containers and json.loads(containers[0].read_text()).get("pid"):
                break
            time.sleep(0.05)
        time.sleep(0.1)
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, err)
        self.assertIn("finish [0/1] long.elf", out)
        self.assertIn("status=interrupted", out)
        self.assertEqual(self.connection().execute("SELECT status FROM run").fetchone()[0], "interrupted")

    def test_make_end_to_end(self):
        self.add("a.elf")
        result = subprocess.run(["make", "run", f"ELF_DIR={self.inputs}", "BACKEND=saturn", f"DB={self.database}",
                                 f"DOCKER={self.docker}", "WAVE=0"], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.connection().execute("SELECT wave FROM run").fetchone()[0], 0)


class StoreTests(unittest.TestCase):
    def test_live_logs_are_plain_text_idempotent_and_preserve_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "result.sqlite", create=True)
            try:
                artifact = store.blob("elf", "input", elf())
                store.db.execute("INSERT INTO jobs VALUES(1,'input',?,NULL)", (artifact,))
                store.db.execute("INSERT INTO attempts(attempt_id,job_id,attempt_no,status,started_at,command_json) "
                                 "VALUES(1,1,1,'running','2026-01-01T00:00:00+00:00','[]')")
                data = ("a" * (CHUNK_BYTES - 1) + "한글\nRVV_KERNEL name=k cycles=7\n").encode()
                log = root / "stdout.log"
                log.write_bytes(data[:-1])
                store.ingest(1, root)
                store.ingest(1, root)
                with log.open("ab") as handle:
                    handle.write(b"\n")
                store.ingest(1, root, final=True)
                rows = store.db.execute("SELECT text,raw_bytes FROM logs ORDER BY sequence").fetchall()
                self.assertEqual("".join(row[0] for row in rows).encode(), data)
                self.assertTrue(all(row[1] is None for row in rows))
                parsed = parse_lines("saturn", store.lines(1))
                self.assertEqual(parsed.measurements[0][3], 7)
                with log.open("ab") as handle:
                    handle.write(b"\xff invalid bytes\n")
                store.ingest(1, root, final=True)
                self.assertEqual(store.db.execute("SELECT raw_bytes FROM logs ORDER BY sequence DESC LIMIT 1").fetchone()[0], b"\xff invalid bytes\n")
            finally:
                store.close()

    def test_lock_excludes_second_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db"
            with run_lock(path):
                with self.assertRaisesRegex(ValueError, "another runner"):
                    with run_lock(path):
                        pass

    def test_discovery_ignores_symlinks_and_keeps_duplicate_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in (root / "a", root / "b"):
                directory.mkdir()
                (directory / "same").write_bytes(elf())
            (root / "link").symlink_to(root / "a", target_is_directory=True)
            found = discover(root, root / "result.sqlite")
            self.assertEqual([row[0] for row in found], ["a/same", "b/same"])


if __name__ == "__main__":
    unittest.main()
