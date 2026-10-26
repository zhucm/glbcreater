#!/bin/sh
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2

# Edit these seven settings before running this file.
INPUT_DIR="$SCRIPT_DIR/images"
OUTPUT_DIR="$SCRIPT_DIR/output"
WIDTH_CM=8
THICKNESS_CM=0.5
BLENDER_EXE=/usr/bin/blender
OVERWRITE=0
RECOVER_CARTOON_COLORS=1

PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_EXE" ]; then
    printf 'Missing Python environment. See README.md for installation.\n' >&2
    exit 2
fi
if [ ! -d "$INPUT_DIR" ]; then
    printf 'Input directory does not exist: %s\n' "$INPUT_DIR" >&2
    exit 2
fi
if [ ! -x "$BLENDER_EXE" ]; then
    printf 'Blender does not exist or is not executable: %s\n' "$BLENDER_EXE" >&2
    exit 2
fi
case "$OVERWRITE" in
    0) set -- ;;
    1) set -- "$@" --overwrite ;;
    *) printf 'OVERWRITE must be 0 or 1.\n' >&2; exit 2 ;;
esac
case "$RECOVER_CARTOON_COLORS" in
    0) ;;
    1) set -- "$@" --recover-cartoon-colors ;;
    *) printf 'RECOVER_CARTOON_COLORS must be 0 or 1.\n' >&2; exit 2 ;;
esac
if ! command -v node >/dev/null 2>&1; then
    printf 'Node.js is not on PATH.\n' >&2
    exit 2
fi
if [ ! -f "$SCRIPT_DIR/node_modules/meshoptimizer/package.json" ] ||
   [ ! -f "$SCRIPT_DIR/node_modules/three/package.json" ]; then
    printf 'Node dependencies are missing. Run npm install in this directory.\n' >&2
    exit 2
fi

printf 'Input: %s\nOutput: %s\nWidth: %s cm  Thickness: %s cm\nBlender: %s\nOverwrite: %s\nRecover cartoon colors: %s\n\n' \
    "$INPUT_DIR" "$OUTPUT_DIR" "$WIDTH_CM" "$THICKNESS_CM" "$BLENDER_EXE" "$OVERWRITE" "$RECOVER_CARTOON_COLORS"
cd "$SCRIPT_DIR" || exit 2
"$PYTHON_EXE" -m glb_tools --input "$INPUT_DIR" --output "$OUTPUT_DIR" \
    --width-cm "$WIDTH_CM" --thickness-cm "$THICKNESS_CM" --blender "$BLENDER_EXE" "$@"
status=$?
if [ "$status" -eq 0 ]; then
    printf '\nFinished successfully.\n'
else
    printf '\nFinished with exit code %s.\n' "$status" >&2
fi
exit "$status"
