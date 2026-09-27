#!/bin/bash
# 安装运行环境：独立 Python 3.12 + yt-dlp + 内置 ffmpeg
# 正常情况不用手动跑，「启动下载器」首次启动会自动调用它。
# 环境坏了想重装时，在本文件夹执行： bash 安装环境.sh

set -e
cd "$(dirname "$0")"

BOOT="./.bootstrap"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

# ---------------------------------------------------------------
# 1. 准备 uv
#    用官方独立安装脚本而不是 pip：对方机器上可能没装 Xcode 命令行工具，
#    直接调 python3 会弹框要求安装，卡住整个流程。
# ---------------------------------------------------------------
say "[1/3] 准备环境管理器 uv…"

UV=""
for c in "$BOOT/uv" "$BOOT/bin/uv" "$HOME/.local/bin/uv" "$(command -v uv || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then UV="$c"; break; fi
done

if [ -z "$UV" ]; then
  echo "  正在下载 uv…"
  if ! curl -LsSf https://astral.sh/uv/install.sh \
       | env UV_INSTALL_DIR="$PWD/.bootstrap" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1; then
    echo ""
    echo "  ✗ 下载失败。请检查网络后重试。"
    echo "    如果需要走代理，先在终端执行（把端口换成你自己的）："
    echo "    export https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890"
    exit 1
  fi
  UV="$BOOT/uv"
fi
echo "  uv: $("$UV" --version)"

# ---------------------------------------------------------------
# 2. 独立 Python
#    系统自带的是 3.9，而 yt-dlp 2026 版要求 3.10+，所以单独装一份，
#    不动系统环境，也不需要 sudo。
# ---------------------------------------------------------------
say "[2/3] 安装独立 Python 3.12…"
"$UV" python install 3.12

# ---------------------------------------------------------------
# 3. 虚拟环境 + 依赖
# ---------------------------------------------------------------
say "[3/3] 安装 yt-dlp 和 ffmpeg…"
rm -rf .venv
"$UV" venv --python 3.12 .venv
"$UV" pip install --python .venv/bin/python yt-dlp imageio-ffmpeg

chmod +x 启动下载器.command 下载.command 2>/dev/null || true

say "安装完成"
echo "  yt-dlp $(.venv/bin/yt-dlp --version)"
echo "  现在可以双击「启动下载器.command」使用了。"
