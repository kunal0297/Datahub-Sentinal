#!/usr/bin/env sh
set -eu

# The sentinel package itself (src/sentinel) is installed from the checked-
# out workspace at container runtime, not baked into the image at build
# time — see Dockerfile's comment for why the build context can't reach it.
# GITHUB_WORKSPACE is the mounted checkout of whichever repo invoked this
# action, and --no-deps skips re-resolving dependencies already installed
# at build time (registers only the sentinel package itself).
pip install --no-cache-dir --no-deps -e "${GITHUB_WORKSPACE}"

exec python -m sentinel.agents.pr_impact.action_entrypoint
