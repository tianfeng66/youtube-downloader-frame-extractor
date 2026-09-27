#!/usr/bin/env python3
"""视频抽帧：探测媒体信息、计算抽帧时间点、调 ffmpeg 出图。

时间点算法与「视频抽帧.html」保持一致，换成服务端 ffmpeg 执行，
好处是能正确做 HDR 色调映射，且 4K/AV1 这类重编码不用塞进浏览器。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts", ".wmv"}
MAX_FRAMES = 5000  # 和原工具一致的上限，防止手滑抽出几万张


# --------------------------------------------------------------------------
# 媒体探测（内置 ffmpeg 不带 ffprobe，所以解析 ffmpeg -i 的输出）
# --------------------------------------------------------------------------


@dataclass
class MediaInfo:
    path: str = ""
    name: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    pix_fmt: str = ""
    color: str = ""
    hdr: bool = False
    size: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
# 整行先拿下来再逐项解析：像素格式那一段自带括号和逗号
# （yuv420p10le(tv, bt2020nc/bt2020/smpte2084)），一条正则啃不下来
_VIDLINE_RE = re.compile(r"^\s*Stream #\d+:\d+.*?:\s*Video:\s*(.+)$", re.M)
_AUDLINE_RE = re.compile(r"^\s*Stream #\d+:\d+.*?:\s*Audio:\s*([\w.]+)", re.M)
_CODEC_RE = re.compile(r"^([\w.]+)")
_SIZE_RE = re.compile(r"(?<![\dx])(\d{2,5})x(\d{2,5})(?![\dx])")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_PIXFMT_RE = re.compile(r"\b(yuv\w+|gbr\w+|rgb\w+|gray\w*|bgr\w+|p010\w*|nv12)\b")
_HDR_RE = re.compile(r"\b(smpte2084|arib-std-b67|bt2020\w*)\b")


def probe(ffmpeg: str, path: str) -> MediaInfo:
    p = Path(path).expanduser()
    info = MediaInfo(path=str(p), name=p.name)
    if not p.is_file():
        info.error = "文件不存在"
        return info
    info.size = p.stat().st_size

    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(p)],
        capture_output=True, text=True, errors="replace",
    )
    out = r.stderr or ""

    m = _DUR_RE.search(out)
    if m:
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])

    m = _VIDLINE_RE.search(out)
    if m:
        line = m[1]
        c = _CODEC_RE.match(line)
        info.vcodec = c[1] if c else ""
        s = _SIZE_RE.search(line)
        if s:
            info.width, info.height = int(s[1]), int(s[2])
        f = _FPS_RE.search(line)
        if f:
            info.fps = float(f[1])
        p = _PIXFMT_RE.search(line)
        if p:
            info.pix_fmt = p[1]
        tags = _HDR_RE.findall(line)
        if tags:
            info.color = "/".join(dict.fromkeys(tags))
        # bt2020 色域单独出现不一定是 HDR，只有 PQ / HLG 传输曲线才需要色调映射
        info.hdr = any(t in ("smpte2084", "arib-std-b67") for t in tags)
    else:
        info.error = "没找到视频轨"

    m = _AUDLINE_RE.search(out)
    if m:
        info.acodec = m[1]

    return info


def list_videos(*dirs: str | Path) -> list[dict]:
    """列出候选视频，按修改时间新的在前。"""
    seen: set[str] = set()
    items: list[dict] = []
    for d in dirs:
        p = Path(d).expanduser()
        if not p.is_dir():
            continue
        for f in p.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS and str(f) not in seen:
                seen.add(str(f))
                st = f.stat()
                items.append({
                    "path": str(f), "name": f.name,
                    "size": st.st_size, "mtime": st.st_mtime,
                })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# --------------------------------------------------------------------------
# 时间点计算（与原 HTML 工具的 computeTimes 等价）
# --------------------------------------------------------------------------


def _boxes(f, start: int, end: int):
    """遍历一层 MP4 box，产出 (类型, 内容起点, 内容终点)。"""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size = int.from_bytes(head[:4], "big")
        typ = head[4:8].decode("latin-1")
        body = pos + 8
        if size == 1:                      # 64 位长度
            size = int.from_bytes(f.read(8), "big")
            body = pos + 16
        elif size == 0:                    # 延伸到文件末尾
            size = end - pos
        if size < 8:
            return
        yield typ, body, min(pos + size, end)
        pos += size


def _find(f, start: int, end: int, path: tuple[str, ...]):
    """按路径逐层下钻，例如 ('moov','trak')。返回 [(起点, 终点), ...]"""
    level = [(start, end)]
    for want in path:
        nxt = []
        for s, e in level:
            for typ, bs, be in _boxes(f, s, e):
                if typ == want:
                    nxt.append((bs, be))
        level = nxt
        if not level:
            return []
    return level


def _read_table(f, s: int, e: int) -> bytes:
    f.seek(s)
    return f.read(max(0, e - s))


def _mp4_keyframe_times(path: str) -> list[float] | None:
    """直接读 MP4 的 stss / stts / ctts 算 I 帧时间，不做任何解码。

    对 AV1 这类编码尤其重要：`-skip_frame nokey` 会被解码器忽略，
    真去解一遍 10 分钟 4K AV1 要十几分钟，而读容器索引是毫秒级。
    """
    p = Path(path)
    if p.suffix.lower() not in (".mp4", ".m4v", ".mov"):
        return None
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            traks = _find(f, 0, size, ("moov", "trak"))
            for ts, te in traks:
                # 只要视频轨
                hdlr = _find(f, ts, te, ("mdia", "hdlr"))
                if not hdlr:
                    continue
                hs, he = hdlr[0]
                if _read_table(f, hs + 8, hs + 12) != b"vide":
                    continue

                mdhd = _find(f, ts, te, ("mdia", "mdhd"))
                if not mdhd:
                    continue
                ms, me = mdhd[0]
                raw = _read_table(f, ms, me)
                ver = raw[0]
                timescale = int.from_bytes(
                    raw[20:24] if ver == 1 else raw[12:16], "big")
                if not timescale:
                    continue

                stbl = _find(f, ts, te, ("mdia", "minf", "stbl"))
                if not stbl:
                    continue
                bs, be = stbl[0]

                stss = stts = ctts = None
                for typ, cs, ce in _boxes(f, bs, be):
                    if typ == "stss":
                        stss = _read_table(f, cs, ce)
                    elif typ == "stts":
                        stts = _read_table(f, cs, ce)
                    elif typ == "ctts":
                        ctts = _read_table(f, cs, ce)
                if not stss or not stts:
                    return None       # 分片 MP4 没有 stss，交给兜底方案

                n = int.from_bytes(stss[4:8], "big")
                syncs = [int.from_bytes(stss[8 + i * 4: 12 + i * 4], "big")
                         for i in range(n)]
                if not syncs:
                    return []

                # stts: (count, delta) 展开成每个样本的 DTS
                entries = int.from_bytes(stts[4:8], "big")
                need = max(syncs)
                dts: list[int] = [0]
                t = 0
                off = 8
                for _ in range(entries):
                    if len(dts) > need:
                        break
                    cnt = int.from_bytes(stts[off: off + 4], "big")
                    delta = int.from_bytes(stts[off + 4: off + 8], "big")
                    off += 8
                    for _ in range(cnt):
                        t += delta
                        dts.append(t)
                        if len(dts) > need:
                            break

                # ctts: DTS -> PTS 的偏移（有 B 帧时才有）
                offs: list[int] = [0]
                if ctts:
                    centries = int.from_bytes(ctts[4:8], "big")
                    cver = ctts[0]
                    off = 8
                    for _ in range(centries):
                        if len(offs) > need:
                            break
                        cnt = int.from_bytes(ctts[off: off + 4], "big")
                        raw_off = ctts[off + 4: off + 8]
                        val = int.from_bytes(raw_off, "big",
                                             signed=(cver == 1))
                        off += 8
                        for _ in range(cnt):
                            offs.append(val)
                            if len(offs) > need:
                                break

                out = []
                for s in syncs:
                    if s - 1 >= len(dts):
                        break
                    pts = dts[s - 1] + (offs[s] if s < len(offs) else 0)
                    out.append(round(max(0, pts) / timescale, 4))
                return sorted(set(out))
    except Exception:
        return None
    return None


def keyframe_times(ffmpeg: str, path: str) -> list[float]:
    """列出全部 I 帧时间戳。优先读容器索引，不行才退回 ffmpeg 扫描。"""
    t = _mp4_keyframe_times(path)
    if t is not None:
        return t
    # 兜底：-discard nokey 在解封装层就丢掉非关键帧包，比 -skip_frame 快得多
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-discard", "nokey", "-i", path,
         "-vf", "showinfo", "-an", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    return sorted({
        round(float(t), 4)
        for t in re.findall(r"pts_time:(\d+(?:\.\d+)?)", r.stderr or "")
    })


def compute_times(mode: str, duration: float, params: dict,
                  iframes: list[float] | None = None) -> list[float]:
    end_raw = params.get("end_sec")
    try:
        end_raw = float(end_raw) if end_raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        end_raw = 0.0
    end = min(end_raw, duration) if end_raw > 0 else duration
    if end <= 0:
        return []

    def num(key, default):
        try:
            v = float(params.get(key, default))
        except (TypeError, ValueError):
            return default
        return v

    times: list[float] = []

    if mode == "at":
        # 预览里「截取当前帧」用：直接给定时间点
        for raw in (params.get("times") or []):
            try:
                t = float(raw)
            except (TypeError, ValueError):
                continue
            times.append(min(max(0.0, t), max(0.0, end - 1e-4)))

    elif mode == "iframe":
        start = max(0.0, num("start_sec", 0))
        min_gap = max(0.0, num("min_gap", 0))
        last = float("-inf")
        for t in (iframes or []):
            if t < start - 1e-6 or t > end + 1e-6:
                continue
            if t - last + 1e-9 < min_gap:
                continue
            times.append(min(t, max(0.0, end - 1e-4)))
            last = t

    elif mode == "count":
        n = max(1, int(num("frame_count", 12)))
        include_ends = bool(params.get("include_ends", True))
        if n == 1:
            times.append(min(end / 2, end - 1e-4))
        elif include_ends:
            times = [min(i / (n - 1) * end, end - 1e-4) for i in range(n)]
        else:
            times = [min(i / (n + 1) * end, end - 1e-4) for i in range(1, n + 1)]

    else:  # interval / fps
        if mode == "fps":
            step = 1.0 / max(0.05, num("extract_fps", 1))
            t = max(0.0, num("start_sec", 0))
        else:
            step = max(0.05, num("interval_sec", 1))
            t = max(0.0, num("start_sec", 0))
        while t <= end + 1e-6:
            times.append(min(t, end - 1e-4))
            t += step
            if len(times) > MAX_FRAMES:
                break

    out: list[float] = []
    for t in times:
        if not out or abs(out[-1] - t) > 1e-4:
            out.append(round(max(0.0, t), 4))
    return out[:MAX_FRAMES]


# --------------------------------------------------------------------------
# 抽帧执行
# --------------------------------------------------------------------------

# HDR(PQ/HLG) -> SDR。不做这一步的话，4K HDR 抽出来的图会灰暗发白。
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


def _filters(info: MediaInfo, max_side: int, tonemap: bool) -> list[str]:
    chain: list[str] = []
    if tonemap and info.hdr:
        chain.append(TONEMAP)
    if max_side > 0 and max(info.width, info.height) > max_side:
        chain.append(
            f"scale=w={max_side}:h={max_side}"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    return chain


def _encode_args(fmt: str, quality: float) -> list[str]:
    q = min(1.0, max(0.5, quality))
    if fmt == "png":
        return ["-c:v", "png", "-compression_level", "6"]
    if fmt == "webp":
        return ["-c:v", "libwebp", "-quality", str(int(round(q * 100))), "-lossless", "0"]
    # JPEG：mjpeg 的 -q:v 是 2(最好)~31(最差)，把 0.5~1 映射过去
    qv = int(round(31 - (q - 0.5) / 0.5 * 29))
    return ["-c:v", "mjpeg", "-q:v", str(max(2, min(31, qv))), "-pix_fmt", "yuvj420p"]


def ext_for(fmt: str) -> str:
    return {"png": "png", "webp": "webp"}.get(fmt, "jpg")


@dataclass
class ExtractPlan:
    mode: str = "interval"
    times: list[float] = field(default_factory=list)
    out_dir: Path = Path(".")
    prefix: str = "frame"
    fmt: str = "png"
    quality: float = 0.92
    max_side: int = 0
    tonemap: bool = True
    step: float = 1.0          # interval / fps 模式单程抽帧用
    start: float = 0.0
    end: float = 0.0
    all_keyframes: list[float] = field(default_factory=list)


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stderr or "")[-1500:]


def _run_stream(cmd: list[str], total: int, on_progress=None,
                should_cancel=None) -> tuple[int, str]:
    """跑 ffmpeg 并实时读进度。

    单程解码一个 10 分钟 4K 视频要好几分钟，没有进度条界面会像卡死。
    """
    full = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace")
    cancelled = False
    try:
        for line in proc.stdout or []:
            if should_cancel and should_cancel():
                cancelled = True
                proc.kill()
                break
            line = line.strip()
            if line.startswith("frame=") and total and on_progress:
                try:
                    n = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                on_progress(min(n, total), total)
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = (proc.stderr.read() if proc.stderr else "")[-1500:]
    if cancelled:
        return -1, "已取消"
    return proc.returncode or 0, err


def measure_decode_fps(ffmpeg: str, info: MediaInfo, frames: int = 48) -> float:
    """实测这个片源的解码速度（帧/秒），用来估算抽帧耗时。

    差距很大：1080p H.264 能有几百 fps，4K AV1 软解只有 10 fps 上下，
    不实测的话没法给出有意义的时间预估。
    """
    if info.duration <= 0:
        return 0.0
    at = max(0.0, info.duration / 3)
    t0 = time.time()
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}",
         "-i", str(info.path), "-frames:v", str(frames), "-an", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    dt = time.time() - t0
    if r.returncode != 0 or dt <= 0.01:
        return 0.0
    # 减掉一次进程启动 + 定位的固定开销，剩下的才是纯解码
    pure = max(0.05, dt - 0.25)
    return round(frames / pure, 2)


def estimate_seconds(info: MediaInfo, plan: ExtractPlan, decode_fps: float) -> float:
    """粗略估算抽帧耗时。只求量级对，让人知道是 10 秒还是 10 分钟。"""
    if decode_fps <= 0 or info.fps <= 0:
        return 0.0
    n = len(plan.times)
    if not n:
        return 0.0
    span = max(0.0, (plan.end or info.duration) - plan.start)
    gop = (info.duration / len(plan.all_keyframes)) if plan.all_keyframes else 2.0
    strategy = pick_strategy(info, plan)

    if strategy == "pass":
        return round(span * info.fps / decode_fps, 1)
    if strategy == "keyframe":
        # 只解关键帧，但仍要把范围内的关键帧都过一遍
        in_range = len([k for k in plan.all_keyframes
                        if plan.start <= k <= (plan.end or info.duration)]) or n
        return round(in_range / max(1.0, decode_fps * 0.6) + 1.0, 1)
    # seek：每帧平均要从半个 GOP 前开始解
    per = gop / 2 * info.fps / decode_fps + 0.3
    return round(n * per, 1)


def pick_strategy(info: MediaInfo, plan: ExtractPlan) -> str:
    """决定用「单程解码」还是「逐点定位」。

    单程要把整段范围全部解一遍；逐点定位每帧要从最近的关键帧解到目标帧，
    成本大约是半个 GOP。所以帧越稀疏，逐点越划算。
    4K AV1 这类软解只有 10 fps 左右的片源，选错能差好几倍时间。
    """
    if plan.mode == "iframe":
        return "keyframe"
    if plan.mode in ("count", "at"):
        return "seek"

    span = max(0.0, (plan.end or info.duration) - plan.start)
    n = len(plan.times)
    if not span or not n:
        return "pass"
    gop = (info.duration / len(plan.all_keyframes)) if plan.all_keyframes else 2.0
    return "seek" if n * gop < span else "pass"


def extract(ffmpeg: str, info: MediaInfo, plan: ExtractPlan,
            on_progress=None, should_cancel=None) -> tuple[list[str], str]:
    """返回 (生成的文件列表, 错误信息)。"""
    out_dir = plan.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = ext_for(plan.fmt)
    chain = _filters(info, plan.max_side, plan.tonemap)
    enc = _encode_args(plan.fmt, plan.quality)
    total = len(plan.times)
    if not total:
        return [], "没有可抽取的时间点"

    pattern = str(out_dir / f"{plan.prefix}_%04d.{ext}")
    strategy = pick_strategy(info, plan)

    if strategy == "pass":
        vf = ",".join(chain + [f"fps=1/{plan.step:.6f}"])
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{plan.start:.4f}"]
        if plan.end > plan.start:
            # 多放一个步长再截。fps 滤镜在输入末尾会丢掉最后一个采样点，
            # 不放宽的话 end 正好落在采样点上时就比界面预计少一帧；
            # 放宽可能多出一帧，抽完按计划帧数裁掉。
            cmd += ["-to", f"{min(info.duration, plan.end + plan.step):.4f}"]
        cmd += ["-i", str(info.path), "-vf", vf, "-vsync", "0", "-an"]
        cmd += enc + [pattern]
        code, err = _run_stream(cmd, total + 1, on_progress, should_cancel)
        if code != 0:
            got = _collected(out_dir, plan.prefix, ext)
            return got, err if "已取消" in err else f"ffmpeg 退出码 {code}：{err}"
        got = _collected(out_dir, plan.prefix, ext)
        for extra in got[total:]:
            try:
                Path(extra).unlink()
            except OSError:
                pass
        return got[:total], ""

    elif strategy == "keyframe":
        # -discard nokey 在解封装层就丢掉非关键帧。注意不能用 -skip_frame，
        # AV1 解码器会忽略它，结果是把整片都软解一遍（慢几十倍）。
        allkf = plan.all_keyframes or plan.times
        seek = max([k for k in allkf if k <= plan.start + 1e-6] or [0.0])
        after = [k for k in allkf if k >= seek - 1e-6]

        # -ss 落在关键帧上，所以解码后的帧序号 n 和 after 一一对应，
        # 可以用 select 精确挑出 min_gap 过滤后剩下的那些帧
        idx: list[int] = []
        for t in plan.times:
            for j, k in enumerate(after):
                if abs(k - t) < 1e-3:
                    idx.append(j)
                    break

        chain2 = list(chain)
        in_range = [k for k in after if k <= plan.end + 1e-6]
        if idx and len(idx) < len(in_range):
            expr = "+".join(f"eq(n\\,{i})" for i in idx)
            chain2.insert(0, f"select={expr}")

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-discard", "nokey", "-ss", f"{seek:.4f}"]
        if plan.times:
            # 抽到最后一个目标帧就停，别再白解后面的内容
            cmd += ["-t", f"{max(plan.times) - seek + 1.0:.4f}"]
        cmd += ["-i", str(info.path)]
        if chain2:
            cmd += ["-vf", ",".join(chain2)]
        cmd += ["-vsync", "0", "-an"] + enc + [pattern]
        code, err = _run_stream(cmd, total, on_progress, should_cancel)
        if code != 0:
            got = _collected(out_dir, plan.prefix, ext)
            return got, err if "已取消" in err else f"ffmpeg 退出码 {code}：{err}"

    else:  # seek：逐个时间点精确定位
        vf = ",".join(chain) if chain else None
        files: list[str] = []
        for i, t in enumerate(plan.times, 1):
            if should_cancel and should_cancel():
                return files, "已取消"
            dst = out_dir / f"{plan.prefix}_{i:04d}.{ext}"
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{t:.4f}", "-i", str(info.path), "-frames:v", "1"]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-an"] + enc + [str(dst)]
            code, err = _run(cmd)
            if code != 0 and not dst.exists():
                return files, f"第 {i} 帧失败：{err}"
            files.append(str(dst))
            if on_progress:
                on_progress(i, total)
        return files, ""

    return _collected(out_dir, plan.prefix, ext), ""


def _collected(out_dir: Path, prefix: str, ext: str) -> list[str]:
    return sorted(str(p) for p in out_dir.glob(f"{prefix}_*.{ext}"))


def frames_dir_for(video_path: str, out_root: str | Path | None = None) -> Path:
    """抽帧输出目录：视频同级的「<视频名>_帧」文件夹。"""
    p = Path(video_path).expanduser()
    root = Path(out_root).expanduser() if out_root else p.parent
    return root / f"{p.stem}_帧"


def cleanup_dir(d: Path, prefix: str, ext: str) -> None:
    """重复抽帧前清掉上一轮的同名文件，避免旧帧混进结果。"""
    if not d.is_dir():
        return
    for f in d.glob(f"{prefix}_*.{ext}"):
        try:
            f.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    import sys
    from app import FFMPEG

    if len(sys.argv) < 2:
        print("用法: python extract.py <视频文件>")
        raise SystemExit(1)
    i = probe(FFMPEG, sys.argv[1])
    print(json.dumps(i.as_dict(), ensure_ascii=False, indent=2))
