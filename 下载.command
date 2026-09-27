#!/bin/bash
# 命令行版：不想开网页界面时用这个，双击后直接粘贴网址回车
cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
if [ ! -x "$PY" ] || ! "$PY" -c "import yt_dlp" >/dev/null 2>&1; then
  echo "首次使用，正在安装运行环境（需要联网，约 3–5 分钟）…"
  bash "安装环境.sh" || { read -n 1 -s -r -p "安装失败，按任意键关闭…"; exit 1; }
fi

exec "$PY" cli.py
