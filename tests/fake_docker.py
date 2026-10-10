#!/usr/bin/env python3
"""Process-level Docker test double; simulator children survive a killed controller.

Not used by the runtime. Every mutation stays in RVV_FAKE_DOCKER_ROOT.
"""

import base64
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(os.environ["RVV_FAKE_DOCKER_ROOT"])
ROOT.mkdir(parents=True, exist_ok=True)


def path(name):
    return ROOT / f"{name}.json"


def read(name):
    return json.loads(path(name).read_text())


def save(item):
    target = path(item["Name"].lstrip("/"))
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(item))
    temporary.replace(target)


def simulation(name):
    item = read(name)
    output = Path(item["work"])
    scenario = (Path(item["input"]) / "program.elf").read_bytes().split(b"FAKE:")[-1].decode()

    def finish(code):
        state = read(name)
        seconds, nanoseconds = divmod(time.time_ns(), 10**9)
        finished = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{nanoseconds:09d}Z"
        state["State"] = dict(Status="exited", Running=False, ExitCode=code, OOMKilled=False,
                              Error="", FinishedAt=finished)
        save(state)

    def stop(sig, frame):
        finish(128 + sig)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        with (output / "stdout.log").open("w", buffering=1) as stdout, (output / "stderr.log").open("w", buffering=1) as stderr:
            stdout.write("fixture start 한글\n")
            if "loop-json" in scenario:
                record = json.dumps(dict(schema_version=2, mode="kernel", seed=0, repetitions=1,
                                         warmups=0, metric="cycles", value=-1 if "invalid" in scenario else 123,
                                         numerical_validation="not_run")) + "\n"
                # Guest UART output arrives across polling boundaries, not whole lines.
                stdout.write(record[:37])
                stdout.flush()
                time.sleep(0.3)
                stdout.write(record[37:])
            elif "multiple" in scenario:
                stdout.write("RVV_KERNEL name=first cycles=11\nRVV_KERNEL name=second cycles=22\n")
            elif "missing" not in scenario:
                stdout.write("RVV_KERNEL name=kernel cycles=123\n")
            if "invalid-marker" in scenario:
                stdout.write("RVV_KERNEL name=bad cycles=-1\n")
            if "long" in scenario:
                time.sleep(30)
            else:
                time.sleep(0.25)
            if "cycle-limit" in scenario:
                stderr.write("EXCEEDING CYCLE/INSTR LIMIT\n")
            elif "fail" in scenario:
                stderr.write("HIT BAD TRAP at pc = 0x80000000\n*** FAILED *** code=1\n")
            else:
                if "saturn" in item["Image"]:
                    stdout.write("SATURN simulation cycleCnt = 456\n")
                else:
                    stderr.write("\x1b[32mHIT GOOD TRAP at pc = 0x80000000\x1b[0m\n")
                    if "no-total" not in scenario:
                        stdout.write("Core-0 instrCnt = 321, cycleCnt = 456, IPC = 0.7039\n")
                    stdout.write("Seed=1 Guest cycle spent: 460\n")
                if "--wave" in item["command"] and "no-wave" not in scenario:
                    (output / "wave.fst").write_bytes(b"fixture FST\x00\xff")
        finish(1 if "fail" in scenario else 0)
    except BaseException:
        if read(name)["State"]["Running"]:
            finish(1)
        raise


def main(args):
    command = args[0]
    if command == "_simulate":
        simulation(args[1])
        return
    # One JSON line per CLI invocation; O_APPEND keeps independent child writes separate.
    with (ROOT / "calls.jsonl").open("a") as handle:
        handle.write(json.dumps(args) + "\n")
    if command == "info":
        print(json.dumps(dict(OSType="linux", NCPU=4)))
    elif command == "image":
        reference = args[-1]
        backend = "saturn" if "saturn" in reference else "xiangshan-v3" if "v3" in reference else "xiangshan-v2"
        print(json.dumps([dict(Id=f"sha256:{backend}", Architecture="amd64", Os="linux", Config={})]))
    elif command == "run":
        backend = next(x for x in args if x.startswith("sha256:"))
        key = "simulator_threads" if "saturn" in backend else "emu_threads"
        threads = os.environ.get("RVV_FAKE_THREADS", "1")
        for name, value in (("config.txt", f"config=fixture\n{key}={threads}\n"), ("sources.txt", "fixture revision\n")):
            print(f"manifest\t{name}\t{base64.b64encode(value.encode()).decode()}")
        print("cpus\t0-3\nquota\tmax 100000")
    elif command == "create":
        def value(flag):
            return args[args.index(flag) + 1]
        mounts = {}
        for i, arg in enumerate(args):
            if arg == "--mount":
                fields = dict(part.split("=", 1) for part in next(csv.reader([args[i + 1]])) if "=" in part)
                mounts[fields["dst"]] = fields["src"]
        image = next(x for x in args if x.startswith("sha256:"))
        wrapper = args.index("rvv-batch")
        labels = [args[i + 1] for i, arg in enumerate(args) if arg == "--label"]
        item = dict(Name="/" + value("--name"), Image=image, labels=labels, cpu=value("--cpuset-cpus"),
                    command=args[wrapper + 1:], input=mounts["/input"], work=mounts["/work"],
                    State=dict(Status="created", Running=False, ExitCode=0, OOMKilled=False, Error=""))
        save(item)
        print(item["Name"])
    elif command == "start":
        item = read(args[1])
        item["State"].update(Status="running", Running=True)
        save(item)
        process = subprocess.Popen([sys.executable, __file__, "_simulate", args[1]], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        item["pid"] = process.pid
        save(item)
    elif command == "inspect":
        delay = float(os.environ.get("RVV_FAKE_INSPECT_DELAY", "0"))
        if delay:
            time.sleep(delay)
        print(json.dumps([read(name) for name in args[1:]]))
    elif command == "ps":
        label = args[args.index("--filter") + 1].removeprefix("label=")
        for filename in ROOT.glob("rvv-*.json"):
            item = json.loads(filename.read_text())
            if label in item["labels"]:
                print(item["Name"].lstrip("/"))
    elif command == "stop":
        name = args[-1]
        item = read(name)
        if item["State"]["Running"]:
            os.kill(item["pid"], signal.SIGTERM)
            for _ in range(100):
                if not read(name)["State"]["Running"]:
                    break
                time.sleep(0.01)
    elif command == "rm":
        path(args[-1]).unlink()
    else:
        raise ValueError(args)


if __name__ == "__main__":
    main(sys.argv[1:])
