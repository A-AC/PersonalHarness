#!/bin/sh
set -eu

# A fresh volume plus a token in the environment is a headless install:
# bootstrap the config file once so `serve` starts configured. Without
# HARNESS_TOKEN the web wizard takes over instead.
DATA_DIR="${HARNESS_DATA_DIR:-/data}"
if [ ! -f "$DATA_DIR/config.yaml" ] && [ -n "${HARNESS_TOKEN:-}" ]; then
  python -m harness init --non-interactive
fi

exec python -m harness "$@"
