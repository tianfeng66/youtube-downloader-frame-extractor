#!/bin/bash
# 打出可以发给别人的压缩包。
# 只带源码和文档，不带 .venv / .bootstrap —— 虚拟环境里存的是本机绝对路径，
# 拷到别人电脑上必然失效，对方首次启动时会自动重装。

set -e
cd "$(dirname "$0")"

NAME="YouTube下载抽帧工具"
VER="${1:-1.0}"
STAGE="/tmp/${NAME}-pack"
OUT="$PWD/${NAME}-v${VER}.zip"

# 先打到临时位置，成功后才覆盖正式产物，避免构建失败时把上一个能用的包也删掉
TMPZIP="$STAGE/out.zip"

rm -rf "$STAGE"
mkdir -p "$STAGE/$NAME"

# 要带的文件（白名单，避免把测试产物、日志、配置一起发出去）
FILES=(
  "① 先看这里.txt"
  "使用说明.txt"
  "app.py"
  "extract.py"
  "cli.py"
  "ui.html"
  "启动下载器.command"
  "下载.command"
  "安装环境.sh"
)
for f in "${FILES[@]}"; do
  cp "$f" "$STAGE/$NAME/"
done

chmod +x "$STAGE/$NAME/"*.command "$STAGE/$NAME/安装环境.sh"

cd "$STAGE"
# -x 排除 macOS 的元数据垃圾文件。
# 中文名在 unzip -l 里会显示成乱码：macOS 自带的 zip 没编译 Unicode 支持，
# 不会设 UTF-8 标志位（-UN=UTF8 会直接报错）。文件名本身存的就是 UTF-8 字节，
# 访达和「归档实用工具」解压出来是正确的，本包只发 macOS，不必处理。
zip -r -q "$TMPZIP" "$NAME" -x "*.DS_Store" -x "__MACOSX*"

rm -f "$OUT"
mv "$TMPZIP" "$OUT"

echo "打包完成：$OUT"
echo "大小：$(du -h "$OUT" | cut -f1)"
echo ""
echo "包内结构："
# macOS 的 head 不支持 -n -2，用 unzip -Z1 直接列条目
unzip -Z1 "$OUT" | sed 's/^/  /'
