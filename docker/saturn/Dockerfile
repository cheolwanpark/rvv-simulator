# syntax=docker/dockerfile:1

FROM ubuntu:22.04 AS build-base
ARG TARGETARCH
ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ENV LC_ALL=C.UTF-8
RUN test "${TARGETARCH}" = amd64 \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries
RUN --mount=type=cache,id=rtl-jammy-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=rtl-jammy-lists,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        autoconf automake bc bison build-essential ca-certificates cmake curl \
        device-tree-compiler file flex gawk git jq libfl-dev libreadline-dev \
        libssl-dev libtool libzstd-dev numactl patch pkg-config python3 rsync \
        texinfo unzip wget xz-utils zlib1g-dev
COPY --chmod=755 scripts/fetch-source scripts/build-jobs /usr/local/bin/

FROM build-base AS sources
ARG CHIPYARD_REPO=https://github.com/ucb-bar/chipyard.git
ARG CHIPYARD_REV=0acc1e1de2d3284bcd4d876956932a013ffe1949
ARG SATURN_REV=dfe75de2a8868d51d42c20612821431a9fcf645e
ARG BUILD_JOBS=auto
RUN fetch-source "${CHIPYARD_REPO}" "${CHIPYARD_REV}" /workspace/Chipyard \
    && cd /workspace/Chipyard && jobs=$(build-jobs fetch) && git config submodule.fetchJobs "$jobs" \
    && ./scripts/init-submodules-no-riscv-tools.sh --saturn \
    && git submodule update --init --depth 1 -- toolchains/riscv-tools/riscv-isa-sim toolchains/libgloss \
    && test "$(git -C generators/saturn rev-parse HEAD)" = "$SATURN_REV"

# Conda depends only on the pinned lockfile, not the RTL or local patches.
FROM build-base AS conda
ARG MINIFORGE_VERSION=23.1.0-1
RUN curl --retry 5 -fsSLo /tmp/miniforge.sh \
        "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-${MINIFORGE_VERSION}-Linux-x86_64.sh" \
    && bash /tmp/miniforge.sh -b -p /opt/conda && rm /tmp/miniforge.sh
COPY --from=sources /workspace/Chipyard/conda-reqs/conda-lock-reqs/conda-requirements-riscv-tools-linux-64-lean.conda-lock.yml /tmp/conda-lock.yml
RUN --mount=type=cache,id=saturn-conda-pkgs,target=/opt/conda/pkgs,sharing=locked \
    /opt/conda/bin/conda config --system --set remote_max_retries 10 \
    && /opt/conda/bin/conda config --system --set remote_connect_timeout_secs 30 \
    && /opt/conda/bin/conda config --system --set remote_read_timeout_secs 120 \
    && /opt/conda/bin/conda create -y -p /tmp/conda-lock-env -c conda-forge conda-lock=2.5.7 \
    && /tmp/conda-lock-env/bin/conda-lock install --conda /opt/conda/bin/conda \
        -p /workspace/Chipyard/.conda-env /tmp/conda-lock.yml \
    && rm -rf /tmp/conda-lock-env

FROM conda AS builder
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
COPY --from=sources /workspace/Chipyard/ /workspace/Chipyard/
COPY saturn/build-env.sh /workspace/Chipyard/env.sh
WORKDIR /workspace/Chipyard
RUN source env.sh && mkdir -p "$RISCV" \
    && tools/install-circt/bin/download-release-or-nightly-circt.sh \
        -f circt-full-static-linux-x64.tar.gz -i "$RISCV" \
        -v version-file -x conda-reqs/circt.json -g null
ARG BUILD_JOBS=auto
# These dependencies are independent of the SATURN configuration and patches.
RUN source env.sh \
    && espresso_jobs=$(build-jobs espresso) && spike_jobs=$(build-jobs spike) && libgloss_jobs=$(build-jobs libgloss) \
    && cmake -S generators/constellation/espresso -B generators/constellation/espresso/build \
        -DBUILD_DOC=OFF -DCMAKE_INSTALL_PREFIX="$RISCV" \
    && cmake --build generators/constellation/espresso/build --target install --parallel "$espresso_jobs" \
    && command -v espresso \
    && mkdir -p toolchains/riscv-tools/riscv-isa-sim/build \
    && cd toolchains/riscv-tools/riscv-isa-sim/build \
    && ../configure --prefix="$RISCV" --with-boost=no --with-boost-asio=no --with-boost-regex=no \
    && make -j"$spike_jobs" && make install \
    && test -f "$RISCV/lib/libriscv.so" && test -f "$RISCV/lib/libfesvr.a" \
    && cd /workspace/Chipyard && mkdir -p toolchains/libgloss/build && cd toolchains/libgloss/build \
    && CC='' CXX='' ../configure --prefix="$RISCV/riscv64-unknown-elf" --host=riscv64-unknown-elf \
    && make -j"$libgloss_jobs" && make install \
    && test -f "$(riscv64-unknown-elf-g++ -print-file-name=htif_nano.specs)"
COPY saturn/patches/ /tmp/saturn-patches/
RUN git apply --check /tmp/saturn-patches/*.patch && git apply /tmp/saturn-patches/*.patch
ARG SATURN_CONFIG=GENV256D128ShuttleConfig
ARG SATURN_JVM_HEAP=auto
ARG SATURN_SIM_THREADS=1
ENV SATURN_CONFIG=${SATURN_CONFIG}
RUN --mount=type=cache,id=saturn-coursier,target=/root/.cache/coursier,sharing=locked \
    --mount=type=cache,id=saturn-ivy,target=/root/.ivy2,sharing=locked \
    --mount=type=cache,id=saturn-sbt,target=/root/.sbt,sharing=locked \
    source env.sh; heap="$SATURN_JVM_HEAP"; if [ "$heap" = auto ]; then heap=$(build-jobs --heap saturn); fi; \
    cpus=$(build-jobs scala); export JDK_JAVA_OPTIONS="-XX:ActiveProcessorCount=$cpus"; \
    make -C sims/verilator /workspace/Chipyard/.classpath_cache/tapeout.jar \
        CONFIG="$SATURN_CONFIG" JAVA_HEAP_SIZE="$heap"
RUN --mount=type=cache,id=saturn-coursier,target=/root/.cache/coursier,sharing=locked \
    --mount=type=cache,id=saturn-ivy,target=/root/.ivy2,sharing=locked \
    --mount=type=cache,id=saturn-sbt,target=/root/.sbt,sharing=locked \
    source env.sh; heap="$SATURN_JVM_HEAP"; if [ "$heap" = auto ]; then heap=$(build-jobs --heap saturn); fi; \
    cpus=$(build-jobs scala); export JDK_JAVA_OPTIONS="-XX:ActiveProcessorCount=$cpus"; \
    normal_jobs=$(build-jobs saturn); trace_jobs=$(build-jobs saturn-trace); workload_jobs=$(build-jobs benchmarks); \
    make -C sims/verilator CONFIG="$SATURN_CONFIG" JAVA_HEAP_SIZE="$heap" \
        VERILATOR_THREADS="$SATURN_SIM_THREADS" -j"$normal_jobs" \
    && make -C sims/verilator debug CONFIG="$SATURN_CONFIG" JAVA_HEAP_SIZE="$heap" \
        VERILATOR_THREADS="$SATURN_SIM_THREADS" USE_FST=1 -j"$trace_jobs" \
    && make -C generators/saturn/benchmarks vec-daxpy.riscv vec-strlen.riscv -j"$workload_jobs"
COPY scripts/collect-libs.py scripts/record-sources.py /tmp/pack/
COPY saturn/package-runtime.sh /tmp/package-runtime.sh
RUN bash /tmp/package-runtime.sh

FROM ubuntu:22.04 AS runtime
ARG CHIPYARD_REPO=https://github.com/ucb-bar/chipyard.git
ARG CHIPYARD_REV=0acc1e1de2d3284bcd4d876956932a013ffe1949
ARG SATURN_REV=dfe75de2a8868d51d42c20612821431a9fcf645e
ARG SATURN_CONFIG=GENV256D128ShuttleConfig
LABEL org.opencontainers.image.source="${CHIPYARD_REPO}" \
      org.opencontainers.image.revision="${CHIPYARD_REV}" \
      cc.rvv-simulator.saturn.revision="${SATURN_REV}"
ENV SATURN_HOME=/workspace/Chipyard \
    SATURN_CONFIG=${SATURN_CONFIG} \
    LC_ALL=C.UTF-8 \
    PATH=/opt/saturn-rtl/bin:${PATH}
# file is used to validate custom workload architecture before running it.
RUN apt-get update && apt-get install -y --no-install-recommends file \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /runtime-root/ /
COPY --chmod=755 saturn/saturn-run /opt/saturn-rtl/bin/saturn-run
COPY --chmod=755 scripts/check-runtime /usr/local/bin/check-runtime
RUN check-runtime /opt/saturn-rtl/bin/saturn-sim /opt/saturn-rtl/bin/saturn-sim-trace \
    && saturn-run --list && saturn-run --dry-run && saturn-run --wave --dry-run
WORKDIR /host
CMD ["/bin/bash"]
