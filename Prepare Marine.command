#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then uv_bin="$HOME/.local/bin/uv"; fi
if [ -z "$uv_bin" ]; then echo 'Install uv and Python 3.12, then run Prepare Marine.command.' >&2; exit 2; fi
cd "$app_dir"
python_bin=$("$uv_bin" python find --system --no-project 3.12)
export PYTHONPATH="$app_dir"
exec "$python_bin" "$app_dir/tools/runtime_environment.py" --prepare "$uv_bin"
