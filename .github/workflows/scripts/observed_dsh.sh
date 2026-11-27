#!/bin/sh
set -eu
probe_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
export NODE_OPTIONS="--require=$probe_dir/review_wire_probe.cjs"
exec dsh "$@"
