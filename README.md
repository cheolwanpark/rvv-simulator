# RVV simulator batches

Run a directory of prebuilt RV64 bare-metal ELF files on XiangShan v2/v3 or
Saturn. Each request creates **one ordinary SQLite database** containing the
inputs, logs, settings, measurements and execution history. Resume updates that
same database. Analysis needs only SQLite; there is no extraction command,
compressed payload, custom SQL function or Python package required to read it.

Requires Python 3.10+, a POSIX host (Linux or macOS), and a Linux Docker daemon
with the runtime images already built. Images target `linux/amd64`; Docker Desktop
on Apple Silicon can run them through emulation. Bind mounts must refer to the
runner's filesystem, as with local Docker or Docker Desktop.

## Run

```sh
# Prepare the desired image separately; run never builds or pulls an image.
make build BACKEND=xiangshan-v3

make run ELF_DIR=./elfs BACKEND=xiangshan-v3 JOBS=8 \
    DB=./results/experiment.sqlite

# After interruption: same DB, original ELF bytes and immutable image ID.
make resume DB=./results/experiment.sqlite JOBS=4

# Direct CLI equivalents:
./rvv-batch run ./elfs --backend xiangshan-v3 --jobs 8 \
    --output ./results/experiment-2.sqlite
./rvv-batch resume ./results/experiment-2.sqlite --jobs 4
```

`BACKEND` is `xiangshan-v2`, `xiangshan-v3` or `saturn`. One request uses one
backend. `ELF_DIR`, `BACKEND` and `DB` are required for `make run`; only `DB` is
required for `make resume`. Relative paths are relative to the working directory.

| Make variable | CLI argument | Default for a new request |
| --- | --- | --- |
| `JOBS` | `--jobs` | 1 |
| `WAVE` | `--wave` when `WAVE=1` | **0: off** |
| `SEED` | `--seed` | 1 |
| `MAX_CYCLES` | `--max-cycles` | 10,000,000 |
| `TIMEOUT` | `--timeout` | 3,600 seconds per attempt |
| `IMAGE` | `--image` | `rvv-simulator/<backend>-rtl:latest` |
| `CPU_SET` | `--cpu-set` | Docker's available CPU set |
| `MEMORY` | `--memory` | no additional per-container memory limit |
| `DOCKER` | `--docker` | `docker` |

`CPU_SET=0,2,4,6`, `MEMORY=8g` and paths with spaces are supported. `PYTHON` selects
the Python executable for Make. Resume inherits its previous concurrency if
`JOBS` is omitted; all simulation settings come from the DB. Make rejects attempts
to change those settings during resume.

Every simulator has **one Verilator model thread and one logical CPU**. The runner
checks the image's `emu_threads=1` or `simulator_threads=1`, assigns distinct CPU IDs
to active jobs in this request, and applies `--cpus=1` and `--cpuset-cpus`. `JOBS`
must fit the selected CPU set and detected cgroup quota. CPU IDs refer to the
Docker Linux host/VM. Allocation is per request, not a machine-wide reservation.

The directory is scanned recursively in relative-path order. ELF magic identifies
extensionless executables too. Broken `.elf`/`.riscv` files become `invalid_input`
jobs; other non-ELF files and symlinks are skipped. Identical basenames in different
directories remain separate jobs. Original ELF bytes are snapshotted into SQLite
before simulations begin, with identical content stored once.

ELFs must be static, little-endian ELF64 RISC-V executables with valid load
segments. They must already contain the selected target's startup, memory layout
and completion convention (XiangShan GOOD TRAP vs Saturn's HTIF/test harness).
The runner does not compile, instrument or make one target's ELF compatible with
another. Both current XiangShan pins and Saturn accept ELF directly.

Progress is printed one line at a time; numbers identify jobs, not completion order:

```text
start [3/20] kernels/matmul.elf
finish [3/20] kernels/matmul.elf total_cycle=145678 kernel_cycle=123456 status=succeeded
```

`finish` is printed after the results are committed. Multiple kernel samples are
shown as `kernel_cycle=multiple(N)`; missing samples as `kernel_cycle=null`.

## Cycle contract

| Measurement | Meaning |
| --- | --- |
| XiangShan total cycle | final non-warmup `Core-0 ... cycleCnt` |
| Saturn total cycle | patched `SATURN simulation cycleCnt`, the TestDriver counter |
| Kernel cycle | guest-reported `rdcycle` difference from the marker below |
| Wall seconds | host time from attempt creation to container exit, including container startup |

The two total counters have their backend-specific boot/reset/harness scopes.
They are not substituted for kernel cycles. XiangShan's other core counts,
instruction counts, IPC, warmup counts and `Guest cycle spent`, and Saturn's
termination-reported counts are also retained as separate measurements.

To report a kernel measurement, emit a complete line:

```text
RVV_KERNEL name=matmul cycles=123456
```

Names match `[A-Za-z0-9_.:-]+`; cycle values are nonnegative decimal integers fitting
SQLite's signed 64-bit INTEGER. Each occurrence is a separate sample; nothing is
implicitly averaged or summed. Old `CYCLES=`, `MB_ROI` and `FISSION_ROI` formats are
not interpreted. A missing marker leaves execution success intact and records
`kernel_status=missing`, `kernel_cycle=NULL`. Malformed markers generate an event
and an invalid measurement status; their original text is retained in the logs.

For an ELF runtime that already provides `printf`, a measurement can look like:

```c
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

static inline uint64_t cycles(void) {
    uint64_t value;
    __asm__ volatile("fence rw, rw\n\trdcycle %0" : "=r"(value) :: "memory");
    return value;
}

void kernel(void);

void measured_kernel(void) {
    /* Initialize inputs and perform any chosen warmup before this region. */
    uint64_t start = cycles();
    kernel();
    uint64_t elapsed = cycles() - start;
    printf("RVV_KERNEL name=kernel cycles=%" PRIu64 "\n", elapsed);
}
```

This reports the instrumented region, including counter/fence overhead; the runner
does not subtract overhead or infer a warmup policy.

## Analyze the SQLite file

See [the schema contract](docs/sqlite-schema.md) for columns, statuses and recovery
semantics. `PRAGMA user_version` and `run.schema_version` are both `1`.

```sh
sqlite3 -header -column results/experiment.sqlite \
  'SELECT job_id,name,status,total_cycle,kernel_cycle,kernel_sample_count FROM job_results ORDER BY job_id;'
```

```sql
-- All kernel samples, including historical/interrupted attempts.
SELECT j.name, a.attempt_no, m.name AS kernel, m.sample_index,
       m.value AS cycles, m.validity
FROM measurements m
JOIN attempts a USING (attempt_id)
JOIN jobs j USING (job_id)
WHERE m.metric = 'kernel_cycle'
ORDER BY j.job_id, a.attempt_no, m.name, m.sample_index;

-- Ordinary text; rows are stream chunks, not necessarily complete lines.
SELECT stream, sequence, text
FROM logs WHERE attempt_id = 1
ORDER BY stream, sequence;

-- Original ELF bytes, directly readable with any SQLite client.
SELECT j.name, ar.sha256, ar.data
FROM jobs j JOIN artifacts ar ON ar.artifact_id = j.elf_artifact_id;
```

Raw logs preserve ANSI escape sequences. The parser strips them only for
measurements. Valid UTF-8 stays in `logs.text`; a chunk containing invalid UTF-8 also
stores its exact original bytes in `logs.raw_bytes` (an uncompressed BLOB).

## Waveforms and other files

Waveforms are off unless `WAVE=1` / `--wave` is requested:

```sh
make run ELF_DIR=./elfs BACKEND=saturn JOBS=2 \
    DB=./results/with-wave.sqlite WAVE=1
```

Saturn selects its normal executable by default and its FST executable only with
wave enabled. XiangShan's trace-capable executable gets no wave-dump flags by
default. A successful wave-enabled job must produce a nonempty `wave.fst`.

ELFs, stdout/stderr, image/source manifests, command arguments, settings and every
recognized cycle sample always go into SQLite, without compression. FST and any
other simulator-created files always stay as original files under:

```text
experiment.sqlite.artifacts/<job-id>/attempt-<n>/
```

SQLite indexes their relative paths, sizes and SHA-256 hashes. Storage location is
selected by artifact type, never by file size. There are no extra archives or an
`extract` step. Copy the SQLite file alone for input/log/metric analysis; copy its
`.artifacts` directory alongside it when you also need the waveforms.

## Interruption and resume

The scheduler is the sole SQLite writer. Detached containers write into isolated
host directories while the scheduler regularly commits their logs. A lock prevents
two runners from updating the same DB. On Ctrl-C/SIGTERM the runner stops its
containers, saves partial outputs and exits with 130/143. A hard controller kill
may leave containers running; resume identifies them by request labels and stops
them before retrying.

Resume keeps the same request ID, ELF snapshots, image ID and settings. It retains
all attempt history and executes only `pending` or `interrupted` jobs. Failed,
invalid-input, cycle-limited and timed-out jobs are terminal; create a new request
to retry them with different settings. A simulator that exited before the
controller died is finalized from its existing outputs instead of being rerun.

During execution, `<DB>.work/`, `<DB>-wal` and `<DB>-shm` can exist. Keep them for
recovery after an unclean exit; they are not additional result databases. Successful
shutdown checkpoints WAL into the main SQLite file and removes staged work files.
Copy the database after shutdown. Close concurrent SQL readers if they prevent the
final checkpoint. The empty `<DB>.lock` inode is retained for safe locking.

CLI exit codes: `0` for completed jobs (missing kernel markers are allowed), `1` for
job failures or invalid/missing total measurements, `2` for setup/controller errors,
and `130`/`143` for interruption. Make reports nonzero recipe exits using its own
standard failure exit code. A controller error preserves recovery data.

## Validation

```sh
make test                         # No Docker daemon required
make smoke BACKEND=xiangshan-v3   # Existing image smoke checks
make smoke BACKEND=saturn
```

The local tests exercise the real CLI against a process-level Docker double,
including parallel execution, forced controller death, resumed attempts, SIGINT,
plain SQLite queries, waveform retention and Make argument forwarding. They do
not establish that a particular ELF runs correctly on real RTL. After building an
image, also run a small batch of target-specific ELFs with and without `WAVE=1` and
compare the database counters with the retained simulator logs.

Image recipes and source pins are documented in [docker/README.md](docker/README.md).
