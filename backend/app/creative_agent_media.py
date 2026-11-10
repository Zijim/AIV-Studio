"""Bounded local evidence preparation. No model-authored commands or paths."""

import base64
import hashlib
import io
import re
import shutil
import subprocess
import time

import av
from PIL import Image, ImageOps

from .errors import AppError


def write_frame(image, folder, index, timestamp, frame_index, crop, max_edge):
    image = ImageOps.exif_transpose(image).convert("RGB")
    original = image.size
    if crop:
        x, y, width, height = crop
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x and 0 < height <= 1 - y):
            raise AppError("media_crop", "裁剪必须为原图范围内的归一化 x、y、宽、高。")
        image = image.crop(
            (
                round(x * image.width),
                round(y * image.height),
                round((x + width) * image.width),
                round((y + height) * image.height),
            )
        )
        if min(image.size) < 1:
            raise AppError("media_crop", "裁剪区域过小。")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    payload = io.BytesIO()
    image.save(payload, "JPEG", quality=80, optimize=True)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{index}.jpg"
    path.write_bytes(payload.getvalue())
    return {
        "file": path.name,
        "timestamp": timestamp,
        "frame_index": frame_index,
        "original_size": list(original),
        "sent_size": list(image.size),
        "crop": crop,
        "sha256": hashlib.sha256(payload.getvalue()).hexdigest(),
    }


def ffmpeg_signals(path, seconds):
    executable = shutil.which("ffmpeg")
    if not executable:
        return {"available": False, "reason": "FFmpeg 不在 PATH，黑场、冻结、静音检测未执行。", "intervals": []}
    filters = {"video": "blackdetect=d=0.2:pix_th=0.1,freezedetect=n=-50dB:d=1", "audio": "silencedetect=n=-50dB:d=0.5"}
    intervals, errors = [], []
    for media, expression in filters.items():
        args = [executable, "-hide_banner", "-nostdin", "-i", str(path), "-t", str(seconds)]
        args += ["-an", "-vf", expression] if media == "video" else ["-vn", "-af", expression]
        try:
            # Output is bounded by the decoded interval and the finite detector log rate.
            result = subprocess.run(
                [*args, "-f", "null", "-"],
                capture_output=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log = result.stderr.decode("utf-8", errors="replace")[-200000:]
            if result.returncode:
                errors.append(f"{media} detection unavailable")
                continue
            for kind, start_label, end_label in (
                ("black", "black_start", "black_end"),
                ("freeze", "freeze_start", "freeze_end"),
                ("silence", "silence_start", "silence_end"),
            ):
                pending = None
                for match in re.finditer(rf"({start_label}|{end_label}):\s*([0-9.]+)", log):
                    if match[1] == start_label:
                        pending = float(match[2])
                    elif pending is not None:
                        intervals.append({"kind": kind, "start": pending, "end": float(match[2])})
                        pending = None
                if pending is not None:
                    intervals.append({"kind": kind, "start": pending, "end": seconds, "open_end": True})
        except (subprocess.TimeoutExpired, OSError):
            errors.append(f"{media} detection interrupted")
    return {
        "available": True,
        "filters": filters,
        "intervals": intervals,
        "errors": errors,
        "coverage_end": seconds,
        "interpretation": "Only candidate signals; interpret against user intent. Not proof of a defect.",
    }


def observe(path, media_type, args, folder, settings):
    start = time.monotonic()
    if len(args.timestamps) > settings.vision_max_images or args.sample_count > settings.vision_max_images:
        raise AppError("media_image_limit", f"单次最多提供 {settings.vision_max_images} 张观察图。")
    if any(t < args.start_seconds for t in args.timestamps):
        raise AppError("media_time", "指定时间不能早于观察区间。")
    if media_type == "image":
        with Image.open(path) as image:
            if image.width * image.height > 50_000_000:
                raise AppError("media_pixels", "图片超过安全解码范围。")
            image.load()
            frames = [write_frame(image, folder, 0, None, 0, args.crop, settings.vision_max_edge)]
        return {
            "technical": {
                "decode": "complete",
                "width": frames[0]["original_size"][0],
                "height": frames[0]["original_size"][1],
            },
            "frames": frames,
            "coverage": "Image pixels; animated input uses first frame only.",
            "limitations": ["No independent aesthetic score."],
        }
    if media_type not in ("video", "audio"):
        raise AppError("media_type", "此素材不是可观察的图片、视频或音频。")
    frames, technical = [], {"decode": "not_started"}
    covered, decoded, reached_eof, error = None, 0, False, None
    try:
        with av.open(str(path)) as container:
            duration = container.duration / av.time_base if container.duration else None
            technical.update(
                duration=duration,
                audio_tracks=len(container.streams.audio),
                required_audio_present=bool(container.streams.audio) if args.require_audio else None,
            )
            end = args.end_seconds if args.end_seconds is not None else duration
            if end is None:
                end = args.start_seconds + min(settings.agent_media_seconds, 10)
            if end <= args.start_seconds or any(t > end for t in args.timestamps):
                raise AppError("media_time", "观察区间或时间点无效。")
            end = min(end, args.start_seconds + settings.agent_media_seconds)
            if any(t > end for t in args.timestamps):
                raise AppError("media_time_limit", "指定时间点超出本次观察区间上限，请缩小区间后分次观察。")
            targets = (
                sorted(args.timestamps)
                if args.timestamps
                else [
                    args.start_seconds + (end - args.start_seconds) * i / max(1, args.sample_count - 1)
                    for i in range(args.sample_count)
                ]
            )
            # One full sequential decode, bounded by wall time and frames. PTS is
            # retained; never infer timestamps from average frame rate.
            stream = container.streams.video[0] if container.streams.video else container.streams.audio[0]
            if container.streams.video:
                if stream.width * stream.height > 50_000_000:
                    raise AppError("media_pixels", "视频单帧超过安全解码范围。")
                technical.update(width=stream.width, height=stream.height)
            previous = None
            for frame in container.decode(stream):
                if (
                    time.monotonic() - start > settings.agent_media_seconds
                    or decoded >= settings.agent_media_max_frames
                ):
                    technical["decode"] = "partial_resource_limit"
                    break
                decoded += 1
                timestamp = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
                if timestamp is None:
                    continue
                covered = timestamp
                if media_type == "video":
                    while targets and timestamp >= targets[0]:
                        targets.pop(0)
                        frames.append(
                            write_frame(
                                frame.to_image(),
                                folder,
                                len(frames),
                                timestamp,
                                decoded - 1,
                                args.crop,
                                settings.vision_max_edge,
                            )
                        )
                    previous = (frame, timestamp)
                # Still decode the entire file, even when samples cover a subrange.
            else:
                reached_eof = True
                technical["decode"] = "complete"
            if media_type == "video" and previous and targets and previous[1] >= args.start_seconds:
                frame, timestamp = previous
                if not frames or timestamp != frames[-1]["timestamp"]:
                    frames.append(
                        write_frame(
                            frame.to_image(),
                            folder,
                            len(frames),
                            timestamp,
                            decoded - 1,
                            args.crop,
                            settings.vision_max_edge,
                        )
                    )
    except AppError:
        raise
    except (av.error.FFmpegError, OSError, ValueError, IndexError) as exc:
        error = type(exc).__name__
        technical["decode"] = "failed"
    technical.update(
        decoded_frames=decoded,
        decoded_through=covered,
        reached_eof=reached_eof,
        error=error,
        audio_decode="not_checked" if media_type == "video" else technical["decode"],
    )
    signals = ffmpeg_signals(
        path, min(technical.get("duration") or settings.agent_media_seconds, settings.agent_media_seconds)
    )
    return {
        "technical": technical,
        "frames": frames,
        "signals": signals,
        "coverage": {
            "requested_start": args.start_seconds,
            "requested_end": args.end_seconds,
            "sample_timestamps": [f["timestamp"] for f in frames],
            "full_decode": reached_eof,
        },
        "limitations": [
            "Sampled images do not verify every frame, motion continuity, physics or lip sync.",
            "Audio existence/silence does not verify dialogue or sound content.",
            "Detection or decoding errors are observation failures, not proof generation is bad.",
        ],
    }


def image_part(path, expected_hash):
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise AppError("evidence_changed", "观察副本校验失败，请重新观察。", 409)
    return {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64," + base64.b64encode(payload).decode(),
        "detail": "high",
    }
