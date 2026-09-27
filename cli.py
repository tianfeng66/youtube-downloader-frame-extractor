#!/usr/bin/env python3
"""命令行版下载器：粘贴网址回车即下，或 python cli.py <网址> ..."""

from __future__ import annotations

import sys
from pathlib import Path

from app import CONFIG, FFMPEG, PLAYER_CLIENTS, build_format, extract_urls

import yt_dlp

QUALITY_MENU = [
    ("1", "最高画质（自动，含 4K/8K）", "best", False),
    ("2", "1080p", "1080", False),
    ("3", "1080p 兼容模式 H.264（Mac 直接播放）", "1080", True),
    ("4", "720p", "720", False),
    ("5", "仅音频 MP3", "audio", False),
]


def download(url: str, quality: str, compat: bool, out_dir: Path) -> bool:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = "%(title).130B [%(id)s].%(ext)s" if quality == "audio" \
        else "%(title).130B [%(id)s] %(height)sp.%(ext)s"

    for client in PLAYER_CLIENTS:
        opts = {
            "format": build_format(quality, compat),
            "outtmpl": str(out_dir / name),
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "socket_timeout": 30,
        }
        if FFMPEG:
            opts["ffmpeg_location"] = FFMPEG
        if quality == "audio":
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": "0"}
            ]
        else:
            opts["merge_output_format"] = "mp4"
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if CONFIG.get("proxy"):
            opts["proxy"] = CONFIG["proxy"]

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  × 客户端 {client or '默认'} 失败：{str(e)[:160]}")
    return False


def main() -> None:
    out_dir = Path(CONFIG["output_dir"]).expanduser()
    args = [a for a in sys.argv[1:] if a.startswith("http")]

    if args:
        quality, compat = "best", False
        urls = args
    else:
        print("=" * 54)
        print("  YouTube 视频下载器（命令行版）")
        print(f"  保存到：{out_dir}")
        print("=" * 54)
        for key, label, *_ in QUALITY_MENU:
            print(f"  [{key}] {label}")
        pick = input("\n选择画质（直接回车 = 最高画质）： ").strip() or "1"
        match = next((m for m in QUALITY_MENU if m[0] == pick), QUALITY_MENU[0])
        quality, compat = match[2], match[3]
        raw = input("粘贴视频网址（多个用空格隔开）： ").strip()
        urls = extract_urls(raw)

    if not urls:
        print("没识别到网址。")
        return

    ok = 0
    for i, u in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] {u}")
        if download(u, quality, compat, out_dir):
            ok += 1
            print("  ✓ 完成")

    print(f"\n全部结束：成功 {ok} / {len(urls)}，保存在 {out_dir}")
    if not args:
        input("按回车关闭…")


if __name__ == "__main__":
    main()
