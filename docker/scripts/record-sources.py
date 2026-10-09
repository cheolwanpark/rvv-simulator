#!/usr/bin/env python3
"""Preserve source revisions and notices alongside the packaged binaries."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

destination = Path(sys.argv[1]) / "usr/share/rvv-simulator"
destination.mkdir(parents=True, exist_ok=True)
for argument in sys.argv[2:]:
    source = Path(argument)
    with (destination / f"{source.name}.txt").open("w") as manifest:
        manifest.write(f"\n{source}\n")
        for command in (["rev-parse", "HEAD"], ["submodule", "status", "--recursive"]):
            manifest.write(subprocess.check_output(["git", "-C", str(source), *command], text=True))
        for directory, children, files in os.walk(source):
            children[:] = [name for name in children if name not in {
                ".git", ".conda-env", ".conda-lock-env", "build", "out", "target",
                ".classpath_cache", "generated-src", "verilator-compile",
            }]
            for name in files:
                if name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                    path = Path(directory) / name
                    target = destination / "licenses" / source.name / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
