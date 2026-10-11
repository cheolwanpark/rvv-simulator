import csv
from datetime import datetime, timedelta, timezone
import json
import struct
import unittest

from rvv_batch.backends import command, parse_lines, validate_elf
from rvv_batch.docker import bind_mount, cpu_list
from rvv_batch.make import arguments
from rvv_batch.runner import parse_docker_timestamp

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
                self.assertEqual(parsed.errors[0].category, "kernel_cycle")

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
        self.assertEqual(arguments("resume", {"DB": "x", "JOBS": "2", "TIMEOUT": "7200"}),
                         ["resume", "x", "--jobs", "2", "--timeout", "7200"])


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



if __name__ == "__main__":
    unittest.main()
