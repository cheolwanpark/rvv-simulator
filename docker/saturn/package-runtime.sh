#!/usr/bin/env bash
set -euo pipefail

cd /workspace/Chipyard
shopt -s nullglob
normal=(sims/verilator/simulator-*"-${SATURN_CONFIG}")
trace=(sims/verilator/simulator-*"-${SATURN_CONFIG}-debug")
[ "${#normal[@]}" = 1 ] && [ "${#trace[@]}" = 1 ]
test -x "${normal[0]}" && test -x "${trace[0]}"
install -D "${normal[0]}" /runtime-root/opt/saturn-rtl/bin/saturn-sim
install -D "${trace[0]}" /runtime-root/opt/saturn-rtl/bin/saturn-sim-trace
strip --strip-debug /runtime-root/opt/saturn-rtl/bin/saturn-sim{,-trace}
mkdir -p /runtime-root/opt/saturn-rtl/workloads
cp generators/saturn/benchmarks/{vec-daxpy,vec-strlen}.riscv /runtime-root/opt/saturn-rtl/workloads/
data=generators/testchipip/src/main/resources/dramsim2_ini
mkdir -p "/runtime-root/workspace/Chipyard/$data"
cp -a "$data/." "/runtime-root/workspace/Chipyard/$data/"

# Scan at the *installed* locations: moving an executable can change $ORIGIN
# resolution. Preserve non-system libraries at their original absolute paths,
# including the small subset under .conda-env referenced by simulator RPATHs.
python3 /tmp/pack/collect-libs.py /runtime-root \
    /runtime-root/opt/saturn-rtl/bin/saturn-sim \
    /runtime-root/opt/saturn-rtl/bin/saturn-sim-trace
python3 /tmp/pack/record-sources.py /runtime-root /workspace/Chipyard
printf 'config=%s\nsimulator_threads=%s\npatches=saturn-report-cycle-count,saturn-enable-pipelined-vat-clear\n' \
    "$SATURN_CONFIG" "$SATURN_SIM_THREADS" > /runtime-root/usr/share/rvv-simulator/config.txt

# Keep redistribution notices for Conda-supplied runtime libraries, without
# packaging Conda, its package cache, or its compiler/JVM installations.
if [ -d .conda-env/share/licenses ]; then
    cp -a .conda-env/share/licenses /runtime-root/usr/share/rvv-simulator/licenses/conda
fi
