#!/usr/bin/env python3
"""
Single-file webhook listener for freemkv's autorip service.

Configure autorip's `webhook_urls` setting to include:
    http://<this-host>:9000/webhook

`rip_complete` carries format/year metadata but a path that may still move;
`move_complete` carries the final path but no metadata. This caches
rip_complete's metadata (keyed by title) and consumes it when the matching
move_complete arrives, so the transcode profile (DVD/Blu-ray/UHD bitrate
ceiling) can be picked correctly against the final file location.

Quality (global_quality) is intentionally constant across all disc types —
it targets perceptual fidelity, not a data rate, so it doesn't need to vary
by resolution. Only maxrate/bufsize (absolute bitrate ceilings) vary by tier.

Logs: the last LOG_BUFFER_LINES lines are served at /logs (phone-friendly,
auto-refreshing, newest first). ffmpeg progress is logged every
PROGRESS_INTERVAL_SECS while a transcode runs.
"""
import collections
import logging
import os
import queue
import subprocess
import threading
import time
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

# --- logging: stdout (docker logs) + in-memory ring buffer for /logs ---------

LOG_BUFFER_LINES = int(os.environ.get("LOG_BUFFER_LINES", "2000"))
log_buffer: "collections.deque[str]" = collections.deque(maxlen=LOG_BUFFER_LINES)


class BufferHandler(logging.Handler):
    """Keeps the most recent log records in memory for the /logs page."""

    def emit(self, record: logging.LogRecord) -> None:
        log_buffer.append(self.format(record))


_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
logging.basicConfig(level=logging.INFO, format=_fmt._fmt)
_buf_handler = BufferHandler()
_buf_handler.setFormatter(_fmt)
logging.getLogger().addHandler(_buf_handler)

log = logging.getLogger("autorip-webhook")

app = FastAPI(title="autorip -> QSV transcode webhook")

# --- settings ----------------------------------------------------------------

OUTPUT_DIR = Path(os.environ.get("TRANSCODE_OUTPUT_DIR", "/transcoded"))
GLOBAL_QUALITY = os.environ.get("GLOBAL_QUALITY", "24")  # shared across all tiers — see module docstring
AUDIO_CODEC = os.environ.get("AUDIO_CODEC", "copy")
DELETE_SOURCE = os.environ.get("DELETE_SOURCE", "false").lower() == "true"
WAIT_FOR_FILE_SECS = int(os.environ.get("WAIT_FOR_FILE_SECS", "10"))
PROGRESS_INTERVAL_SECS = int(os.environ.get("PROGRESS_INTERVAL_SECS", "60"))

# UHD discs mastered before this year are treated as upscaled-from-2K and
# downgraded to the bluray bitrate ceiling — override if you find exceptions.
UHD_MASTER_YEAR_CUTOFF = int(os.environ.get("UHD_MASTER_YEAR_CUTOFF", "1995"))

# How long a rip_complete's cached metadata is kept waiting for a matching
# move_complete before being pruned (covers movie_dir/tv_dir not configured,
# where move_complete may never fire at all).
PENDING_TTL_SECS = int(os.environ.get("PENDING_TTL_SECS", str(6 * 3600)))

# Per-tier absolute bitrate ceilings — only knob that varies by disc type.
PROFILES = {
    "dvd": {
        "maxrate": os.environ.get("DVD_MAXRATE", "2M"),
        "bufsize": os.environ.get("DVD_BUFSIZE", "4M"),
        "videometa": "H.265 480p",
    },
    "hd dvd": {
        "maxrate": os.environ.get("HD_DVD_MAXRATE", "4M"),
        "bufsize": os.environ.get("HD_DVD_BUFSIZE", "6M"),
        "videometa": "H.265 1080p",
    },
    "bluray": {
        "maxrate": os.environ.get("BLURAY_MAXRATE", "8M"),
        "bufsize": os.environ.get("BLURAY_BUFSIZE", "16M"),
        "videometa": "H.265 1080p",
    },
    "uhd": {
        "maxrate": os.environ.get("UHD_MAXRATE", "16M"),
        "bufsize": os.environ.get("UHD_BUFSIZE", "32M"),
        "videometa": "H.265 4K HDR10",
    },
}

task_queue: "queue.Queue[tuple[str, str, dict]]" = queue.Queue()

# title -> {"format": ..., "year": ..., "cached_at": ...}
pending_rips: dict[str, dict] = {}
pending_lock = threading.Lock()


def _prune_stale_pending() -> None:
    cutoff = time.monotonic() - PENDING_TTL_SECS
    stale = [t for t, meta in pending_rips.items() if meta["cached_at"] < cutoff]
    for t in stale:
        log.warning("pruning stale rip_complete metadata for %r (never got a move_complete)", t)
        pending_rips.pop(t, None)


def resolve_profile(fmt: str | None, year: int | None) -> tuple[str, dict]:
    fmt_norm = (fmt or "").strip().lower()
    if fmt_norm == "dvd":
        tier = "dvd"
    elif fmt_norm in ("uhd", "4k", "4k uhd", "uhd bd", "ultra hd"):
        tier = "uhd"
    elif fmt_norm in ("bluray", "blu-ray", "blu ray", "bd"):
        tier = "bluray"
    else:
        log.warning("unrecognized format %r, defaulting to bluray profile", fmt)
        tier = "bluray"

    if tier == "uhd" and year is not None and year < UHD_MASTER_YEAR_CUTOFF:
        log.info(
            "UHD disc from %s (< %s) is likely an upscaled/older master — "
            "using bluray bitrate ceiling instead of full UHD",
            year, UHD_MASTER_YEAR_CUTOFF,
        )
        tier = "bluray"

    return tier, PROFILES[tier]


# --- ffmpeg progress ---------------------------------------------------------

def probe_duration(path: Path) -> float | None:
    """Total length in seconds, used to turn progress into a percentage."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError):
        return None


def _num(value: str | None) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0  # ffmpeg reports "N/A" early in the run


def format_progress(stats: dict, duration: float | None, elapsed: float) -> str:
    done = _num(stats.get("out_time_us") or stats.get("out_time_ms")) / 1_000_000
    if duration and done > 0:
        pct = f"{100 * done / duration:.1f}%"
        eta_s = elapsed * (duration - done) / done
        eta = f"{int(eta_s // 3600)}h{int(eta_s % 3600 // 60):02d}m"
    else:
        pct, eta = "?", "?"
    size_mb = _num(stats.get("total_size")) / 1e6
    return (
        f"{pct} eta={eta} pos={done:.0f}s fps={stats.get('fps', '?')} "
        f"speed={stats.get('speed', '?')} size={size_mb:.0f}MB "
        f"bitrate={stats.get('bitrate', '?')}"
    )


def run_ffmpeg(cmd: list[str], duration: float | None) -> tuple[int, str]:
    """Runs ffmpeg, logging progress as it goes. Returns (returncode, stderr_tail)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    stderr_tail: "collections.deque[str]" = collections.deque(maxlen=200)

    def drain_stderr() -> None:
        # must be drained concurrently or ffmpeg can block on a full pipe
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_tail.append(line.rstrip())
            if "ratecontrol" in line.lower():
                log.info("ffmpeg: %s", line.strip())

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    start = time.monotonic()
    last_log = 0.0
    stats: dict[str, str] = {}
    for line in proc.stdout:  # type: ignore[union-attr]
        key, _, value = line.strip().partition("=")
        stats[key] = value
        if key == "progress":  # last key of each progress block
            now = time.monotonic()
            if value == "end" or now - last_log >= PROGRESS_INTERVAL_SECS:
                last_log = now
                log.info("progress: %s", format_progress(stats, duration, now - start))

    proc.wait()
    t.join(timeout=5)
    return proc.returncode, "\n".join(stderr_tail)


# --- transcode ---------------------------------------------------------------

def transcode(input_path: str, title: str, meta: dict) -> None:
    src = Path(input_path)

    if not src.exists():
        for _ in range(WAIT_FOR_FILE_SECS):
            time.sleep(1)
            if src.exists():
                break
        else:
            log.error("input not found after waiting %ss, skipping: %s", WAIT_FOR_FILE_SECS, src)
            return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUTPUT_DIR / f"{src.stem}.mkv"

    tier, profile = resolve_profile(meta.get("format"), meta.get("year"))
    duration = probe_duration(src)

    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-nostats", "-progress", "pipe:1",
        "-i", str(src),
        "-max_interleave_delta", "0",
        "-map", "0:v:0",
        "-map", "0:a",
        "-map", "0:s?",
        "-c", "copy",
        "-c:v", "hevc_qsv",
        "-preset", "veryslow",
        "-scenario", "archive",
        "-low_power", "0",
        "-global_quality", GLOBAL_QUALITY,
        "-look_ahead_depth", "40",
        "-extbrc", "1",
        "-mbbrc", "1",
        "-adaptive_i", "1",
        "-adaptive_b", "1",
        "-b_strategy", "1",
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-metadata:s:v", "title=" + profile["videometa"],
        str(dst),
    ]

    log.info(
        "transcoding %r [tier=%s format=%r year=%s duration=%s]: %s -> %s",
        title, tier, meta.get("format"), meta.get("year"),
        f"{duration:.0f}s" if duration else "unknown", src, dst,
    )
    log.info("command: %s", " ".join(cmd))

    returncode, stderr_tail = run_ffmpeg(cmd, duration)

    if returncode != 0:
        log.error("ffmpeg failed for %s (exit %s):\n%s", src, returncode, stderr_tail[-4000:])
        return

    log.info("done: %s", dst)
    if DELETE_SOURCE:
        src.unlink(missing_ok=True)


def worker() -> None:
    while True:
        input_path, title, meta = task_queue.get()
        try:
            transcode(input_path, title, meta)
        except Exception:
            log.exception("unexpected error transcoding %s", input_path)
        finally:
            task_queue.task_done()


threading.Thread(target=worker, daemon=True).start()


# --- HTTP --------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    event = payload.get("event")
    log.info("received event=%s payload=%s", event, payload)

    if event == "rip_complete":
        title = payload.get("title", "unknown")
        with pending_lock:
            _prune_stale_pending()
            pending_rips[title] = {
                "format": payload.get("format"),
                "year": payload.get("year"),
                "cached_at": time.monotonic(),
            }
        return {"ok": True, "cached_metadata_for": title}

    if event != "move_complete":
        return {"ok": True, "skipped": event}

    output_path = payload.get("output_path")
    if not output_path:
        return {"ok": False, "error": "no output_path in payload"}

    title = payload.get("title", "unknown")
    with pending_lock:
        meta = pending_rips.pop(title, None)

    if meta is None:
        log.warning(
            "no cached rip_complete metadata for %r — defaulting to bluray profile "
            "(webhook may have restarted between rip_complete and move_complete)",
            title,
        )
        meta = {"format": None, "year": None}

    task_queue.put((output_path, title, meta))
    return {"ok": True, "queued": output_path, "tier_input": meta, "queue_depth": task_queue.qsize()}


@app.get("/logs")
async def logs(n: int = 300, raw: bool = False):
    # newest first, so the latest lines are at the top of the page on a phone
    lines = list(log_buffer)[-n:][::-1]
    text = "\n".join(lines)

    if raw:
        return PlainTextResponse(text)

    return HTMLResponse(
        "<!doctype html><html><head>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="10">'
        "<title>transcode logs</title>"
        "<style>body{margin:0;background:#111;color:#ddd;font:12px/1.4 monospace}"
        "pre{margin:0;padding:8px;white-space:pre-wrap;word-break:break-word}</style>"
        f"</head><body><pre>{escape(text)}</pre></body></html>"
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}
