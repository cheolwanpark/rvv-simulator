#!/usr/bin/env bash

export CY_DIR=/workspace/Chipyard
saturn_conda_env=/workspace/Chipyard/.conda-env

# shellcheck source=/dev/null
source /opt/conda/etc/profile.d/conda.sh || return 1

if [ "${CONDA_PREFIX:-}" != "$saturn_conda_env" ]; then
    conda activate "$saturn_conda_env" || return 1
fi

export RISCV="${CONDA_PREFIX}/riscv-tools"
case ":${PATH}:" in
    *":${RISCV}/bin:"*) ;;
    *) export PATH="${RISCV}/bin:${PATH}" ;;
esac
