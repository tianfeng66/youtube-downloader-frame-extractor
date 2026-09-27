#!/bin/bash
# YouTube 下载 + 抽帧 —— 双击启动
# 首次启动会自动装好运行环境，之后直接启动。

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"

# 环境不存在、或者被整个文件夹拷过来导致失效，都重装一次。
# （虚拟环境里记的是绝对路径，换机器一定失效，所以这里实际验证能不能导入）
if [ ! -x "$PY" ] || ! "$PY" -c "import yt_dlp, imageio_ffmpeg" >/dev/null 2>&1; then
  clear
  echo "=============================================="
  echo "  首次使用，正在自动安装运行环境"
  echo "  需要联网，大约 3–5 分钟，只需要这一次"
  echo "=============================================="
  if ! bash "安装环境.sh"; then
    echo ""
    echo "安装失败。把上面的报错内容发给我，或检查网络后重新双击本文件。"
    echo ""
    read -n 1 -s -r -p "按任意键关闭…"
    exit 1
  fi
  echo ""
  echo "环境就绪，正在启动…"
  sleep 1
fi

# yt-dlp 需要跟着 YouTube 的改动更新，后台静默升级，失败也不影响启动
for UV in "./.bootstrap/uv" "./.bootstrap/bin/uv"; do
  if [ -x "$UV" ]; then
    "$UV" pip install --python "$PY" -q --upgrade yt-dlp >/dev/null 2>&1 &
    break
  fi
done

exec "$PY" app.py
