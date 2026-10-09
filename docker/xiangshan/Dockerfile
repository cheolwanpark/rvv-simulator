# syntax=docker/dockerfile:1

FROM ubuntu:24.04 AS build-base
ARG TARGETARCH
ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ENV LC_ALL=C.UTF-8
RUN test "${TARGETARCH}" = amd64 \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries
RUN --mount=type=cache,id=rtl-noble-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=rtl-noble-lists,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        autoconf automake bc bison build-essential ca-certificates clang-18 \
        cmake curl device-tree-compiler flex git help2man libcapstone-dev \
        libfl-dev liblz4-dev libprotobuf-dev libreadline-dev libsdl2-dev \
        libsqlite3-dev libssl-dev libzstd-dev numactl openjdk-21-jdk-headless \
        perl pkg-config protobuf-compiler python3 time zlib1g-dev
COPY --chmod=755 scripts/fetch-source scripts/build-jobs /usr/local/bin/

FROM build-base AS nemu
ARG NEMU_REPO=https://github.com/OpenXiangShan/NEMU.git
ARG NEMU_REV=57006a5e9da2ed68698ac83575c581f7ad189ee8
ARG BUILD_JOBS=auto
ENV NEMU_HOME=/workspace/NEMU
RUN fetch-source "${NEMU_REPO}" "${NEMU_REV}" "$NEMU_HOME" \
    && cd "$NEMU_HOME" && make riscv64-xs_defconfig \
    && jobs=$(build-jobs nemu) && make -j"$jobs"

# Serialize CPU-heavy stages so separate BuildKit branches don't each consume
# the entire per-stage CPU/memory budget. NEMU remains shared between v2/v3.
FROM nemu AS verilator
ARG VERILATOR_VERSION=5.026
RUN git clone --depth 1 --branch "v${VERILATOR_VERSION}" https://github.com/verilator/verilator.git /tmp/verilator \
    && cd /tmp/verilator && autoconf \
    && ./configure CC=clang-18 CXX=clang++-18 LINK=clang++-18 \
    && jobs=$(build-jobs verilator) && make -j"$jobs" && make install \
    && mkdir -p /toolchain-notices/usr/share/rvv-simulator/licenses/verilator \
    && cp COPYING* /toolchain-notices/usr/share/rvv-simulator/licenses/verilator/ \
    && verilator --version > /toolchain-notices/usr/share/rvv-simulator/verilator.txt \
    && rm -rf /tmp/verilator

FROM build-base AS sources
ARG XIANSHAN_REPO=https://github.com/OpenXiangShan/XiangShan.git
ARG XIANSHAN_REV=e7bab53e66dfb3c4a1d11cf9519b0396f8576cae
ARG BUILD_JOBS=auto
RUN fetch-source "${XIANSHAN_REPO}" "${XIANSHAN_REV}" /workspace/XiangShan \
    && cd /workspace/XiangShan && jobs=$(build-jobs fetch) && git config submodule.fetchJobs "$jobs" \
    && git submodule update --init --depth 1 \
    && make init

FROM verilator AS builder
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ARG MILL_VERSION=0.12.3
# Mill 0.12.3 publishes an executable assembly jar; newer 0.12 versions use a launcher.
RUN suffix=jar; if [ "$MILL_VERSION" != 0.12.3 ]; then suffix=mill.sh; fi; \
    if [ "$suffix" = jar ]; then artifact="mill-dist-${MILL_VERSION}.jar"; \
    else artifact="mill-dist-${MILL_VERSION}-mill.sh"; fi; \
    curl --retry 5 -fsSL "https://repo.maven.apache.org/maven2/com/lihaoyi/mill-dist/${MILL_VERSION}/${artifact}" \
        -o /usr/local/bin/mill && chmod +x /usr/local/bin/mill
ENV XIANSHAN_HOME=/workspace/XiangShan \
    NOOP_HOME=/workspace/XiangShan \
    IN_XSDEV_DOCKER=y
COPY --from=sources /workspace/XiangShan/ /workspace/XiangShan/
WORKDIR /workspace/XiangShan
RUN --mount=type=cache,id=xs-coursier,target=/root/.cache/coursier,sharing=locked \
    --mount=type=cache,id=xs-mill-downloads,target=/root/.cache/mill,sharing=locked \
    cpus=$(build-jobs scala) && export JAVA_TOOL_OPTIONS="-Xmx2G -XX:ActiveProcessorCount=$cpus" \
    && test "$(cat .mill-version)" = "$MILL_VERSION" && mill --version && make deps
ARG BUILD_JOBS=auto
ARG XIANSHAN_CONFIG=MinimalConfig
ARG XIANSHAN_EMU_THREADS=1
ARG XIANSHAN_JVM_HEAP=auto
RUN --mount=type=cache,id=xs-coursier,target=/root/.cache/coursier,sharing=locked \
    --mount=type=cache,id=xs-mill-downloads,target=/root/.cache/mill,sharing=locked \
    heap="$XIANSHAN_JVM_HEAP"; if [ "$heap" = auto ]; then heap=$(build-jobs --heap xiangshan); fi; \
    cpus=$(build-jobs scala); jobs=$(build-jobs xiangshan); \
    export JAVA_TOOL_OPTIONS="-Xmx2G -XX:ActiveProcessorCount=$cpus"; \
    ulimit -s unlimited \
    && make emu CONFIG="${XIANSHAN_CONFIG}" EMU_TRACE=fst \
        EMU_THREADS="${XIANSHAN_EMU_THREADS}" JVM_XMX="$heap" -j"$jobs" \
    && test -x build/emu
COPY scripts/collect-libs.py scripts/record-sources.py /tmp/pack/
RUN mkdir -p /runtime-root/workspace/XiangShan/ready-to-run \
    && install -D "$NEMU_HOME/build/riscv64-nemu-interpreter" \
        /runtime-root/workspace/NEMU/build/riscv64-nemu-interpreter \
    && strip --strip-debug /runtime-root/workspace/NEMU/build/riscv64-nemu-interpreter \
    && install -D build/emu /runtime-root/workspace/XiangShan/build/emu \
    && strip --strip-debug /runtime-root/workspace/XiangShan/build/emu \
    && cp ready-to-run/*.bin ready-to-run/*-so /runtime-root/workspace/XiangShan/ready-to-run/ \
    && python3 /tmp/pack/collect-libs.py /runtime-root build/emu ready-to-run/*-so "$NEMU_HOME/build/riscv64-nemu-interpreter" \
    && python3 /tmp/pack/record-sources.py /runtime-root /workspace/XiangShan "$NEMU_HOME" \
    && cp -a /toolchain-notices/. /runtime-root/ \
    && printf 'config=%s\nmill=%s\nemu_threads=%s\n' \
        "$XIANSHAN_CONFIG" "$MILL_VERSION" "$XIANSHAN_EMU_THREADS" \
        > /runtime-root/usr/share/rvv-simulator/config.txt

FROM ubuntu:24.04 AS runtime
ARG XIANSHAN_REPO=https://github.com/OpenXiangShan/XiangShan.git
ARG XIANSHAN_REV=e7bab53e66dfb3c4a1d11cf9519b0396f8576cae
LABEL org.opencontainers.image.source="${XIANSHAN_REPO}" \
      org.opencontainers.image.revision="${XIANSHAN_REV}"
ENV XIANSHAN_HOME=/workspace/XiangShan \
    NEMU_HOME=/workspace/NEMU \
    NOOP_HOME=/workspace/XiangShan \
    LC_ALL=C.UTF-8 \
    PATH=/opt/xiangshan-rtl/scripts:${PATH}
COPY --from=builder /runtime-root/ /
COPY --chmod=755 xiangshan/xs-run /opt/xiangshan-rtl/scripts/xs-run
COPY --chmod=755 scripts/check-runtime /usr/local/bin/check-runtime
RUN check-runtime "$XIANSHAN_HOME/build/emu" "$NEMU_HOME/build/riscv64-nemu-interpreter" \
        /workspace/XiangShan/ready-to-run/*-so \
    && xs-run --list && xs-run --dry-run \
    && "$XIANSHAN_HOME/build/emu" --help > /dev/null
WORKDIR /host
CMD ["/bin/bash"]
