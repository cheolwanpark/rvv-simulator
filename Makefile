.DEFAULT_GOAL := help
PYTHON ?= python3
DOCKER ?= docker

# Empty variables let the CLI own defaults; resume inherits its saved settings.
export ELF_DIR BACKEND DB JOBS WAVE SEED MAX_CYCLES TIMEOUT IMAGE CPU_SET MEMORY DOCKER

.PHONY: help run resume test build smoke
help:
	@echo 'make run ELF_DIR=./elfs BACKEND=xiangshan-v2|xiangshan-v3|saturn DB=./results/run.sqlite [JOBS=1]'
	@echo '         [WAVE=0|1] [SEED=1] [MAX_CYCLES=10000000] [TIMEOUT=3600] [IMAGE=tag] [CPU_SET=0-7] [MEMORY=8g]'
	@echo 'make resume DB=./results/run.sqlite [JOBS=N]'
	@echo 'make test | build BACKEND=... | smoke BACKEND=...'
	@echo 'One simulator thread and one logical CPU per job. Waveforms are off by default.'

run resume build smoke:
	@$(PYTHON) -m rvv_batch.make $@

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	$(MAKE) --no-print-directory -C docker test
