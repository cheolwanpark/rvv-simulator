import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "scripts" / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


jobs = load("build_jobs", "build-jobs")
libs = load("collect_libs", "collect-libs.py")


class JobPolicyTests(unittest.TestCase):
    def test_scales_with_cpu_and_memory_and_stage(self):
        for cpus, memory in ((1, 4), (4, 8), (8, 24), (64, 128)):
            for stage, (cap, _) in jobs.STAGES.items():
                value = jobs.select(stage, cpus, memory * jobs.GIB)
                self.assertGreaterEqual(value, 1)
                self.assertLessEqual(value, min(cpus, cap))
        self.assertEqual(jobs.select("xiangshan", 8, 24 * jobs.GIB), 6)
        self.assertEqual(jobs.select("xiangshan", 8, 8 * jobs.GIB), 2)
        self.assertEqual(jobs.select("saturn", 8, 24 * jobs.GIB), 8)
        self.assertEqual(jobs.select("saturn-trace", 8, 24 * jobs.GIB), 4)
        self.assertEqual(jobs.select("scala", 64, 128 * jobs.GIB), 4)

    def test_override_only_reduces_concurrency(self):
        self.assertEqual(jobs.select("verilator", 8, 24 * jobs.GIB, "2"), 2)
        self.assertEqual(jobs.select("scala", 8, 24 * jobs.GIB, "99"), 4)
        for value in ("0", "-1", "", "garbage", "1.5"):
            with self.assertRaises(ValueError):
                jobs.select("verilator", 8, 24 * jobs.GIB, value)

    def test_tiny_memory_never_selects_unlimited_jobs(self):
        self.assertEqual(jobs.select("xiangshan", 64, jobs.GIB), 1)

    def test_heap_uses_memory_limit_and_family_cap(self):
        self.assertEqual(jobs.heap("xiangshan", 24 * jobs.GIB), "14745M")
        self.assertEqual(jobs.heap("saturn", 24 * jobs.GIB), "8192M")
        self.assertEqual(jobs.heap("xiangshan", 128 * jobs.GIB), "40960M")

    def detect(self, files, affinity=range(12)):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            with patch.object(jobs.os, "cpu_count", return_value=64), patch.object(
                jobs.os, "sched_getaffinity", return_value=set(affinity), create=True
            ):
                return jobs.resources(root / "proc", root / "cgroup")

    def test_cgroup_v2_quota_memory_and_parent_limits(self):
        cpus, memory = self.detect({
            "proc/meminfo": "MemTotal:       134217728 kB\n",
            "proc/self/cgroup": "0::/parent/build\n",
            "cgroup/parent/cpu.max": "250000 100000",
            "cgroup/parent/build/cpu.max": "max 100000",
            "cgroup/parent/memory.max": str(8 * jobs.GIB),
            "cgroup/parent/build/memory.high": str(6 * jobs.GIB),
        })
        self.assertEqual((cpus, memory), (2, 6 * jobs.GIB))

    def test_cgroup_v1_and_affinity(self):
        cpus, memory = self.detect({
            "proc/meminfo": "MemTotal:       33554432 kB\n",
            "proc/self/cgroup": "2:cpu,cpuacct:/job\n3:memory:/job\n",
            "cgroup/cpu,cpuacct/job/cpu.cfs_quota_us": "400000",
            "cgroup/cpu,cpuacct/job/cpu.cfs_period_us": "100000",
            "cgroup/memory/job/memory.limit_in_bytes": str(4 * jobs.GIB),
        }, affinity=range(2))
        self.assertEqual((cpus, memory), (2, 4 * jobs.GIB))

    def test_unlimited_cgroup_uses_detected_machine_resources(self):
        self.assertEqual(self.detect({
            "proc/meminfo": "MemTotal:       25165824 kB\n",
            "proc/self/cgroup": "0::/\n",
            "cgroup/cpu.max": "max 100000",
            "cgroup/memory.max": "max",
        }, affinity=range(8)), (8, 24 * jobs.GIB))


class LibraryPackagingTests(unittest.TestCase):
    def test_parses_transitive_libraries_and_loader_in_clean_environment(self):
        with tempfile.NamedTemporaryFile() as binary:
            binary.write(b"\x7fELF")
            binary.flush()
            result = subprocess.CompletedProcess([], 0,
                "linux-vdso.so.1 (0xffff)\n"
                "libfoo.so => /opt/env/lib/libfoo.so (0xaaaa)\n"
                "/lib64/ld-linux-x86-64.so.2 (0xbbbb)\n", "")
            with patch.object(libs.subprocess, "run", return_value=result) as run:
                self.assertEqual(libs.dependencies(binary.name), [
                    Path("/opt/env/lib/libfoo.so"), Path("/lib64/ld-linux-x86-64.so.2")])
                self.assertNotIn("LD_LIBRARY_PATH", run.call_args.kwargs["env"])

    def test_missing_dependency_fails_packaging(self):
        with tempfile.NamedTemporaryFile() as binary:
            binary.write(b"\x7fELF")
            binary.flush()
            result = subprocess.CompletedProcess([], 0, "libfoo.so => not found", "")
            with patch.object(libs.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "unresolved"):
                    libs.dependencies(binary.name)

    def test_copy_dereferences_soname_and_normalizes_parent_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            library = root / "usr/lib"
            library.mkdir(parents=True)
            (library / "libtest.so.1.2").write_bytes(b"library contents")
            (library / "libtest.so.1").symlink_to("libtest.so.1.2")
            (root / "lib").symlink_to("usr/lib")
            source = root / "lib/libtest.so.1"
            destination = root / "runtime"
            libs.copy_library(source, destination)
            target = destination / str(library / "libtest.so.1").lstrip("/")
            self.assertEqual(target.read_bytes(), b"library contents")
            self.assertFalse(target.is_symlink())


class SourceFetchTests(unittest.TestCase):
    def test_fetches_exact_commit_instead_of_tip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "upstream"
            source.mkdir()
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

            def git(*args):
                return subprocess.check_output(["git", "-C", str(source), *args], env=env, text=True).strip()

            git("init", "-q")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.invalid")
            payload = source / "payload"
            payload.write_text("pinned\n")
            git("add", ".")
            git("commit", "-qm", "pinned")
            pinned = git("rev-parse", "HEAD")
            payload.write_text("new tip\n")
            git("commit", "-qam", "new tip")
            destination = root / "checkout with spaces"
            result = subprocess.run(["bash", str(ROOT / "scripts/fetch-source"), str(source), pinned,
                                     str(destination)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / "payload").read_text(), "pinned\n")

    def test_rejects_moving_reference_without_creating_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "checkout"
            result = subprocess.run(["bash", str(ROOT / "scripts/fetch-source"),
                                     "https://example.invalid/repo", "main", str(destination)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rvv tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "arguments.json"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        simulator = self.bin / "simulator"
        simulator.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                             "open(os.environ['ARG_LOG'],'w').write(json.dumps(sys.argv[1:]))\n")
        simulator.chmod(0o755)
        stdbuf = self.bin / "stdbuf"
        stdbuf.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
        stdbuf.chmod(0o755)
        ready = self.root / "ready-to-run"
        ready.mkdir()
        for name in ("microbench.bin", "coremark-2-iteration.bin", "riscv64-nemu-interpreter-so"):
            (ready / name).touch()
        self.elf = self.root / "custom workload.elf"
        # A real ELF64/RISC-V header allows testing file(1), not a mocked validator.
        ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
        self.elf.write_bytes(struct.pack("<16sHHIQQQIHHHHHH", ident, 2, 243, 1,
                                       0x80000000, 0, 0, 0, 64, 0, 0, 0, 0, 0))
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        ARG_LOG=str(self.log), XIANSHAN_HOME=str(self.root),
                        XS_EMU=str(simulator), SATURN_HOME=str(self.root),
                        SATURN_SIM=str(simulator), SATURN_TRACE_SIM=str(simulator))

    def run_wrapper(self, family, *arguments):
        filename = "xs-run" if family == "xiangshan" else "saturn-run"
        return subprocess.run(["bash", str(ROOT / family / filename), *arguments],
                              env=self.env, text=True, capture_output=True)

    def test_xiangshan_default_reference_and_passthrough_preserve_spaces(self):
        result = self.run_wrapper("xiangshan", "--workload", str(self.elf), "--", "argument with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        self.assertEqual(args, ["-i", str(self.elf), "--diff",
                              str(self.root / "ready-to-run/riscv64-nemu-interpreter-so"),
                              "argument with spaces"])

    def test_xiangshan_no_diff_dry_run_does_not_execute(self):
        result = self.run_wrapper("xiangshan", "--no-diff", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-diff", shlex.split(result.stdout))
        self.assertFalse(self.log.exists())

    def test_saturn_runs_without_conda_and_forwards_trace_arguments(self):
        wave = self.root / "wave file.fst"
        result = self.run_wrapper("saturn", "--workload", str(self.elf), "--wave-path", str(wave),
                                  "--seed", "3", "--", "+custom=value with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        self.assertIn(f"+vcdfile={wave}", args)
        self.assertIn("+verilator+seed+3", args)
        self.assertEqual(args[-2:], [str(self.elf), "+custom=value with spaces"])

    def test_saturn_rejects_invalid_input_before_execution(self):
        self.elf.write_text("not an ELF")
        result = self.run_wrapper("saturn", "--workload", str(self.elf))
        self.assertEqual(result.returncode, 2)
        self.assertIn("ELF64", result.stderr)
        self.assertFalse(self.log.exists())

    def test_saturn_dry_run_without_extra_arguments(self):
        result = self.run_wrapper("saturn", "--workload", str(self.elf), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.elf), shlex.split(result.stdout))
        self.assertFalse(self.log.exists())

    def test_missing_arguments_and_wave_directory_fail(self):
        for family in ("xiangshan", "saturn"):
            self.assertEqual(self.run_wrapper(family, "--workload").returncode, 2)
        result = self.run_wrapper("saturn", "--workload", str(self.elf), "--wave-path",
                                  str(self.root / "missing/wave.fst"))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())


class MakefileTests(unittest.TestCase):
    def test_builds_route_to_pinned_sources_with_automatic_jobs(self):
        for name, revision, dockerfile in (
            ("xiangshan-v2-rtl", "e7bab53e66dfb3c4a1d11cf9519b0396f8576cae", "xiangshan"),
            ("xiangshan-v3-rtl", "a7b9dea601f2f08dcca7d97ac544221cf02b1fe9", "xiangshan"),
            ("saturn-rtl", "0acc1e1de2d3284bcd4d876956932a013ffe1949", "saturn"),
        ):
            result = subprocess.run(["make", "-n", "build", name], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(revision, result.stdout)
            self.assertIn(str(ROOT / dockerfile / "Dockerfile"), result.stdout)
            self.assertIn('BUILD_JOBS="auto"', result.stdout)
            self.assertNotIn("container-images/base", result.stdout)

    def test_alias_launch_uses_host_ownership_and_handles_spaces(self):
        with tempfile.TemporaryDirectory(prefix="rvv make ") as temporary:
            root = Path(temporary)
            docker = root / "docker"
            log = root / "log"
            docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                              "if sys.argv[1]=='run': open(os.environ['DOCKER_LOG'],'w').write(json.dumps(sys.argv[1:]))\n")
            docker.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", DOCKER_LOG=str(log))
            host = root / "host dir"
            result = subprocess.run(["make", "launch", "xiangshan-rtl", f"DIR={host}",
                                     "XIANSHAN_V2_IMAGE=test/v2:custom"], cwd=ROOT,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(log.read_text())
            self.assertEqual(args[-1], "test/v2:custom")
            self.assertEqual(args[args.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
            self.assertEqual(args[args.index("-v") + 1], f"{host.resolve()}:/host")
            self.assertTrue((host / ".home").is_dir())


if __name__ == "__main__":
    unittest.main()
