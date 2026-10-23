#!/bin/sh
# Install the runtime, skill, dependencies and native bridge from this bundle.
set -eu
WX_AGENT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${WX_AGENT_PYTHON:-python3}" "$WX_AGENT_ROOT/scripts/install.py" --install-skill --build-bridge "$@"
