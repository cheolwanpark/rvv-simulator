# SQLite schema v1

The database is the result artifact and resume state for exactly one request.
It requires only standard SQLite. All BLOBs contain original bytes; no compression,
extension, UDF or extraction tool is involved. Times are UTC ISO 8601 strings.
Foreign keys link jobs, attempts, samples and artifacts. `PRAGMA user_version`
is the schema compatibility version; incompatible versions are rejected on resume.

## Tables

| Table | Key and principal columns |
| --- | --- |
| `run` | `id` request UUID; `schema_version`, `parser_version`, `tool_version`, `backend`, `image_id`, `image_ref`, `input_dir`, `created_at`, `updated_at`, `status`, `jobs`, `seed`, `max_cycles`, `timeout`, `wave`, `cpu_set`, `memory` |
| `jobs` | `job_id` (1-based sorted input index), unique `name` (original relative path), `elf_artifact_id`, `validation_error` |
| `attempts` | `attempt_id`, `job_id`, `attempt_no` (1-based per job), `status`, `started_at`, `finished_at`, `wall_seconds`, `exit_code`, `reason`, `container_name`, `cpu`, `command_json`, `total_cycle`, `measurement_status`, `kernel_status` |
| `measurements` | `measurement_id`, `attempt_id`, `metric`, `scope`, `name`, `sample_index`, `value`, `source_stream`, `source_line`, `source`, `validity` |
| `logs` | primary key (`attempt_id`, `stream`, `sequence`); `byte_offset`, `byte_length`, `text`, nullable `raw_bytes` |
| `artifacts` | `artifact_id`, nullable `attempt_id`, `kind`, `name`, `storage`, `sha256`, `size_bytes`, `data`, `relative_path` |
| `events` | `event_id`, `time`, nullable `attempt_id`, `kind`, `message` |

`image_id` is the immutable Docker image used for every attempt. `image_ref` records
the originally requested tag or ID. Resume changes `run.jobs` when requested and
records each scheduler invocation's CPU IDs in `events`. Simulator settings remain
unchanged. `command_json` is a standard JSON array of simulator argv strings, not
shell code; the input pathname refers to the staged ELF inside the container.

An ELF is stored once by content hash even when multiple jobs use identical bytes.
`artifacts.kind='elf'` and `kind='manifest'` have `storage='sqlite'` with `data` BLOB.
Manifest artifacts include the image config, source revisions and Docker image
inspection JSON. `wave` and `auxiliary` artifacts use `storage='external'` with
`relative_path` relative to the database's directory. Their `data` is NULL. Log
content lives in `logs`, not duplicated in `artifacts`.

## Analysis view

`job_results` has one row per job and selects its **latest attempt**:

```text
job_id, name, attempt_id, attempt_no, status, exit_code, reason,
total_cycle, kernel_cycle, kernel_sample_count, measurement_status,
kernel_status, wall_seconds, started_at, finished_at, attempt_count
```

`kernel_cycle` is non-NULL only when that attempt has exactly one recognized kernel
sample. With multiple samples, inspect `measurements`; the view does not combine
different kernels or repeated regions. Pending jobs have no attempt and NULL
measurement values. To analyze earlier attempts, join the underlying tables.

## Measurements

`sample_index` starts at 0 within each `(attempt_id, metric, scope, name)` group.
Every recognized occurrence is preserved, even if the simulator repeats a summary.
`value` has NUMERIC affinity: cycle/instruction counts use INTEGER and IPC uses
REAL when nonintegral. Non-finite IPC and integers outside `0..2^63-1` are rejected
as measurements, with events and original logs retained.

| `metric` | `scope` / `name` | Source |
| --- | --- | --- |
| `cycle` | `simulation`, `core-N` | XiangShan `cycleCnt` |
| `cycle` | `warmup`, `core-N` | XiangShan Soft Warmup `cycleCnt` |
| `instructions` | `simulation` or `warmup`, `core-N` | XiangShan `instrCnt` |
| `ipc` | `simulation` or `warmup`, `core-N` | XiangShan IPC |
| `guest_cycle` | `simulation`, `guest` | XiangShan `Guest cycle spent` |
| `cycle` | `simulation`, `simulation` | Saturn patched `trace_count` |
| `reported_cycle` | `simulation`, `simulation` | Saturn PASSED/FAILED cycle report |
| `kernel_cycle` | `kernel`, marker's name | `RVV_KERNEL name=... cycles=...` |
| `kernel_cycle` | `kernel`, `bench_kernel` | `loop-benchmarks.v2`: schema 2 JSON with `metric="cycles"`, mode `kernel` or `full`, integer `value` |

Parser version 2 accepts loop-benchmarks JSON. Its `value` is the sum of measured
kernel cycles over `repetitions` (positive integer), excluding `warmups` (nonnegative
integer), in either mode. The parser preserves this sum without averaging; the
original JSON and its metadata remain in `logs`. Hosted `elapsed_ns` values are
not cycle measurements. Invalid cycle records produce `measurement_error` events
and invalid kernel/measurement statuses. The schema version remains 1; resume
requires the same parser version as the original request.

The final non-warmup core-0 `cycle` becomes XiangShan's `total_cycle`. Saturn uses
the final patched `cycle`; `reported_cycle` is not silently used as a substitute.
No counter is derived from wall time. Raw logs retain other performance-counter
output not covered by these versioned parsers.

`source_stream` is stdout or stderr; `source_line` is its 1-based original line
number. `source` names the backend counter/marker. `validity='partial'` marks samples
from attempts that did not succeed; partial values remain available for debugging.
Order between stdout and stderr is not inferred; per-stream order is preserved.

## Log bytes and text

`logs.sequence` starts at 0 per attempt and stream. Rows contain bounded chunks,
not guaranteed complete lines. Valid UTF-8 characters are kept intact across rows.
`text` is directly queryable, retaining ANSI codes and newline characters.
`byte_offset` and `byte_length` describe the original byte stream and also make
recovery ingestion idempotent.

For invalid UTF-8, `text` contains replacement characters for readability and
`raw_bytes` contains the original uncompressed bytes. The bytes of a row are
`COALESCE(raw_bytes, CAST(text AS BLOB))`. Reading these rows in sequence reproduces
the stream with ordinary SQLite operations. No decompressor is needed.

## Statuses and resume

Request statuses: `pending`, `running`, `interrupted`, `completed`,
`completed_with_errors`.

Attempt statuses:

| Status | Meaning | Resume behavior |
| --- | --- | --- |
| `starting`, `running`, `stopping` | nonterminal attempt, possibly from a dead controller | recover outputs and container state first |
| `interrupted` | explicitly interrupted or unfinished when recovered | create another attempt |
| `succeeded` | exit 0 and backend success marker, no failure marker | retain |
| `failed` | nonzero exit, missing success marker, OOM, or missing requested waveform | create another attempt |
| `timeout` | per-attempt wall limit | create another attempt |
| `cycle_limit` | simulator-reported cycle/instruction limit | create another attempt |
| `invalid_input` | rejected ELF | create another rejected attempt without launching a simulator |

A job with no attempt appears as `pending` in the view and will be scheduled.
After recovery, every job whose latest status is not `succeeded` is scheduled
once per resume invocation. Failures during that invocation await the next resume;
all previous attempts and their outputs are retained. Successful jobs are skipped
regardless of measurement status. A new request is required to change the input
or simulator settings, including the timeout.

`measurement_status`: `pending`, `complete`, `missing_kernel`, `missing_total`,
`invalid` or `partial`. `kernel_status`: `pending`, `available`, `missing` or
`invalid`. Execution success is separate from measurement completeness. For
example, an uninstrumented successful program has `status='succeeded'`,
`measurement_status='missing_kernel'`, and `kernel_cycle=NULL`.

Logs are committed while simulations run. Measurements are parsed and committed
when an attempt is finalized, including interruption recovery. External files are
moved to deterministic attempt directories before indexing; recovery can re-index
a file moved just before a crash. The terminal status is committed with the parsed
samples and artifact index before `finish` is printed or the container removed.

The scheduler is the only writer. WAL allows concurrent analysis while it runs.
After clean shutdown/checkpoint the SQLite file can be copied by itself for
analysis. After an unclean exit, keep WAL and staged work files for recovery rather
than copying only the main file. Keep the `.artifacts` directory too when waveform
or auxiliary file contents are needed. An empty `.lock` file is intentionally kept
to avoid races between processes holding different lock inodes.
