#!/usr/bin/env python3
"""Stage ELF dependency closures at their original paths, without build tools.

Call on every runtime executable AND dlopen-loaded reference model. ldd already
resolves transitive dependencies using the object's RPATH/RUNPATH. Run with a
clean environment so the closure doesn't depend on build-only LD_LIBRARY_PATH.
"""

from pathlib import Path
import re
import shutil
import subprocess
import sys


def dependencies(binary):
    with open(binary, "rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise RuntimeError(f"not an ELF file: {binary}")
    result = subprocess.run(
        ["/usr/bin/ldd", str(binary)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    output = result.stdout + result.stderr
    if "not found" in output:
        raise RuntimeError(f"unresolved libraries for {binary}:\n{output}")
    if result.returncode and "statically linked" not in output:
        raise RuntimeError(f"ldd failed for {binary}:\n{output}")
    return [Path(match.group(1)) for line in output.splitlines()
            if (match := re.match(r"\s*(?:\S+\s+=>\s+)?(/\S+)\s+\(", line))]


def copy_library(source, destination):
    # Dereference the final component: SONAME paths remain usable even when the
    # builder's symlink target lies outside the runtime subset. Canonicalize only
    # parents to respect Ubuntu's merged-/usr layout without replacing /lib and
    # /lib64 symlinks with directories during COPY into the final image.
    installed_path = source.parent.resolve() / source.name
    target = destination / str(installed_path).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise RuntimeError(f"conflicting runtime library: {source}")
        return
    shutil.copy2(source, target, follow_symlinks=True)


def collect(destination, binaries):
    destination = Path(destination).absolute()
    for binary in binaries:
        for library in dependencies(binary):
            copy_library(library, destination)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: collect-libs.py STAGING_ROOT ELF...")
    try:
        collect(sys.argv[1], sys.argv[2:])
    except (OSError, RuntimeError) as error:
        sys.exit(str(error))
