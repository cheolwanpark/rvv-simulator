"""The sole single-ELF execution path, shared by standalone and batch callers."""

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import time

from . import __version__
from .artifacts import FORMAT_VERSION, RunResult, inventory, log_lines, now, publish_result
from .backends import PARSER_VERSION, command, parse_lines, validate_elf


def parse_docker_timestamp(value):
    value = re.sub(r"\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class RunSpec:
    backend: str
    image_id: str
    image_ref: str
    seed: int
    max_cycles: int
    timeout: float
    wave: bool
    cpu: int
    memory: str | None
    run_id: str
    attempt_id: int
    directory: Path
    started_at: str

    @property
    def container(self):
        return f"rvv-{self.run_id}-{self.attempt_id}"

    @property
    def command(self):
        return command(self.backend, self.max_cycles, self.seed, self.wave)

    def settings(self):
        return {key: getattr(self, key) for key in
                ("backend", "image_id", "image_ref", "seed", "max_cycles", "timeout", "wave", "cpu", "memory")}


def classify(backend, parsed, state, wave_ok=True, forced=None, reason=None):
    if forced:
        status = forced
    elif state.get("OOMKilled"):
        status, reason = "failed", "container exceeded its memory limit"
    elif parsed.limit:
        status, reason = "cycle_limit", "simulator cycle/instruction limit reached"
    elif state.get("ExitCode") == 0 and parsed.good and not parsed.bad:
        status = "succeeded"
    else:
        status, reason = "failed", state.get("Error") or "simulator failed or did not report normal completion"
    if status == "succeeded" and not wave_ok:
        status, reason = "failed", "requested waveform is missing or empty"
    core = "simulation" if backend == "saturn" else "core-0"
    totals = [m.value for m in parsed.measurements
              if m.metric == "cycle" and m.scope == "simulation" and m.name == core]
    kernels = [m.value for m in parsed.measurements if m.metric == "kernel_cycle"]
    total = totals[-1] if totals else None
    measurement_status = ("invalid" if parsed.errors else "missing_total" if total is None
                          else "partial" if status != "succeeded" else "missing_kernel" if not kernels else "complete")
    kernel_status = ("invalid" if any(e.category == "kernel_cycle" for e in parsed.errors)
                     else "available" if kernels else "missing")
    return dict(status=status, reason=reason, total_cycle=total,
                kernel_cycle=kernels[0] if len(kernels) == 1 else None,
                kernel_sample_count=len(kernels), measurement_status=measurement_status,
                kernel_status=kernel_status)


def cleanup_request(docker, run_id):
    names = docker.containers(run_id)
    states = docker.states(names)
    for name in names:
        if states[name].get("Running"):
            docker.stop(name)
        docker.remove(name)


def run_single(spec, docker, cancel_event, *, clock=time.monotonic, poll_seconds=0.25):
    """Execute a staged input, publish result.json last, and return its result."""
    directory = spec.directory
    data = (directory / "input/program.elf").read_bytes()
    output = directory / "output"
    output.mkdir()
    for stream in ("stdout", "stderr"):
        (output / f"{stream}.log").touch()
    args = spec.command
    state, forced, reason = {}, None, None
    try:
        validate_elf(data)
    except ValueError as error:
        forced, reason = "invalid_input", str(error)
    created = False
    try:
        if not forced:
            if cancel_event.is_set():
                forced, reason = "interrupted", "user interrupted"
            else:
                started = clock()
                # Set before create: even a failed create/start may leave a container.
                created = True
                docker.create(spec.container, spec.run_id, spec.attempt_id, spec.image_id, spec.cpu,
                              spec.memory, os.getuid(), os.getgid(), directory / "input", output, args)
                docker.start(spec.container)
                while True:
                    state = docker.states([spec.container])[spec.container]
                    if not state.get("Running"):
                        break
                    if cancel_event.is_set() or clock() - started >= spec.timeout:
                        forced = "interrupted" if cancel_event.is_set() else "timeout"
                        reason = "user interrupted" if forced == "interrupted" else "wall timeout exceeded"
                        docker.stop(spec.container)
                        state = docker.states([spec.container])[spec.container]
                        break
                    cancel_event.wait(poll_seconds)
        parsed = parse_lines(spec.backend, log_lines(directory))
        wave = output / "wave.fst"
        summary = classify(spec.backend, parsed, state,
                           not spec.wave or (wave.is_file() and wave.stat().st_size > 0), forced, reason)
        ended = now()
        if state.get("FinishedAt"):
            candidate = parse_docker_timestamp(state["FinishedAt"])
            if candidate >= datetime.fromisoformat(spec.started_at):
                ended = candidate.isoformat(timespec="milliseconds")
        wall = max(0, (datetime.fromisoformat(ended) - datetime.fromisoformat(spec.started_at)).total_seconds())
        result = RunResult(format_version=FORMAT_VERSION, parser_version=PARSER_VERSION,
                           tool_version=__version__, run_id=spec.run_id, attempt_id=spec.attempt_id,
                           backend=spec.backend, input_sha256=hashlib.sha256(data).hexdigest(),
                           settings=spec.settings(), command=args, started_at=spec.started_at,
                           finished_at=ended, wall_seconds=wall, exit_code=state.get("ExitCode"), **summary,
                           measurements=[m._asdict() for m in parsed.measurements],
                           errors=[asdict(e) for e in parsed.errors], files=inventory(directory))
        publish_result(directory, result)
    except BaseException:
        if created:
            try:
                docker.stop(spec.container)
                docker.remove(spec.container)
            except Exception:
                pass  # Preserve the original error and work for request cleanup on resume.
        raise
    if created:
        docker.remove(spec.container)
    return result


def stage_input(directory, data, manifests):
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "input").mkdir()
    elf = directory / "input/program.elf"
    elf.write_bytes(data)
    elf.chmod(0o444)
    (directory / "manifests").mkdir()
    for name, contents in manifests.items():
        if Path(name).name != name:
            raise ValueError(f"invalid manifest name: {name}")
        (directory / "manifests" / name).write_bytes(contents)
