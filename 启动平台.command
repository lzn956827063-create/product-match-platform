#!/bin/zsh
set -e
TASK_PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$TASK_PROJECT_DIR"
if [ -x "$TASK_PROJECT_DIR/.venv/bin/python" ]; then
  TASK_PYTHON="$TASK_PROJECT_DIR/.venv/bin/python"
elif [ -x "$TASK_PROJECT_DIR/../../work/venv/bin/python" ]; then
  TASK_PYTHON="$TASK_PROJECT_DIR/../../work/venv/bin/python"
else
  print '请先按 README.md 创建 Python 环境并安装 requirements.lock。'
  read -k 1 '?按任意键退出'
  exit 1
fi
if "$TASK_PYTHON" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:18765/api/v1/health", timeout=2)' 2>/dev/null; then
  print '商品匹配平台已在 18765 端口运行。'
  open 'http://127.0.0.1:18765'
else
  print '正在启动平台。请在浏览器访问 http://127.0.0.1:18765'
  "$TASK_PYTHON" -m scripts.dev --seed
fi
