"""Export the lecture to a single MP4: capture the player as video, concat the
per-beat narration, and mux them together (H.264 + AAC).

Capture is the bottleneck because the player must be recorded in real time. To speed it up
without touching quality or timing accuracy, the lecture is split into contiguous beat ranges
that are captured **in parallel** (each in its own headless browser, via the identical real-time
path), then losslessly stitched. Each segment renders a black mask during its lead-in so the
exact content start can be found with ffmpeg `blackdetect` and trimmed frame-accurately — so
seams line up perfectly and cues stay synced to the narration.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FPS = 30
MAX_SEGMENTS = 6          # cap on parallel headless browsers
SECONDS_PER_SEGMENT = 40  # aim for ~this much lecture per segment


def _run(args: list[str]):
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def build_narration(out_dir: Path, manifest: list[dict]) -> Path:
    """Concatenate beat mp3s in order, inserting silence for any `hold`."""
    work = out_dir / "_audio_concat"
    work.mkdir(exist_ok=True)
    listing = work / "list.txt"
    lines = []
    for i, m in enumerate(manifest):
        lines.append(f"file '{(out_dir / m['file']).resolve()}'")
        hold = m.get("hold", 0)
        if hold and hold > 0:
            sil = work / f"sil{i}.mp3"
            _run(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(hold),
                  "-q:a", "9", str(sil)])
            lines.append(f"file '{sil.resolve()}'")
    listing.write_text("\n".join(lines))
    narration = work / "narration.m4a"
    _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "aac", str(narration)])
    return narration


# ---------------- single-pass capture (fallback) ----------------
def capture_video(out_dir: Path, seg: tuple[int, int] | None = None) -> tuple[Path, float]:
    """Record the player (record mode) to webm. Returns (path, lead_seconds).
    `seg=(a,b)` records only beats [a,b); prior beats are pre-rendered instantly."""
    url = (out_dir / "index.html").as_uri() + "?autoplay=1&record=1"
    if seg is not None:
        url += f"&seg={seg[0]}-{seg[1]}"
    vid_dir = out_dir / ("_video" if seg is None else f"_seg{seg[0]}_{seg[1]}")
    vid_dir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
        ctx = b.new_context(viewport={"width": 1280, "height": 720},
                            record_video_dir=str(vid_dir),
                            record_video_size={"width": 1280, "height": 720})
        pg = ctx.new_page()
        t0 = time.monotonic()
        pg.goto(url)
        pg.wait_for_function("window.__started===true", timeout=60000)
        lead = time.monotonic() - t0
        pg.wait_for_function("window.__done===true", timeout=600000)
        time.sleep(0.4)
        vpath = Path(pg.video.path())
        ctx.close()
        b.close()
    return vpath, lead


def _export_single(out_dir: Path, manifest: list[dict], progress=None) -> Path:
    total = sum((m.get("dur") or 0) + (m.get("hold") or 0) for m in manifest)
    if progress:
        progress(f"recording the {total/60:.0f}-min lecture in real time (~{total/60:.0f} min)…")
    narration = build_narration(out_dir, manifest)
    video, lead = capture_video(out_dir)
    print(f"  captured video ({video.name}), lead-in={lead:.2f}s")
    if progress:
        progress("encoding & finalizing the MP4…")
    mp4 = out_dir / "lecture.mp4"
    _run(["-i", str(video), "-itsoffset", f"{lead:.3f}", "-i", str(narration),
          "-map", "0:v:0", "-map", "1:a:0",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
          "-shortest", str(mp4)])
    return mp4


# ---------------- parallel segment capture ----------------
def _segment_bounds(manifest: list[dict], k: int) -> tuple[list[tuple[int, int]], list[float]]:
    """Split beats into k contiguous ranges balanced by duration."""
    durs = [(m.get("dur") or 0) + (m.get("hold") or 0) for m in manifest]
    total = sum(durs) or 1.0
    target = total / k
    bounds, start, acc = [], 0, 0.0
    for i, d in enumerate(durs):
        acc += d
        if acc >= target and len(bounds) < k - 1 and i + 1 < len(durs):
            bounds.append((start, i + 1))
            start, acc = i + 1, 0.0
    bounds.append((start, len(durs)))
    return bounds, durs


def _capture_one(out_dir: Path, a: int, b: int) -> Path:
    """Capture one segment and save it to a deterministic path; return that path."""
    vpath, _ = capture_video(out_dir, seg=(a, b))
    dest = out_dir / f"_seg_{a}_{b}.webm"
    if dest.exists():
        dest.unlink()
    Path(vpath).replace(dest)
    return dest


def _capture_segment_subproc(out_dir: Path, a: int, b: int, timeout_s: float = 600) -> Path:
    """Run one segment capture in its own process (Playwright sync API isn't thread-safe)."""
    dest = out_dir / f"_seg_{a}_{b}.webm"
    if dest.exists():
        dest.unlink()
    try:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                            "--capture", str(out_dir), str(a), str(b)],
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"segment [{a},{b}) capture timed out after {timeout_s:.0f}s")
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"segment [{a},{b}) capture failed: {r.stderr[-400:]}")
    return dest


def _black_end(path: Path) -> float:
    """Seconds of leading black (the mask lead-in) to trim, via ffmpeg blackdetect."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path),
                        "-vf", "blackdetect=d=0.05:pix_th=0.10", "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    ends = []
    for m in re.finditer(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)", r.stderr):
        if float(m.group(1)) < 1.5:           # a leading black interval (the mask)
            ends.append(float(m.group(2)))
    # +0.04s margin so no black frame survives; fall back to the ~0.5s mask hold if undetected
    return (max(ends) + 0.04) if ends else 0.55


def _trim_segment(vpath: Path, idx: int, out_dir: Path, content_dur: float) -> Path:
    start = _black_end(vpath)
    seg_mp4 = out_dir / f"_segclip{idx}.mp4"
    _run(["-ss", f"{start:.3f}", "-i", str(vpath), "-t", f"{content_dur:.3f}",
          "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
          "-an", str(seg_mp4)])
    return seg_mp4


def export_parallel(out_dir: Path, manifest: list[dict], k: int, progress=None) -> Path:
    def say(msg):
        if progress:
            progress(msg)
        print(f"  {msg}")

    bounds, durs = _segment_bounds(manifest, k)
    total = sum(durs)
    say(f"capturing the {total/60:.0f}-min lecture as {len(bounds)} parallel segments "
        f"(~{max(1, round(total/len(bounds)/60))} min)…")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(bounds)) as ex:
        vpaths = list(ex.map(
            lambda ab: _capture_segment_subproc(out_dir, ab[0], ab[1],
                                                timeout_s=max(240, sum(durs[ab[0]:ab[1]]) * 3 + 90)),
            bounds))
    say(f"captured in {time.monotonic()-t0:.0f}s — trimming & stitching segments…")

    seg_mp4s = []
    for idx, ((a, b), vpath) in enumerate(zip(bounds, vpaths)):
        seg_mp4s.append(_trim_segment(vpath, idx, out_dir, sum(durs[a:b])))

    listing = out_dir / "_segs.txt"
    listing.write_text("\n".join(f"file '{p.resolve()}'" for p in seg_mp4s))
    silent = out_dir / "_video_concat.mp4"
    _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(silent)])

    say("adding narration & finalizing the MP4…")
    narration = build_narration(out_dir, manifest)
    mp4 = out_dir / "lecture.mp4"
    _run(["-i", str(silent), "-i", str(narration), "-map", "0:v:0", "-map", "1:a:0",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
          "-movflags", "+faststart", "-shortest", str(mp4)])
    return mp4


def export(out_dir: Path, progress=None) -> Path:
    out_dir = Path(out_dir).resolve()
    manifest = json.loads((out_dir / "audio_manifest.json").read_text())
    n = len(manifest)
    total = sum((m.get("dur") or 0) + (m.get("hold") or 0) for m in manifest)
    k = max(1, min(n, MAX_SEGMENTS, math.ceil(total / SECONDS_PER_SEGMENT)))
    if k <= 1:
        return _export_single(out_dir, manifest, progress=progress)
    try:
        return export_parallel(out_dir, manifest, k, progress=progress)
    except Exception as e:
        print(f"  parallel export failed ({e}); falling back to single-pass capture")
        if progress:
            progress("re-capturing in single-pass mode (this is slower)…")
        return _export_single(out_dir, manifest)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--capture":
        _capture_one(Path(sys.argv[2]).resolve(), int(sys.argv[3]), int(sys.argv[4]))
    else:
        out = export(ROOT / "out")
        print(f"wrote {out} ({out.stat().st_size//1024} KB)")
