"""Small argv-only Docker CLI adapter; simulators never write to the result DB."""

import base64
import csv
import io
import json
import subprocess


class DockerError(RuntimeError):
    pass


def cpu_list(text):
    cpus = set()
    try:
        for part in text.split(","):
            bounds = part.split("-")
            if len(bounds) > 2:
                raise ValueError
            start, end = int(bounds[0]), int(bounds[-1])
            if start < 0 or end < start or end > 1048576:
                raise ValueError
            cpus.update(range(start, end + 1))
    except ValueError as error:
        raise ValueError(f"invalid CPU set: {text!r}") from error
    return sorted(cpus)


def bind_mount(source, destination, readonly=False):
    # Docker's --mount syntax is CSV; handle commas as well as spaces in paths.
    buffer = io.StringIO()
    fields = ["type=bind", f"src={source}", f"dst={destination}"]
    if readonly:
        fields.append("readonly")
    csv.writer(buffer, lineterminator="").writerow(fields)
    return buffer.getvalue()


PROBE = r'''
set -eu
for f in /usr/share/rvv-simulator/*.txt; do
    [ -f "$f" ] || continue
    printf 'manifest\t%s\t' "${f##*/}"
    base64 -w0 "$f"
    printf '\n'
done
printf 'cpus\t'
sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status
if [ -f /sys/fs/cgroup/cpu.max ]; then
    printf 'quota\t'; cat /sys/fs/cgroup/cpu.max
elif [ -f /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
    printf 'quota\t%s %s\n' "$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)" "$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)"
elif [ -f /sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us ]; then
    printf 'quota\t%s %s\n' "$(cat /sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us)" "$(cat /sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us)"
fi
'''


class Docker:
    def __init__(self, binary="docker"):
        self.binary = binary

    def call(self, *args, timeout=60, check=True):
        try:
            result = subprocess.run([self.binary, *map(str, args)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DockerError(f"Docker command failed ({args[0]}): {error}") from error
        if check and result.returncode:
            raise DockerError(result.stderr.decode("utf-8", "replace").strip()
                              or f"Docker {args[0]} exited with {result.returncode}")
        return result

    def preflight(self, image, backend, jobs, requested_cpus=None):
        info = json.loads(self.call("info", "--format", "{{json .}}").stdout)
        if info.get("OSType") != "linux":
            raise ValueError("a Linux Docker daemon is required (Docker Desktop is supported)")
        try:
            metadata = json.loads(self.call("image", "inspect", image).stdout)[0]
        except DockerError as error:
            raise DockerError(f"{error}\nPrepare the image with: make build BACKEND={backend}") from error
        if metadata.get("Architecture") != "amd64" or metadata.get("Os") != "linux":
            raise ValueError("the simulator image must target linux/amd64")
        image_id = metadata["Id"]
        result = self.call("run", "--rm", "--platform", "linux/amd64", "--network", "none",
                           "--entrypoint", "/bin/sh", image_id, "-c", PROBE)
        manifests, allowed, quota = {}, [], None
        for line in result.stdout.decode().splitlines():
            parts = line.split("\t")
            if parts[0] == "manifest" and len(parts) == 3:
                manifests[parts[1]] = base64.b64decode(parts[2], validate=True)
            elif parts[0] == "cpus":
                allowed = cpu_list(parts[1])
            elif parts[0] == "quota":
                maximum, period = parts[1].split()
                if maximum not in ("max", "-1"):
                    quota = max(1, int(maximum) // int(period))
        config = dict(line.split("=", 1) for line in manifests.get("config.txt", b"").decode().splitlines()
                      if "=" in line)
        key = "simulator_threads" if backend == "saturn" else "emu_threads"
        if config.get(key) != "1":
            variable = "SATURN_SIM_THREADS" if backend == "saturn" else "XIANSHAN_EMU_THREADS"
            raise ValueError(f"image must declare {key}=1; rebuild with "
                             f"make -C docker build {backend}-rtl {variable}=1")
        if not allowed:
            raise ValueError("could not determine Docker's available CPU set")
        selected = cpu_list(requested_cpus) if requested_cpus else allowed
        if not set(selected) <= set(allowed):
            raise ValueError(f"requested CPUs are outside Docker's allowed set: {allowed}")
        capacity = min(len(selected), quota if quota is not None else len(selected))
        if jobs > capacity:
            raise ValueError(f"--jobs={jobs} exceeds Docker CPU capacity {capacity}")
        manifests["image-inspect.json"] = json.dumps(metadata, sort_keys=True).encode()
        return image_id, selected[:jobs], manifests

    def create(self, name, run_id, attempt_id, image_id, cpu, memory, uid, gid, inputs, work, command):
        args = ["create", "--name", name, "--platform", "linux/amd64", "--network", "none",
                "--label", f"rvv.batch.run={run_id}", "--label", f"rvv.batch.attempt={attempt_id}",
                "--user", f"{uid}:{gid}", "--env", "HOME=/tmp", "--env", "LC_ALL=C.UTF-8",
                "--cpus", "1", "--cpuset-cpus", str(cpu), "--workdir", "/work",
                "--mount", bind_mount(inputs, "/input", True),
                "--mount", bind_mount(work, "/work"), "--ulimit", "core=0",
                "--entrypoint", "/bin/sh"]
        if memory:
            args += ["--memory", memory]
        # Fixed shell program; all simulator arguments remain distinct argv entries.
        args += [image_id, "-c", 'exec stdbuf -oL -e0 "$@" >stdout.log 2>stderr.log', "rvv-batch", *command]
        self.call(*args)

    def start(self, name):
        self.call("start", name)

    def states(self, names):
        if not names:
            return {}
        data = json.loads(self.call("inspect", *names).stdout)
        return {item["Name"].lstrip("/"): item["State"] for item in data}

    def containers(self, run_id):
        result = self.call("ps", "-a", "--filter", f"label=rvv.batch.run={run_id}",
                           "--format", "{{.Names}}")
        return result.stdout.decode().splitlines()

    def stop(self, name):
        self.call("stop", "--time", "5", name, timeout=15)

    def remove(self, name):
        # Call only after the final logs/results have been durably committed.
        self.call("rm", name)
