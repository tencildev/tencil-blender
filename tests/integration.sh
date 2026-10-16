#!/usr/bin/env bash
# The add-on against a real daemon from a Tencil checkout and its fake T3 Code.
#   TENCIL_REPO=../tencil BLENDER=/Applications/Blender.app/Contents/MacOS/Blender tests/integration.sh
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
tencil_repo="$(cd "${TENCIL_REPO:-$here/../tencil}" && pwd)"
blender="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
fake_port=47490
daemon_port=47491

work="$(mktemp -d)"
pids=()
cleanup() {
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; done
    rm -rf "$work"
}
trap cleanup EXIT

project="$work/scene-project"
mkdir -p "$project/scenes"
git -C "$project" init -q
"$blender" -b --factory-startup --python-expr \
    "import bpy; bpy.ops.wm.save_as_mainfile(filepath='$project/scenes/cube.blend')" >/dev/null

(cd "$tencil_repo/apps/playground" &&
    FAKE_T3_PORT=$fake_port node --experimental-strip-types --no-warnings e2e/fake-t3-server.ts) \
    >"$work/fake-t3.log" 2>&1 &
pids+=($!)
TENCIL_T3_URL="http://127.0.0.1:$fake_port" TENCIL_T3_TOKEN=test-token XDG_CONFIG_HOME="$work/config" \
    node "$tencil_repo/packages/tencil/dist/cli.mjs" daemon --port $daemon_port >"$work/daemon.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 100); do
    curl -sf "http://127.0.0.1:$daemon_port/health" >/dev/null &&
        curl -sf "http://127.0.0.1:$fake_port/.well-known/t3/environment" >/dev/null && break
    sleep 0.1
done

if ! TENCIL_PORT=$daemon_port EXPECTED_ROOT="$project" "$blender" -b --factory-startup \
    "$project/scenes/cube.blend" --python-exit-code 1 --python "$here/tests/blender_integration.py" \
    2>&1 | grep -vE "^(Blender |Read blend|Warning: .*color management)"; then
    echo "--- daemon log"; cat "$work/daemon.log"
    echo "--- fake T3 Code log"; cat "$work/fake-t3.log"
    exit 1
fi
