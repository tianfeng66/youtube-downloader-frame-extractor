#!/usr/bin/env python3
"""YouTube 视频下载器 —— 本地网页版

启动后在浏览器里粘贴网址即可下载，依赖全部打包在 .venv 里（yt-dlp + ffmpeg）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

import yt_dlp

import extract as fx

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "YouTube下载"

# YouTube 会按客户端指纹拦截请求，默认客户端失败时依次换这些重试
PLAYER_CLIENTS = [None, "android_vr", "tv", "ios", "web_safari", "mweb"]

SUBTITLE_LANGS = ["zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh", "en", "en-US"]


def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = ffmpeg_path()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def load_config() -> dict:
    cfg = {"output_dir": str(DEFAULT_OUTPUT_DIR)}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


CONFIG = load_config()


# --------------------------------------------------------------------------
# 任务状态
# --------------------------------------------------------------------------

TASKS: dict[str, dict] = {}
TASK_ORDER: list[str] = []
LOCK = threading.Lock()
WORK_QUEUE: "queue.Queue[str]" = queue.Queue()


class Cancelled(Exception):
    pass


def new_task(url: str, opts: dict) -> str:
    tid = uuid.uuid4().hex[:12]
    with LOCK:
        TASKS[tid] = {
            "id": tid,
            "kind": "download",
            "url": url,
            "title": url,
            "status": "queued",  # queued / running / done / error / cancelled
            "stage": "排队中",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "size": "",
            "file": "",
            "error": "",
            "opts": opts,
            "cancel": False,
            "created": time.time(),
        }
        TASK_ORDER.append(tid)
    WORK_QUEUE.put(tid)
    return tid


def update(tid: str, **kw) -> None:
    with LOCK:
        if tid in TASKS:
            TASKS[tid].update(kw)


# --------------------------------------------------------------------------
# 下载核心
# --------------------------------------------------------------------------


def build_format(quality: str, compat: bool) -> str:
    """把界面上的画质选项翻译成 yt-dlp 的 format 表达式。

    注意 yt-dlp 把 `A+B/C` 解析成 `(A+B)/C`，所以每个候选必须写成完整表达式
    再用 `/` 串起来，不能在 `+` 右边嵌 `/`。
    """
    if quality == "audio":
        return "bestaudio/best"

    lim = "" if quality == "best" else f"[height<={int(quality)}]"

    # AAC 音轨优先：YouTube 的 Opus 装进 MP4 后 QuickTime 放不出声音，
    # 而 AAC 和 Opus 的听感差距远小于这个代价
    aac = "ba[acodec^=mp4a]"

    if compat:
        # H.264 + AAC，QuickTime 和剪辑软件都能直接用，但上限一般是 1080p
        candidates = [
            f"bv*[vcodec^=avc1]{lim}+{aac}",
            f"bv*[vcodec^=avc1]{lim}+ba",
            f"bv*{lim}+{aac}",
            f"bv*{lim}+ba",
        ]
    else:
        candidates = [f"bv*{lim}+{aac}", f"bv*{lim}+ba"]

    if lim:
        candidates.append(f"b{lim}")
    candidates.append("b")
    return "/".join(candidates)


def build_opts(task: dict, client: str | None) -> dict:
    o = task["opts"]
    out_dir = Path(CONFIG["output_dir"]).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    audio_only = o.get("quality") == "audio"

    # 文件名带上画质，否则同一个视频换画质重下会被当成已存在而跳过
    name = "%(title).130B [%(id)s].%(ext)s" if audio_only \
        else "%(title).130B [%(id)s] %(height)sp.%(ext)s"

    ydl_opts: dict = {
        "format": build_format(o.get("quality", "best"), o.get("compat", False)),
        "outtmpl": str(out_dir / name),
        "noplaylist": not o.get("playlist", False),
        "restrictfilenames": False,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [make_hook(task["id"])],
        "postprocessor_hooks": [make_pp_hook(task["id"])],
        "logger": TaskLogger(task["id"]),
    }

    if FFMPEG:
        ydl_opts["ffmpeg_location"] = FFMPEG

    if audio_only:
        codec = o.get("audio_format", "mp3")
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": "0" if codec == "mp3" else None,
            }
        ]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    if o.get("subs"):
        ydl_opts.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": bool(o.get("auto_subs", True)),
                "subtitleslangs": SUBTITLE_LANGS,
                "subtitlesformat": "srt/vtt/best",
            }
        )
        ydl_opts.setdefault("postprocessors", []).append(
            {"key": "FFmpegSubtitlesConvertor", "format": "srt"}
        )

    if o.get("thumbnail"):
        ydl_opts["writethumbnail"] = True

    if client:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [client]}}

    cookies_from = o.get("cookies_browser")
    if cookies_from:
        ydl_opts["cookiesfrombrowser"] = (cookies_from,)

    proxy = o.get("proxy") or CONFIG.get("proxy")
    if proxy:
        ydl_opts["proxy"] = proxy

    return ydl_opts


class TaskLogger:
    """把 yt-dlp 的日志收进任务，方便界面上显示错误原因。"""

    def __init__(self, tid: str):
        self.tid = tid

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        update(self.tid, error=str(msg)[:500])


def _fmt_size(n) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return ""


def make_hook(tid: str):
    def hook(d):
        with LOCK:
            task = TASKS.get(tid)
            if task and task["cancel"]:
                raise Cancelled()

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            info = d.get("info_dict") or {}
            vcodec = info.get("vcodec") or ""
            stage = "下载音频" if vcodec in ("none", "") else "下载视频"
            update(
                tid,
                status="running",
                stage=stage,
                percent=round(pct, 1),
                speed=_fmt_size(d.get("speed")) + "/s" if d.get("speed") else "",
                eta=f"{int(d['eta'])}s" if d.get("eta") else "",
                size=_fmt_size(total),
            )
        elif status == "finished":
            update(tid, stage="处理中", percent=100.0, speed="", eta="")

    return hook


def make_pp_hook(tid: str):
    names = {
        "FFmpegMerger": "合并音视频",
        "FFmpegExtractAudio": "提取音频",
        "FFmpegVideoRemuxer": "转封装",
        "FFmpegSubtitlesConvertor": "转换字幕",
        "MoveFiles": "整理文件",
    }

    def hook(d):
        if d.get("status") == "started":
            update(tid, stage=names.get(d.get("postprocessor", ""), "后处理"))

    return hook


def run_task(tid: str) -> None:
    with LOCK:
        task = TASKS.get(tid)
        if not task or task["cancel"]:
            if task:
                update(tid, status="cancelled", stage="已取消")
            return

    update(tid, status="running", stage="解析中")
    last_err = ""

    for client in PLAYER_CLIENTS:
        with LOCK:
            if TASKS[tid]["cancel"]:
                update(tid, status="cancelled", stage="已取消")
                return
        try:
            opts = build_opts(task, client)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(task["url"], download=True)
            if info:
                if info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    title = info.get("title") or task["title"]
                    update(tid, title=f"{title}（{len(entries)} 个视频）")
                    fname = ""
                else:
                    update(tid, title=info.get("title") or task["title"])
                    fname = info.get("requested_downloads", [{}])[0].get("filepath", "")
                    if not fname:
                        fname = info.get("_filename", "")
            else:
                fname = ""
            update(
                tid,
                status="done",
                stage="完成" + (f"（客户端 {client}）" if client else ""),
                percent=100.0,
                file=fname,
                error="",
                speed="",
                eta="",
            )
            return
        except Cancelled:
            update(tid, status="cancelled", stage="已取消", speed="", eta="")
            return
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            # 明确不可重试的情况，直接失败，别把 6 个客户端都试一遍
            if any(
                k in last_err
                for k in ("Private video", "members-only", "This video is unavailable in your country")
            ):
                break
            update(tid, stage=f"重试中（换客户端 {client or '默认'} 失败）", percent=0.0)

    update(
        tid,
        status="error",
        stage="失败",
        error=re.sub(r"\x1b\[[0-9;]*m", "", last_err)[:600],
        speed="",
        eta="",
    )


def worker() -> None:
    while True:
        tid = WORK_QUEUE.get()
        try:
            run_task(tid)
        except Exception as e:  # noqa: BLE001
            update(tid, status="error", stage="失败", error=str(e)[:600])
        finally:
            WORK_QUEUE.task_done()


# --------------------------------------------------------------------------
# 抽帧任务（独立队列，不和下载互相排队）
# --------------------------------------------------------------------------

EXTRACT_QUEUE: "queue.Queue[str]" = queue.Queue()
KEYFRAME_CACHE: dict[str, list[float]] = {}
DECODE_FPS_CACHE: dict[str, float] = {}


def keyframes_cached(path: str) -> list[float]:
    key = str(Path(path).resolve())
    if key not in KEYFRAME_CACHE:
        KEYFRAME_CACHE[key] = fx.keyframe_times(FFMPEG, key)
    return KEYFRAME_CACHE[key]


def decode_fps_cached(info: fx.MediaInfo) -> float:
    key = f"{info.path}:{info.size}"
    if key not in DECODE_FPS_CACHE:
        DECODE_FPS_CACHE[key] = fx.measure_decode_fps(FFMPEG, info)
    return DECODE_FPS_CACHE[key]


def new_extract_task(path: str, opts: dict) -> str:
    tid = uuid.uuid4().hex[:12]
    with LOCK:
        TASKS[tid] = {
            "id": tid,
            "kind": "extract",
            "url": path,
            "title": f"抽帧：{Path(path).name}",
            "status": "queued",
            "stage": "排队中",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "size": "",
            "file": "",
            "frames_dir": "",
            "frame_count": 0,
            "error": "",
            "opts": opts,
            "cancel": False,
            "created": time.time(),
        }
        TASK_ORDER.append(tid)
    EXTRACT_QUEUE.put(tid)
    return tid


def run_extract(tid: str) -> None:
    with LOCK:
        task = TASKS.get(tid)
        if not task or task["cancel"]:
            if task:
                update(tid, status="cancelled", stage="已取消")
            return
        o = dict(task["opts"])

    src = o["path"]
    update(tid, status="running", stage="读取视频信息")
    info = fx.probe(FFMPEG, src)
    if info.error:
        update(tid, status="error", stage="失败", error=info.error)
        return

    mode = o.get("mode", "interval")
    # 关键帧表对 MP4 是直接读索引（毫秒级），除了 I 帧模式要用，
    # 也是判断「单程解码还是逐点定位更快」的依据，所以一律先取
    update(tid, stage="读取关键帧索引")
    try:
        iframes = keyframes_cached(src)
    except Exception:
        iframes = []

    times = fx.compute_times(mode, info.duration, o, iframes)
    if not times:
        update(tid, status="error", stage="失败", error="按当前参数算不出任何抽帧点")
        return

    out_dir = Path(o["out_dir"]).expanduser() if o.get("out_dir") \
        else fx.frames_dir_for(src)
    prefix = (o.get("prefix") or "frame").strip() or "frame"
    fmt = o.get("fmt", "png")
    ext = fx.ext_for(fmt)

    if o.get("clean"):
        fx.cleanup_dir(out_dir, prefix, ext)

    # interval / fps 走单程解码，start/end 交给 ffmpeg 的 -ss/-to
    step = 1.0
    if mode == "fps":
        step = 1.0 / max(0.05, float(o.get("extract_fps") or 1))
    elif mode == "interval":
        step = max(0.05, float(o.get("interval_sec") or 1))

    end_raw = float(o.get("end_sec") or 0)
    plan = fx.ExtractPlan(
        mode=mode,
        times=times,
        out_dir=out_dir,
        prefix=prefix,
        fmt=fmt,
        quality=float(o.get("quality") or 0.92),
        max_side=int(o.get("max_side") or 0),
        tonemap=bool(o.get("tonemap", True)),
        step=step,
        start=max(0.0, float(o.get("start_sec") or 0)),
        end=min(end_raw, info.duration) if end_raw > 0 else info.duration,
        all_keyframes=iframes or [],
    )

    hdr_note = "（含 HDR 转换）" if (info.hdr and plan.tonemap) else ""
    strategy = fx.pick_strategy(info, plan)
    how = {"pass": "整段解码", "seek": "逐点定位", "keyframe": "只解关键帧"}[strategy]
    update(tid, stage=f"抽帧中 · {how}{hdr_note}",
           size=f"预计 {len(times)} 帧", percent=0.0)

    def on_progress(i: int, total: int) -> None:
        update(tid, percent=round(i / total * 100, 1),
               stage=f"抽帧中 {i}/{total} · {how}{hdr_note}")

    def cancelled() -> bool:
        with LOCK:
            return bool(TASKS.get(tid, {}).get("cancel"))

    files, err = fx.extract(FFMPEG, info, plan, on_progress, cancelled)

    if cancelled():
        update(tid, status="cancelled", stage="已取消",
               frames_dir=str(out_dir), frame_count=len(files))
        return
    if err and not files:
        update(tid, status="error", stage="失败", error=err)
        return

    update(
        tid,
        status="done",
        stage=f"完成，{len(files)} 帧{hdr_note}",
        percent=100.0,
        frames_dir=str(out_dir),
        frame_count=len(files),
        file=files[0] if files else "",
        size=f"{len(files)} 帧",
        error=err[:400] if err else "",
    )


def extract_worker() -> None:
    while True:
        tid = EXTRACT_QUEUE.get()
        try:
            run_extract(tid)
        except Exception as e:  # noqa: BLE001
            update(tid, status="error", stage="失败", error=str(e)[:600])
        finally:
            EXTRACT_QUEUE.task_done()


# --------------------------------------------------------------------------
# 探测视频信息（不下载）
# --------------------------------------------------------------------------


def probe(url: str, cookies_browser: str = "", proxy: str = "") -> dict:
    last_err = ""
    for client in PLAYER_CLIENTS:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
            "extract_flat": "in_playlist",
            "socket_timeout": 25,
        }
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if cookies_browser:
            opts["cookiesfrombrowser"] = (cookies_browser,)
        if proxy or CONFIG.get("proxy"):
            opts["proxy"] = proxy or CONFIG["proxy"]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                return {
                    "ok": True,
                    "kind": "playlist",
                    "title": info.get("title") or "播放列表",
                    "count": len(entries),
                    "thumbnail": (entries[0].get("thumbnails") or [{}])[-1].get("url", "")
                    if entries
                    else "",
                    "items": [
                        {"title": e.get("title") or e.get("id"), "duration": e.get("duration")}
                        for e in entries[:50]
                    ],
                }
            heights = sorted(
                {f.get("height") for f in (info.get("formats") or []) if f.get("height")},
                reverse=True,
            )
            return {
                "ok": True,
                "kind": "video",
                "title": info.get("title") or "",
                "uploader": info.get("uploader") or "",
                "duration": info.get("duration") or 0,
                "thumbnail": info.get("thumbnail") or "",
                "heights": heights,
                "client": client or "默认",
            }
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            continue
    return {"ok": False, "error": re.sub(r"\x1b\[[0-9;]*m", "", last_err)[:600]}


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s,，、;；]+")


def extract_urls(text: str) -> list[str]:
    """从粘贴的文本里抓出所有网址，顺带修掉两条网址粘在一起的情况。"""
    urls: list[str] = []
    for raw in URL_RE.findall(text or ""):
        # 例如 ...watch?v=AAAhttps://www.youtube.com/watch?v=AAA
        parts = re.split(r"(?=https?://)", raw)
        for p in parts:
            p = p.strip().rstrip(".,;、，；)】]")
            if p.startswith("http") and p not in urls:
                urls.append(p)
    return urls


HOME = Path.home().resolve()
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def safe_path(raw: str, allow_exts: set[str] | None = None) -> Path | None:
    """只放行家目录内、且扩展名符合预期的文件。

    服务只监听 127.0.0.1，但页面里的路径是可以被随意构造的，
    所以仍然限定范围，避免把任意文件读出去。
    """
    if not raw:
        return None
    try:
        p = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not p.is_file() or HOME not in p.parents:
        return None
    if allow_exts and p.suffix.lower() not in allow_exts:
        return None
    return p


def thumb_for(img: Path, size: int = 320) -> Path | None:
    """给帧生成小缩略图并缓存，否则一屏几十张 4K PNG 会把浏览器拖死。"""
    cache = img.parent / ".thumb"
    dst = cache / f"{img.stem}_{size}.jpg"
    if dst.is_file() and dst.stat().st_mtime >= img.stat().st_mtime:
        return dst
    cache.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(img),
         "-vf", f"scale=w={size}:h={size}:force_original_aspect_ratio=decrease",
         "-c:v", "mjpeg", "-q:v", "6", "-pix_fmt", "yuvj420p", str(dst)],
        capture_output=True,
    )
    return dst if r.returncode == 0 and dst.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "YTDL/1.0"
    protocol_version = "HTTP/1.1"  # Range 请求需要，否则视频没法拖进度条

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send_file(self, p: Path, ctype: str, ranged: bool = False) -> None:
        """发送文件，支持 Range（视频拖动进度条要用）。"""
        size = p.stat().st_size
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range") if ranged else None
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                if m[1]:
                    start = int(m[1])
                    end = int(m[2]) if m[2] else size - 1
                else:  # bytes=-N，取末尾 N 字节
                    start = max(0, size - int(m[2] or 0))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206

        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        if ranged:
            self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with p.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            f = BASE_DIR / "ui.html"
            data = f.read_bytes() if f.exists() else b"<h1>ui.html missing</h1>"
            return self._send(200, data, "text/html; charset=utf-8")

        if path == "/api/state":
            with LOCK:
                tasks = [
                    {k: v for k, v in TASKS[t].items() if k not in ("opts", "cancel")}
                    for t in reversed(TASK_ORDER)
                ]
            return self._json(
                {
                    "tasks": tasks,
                    "output_dir": CONFIG["output_dir"],
                    "ffmpeg": bool(FFMPEG),
                    "ytdlp": yt_dlp.version.__version__,
                }
            )

        if path == "/api/videos":
            dirs = [CONFIG["output_dir"], str(Path.home() / "Downloads"),
                    str(Path.home() / "Movies")]
            extra = self.path.split("dir=")[1] if "dir=" in self.path else ""
            if extra:
                dirs.insert(0, unquote(extra))
            return self._json({"videos": fx.list_videos(*dirs)})

        if path == "/media":
            q = parse_qs(self.path.split("?", 1)[-1])
            p = safe_path(q.get("path", [""])[0], fx.VIDEO_EXTS)
            if not p:
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(p.name)[0] or "video/mp4"
            return self._send_file(p, ctype, ranged=True)

        if path in ("/thumb", "/image"):
            q = parse_qs(self.path.split("?", 1)[-1])
            p = safe_path(q.get("path", [""])[0], IMAGE_EXTS)
            if not p:
                return self._send(404, b"not found", "text/plain")
            if path == "/thumb":
                t = thumb_for(p)
                if t:
                    return self._send_file(t, "image/jpeg")
            ctype = mimetypes.guess_type(p.name)[0] or "image/png"
            return self._send_file(p, ctype)

        if path.startswith("/assets/"):
            f = (BASE_DIR / path.lstrip("/")).resolve()
            if f.is_file() and str(f).startswith(str(BASE_DIR)):
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype)

        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._body()

        if path == "/api/probe":
            urls = extract_urls(body.get("url", ""))
            if not urls:
                return self._json({"ok": False, "error": "没识别到网址"})
            return self._json(
                probe(urls[0], body.get("cookies_browser", ""), body.get("proxy", ""))
            )

        if path == "/api/add":
            urls = extract_urls(body.get("urls", ""))
            if not urls:
                return self._json({"ok": False, "error": "没识别到网址"})
            opts = {
                "quality": body.get("quality", "best"),
                "compat": bool(body.get("compat")),
                "subs": bool(body.get("subs")),
                "auto_subs": bool(body.get("auto_subs", True)),
                "thumbnail": bool(body.get("thumbnail")),
                "playlist": bool(body.get("playlist")),
                "audio_format": body.get("audio_format", "mp3"),
                "cookies_browser": body.get("cookies_browser", ""),
                "proxy": body.get("proxy", ""),
            }
            ids = [new_task(u, dict(opts)) for u in urls]
            return self._json({"ok": True, "count": len(ids), "ids": ids})

        if path == "/api/cancel":
            tid = body.get("id", "")
            with LOCK:
                if tid in TASKS:
                    TASKS[tid]["cancel"] = True
                    if TASKS[tid]["status"] == "queued":
                        TASKS[tid].update(status="cancelled", stage="已取消")
            return self._json({"ok": True})

        if path == "/api/clear":
            with LOCK:
                keep = [t for t in TASK_ORDER if TASKS[t]["status"] in ("queued", "running")]
                for t in list(TASK_ORDER):
                    if t not in keep:
                        TASKS.pop(t, None)
                TASK_ORDER[:] = keep
            return self._json({"ok": True})

        if path == "/api/set_dir":
            d = (body.get("dir") or "").strip()
            if d:
                p = Path(d).expanduser()
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": str(e)})
                CONFIG["output_dir"] = str(p)
                save_config(CONFIG)
            return self._json({"ok": True, "output_dir": CONFIG["output_dir"]})

        if path == "/api/pick_dir":
            d = pick_folder()
            if d:
                CONFIG["output_dir"] = d
                save_config(CONFIG)
            return self._json({"ok": True, "output_dir": CONFIG["output_dir"]})

        if path == "/api/reveal":
            target = (body.get("path") or "").strip() or CONFIG["output_dir"]
            p = Path(target).expanduser()
            try:
                if p.is_file():
                    subprocess.Popen(["open", "-R", str(p)])
                else:
                    subprocess.Popen(["open", str(p)])
            except Exception:
                pass
            return self._json({"ok": True})

        # ---- 抽帧 ----------------------------------------------------
        if path == "/api/media_info":
            p = safe_path(body.get("path", ""), fx.VIDEO_EXTS)
            if not p:
                return self._json({"ok": False, "error": "找不到这个视频文件"})
            info = fx.probe(FFMPEG, str(p))
            d = info.as_dict()
            d["ok"] = not info.error
            d["frames_dir"] = str(fx.frames_dir_for(str(p)))
            if not info.error:
                d["decode_fps"] = decode_fps_cached(info)
                d["keyframes"] = len(keyframes_cached(str(p)))
            return self._json(d)

        if path == "/api/plan":
            p = safe_path(body.get("path", ""), fx.VIDEO_EXTS)
            if not p:
                return self._json({"ok": False, "error": "找不到这个视频文件"})
            info = fx.probe(FFMPEG, str(p))
            mode = body.get("mode", "interval")
            iframes = keyframes_cached(str(p))
            times = fx.compute_times(mode, info.duration, body, iframes)

            end_raw = float(body.get("end_sec") or 0)
            step = 1.0
            if mode == "fps":
                step = 1.0 / max(0.05, float(body.get("extract_fps") or 1))
            elif mode == "interval":
                step = max(0.05, float(body.get("interval_sec") or 1))
            plan = fx.ExtractPlan(
                mode=mode, times=times, step=step,
                start=max(0.0, float(body.get("start_sec") or 0)),
                end=min(end_raw, info.duration) if end_raw > 0 else info.duration,
                all_keyframes=iframes,
            )
            return self._json({
                "ok": True,
                "count": len(times),
                "iframe_total": len(iframes) if mode == "iframe" else None,
                "first": times[:6],
                "capped": len(times) >= fx.MAX_FRAMES,
                "strategy": fx.pick_strategy(info, plan),
                "eta": fx.estimate_seconds(info, plan, decode_fps_cached(info)),
            })

        if path == "/api/extract":
            p = safe_path(body.get("path", ""), fx.VIDEO_EXTS)
            if not p:
                return self._json({"ok": False, "error": "找不到这个视频文件"})
            o = dict(body)
            o["path"] = str(p)
            # 同一个视频 + 同一个前缀的任务已经在跑就别再排一个：
            # 两个进程写同一批文件名，结果会互相覆盖
            sig = (str(p), o.get("prefix") or "frame", o.get("out_dir") or "")
            with LOCK:
                for t in TASKS.values():
                    if (t.get("kind") == "extract"
                            and t["status"] in ("queued", "running")
                            and (t["opts"].get("path"),
                                 t["opts"].get("prefix") or "frame",
                                 t["opts"].get("out_dir") or "") == sig):
                        return self._json({
                            "ok": False,
                            "error": "这个视频的同名抽帧任务已经在进行中了",
                        })
            tid = new_extract_task(str(p), o)
            return self._json({"ok": True, "id": tid})

        if path == "/api/frames":
            d = body.get("dir", "")
            try:
                dd = Path(d).expanduser().resolve()
            except OSError:
                return self._json({"ok": False, "error": "目录无效"})
            if not dd.is_dir() or HOME not in dd.parents:
                return self._json({"ok": False, "error": "目录无效"})
            files = sorted(
                (f for f in dd.iterdir()
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTS),
                key=lambda f: f.name,
            )
            limit = int(body.get("limit") or 120)
            return self._json({
                "ok": True,
                "dir": str(dd),
                "total": len(files),
                "frames": [{"path": str(f), "name": f.name,
                            "size": f.stat().st_size} for f in files[:limit]],
            })

        if path == "/api/pick_video":
            f = pick_file()
            return self._json({"ok": bool(f), "path": f})

        if path == "/api/quit":
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return self._json({"ok": True})

        return self._json({"ok": False, "error": "unknown endpoint"}, 404)


def _osascript(script: str) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=300
        )
        out = r.stdout.strip()
        return out if r.returncode == 0 else ""
    except Exception:
        return ""


def pick_folder() -> str:
    """用 macOS 原生选择框挑目录。"""
    out = _osascript(
        'set d to choose folder with prompt "选择下载保存位置"\nPOSIX path of d'
    )
    return out.rstrip("/") if out else ""


def pick_file() -> str:
    """挑一个视频文件（下载目录之外的也能抽帧）。"""
    types = '{"mp4","m4v","mov","mkv","webm","avi","public.movie"}'
    return _osascript(
        f'set f to choose file with prompt "选择要抽帧的视频" of type {types}\n'
        "POSIX path of f"
    )


def main() -> None:
    port = int(os.environ.get("YTDL_PORT", "8733"))
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    else:
        print("找不到可用端口", file=sys.stderr)
        sys.exit(1)

    for _ in range(2):  # 同时最多跑 2 个下载，太多容易被限速
        threading.Thread(target=worker, daemon=True).start()
    # 抽帧单独一条队列：ffmpeg 抽帧很吃 CPU，串行跑，也不和下载抢位置
    threading.Thread(target=extract_worker, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print("=" * 52)
    print("  YouTube 下载 + 抽帧工具已启动")
    print(f"  界面地址: {url}")
    print(f"  yt-dlp:   {yt_dlp.version.__version__}")
    print(f"  ffmpeg:   {'内置可用' if FFMPEG else '缺失（只能下单文件格式）'}")
    print(f"  保存目录: {CONFIG['output_dir']}")
    print("  关闭窗口或按 Ctrl+C 退出")
    print("=" * 52)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
