"""Translate exported Make variables to argv without interpolating paths into shell code."""

import os
from pathlib import Path
import subprocess
import sys

from . import cli
from .backends import BACKENDS


def arguments(target, env):
    def required(name):
        value = env.get(name, "")
        if not value:
            raise ValueError(f"{name} is required for make {target}")
        return value

    if target == "run":
        args = ["run", required("ELF_DIR"), "--backend", required("BACKEND"), "--output", required("DB")]
        for name, flag in (("JOBS", "--jobs"), ("SEED", "--seed"), ("MAX_CYCLES", "--max-cycles"),
                           ("TIMEOUT", "--timeout"), ("IMAGE", "--image"), ("CPU_SET", "--cpu-set"),
                           ("MEMORY", "--memory")):
            if env.get(name):
                args += [flag, env[name]]
        wave = env.get("WAVE") or "0"
        if wave not in ("0", "1"):
            raise ValueError("WAVE must be 0 or 1")
        if wave == "1":
            args.append("--wave")
    elif target == "resume":
        args = ["resume", required("DB")]
        for name, flag in (("JOBS", "--jobs"), ("TIMEOUT", "--timeout"), ("MAX_CYCLES", "--max-cycles")):
            if env.get(name):
                args += [flag, env[name]]
        for name in ("ELF_DIR", "BACKEND", "SEED", "IMAGE", "CPU_SET", "MEMORY", "WAVE"):
            if env.get(name):
                raise ValueError(f"{name} cannot change during resume; settings come from SQLite")
    else:
        raise ValueError(f"unsupported target: {target}")
    if env.get("DOCKER"):
        args += ["--docker", env["DOCKER"]]
    return args


def main():
    target = sys.argv[1]
    try:
        if target in ("build", "smoke"):
            backend = os.environ.get("BACKEND")
            if backend not in BACKENDS:
                raise ValueError(f"BACKEND must be one of {', '.join(BACKENDS)}")
            directory = Path(__file__).resolve().parents[1] / "docker"
            args = ["make", "--no-print-directory", "-C", str(directory), target, f"NAME={backend}-rtl"]
            if os.environ.get("IMAGE"):
                variable = {"xiangshan-v2": "XIANSHAN_V2_IMAGE", "xiangshan-v3": "XIANSHAN_V3_IMAGE", "saturn": "SATURN_IMAGE"}[backend]
                args.append(f"{variable}={os.environ['IMAGE']}")
            return subprocess.call(args)
        return cli.main(arguments(target, os.environ))
    except (ValueError, OSError) as error:
        print(f"rvv-batch: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
