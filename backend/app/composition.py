"""Timeline composition for autonomous short films.

Validation happens before any encoding: every clip must decode, share one
geometry and frame rate, carry native audio, and fit the declared ranges.
The encoder itself is :func:`app.production_media.compose`, already verified on
the 30-second sample production, so both paths stay frame-accurate.
"""

from pathlib import Path

import av

from .production_media import compose as encode


class CompositionError(ValueError):
    """A refused composition with a stable code the agent tool can report."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def probe_clip(path):
    """Read geometry, frame rate, frame count and audio presence without encoding."""
    path = Path(path)
    try:
        source = av.open(str(path))
    except Exception as exc:
        raise CompositionError("not_video", "素材无法作为视频打开，不能参与合成。", {"error": str(exc)}) from exc
    with source:
        streams = source.streams.video
        if not streams:
            raise CompositionError("not_video", "素材不是视频，不能参与合成。")
        stream = streams[0]
        frames = int(stream.frames or 0)
        if frames <= 0:
            frames = sum(1 for _ in source.decode(video=0))
        return {
            "width": stream.width,
            "height": stream.height,
            "fps": float(stream.average_rate) if stream.average_rate else None,
            "frames": frames,
            "has_audio": bool(source.streams.audio),
            "duration": float(source.duration / av.time_base) if source.duration else None,
        }


def validate_timeline(clips, *, fps=24, target_frames=None, require_audio=True):
    """Raise :class:`CompositionError` before encoding when the timeline cannot be cut."""
    if not clips:
        raise CompositionError("empty_timeline", "没有可合成的片段。")
    probed = []
    for index, clip in enumerate(clips, 1):
        frames = int(clip["frames"])
        if frames < 1:
            raise CompositionError("range_invalid", f"第 {index} 段取用帧数必须大于 0。", {"clip": index})
        try:
            probe = probe_clip(clip["path"])
        except CompositionError:
            raise
        except Exception as exc:
            raise CompositionError(
                "decode_failed", f"第 {index} 段无法解码，不能合成。", {"clip": index, "error": str(exc)}
            ) from exc
        probed.append({**clip, "index": index, "in_frame": int(clip.get("in_frame", 0) or 0), "probe": probe})
    sizes = [(item["probe"]["width"], item["probe"]["height"]) for item in probed]
    if len(set(sizes)) > 1:
        raise CompositionError(
            "media_mismatch",
            "各段画面尺寸不一致，不能合成。",
            {
                "clips": [
                    {"clip": item["index"], "width": item["probe"]["width"], "height": item["probe"]["height"]}
                    for item in probed
                ]
            },
        )
    rates = [item["probe"]["fps"] for item in probed]
    if any(rate is None or abs(rate - fps) > 0.001 for rate in rates):
        raise CompositionError(
            "fps_mismatch",
            "各段帧率与目标帧率不一致，不能合成。",
            {"expected": fps, "clips": [{"clip": item["index"], "fps": rates[item["index"] - 1]} for item in probed]},
        )
    if require_audio:
        missing = [item["index"] for item in probed if not item["probe"]["has_audio"]]
        if missing:
            raise CompositionError("audio_missing", "以下片段没有原生音轨，不能合成。", {"clips": missing})
    for item in probed:
        end = item["in_frame"] + item["frames"]
        if item["in_frame"] < 0 or end > item["probe"]["frames"]:
            raise CompositionError(
                "range_invalid",
                f"第 {item['index']} 段取用区间超出可解码范围。",
                {
                    "clip": item["index"],
                    "in_frame": item["in_frame"],
                    "frames": item["frames"],
                    "available_frames": item["probe"]["frames"],
                },
            )
    total = sum(item["frames"] for item in probed)
    if target_frames is not None and total != target_frames:
        raise CompositionError(
            "frame_mismatch",
            "各段取用帧数之和与目标帧数不一致。",
            {"total_frames": total, "target_frames": target_frames, "difference": total - target_frames},
        )
    return {
        "width": sizes[0][0],
        "height": sizes[0][1],
        "fps": fps,
        "frames": total,
        "clips": probed,
    }


def save_frame(path, frame_index, output):
    """Write one decoded frame as a PNG; returns ``(index, total_frames)``."""
    path, output = Path(path), Path(output)
    with av.open(str(path)) as source:
        streams = source.streams.video
        if not streams:
            raise CompositionError("not_video", "素材不是视频，不能抽帧。")
        expected = int(streams[0].frames or 0)
        last = None
        total = 0
        for total, frame in enumerate(source.decode(video=0), 1):
            if frame_index < 0:
                last = frame
            elif total - 1 == frame_index:
                frame.to_image().save(output, "PNG")
                return frame_index, expected or total
        if frame_index < 0 and last is not None:
            last.to_image().save(output, "PNG")
            return total - 1, expected or total
        raise CompositionError(
            "frame_range", "指定帧超出视频范围。", {"frame_index": frame_index, "frames": expected or total}
        )


def compose_timeline(clips, output_path, *, fps=24, target_frames=None, require_audio=True):
    """Validate, then encode the timeline through the verified production encoder."""
    timeline = validate_timeline(clips, fps=fps, target_frames=target_frames, require_audio=require_audio)
    sources = [
        {"path": str(item["path"]), "start_frame": item["in_frame"], "frames": item["frames"]}
        for item in timeline["clips"]
    ]
    try:
        result = encode(sources, output_path, fps=fps, width=timeline["width"], height=timeline["height"])
    except ValueError as exc:
        raise CompositionError("encode_failed", str(exc)) from exc
    return {
        "output": result["output"],
        "frames": result["frames"],
        "duration": result["duration"],
        "fps": result["fps"],
        "width": timeline["width"],
        "height": timeline["height"],
        "sha256": result["sha256"],
        "native_audio_preserved": result["native_audio_preserved"],
        "clips": [
            {
                "asset_id": item.get("asset_id"),
                "in_frame": item["in_frame"],
                "frames": item["frames"],
                "sha256": record["sha256"],
            }
            for item, record in zip(timeline["clips"], result["clips"], strict=True)
        ],
    }
