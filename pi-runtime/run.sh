#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cli="$root/.runtime/pi/source/packages/coding-agent/dist/bundle/cli.js"
if [ ! -f "$cli" ]; then
    echo "Pi is not installed. Run make studio-pi." >&2
    exit 1
fi
state="$root/.runtime/studio/pi"
mkdir -p "$state/home" "$state/agent" "$state/sessions"
chmod 700 "$state" "$state/home" "$state/agent" "$state/sessions"
exec env -i PATH="$PATH" HOME="$state/home" PI_CODING_AGENT_DIR="$state/agent" PI_OFFLINE=1 \
    node "$cli" --mode rpc --offline --no-tools --no-extensions --no-mcp \
    --no-skills --no-themes --no-context-files --no-prompt-templates --no-approve \
    --session-dir "$state/sessions" "$@"
