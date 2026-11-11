#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$app_dir"
if [ ! -x .venv/bin/python ]; then
  echo 'Runtime missing. Run Prepare Marine.command once, then start again. Saved data is retained.' >&2
  exit 2
fi
export PYTHONPATH="$app_dir"
exec .venv/bin/python "$app_dir/tools/start_app.py" "$@"
