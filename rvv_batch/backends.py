"""Simulator commands, ELF checks and versioned output parsing."""

from dataclasses import dataclass, field
import math
import re
import struct

BACKENDS = ("xiangshan-v2", "xiangshan-v3", "saturn")
PARSER_VERSION = 1
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NUMBER = r"[0-9][0-9,']*"
CORE = re.compile(
    rf"Core-(\d+)(\(Soft Warmup\))?\s+instrCnt\s*=\s*({NUMBER}),\s*"
    rf"cycleCnt\s*=\s*({NUMBER}),\s*IPC\s*=\s*([0-9.eE+-]+)"
)
KERNEL = re.compile(r"RVV_KERNEL name=([A-Za-z0-9_.:-]+) cycles=([0-9]+)")
MAX_INTEGER = 2**63 - 1


def image_name(backend):
    return f"rvv-simulator/{backend}-rtl:latest"


def command(backend, max_cycles, seed, wave):
    args = ["saturn-run" if backend == "saturn" else "xs-run",
            "--workload", "/input/program.elf", "--max-cycles", str(max_cycles),
            "--seed", str(seed)]
    if wave:
        args += ["--wave", "--wave-path", "/work/wave.fst"]
    return args


def validate_elf(data):
    """Accept static little-endian ELF64 RISC-V executables, without suite symbols."""
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("expected a little-endian ELF64 executable")
    kind, machine, version, entry, phoff, _, _, ehsize, phsize, phnum, *_ = struct.unpack_from(
        "<HHIQQQIHHHHHH", data, 16)
    if (kind, machine, version, ehsize, phsize) != (2, 243, 1, 64, 56):
        raise ValueError("expected a static RISC-V ELF executable with program headers")
    if not phnum or phoff + phsize * phnum > len(data):
        raise ValueError("missing or truncated ELF program headers")
    executable_entry = False
    loads = []
    for i in range(phnum):
        typ, flags, offset, virtual, physical, filesz, memsz, align = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + i * phsize)
        if typ in (2, 3):
            raise ValueError("dynamic ELF files are not supported")
        if typ != 1:
            continue
        if filesz > memsz or offset + filesz > len(data) or physical + memsz > 2**64:
            raise ValueError("invalid ELF load segment")
        if align > 1 and (align & (align - 1) or (virtual - offset) % align):
            raise ValueError("invalid ELF load alignment")
        executable_entry |= bool(flags & 1 and virtual <= entry < virtual + filesz)
        if memsz:
            loads.append((physical, physical + memsz))
    if not loads or not executable_entry:
        raise ValueError("ELF entry is not in a file-backed executable segment")
    loads.sort()
    if any(left[1] > right[0] for left, right in zip(loads, loads[1:])):
        raise ValueError("overlapping ELF load segments")


@dataclass
class Parsed:
    measurements: list = field(default_factory=list)
    good: bool = False
    bad: bool = False
    limit: bool = False
    errors: list = field(default_factory=list)

    def add(self, metric, name, value, stream, line_no, source, scope="simulation"):
        if isinstance(value, int) and not 0 <= value <= MAX_INTEGER:
            self.errors.append(f"{stream}:{line_no}: {metric} outside SQLite INTEGER range")
            return
        if isinstance(value, float) and not math.isfinite(value):
            self.errors.append(f"{stream}:{line_no}: non-finite {metric}")
            return
        self.measurements.append((metric, scope, name, value, stream, line_no, source))


def parse_lines(backend, lines):
    """lines yields (stream, line number, text); retain every recognized sample."""
    result = Parsed()
    def integer(value):
        digits = value.replace(",", "").replace("'", "").lstrip("0") or "0"
        # Avoid Python's maximum-decimal-digits exception for malformed output.
        return int(digits) if len(digits) <= 19 else MAX_INTEGER + 1
    for stream, line_no, raw in lines:
        text = ANSI.sub("", raw).strip()
        marker = KERNEL.fullmatch(text)
        if marker:
            result.add("kernel_cycle", marker[1], integer(marker[2]), stream, line_no,
                       "RVV_KERNEL", "kernel")
        elif "RVV_KERNEL" in text:
            result.errors.append(f"{stream}:{line_no}: malformed RVV_KERNEL marker")
        if backend.startswith("xiangshan"):
            result.good |= "HIT GOOD TRAP" in text
            result.bad |= any(term in text for term in ("HIT BAD TRAP", "ABORT at pc", "DIFFTEST MISMATCH"))
            result.limit |= "EXCEEDING CYCLE/INSTR LIMIT" in text
            match = CORE.search(text)
            if match:
                core, warmup, instructions, cycles, ipc = match.groups()
                scope = "warmup" if warmup else "simulation"
                result.add("cycle", f"core-{core}", integer(cycles), stream, line_no,
                           "xiangshan.cycleCnt", scope)
                result.add("instructions", f"core-{core}", integer(instructions), stream,
                           line_no, "xiangshan.instrCnt", scope)
                result.add("ipc", f"core-{core}", float(ipc), stream, line_no, "xiangshan.IPC", scope)
            match = re.search(rf"Guest cycle spent:\s*({NUMBER})", text)
            if match:
                result.add("guest_cycle", "guest", integer(match[1]), stream, line_no,
                           "xiangshan.Guest cycle spent")
        else:
            match = re.search(r"SATURN simulation cycleCnt\s*=\s*(\d+)", text)
            if match:
                result.good = True
                result.add("cycle", "simulation", integer(match[1]), stream, line_no,
                           "saturn.trace_count")
            result.bad |= "*** FAILED ***" in text
            result.limit |= "timeout" in text.lower() and "*** FAILED ***" in text
            match = re.search(r"\*\*\* (?:FAILED|PASSED) \*\*\*.*?after\s+(\d+)\s+(?:simulation )?cycles", text)
            if match:
                result.add("reported_cycle", "simulation", integer(match[1]), stream, line_no,
                           "saturn.termination")
    return result
