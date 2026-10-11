"""Completed execution directories and portable ZIP artifacts; no database dependency."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import zipfile

from .backends import PARSER_VERSION

FORMAT_VERSION = 1
CHUNK_BYTES = 64 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"invalid artifact path: {relative}")
    path = Path(root)
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"refusing artifact symlink: {path}")
    if not path.is_file():
        raise ValueError(f"missing artifact: {path}")
    return path


def log_chunks(path):
    """Yield byte-exact chunks, keeping valid UTF-8 characters together."""
    with Path(path).open("rb") as handle:
        while data := handle.read(CHUNK_BYTES):
            try:
                text, raw = data.decode("utf-8"), None
            except UnicodeDecodeError as error:
                if error.reason == "unexpected end of data" and error.start:
                    handle.seek(error.start - len(data), os.SEEK_CUR)
                    data = data[:error.start]
                    text, raw = data.decode("utf-8"), None
                else:
                    text, raw = data.decode("utf-8", "replace"), data
            yield data, text, raw


def log_lines(directory):
    for stream in ("stdout", "stderr"):
        path = regular_file(directory, f"output/{stream}.log")
        # A bounded readline prevents malformed unbroken output consuming all RAM.
        with path.open("r", encoding="utf-8", errors="replace", newline="\n") as handle:
            for number, line in enumerate(iter(lambda: handle.readline(1024 * 1024), ""), 1):
                yield stream, number, line.rstrip("\n")


@dataclass
class RunResult:
    format_version: int
    parser_version: int
    tool_version: str
    run_id: str
    attempt_id: int
    backend: str
    input_sha256: str
    settings: dict
    command: list
    started_at: str
    finished_at: str
    wall_seconds: float
    exit_code: int | None
    status: str
    reason: str | None
    total_cycle: int | None
    kernel_cycle: int | None
    kernel_sample_count: int
    measurement_status: str
    kernel_status: str
    measurements: list
    errors: list
    files: list

    @property
    def failed(self):
        return self.status != "succeeded" or self.measurement_status in ("invalid", "missing_total")

    def summary(self):
        kernel = (f"multiple({self.kernel_sample_count})" if self.kernel_sample_count > 1
                  else str(self.kernel_cycle) if self.kernel_cycle is not None else "null")
        total = self.total_cycle if self.total_cycle is not None else "null"
        return f"total_cycle={total} kernel_cycle={kernel} status={self.status}"


def inventory(directory):
    rows = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_symlink():
            raise ValueError(f"refusing artifact symlink: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(directory).as_posix()
        regular_file(directory, relative)
        rows.append(dict(path=relative, size_bytes=path.stat().st_size, sha256=file_hash(path)))
    return rows


def publish_result(directory, result):
    temporary = Path(directory) / "result.json.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(asdict(result), handle, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(Path(directory) / "result.json")


def read_result(directory):
    result = RunResult(**json.loads(regular_file(directory, "result.json").read_text()))
    if result.format_version != FORMAT_VERSION or result.parser_version != PARSER_VERSION:
        raise ValueError("unsupported completed-result version")
    if result.status not in ("succeeded", "failed", "timeout", "cycle_limit", "invalid_input", "interrupted"):
        raise ValueError("completed result has nonterminal status")
    seen = set()
    for item in result.files:
        path = regular_file(directory, item["path"])
        if item["path"] in seen or path.stat().st_size != item["size_bytes"] or file_hash(path) != item["sha256"]:
            raise ValueError(f"artifact inventory mismatch: {path}")
        seen.add(item["path"])
    if not {"input/program.elf", "output/stdout.log", "output/stderr.log"} <= seen:
        raise ValueError("completed result is missing required files")
    if file_hash(Path(directory) / "input/program.elf") != result.input_sha256:
        raise ValueError("completed result input hash mismatch")
    return result


def write_zip(directory, output, result):
    output = Path(output)
    temporary = output.with_name(output.name + ".tmp")
    created = False
    try:
        with temporary.open("xb") as handle:
            created = True
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for relative in ["result.json", *[item["path"] for item in result.files]]:
                    path = regular_file(directory, relative)
                    compression = zipfile.ZIP_STORED if path.suffix == ".fst" else zipfile.ZIP_DEFLATED
                    archive.write(path, relative, compress_type=compression)
            handle.flush()
            os.fsync(handle.fileno())
        # POSIX link publishes atomically without overwriting a competing output.
        os.link(temporary, output)
    finally:
        if created:
            temporary.unlink()
