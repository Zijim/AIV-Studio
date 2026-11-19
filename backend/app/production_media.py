"""Local review artifacts and frame-accurate composition for a small production."""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageOps


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def review_media(path, folder, kind):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(path)
    if kind == "image":
        with Image.open(path) as raw:
            raw.load()
            im = ImageOps.exif_transpose(raw).convert("RGB")
            info = {"width": im.width, "height": im.height}
            im.thumbnail((1024, 1536))
            im.save(folder / "review.jpg", quality=92)
        return {**info, "review_image": str((folder / "review.jpg").resolve()), "sha256": sha256(path)}
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        info = {"width": stream.width, "height": stream.height, "fps": float(stream.average_rate)}
        total = stream.frames
        if not total:
            total = sum(1 for _ in container.decode(video=0))
    indices = sorted({round(i * (total - 1) / 7) for i in range(8)})
    sheet = Image.new("RGB", (4 * 240, 2 * 458), "#202020")
    draw = ImageDraw.Draw(sheet)
    sampled = []
    with av.open(str(path)) as container:
        for number, frame in enumerate(container.decode(video=0)):
            if number not in indices:
                continue
            im = frame.to_image().convert("RGB")
            im.save(folder / f"frame-{number:04d}.jpg", quality=94)
            im.thumbnail((240, 432))
            slot = len(sampled)
            x, y = (slot % 4) * 240, (slot // 4) * 458
            sheet.paste(im, (x + (240 - im.width) // 2, y))
            draw.text((x + 6, y + 435), f"frame {number} / {number / info['fps']:.2f}s", fill="white")
            sampled.append(number)
    sheet.save(folder / "review.jpg", quality=94)
    # A PCM copy permits listening without relying on the browser's video codec.
    audio_path = folder / "audio.wav"
    with av.open(str(path)) as container:
        has_audio = bool(container.streams.audio)
        if has_audio:
            with av.open(str(audio_path), "w") as output:
                audio = output.add_stream("pcm_s16le", rate=32000)
                audio.layout = "stereo"
                resampler = av.AudioResampler(format="s16", layout="stereo", rate=32000)
                for frame in container.decode(audio=0):
                    for converted in resampler.resample(frame):
                        for packet in audio.encode(converted):
                            output.mux(packet)
                for converted in resampler.resample(None):
                    for packet in audio.encode(converted):
                        output.mux(packet)
                for packet in audio.encode():
                    output.mux(packet)
    return {
        **info,
        "frames": total,
        "duration": total / info["fps"],
        "sampled_frames": sampled,
        "has_audio": has_audio,
        "audio_file": str(audio_path.resolve()) if has_audio else None,
        "review_image": str((folder / "review.jpg").resolve()),
        "sha256": sha256(path),
    }


def compose(clips, output_path, fps=24, width=480, height=864):
    """Decode/trim each approved source, re-encode video and its native audio.

    Each clip has path/start_frame/frames. Audio is resampled to planar float,
    trimmed on the matching sample interval, then timestamped on one timeline.
    Missing/short native sound is an error, never silently replaced by silence.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".partial.mp4")
    sample_rate = 32000
    records, audio_segments = [], []
    for clip in clips:
        path = Path(clip["path"])
        start, count = clip.get("start_frame", 0), clip["frames"]
        if start < 0 or count < 1:
            raise ValueError("Invalid trim range")
        with av.open(str(path)) as source:
            video = source.streams.video[0]
            if video.average_rate != fps:
                raise ValueError("Source frame rate differs from the approved timeline")
            if (video.width, video.height) != (width, height):
                raise ValueError("Source dimensions differ; select a consistent version before composing")
            if not source.streams.audio:
                raise ValueError("Source has no native sound; sound review is required")
        begin, end = start * sample_rate // fps, (start + count) * sample_rate // fps
        channels = [bytearray(), bytearray()]
        # Ignore codec padding using decoded presentation time relative to the stream start.
        with av.open(str(path)) as source:
            stream = source.streams.audio[0]
            origin = float((stream.start_time or 0) * stream.time_base)
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=sample_rate)
            cursor = 0

            def take(frame):
                nonlocal cursor
                position = (
                    max(0, round((float(frame.pts * frame.time_base) - origin) * sample_rate))
                    if frame.pts is not None
                    else cursor
                )
                left, right = max(begin, position), min(end, position + frame.samples)
                if right > left:
                    for ch, plane in enumerate(frame.planes):
                        channels[ch].extend(bytes(plane)[(left - position) * 4 : (right - position) * 4])
                cursor = position + frame.samples

            for frame in source.decode(audio=0):
                for converted in resampler.resample(frame):
                    take(converted)
            for converted in resampler.resample(None):
                take(converted)
        if any(len(ch) < (end - begin) * 4 for ch in channels):
            raise ValueError("Native audio is shorter than the selected interval")
        audio_segments.append([bytes(ch[: (end - begin) * 4]) for ch in channels])
        records.append({**clip, "sha256": sha256(path)})
    frame_index, sample_index = 0, 0
    with av.open(str(temporary), "w") as output:
        video = output.add_stream("libx264", rate=fps)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        video.options = {"preset": "medium", "crf": "19"}
        audio = output.add_stream("aac", rate=sample_rate)
        audio.layout, audio.bit_rate = "stereo", 128000
        sound = [b"".join(segment[ch] for segment in audio_segments) for ch in (0, 1)]
        sample_count = len(sound[0]) // 4

        def write_audio(count):
            nonlocal sample_index
            frame = av.AudioFrame(format="fltp", layout="stereo", samples=count)
            frame.sample_rate, frame.pts, frame.time_base = sample_rate, sample_index, Fraction(1, sample_rate)
            for ch, plane in enumerate(frame.planes):
                data = sound[ch][sample_index * 4 : (sample_index + count) * 4]
                plane.update(data + bytes(plane.buffer_size - len(data)))
            for packet in audio.encode(frame):
                output.mux(packet)
            sample_index += count

        for clip in clips:
            used = 0
            with av.open(str(clip["path"])) as source:
                for index, frame in enumerate(source.decode(video=0)):
                    if index < clip.get("start_frame", 0):
                        continue
                    if used == clip["frames"]:
                        break
                    frame.pts, frame.time_base = frame_index, Fraction(1, fps)
                    for packet in video.encode(frame):
                        output.mux(packet)
                    frame_index += 1
                    used += 1
                    target = frame_index * sample_rate // fps
                    while sample_index + 1024 <= min(target, sample_count):
                        write_audio(1024)
            if used != clip["frames"]:
                raise ValueError("Video is shorter than the selected interval")
        if sample_index < sample_count:
            write_audio(sample_count - sample_index)
        for packet in video.encode():
            output.mux(packet)
        for packet in audio.encode():
            output.mux(packet)
    with av.open(str(temporary)) as check:
        stream = check.streams.video[0]
        duration = float(stream.duration * stream.time_base)
        if sum(1 for _ in check.decode(video=0)) != frame_index or abs(duration - frame_index / fps) > 0.001:
            raise ValueError("Export frame count or duration did not match the timeline")
    with av.open(str(temporary)) as check:
        if not any(True for _ in check.decode(audio=0)):
            raise ValueError("Exported audio cannot be decoded")
    temporary.replace(output_path)
    result = {
        "output": str(output_path.resolve()),
        "frames": frame_index,
        "fps": fps,
        "duration": duration,
        "sha256": sha256(output_path),
        "clips": records,
        "native_audio_preserved": True,
    }
    output_path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
