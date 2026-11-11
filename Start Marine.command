#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$app_dir"
if [ ! -x .venv/bin/python ]; then
  echo '运行环境缺失。请先运行一次 Prepare Marine.command，再重新启动。已保存数据会保留。' >&2
  exit 2
fi
export PYTHONPATH="$app_dir"
exec .venv/bin/python "$app_dir/tools/start_app.py" "$@"
