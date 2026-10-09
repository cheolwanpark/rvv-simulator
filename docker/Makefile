.DEFAULT_GOAL := help
DOCKER_DIR := $(abspath $(CURDIR))
include $(DOCKER_DIR)/versions.mk

DOCKER ?= docker
IMAGE_PREFIX ?= rvv-simulator
XIANSHAN_V2_IMAGE ?= $(IMAGE_PREFIX)/xiangshan-v2-rtl:latest
XIANSHAN_V3_IMAGE ?= $(IMAGE_PREFIX)/xiangshan-v3-rtl:latest
SATURN_IMAGE ?= $(IMAGE_PREFIX)/saturn-rtl:latest
RTL_PLATFORM ?= linux/amd64
BUILD_JOBS ?= auto
XIANSHAN_CONFIG ?= MinimalConfig
XIANSHAN_EMU_THREADS ?= 1
XIANSHAN_JVM_HEAP ?= auto
SATURN_CONFIG ?= GENV256D128ShuttleConfig
SATURN_SIM_THREADS ?= 1
SATURN_JVM_HEAP ?= auto
DIR ?= $(CURDIR)

NAMES := xiangshan-v2-rtl xiangshan-v3-rtl saturn-rtl xiangshan-rtl
NAME ?= $(firstword $(filter $(NAMES),$(MAKECMDGOALS)))
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)

.PHONY: help check-docker build build-all build-xiangshan-rtl build-xiangshan-v2-rtl build-xiangshan-v3-rtl build-saturn-rtl launch smoke smoke-all test $(NAMES)

help:
	@echo 'make -C docker build <xiangshan-v2-rtl|xiangshan-v3-rtl|saturn-rtl>'
	@echo 'make -C docker build-all [BUILD_JOBS=auto|maximum-workers]'
	@echo 'make -C docker launch <image> DIR=<host-directory>'
	@echo 'make -C docker test | smoke <image> | smoke-all'
	@echo 'xiangshan-rtl is an alias for xiangshan-v2-rtl. Images require linux/amd64.'

check-docker:
	@test "$(RTL_PLATFORM)" = linux/amd64 || { echo 'Only linux/amd64 is supported.' >&2; exit 2; }
	@$(DOCKER) buildx version >/dev/null
	@$(DOCKER) info >/dev/null 2>&1 || { echo 'Docker is not running; start Docker before building or running images.' >&2; exit 1; }

build:
	@test -n "$(filter $(NAME),$(NAMES))" || { echo 'Specify a supported image name; see make help.' >&2; exit 2; }
	@$(MAKE) --no-print-directory build-$(NAME)

build-all: build-xiangshan-v2-rtl build-xiangshan-v3-rtl build-saturn-rtl
build-xiangshan-rtl: build-xiangshan-v2-rtl

build-xiangshan-v2-rtl: XS_REV=$(XIANSHAN_V2_REV)
build-xiangshan-v2-rtl: XS_MILL=$(XIANSHAN_V2_MILL)
build-xiangshan-v2-rtl: XS_VERILATOR=$(XIANSHAN_V2_VERILATOR)
build-xiangshan-v2-rtl: XS_IMAGE=$(XIANSHAN_V2_IMAGE)
build-xiangshan-v3-rtl: XS_REV=$(XIANSHAN_V3_REV)
build-xiangshan-v3-rtl: XS_MILL=$(XIANSHAN_V3_MILL)
build-xiangshan-v3-rtl: XS_VERILATOR=$(XIANSHAN_V3_VERILATOR)
build-xiangshan-v3-rtl: XS_IMAGE=$(XIANSHAN_V3_IMAGE)
build-xiangshan-v2-rtl build-xiangshan-v3-rtl: check-docker
	$(DOCKER) buildx build --load --platform "$(RTL_PLATFORM)" \
	    --build-arg XIANSHAN_REPO="$(XIANSHAN_REPO)" --build-arg XIANSHAN_REV="$(XS_REV)" \
	    --build-arg MILL_VERSION="$(XS_MILL)" --build-arg VERILATOR_VERSION="$(XS_VERILATOR)" \
	    --build-arg NEMU_REPO="$(NEMU_REPO)" --build-arg NEMU_REV="$(NEMU_REV)" \
	    --build-arg BUILD_JOBS="$(BUILD_JOBS)" --build-arg XIANSHAN_CONFIG="$(XIANSHAN_CONFIG)" \
	    --build-arg XIANSHAN_EMU_THREADS="$(XIANSHAN_EMU_THREADS)" \
	    --build-arg XIANSHAN_JVM_HEAP="$(XIANSHAN_JVM_HEAP)" \
	    -f "$(DOCKER_DIR)/xiangshan/Dockerfile" -t "$(XS_IMAGE)" "$(DOCKER_DIR)"

build-saturn-rtl: check-docker
	$(DOCKER) buildx build --load --platform "$(RTL_PLATFORM)" \
	    --build-arg CHIPYARD_REPO="$(CHIPYARD_REPO)" --build-arg CHIPYARD_REV="$(CHIPYARD_REV)" \
	    --build-arg SATURN_REV="$(SATURN_REV)" --build-arg SATURN_CONFIG="$(SATURN_CONFIG)" \
	    --build-arg BUILD_JOBS="$(BUILD_JOBS)" --build-arg SATURN_SIM_THREADS="$(SATURN_SIM_THREADS)" \
	    --build-arg SATURN_JVM_HEAP="$(SATURN_JVM_HEAP)" \
	    -f "$(DOCKER_DIR)/saturn/Dockerfile" -t "$(SATURN_IMAGE)" "$(DOCKER_DIR)"

# Shared selection also handles the legacy v2 alias without creating a fourth tag.
define select_image
case "$(NAME)" in \
    xiangshan-rtl|xiangshan-v2-rtl) image="$(XIANSHAN_V2_IMAGE)"; family=xiangshan ;; \
    xiangshan-v3-rtl) image="$(XIANSHAN_V3_IMAGE)"; family=xiangshan ;; \
    saturn-rtl) image="$(SATURN_IMAGE)"; family=saturn ;; \
    *) echo 'Specify a supported image name; see make help.' >&2; exit 2 ;; \
esac
endef

launch: check-docker
	@$(select_image); \
	mkdir -p "$(DIR)/.home" && host_dir=$$(cd "$(DIR)" && pwd -P) && \
	$(DOCKER) run --rm -it --platform "$(RTL_PLATFORM)" \
	    --user "$(HOST_UID):$(HOST_GID)" -e HOME=/host/.home \
	    -v "$$host_dir:/host" -w /host "$$image"

test:
	python3 -m unittest discover -s "$(DOCKER_DIR)/tests" -p 'test_*.py' -v

# Does not rebuild images. Output is isolated in a temporary, host-owned directory.
smoke: check-docker
	@$(select_image); \
	work=$$(mktemp -d "$${TMPDIR:-/tmp}/rvv-smoke.XXXXXX") || exit 1; \
	trap 'rm -rf "$$work"' EXIT HUP INT TERM; \
	$(DOCKER) run --rm --platform "$(RTL_PLATFORM)" \
	    --user "$(HOST_UID):$(HOST_GID)" -e HOME=/tmp \
	    -v "$(DOCKER_DIR)/tests:/tests:ro" -v "$$work:/host" -w /host \
	    "$$image" bash /tests/smoke-image "$$family" && \
	test -s "$$work/smoke.fst" && test -O "$$work/smoke.fst" && \
	$(DOCKER) image inspect "$$image" --format '{{.RepoTags}} size={{.Size}} bytes'

smoke-all:
	@$(MAKE) --no-print-directory smoke NAME=xiangshan-v2-rtl
	@$(MAKE) --no-print-directory smoke NAME=xiangshan-v3-rtl
	@$(MAKE) --no-print-directory smoke NAME=saturn-rtl

$(NAMES):
	@:
