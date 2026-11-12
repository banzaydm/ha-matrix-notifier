"""Camera video recording helper for Matrix Notifier.

Records real video from the camera's RTSP stream via ffmpeg (15 fps).
Falls back to snapshot-slideshow if no stream source is available.
Shared by the REST VideoView and the rule engine.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time as time_mod
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_FPS = 15


async def _async_get_stream_url(
    hass: HomeAssistant, camera_entity: str
) -> str | None:
    """Return the RTSP (or other) stream URL for a camera entity."""
    # 1) try the standard HA camera helper
    try:
        from homeassistant.components.camera import async_get_stream_source

        url = await async_get_stream_source(hass, camera_entity)
        if url:
            _LOGGER.warning("Got stream source for %s: %s", camera_entity, url.split("@")[-1])
            return url
        _LOGGER.warning("async_get_stream_source returned None for %s", camera_entity)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("async_get_stream_source error for %s: %s", camera_entity, err)

    # 2) fallback: construct RTSP URL from the ONVIF config entry
    return await _async_rtsp_from_onvif_config(hass, camera_entity)


async def _async_rtsp_from_onvif_config(
    hass: HomeAssistant, camera_entity: str
) -> str | None:
    """Build an RTSP URL from the matching ONVIF config entry."""
    import re

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(camera_entity)
    if entry is None or entry.config_entry_id is None:
        return None

    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if config_entry is None or config_entry.domain != "onvif":
        return None

    data = dict(config_entry.data)
    host = data.get("host")
    username = data.get("username")
    password = data.get("password")
    if not host:
        return None

    # try a few well-known RTSP URL patterns for ONVIF cameras
    auth = f"{username}:{password}@" if username and password else ""
    candidates = [
        f"rtsp://{auth}{host}:554/Streaming/Channels/101",  # Hikvision main
        f"rtsp://{auth}{host}:554/live/ch00_0",            # Dahua main
        f"rtsp://{auth}{host}:554/11",                     # some Novicam
        f"rtsp://{auth}{host}:554/1",
        f"rtsp://{auth}{host}:554/main",
        f"rtsp://{auth}{host}:554/stream1",
    ]

    # probe in parallel — first success wins
    async def _try(url: str) -> str | None:
        return url if await _async_probe_rtsp(url, timeout=3.0) else None

    results = await asyncio.gather(*(_try(u) for u in candidates))
    for url in results:
        if url:
            _LOGGER.warning(
                "ONVIF fallback RTSP works for %s: %s",
                camera_entity,
                url.split("@")[-1],
            )
            return url

    _LOGGER.warning(
        "No working RTSP URL found for %s (host=%s)", camera_entity, host
    )
    return None


async def _async_probe_rtsp(url: str, timeout: float = 5.0) -> bool:
    """Quickly probe an RTSP URL with ffprobe — True if it responds."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-t", "1",
            "-f", "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


async def async_record_video(
    hass: HomeAssistant,
    camera_entity: str,
    duration: int = 15,
    fps: int = DEFAULT_FPS,
    quality: str = "original",
) -> tuple[bytes, int, int | None, int | None]:
    """Record a clip from a camera entity.

    quality: "original" | "720p" | "480p"
    Returns (video_bytes, duration_ms, w, h).
    Raises RuntimeError on failure.
    """
    stream_url = await _async_get_stream_url(hass, camera_entity)
    if stream_url:
        try:
            return await _async_record_stream(
                hass, stream_url, camera_entity, duration, fps, quality
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Stream record failed for %s (%s), "
                "falling back to snapshots",
                camera_entity,
                err,
            )
    return await _async_record_snapshots(hass, camera_entity, duration, fps)


async def _async_record_stream(
    hass: HomeAssistant,
    stream_url: str,
    camera_entity: str,
    duration: int,
    fps: int,
    quality: str = "original",
) -> tuple[bytes, int, int | None, int | None]:
    """Record directly from an RTSP/HLS stream using ffmpeg."""
    tmpdir = tempfile.mkdtemp(prefix="mn_video_")
    try:
        out_path = Path(tmpdir) / "out.mp4"

        # quality presets: (scale_height, crf, preset)
        # crf: lower = better quality / bigger size; 0-51, ~23 = default
        presets = {
            "original": (None, 23, "veryfast"),   # no scale, decent compress
            "720p":     (720,  23, "faster"),     # scale to 720p
            "480p":     (480,  26, "fast"),       # scale to 480p, more compress
        }
        scale_h, crf, preset = presets.get(quality, presets["original"])

        cmd = [
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", stream_url,
        ]
        if scale_h:
            # scale height, keep aspect ratio, ensure even width
            cmd += ["-vf", f"scale=-2:{scale_h}"]
        cmd += [
            "-r", str(fps),
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-t", str(duration),
            str(out_path),
        ]
        _LOGGER.warning(
            "Recording %ds @ %d fps q=%s from stream %s",
            duration, fps, quality,
            stream_url.split("@")[-1],
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=duration + 20
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("ffmpeg stream recording timed out") from None

        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1000:
            err = (stderr or b"").decode(errors="replace")[:500]
            raise RuntimeError(f"ffmpeg stream failed: {err}")

        video_bytes, width, height = await hass.async_add_executor_job(
            _read_video_meta, out_path
        )
        if not width:
            width, height = await _async_probe_size(out_path)
        duration_ms = duration * 1000
        _LOGGER.warning(
            "Stream-recoded %s: %d bytes, %dx%d, %dms",
            camera_entity,
            len(video_bytes),
            width or 0,
            height or 0,
            duration_ms,
        )
        return video_bytes, duration_ms, width, height
    finally:
        await hass.async_add_executor_job(shutil.rmtree, tmpdir, True)


async def _async_record_snapshots(
    hass: HomeAssistant,
    camera_entity: str,
    duration: int,
    fps: int,
) -> tuple[bytes, int, int | None, int | None]:
    """Fallback: grab snapshots and encode as a slideshow."""
    from .rules import _async_snapshot

    tmpdir = tempfile.mkdtemp(prefix="mn_video_")
    try:
        frames = 0
        interval = 1.0 / fps
        start_mono = time_mod.monotonic()
        deadline = start_mono + duration

        def _write_frame(path_str: str, content: bytes) -> None:
            Path(path_str).write_bytes(content)

        while time_mod.monotonic() < deadline:
            snap = await _async_snapshot(hass, camera_entity, "frame.jpg")
            if snap:
                frame_path = str(Path(tmpdir) / f"frame_{frames:04d}.jpg")
                await hass.async_add_executor_job(
                    _write_frame, frame_path, snap[0]
                )
                frames += 1
            next_tick = time_mod.monotonic() + interval
            sleep_for = min(next_tick, deadline) - time_mod.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        actual_elapsed = time_mod.monotonic() - start_mono
        if frames == 0:
            raise RuntimeError("No frames captured from camera")

        real_fps = frames / max(actual_elapsed, 0.1)
        out_path = Path(tmpdir) / "out.mp4"
        pattern = str(Path(tmpdir) / "frame_%04d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(real_fps),
            "-i", pattern,
            "-c:v", "libx264",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not out_path.exists():
            err = (stderr or b"").decode(errors="replace")[:500]
            raise RuntimeError(f"ffmpeg failed: {err}")

        video_bytes, width, height = await hass.async_add_executor_job(
            _read_video_meta, out_path
        )
        duration_ms = int(actual_elapsed * 1000)
        _LOGGER.warning(
            "Snapshot-recoded %s: %d frames, %d bytes",
            camera_entity,
            frames,
            len(video_bytes),
        )
        return video_bytes, duration_ms, width, height
    finally:
        await hass.async_add_executor_job(shutil.rmtree, tmpdir, True)


async def _async_probe_size(path: Path) -> tuple[int | None, int | None]:
    """Get video dimensions via ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = out.decode().strip()
        if text:
            w, _, h = text.partition(",")
            return int(w), int(h)
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _read_video_meta(
    out_path: Path,
) -> tuple[bytes, int | None, int | None]:
    """Read the MP4 and (best-effort) its dimensions."""
    video = out_path.read_bytes()
    w = h = None
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        # first frame is still a JPEG on disk for the snapshot path;
        # for stream path try ffprobe
        first = out_path.parent / "frame_0000.jpg"
        if first.exists():
            with PILImage.open(BytesIO(first.read_bytes())) as im:
                w, h = im.size
    except Exception:  # noqa: BLE001
        pass
    return video, w, h
