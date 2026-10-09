# RTL simulator images

Self-contained, runtime-only images adapted from `code-lab/container-images`
(source repository commit `d981aba59741e75f767af8683a84a0520637398c`).
Builds fetch upstream sources inside Docker; no checkout, symlink, or image from
`code-lab` is needed. The existing `code-lab` symlink is not used or changed.

## Versions

Source pins were checked on **2026-10-09** and live in [versions.mk](versions.mk).
Builds use full commit IDs, including the nested revisions recorded by upstream,
and never resolve moving branches automatically.

| Image | Source pin | Default configuration |
| --- | --- | --- |
| `rvv-simulator/xiangshan-v2-rtl:latest` | Official [Kunminghu v2](https://github.com/OpenXiangShan/XiangShan/tree/e7bab53e66dfb3c4a1d11cf9519b0396f8576cae), `e7bab53e66dfb3c4a1d11cf9519b0396f8576cae` | `MinimalConfig`, FST, one simulator thread |
| `rvv-simulator/xiangshan-v3-rtl:latest` | Official [Kunminghu v3](https://github.com/OpenXiangShan/XiangShan/tree/a7b9dea601f2f08dcca7d97ac544221cf02b1fe9), `a7b9dea601f2f08dcca7d97ac544221cf02b1fe9` | `MinimalConfig`, FST, one simulator thread |
| `rvv-simulator/saturn-rtl:latest` | [Chipyard 1.14.0](https://github.com/ucb-bar/chipyard/releases/tag/1.14.0), `0acc1e1de2d3284bcd4d876956932a013ffe1949` | `GENV256D128ShuttleConfig`, normal and FST simulators |

V2 is the maintained Kunminghu v2 branch, not the unrelated old `v2.1` tag.
V3 remains work in progress; this pin passed upstream emulator build/basic checks
when selected, but is not a finalized stable release. It uses upstream counters,
without the custom utilization patches from the previous `cheolwanpark` fork.
See the [upstream maintenance status](https://github.com/OpenXiangShan/XiangShan/blob/a7b9dea601f2f08dcca7d97ac544221cf02b1fe9/README.md#branch-maintenance-status).

SATURN has no standalone release tags. This image uses the version integrated
with the latest stable Chipyard release: `dfe75de2a8868d51d42c20612821431a9fcf645e`.
The recipe checks that revision and retains the local cycle-reporting and
pipelined VAT-clear patches. Cospike is not included.

XiangShan uses Java 21, Mill 0.12.3 / 0.12.17 and Verilator 5.026 / 5.052 for
v2 / v3 respectively. The separate NEMU executable retains the original
`57006a5e9da2ed68698ac83575c581f7ad189ee8` pin and `riscv64-xs_defconfig`.
Each XiangShan version uses its own pinned `ready-to-run` difftest models.
SATURN retains Chipyard's lean Conda lockfile, CIRCT version, Spike/FESVR and
libgloss pins in its builder.

## Build

Docker must be running, with Buildx/BuildKit installed. All images target
`linux/amd64`; building on Apple Silicon uses emulation and is slower than a
native amd64 builder. Initial RTL builds remain CPU- and memory-intensive.

From the project root:

```sh
make -C docker build xiangshan-v2-rtl
make -C docker build xiangshan-v3-rtl
make -C docker build saturn-rtl
# Or build all three, sequentially:
make -C docker build-all
```

`xiangshan-rtl` remains an alias for `xiangshan-v2-rtl`. Set `IMAGE_PREFIX` or
`XIANSHAN_V2_IMAGE`, `XIANSHAN_V3_IMAGE`, `SATURN_IMAGE` to change tags.
Use the Makefile to select a version: the shared XiangShan Dockerfile defaults
to v2 when invoked directly. Its context, and SATURN's, is the `docker/` directory.

### Automatic resource allocation

Each build stage runs [build-jobs](scripts/build-jobs) **inside the Linux builder**.
It takes the minimum of CPU count, process affinity and cgroup v1/v2 CPU quotas;
RAM comes from `/proc/meminfo` capped by cgroup memory limits (including parent
limits). This works for local Docker Desktop, remote builders and constrained CI
without copying the initiating machine's core count into the image.

The selector reserves 25% of RAM, at least 2 GiB, and applies stage-specific
worker/memory ceilings:

| Stage | Worker ceiling | RAM budget per worker |
| --- | ---: | ---: |
| Verilator / Spike native compilation | 8 | 1 GiB |
| NEMU compilation | 8 | 0.5 GiB |
| Espresso / libgloss / workloads | 4 | 0.5 GiB |
| Scala/JVM work | 4 | 2 GiB |
| XiangShan generated C++ | 8 | 3 GiB |
| SATURN normal generated C++ | 8 | 2 GiB |
| SATURN trace generated C++ | 4 | 3 GiB |
| Git submodule fetching | 4 | 0.25 GiB |

At least one worker is selected. These are conservative tuning budgets, not
benchmarked optimal counts or a guarantee that a small builder can elaborate
the RTL. The selected count and detected resources appear in the build log.
Downloads/configuration steps do not receive `make -j`. The single SATURN
classpath target uses the JVM CPU limit instead. JVMs use
`-XX:ActiveProcessorCount`; elaboration heaps use 60% of the memory limit,
capped at 40 GiB for XiangShan and 8 GiB for SATURN.

CPU-heavy stages within each image run sequentially to avoid competing resource
budgets. Use plain `make build-all`, not `make -j build-all`, to avoid concurrent
image builds competing for the same builder. Independent builds initiated in
other terminals are not coordinated by this selector.

Optional overrides:

```sh
# Additional worker ceiling; automatic CPU/memory/stage limits still apply.
make -C docker build xiangshan-v3-rtl BUILD_JOBS=3
# Explicit heap override, if you have measured a need for it:
make -C docker build xiangshan-v3-rtl XIANSHAN_JVM_HEAP=20G
```

`BUILD_JOBS=auto`, `XIANSHAN_JVM_HEAP=auto`, and `SATURN_JVM_HEAP=auto` are defaults.
Compilation jobs are independent of `XIANSHAN_EMU_THREADS` and
`SATURN_SIM_THREADS`, both defaulting to one. `XIANSHAN_CONFIG` and
`SATURN_CONFIG` are also overridable; the retained VAT-clear patch targets the
default SATURN configuration specifically.

## Run

```sh
make -C docker launch xiangshan-v2-rtl DIR="$PWD/work"
make -C docker launch xiangshan-v3-rtl DIR="$PWD/work"
make -C docker launch saturn-rtl DIR="$PWD/work"
```

Launch mounts the directory as `/host`, starts there, uses the host UID/GID,
and stores shell home files in `/host/.home`. Custom workloads must already be
compiled; these images do not contain compilers or editable RTL sources.

Inside either XiangShan image:

```sh
xs-run --list
xs-run --dry-run
xs-run --workload coremark
xs-run --workload /host/program.bin --max-cycles 10000000
xs-run --workload coremark --wave --log-begin 0 --log-end 10000 --wave-path /host/coremark.fst
/workspace/NEMU/build/riscv64-nemu-interpreter -b /host/program.bin
```

Inside SATURN:

```sh
saturn-run --list
saturn-run --dry-run
saturn-run --workload vec-daxpy
saturn-run --workload vec-strlen --max-cycles 20000000 --wave --wave-path /host/strlen.fst
saturn-run --workload /host/custom-rv64.elf --seed 1
```

Arguments following `--` are passed unchanged to the simulator. Existing
`XIANSHAN_HOME`, `XS_EMU`, `SATURN_HOME`, `SATURN_SIM`, `SATURN_TRACE_SIM`, and
`SATURN_WORKLOADS` overrides remain supported. SATURN no longer sources `env.sh`
or activates Conda at runtime.

## Size and cache changes

- Multi-stage builds retain only runtime executables, bundled workloads/models,
  notices, data files and resolved shared-library dependencies. They exclude
  LLVM/Node/AI CLIs, Java, Conda installations, source trees and object files.
- SATURN retains only any shared libraries needed under their original
  `.conda-env` paths to preserve RPATH resolution, plus DRAMSim configuration
  files. It does not retain a usable Conda environment.
- XiangShan recipes share dependency and NEMU layers; each keeps its own
  Verilator/Mill version. APT, Scala and Conda downloads use BuildKit caches.
- Wrappers are copied after compilation. Wrapper-only edits reuse compiled
  simulator layers. Conda setup depends only on its lockfile; SATURN patches are
  applied after native tool dependencies are built.
- `check-runtime` checks all packaged executables and XiangShan reference shared
  objects for missing libraries. Source manifests and notices are under
  `/usr/share/rvv-simulator`.

## Validation and updates

```sh
make -C docker test                  # Local fixture tests; no Docker required
make -C docker -n build-all          # Inspect commands without building
make -C docker smoke xiangshan-v2-rtl # Test an already-built image
make -C docker smoke-all             # All three already-built images
```

Image smoke checks run workloads, difftest/reference models, NEMU, normal/FST
SATURN simulations, cycle reporting, absence of development tools, nonempty
waveforms and host ownership of outputs. They print image sizes. Each workload
has a timeout; heavily emulated builders may need a longer timeout in the smoke
script. Full image builds/smokes were not run during the initial migration
because the local Docker daemon was unavailable. No size or speed reduction
has yet been measured.

For an update, edit the full commit IDs in `versions.mk`, align XiangShan's Mill
and Verilator versions with that source, and update matching default build
arguments in the Dockerfiles. Keep Chipyard/SATURN pins paired. Recheck patches,
run local tests, rebuild the affected image and run its smoke checks. Do not
replace pins with moving branch names or `git submodule update --remote`.
