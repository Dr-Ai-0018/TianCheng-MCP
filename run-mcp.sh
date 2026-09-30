#!/usr/bin/env bash
# Run the local stdio server from this checkout. stdout belongs to MCP only.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python="${TIANCHENG_PYTHON:-$project_root/.venv/bin/python}"
if [[ "$python" != /* || ! -x "$python" ]]; then
    printf 'TianCheng Python is unavailable: %s\n' "$python" >&2
    printf 'Run uv sync --frozen --extra test, or set an absolute TIANCHENG_PYTHON.\n' >&2
    exit 2
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$python" -m tiancheng_mcp "$@"
