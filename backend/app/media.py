import codecs
import hashlib
import mimetypes
from pathlib import Path

import av
from PIL import Image, ImageOps


def inspect_file(path: Path, name: str, preview_path: Path):
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    info, kind, preview, pixel_hash = {}, "other", None, None
    try:
        with Image.open(path) as raw:
            if raw.width * raw.height > 50000000:
                width, height = raw.size
                if raw.getexif().get(274) in (6, 8):
                    width, height = height, width
                return dict(
                    media_type="image",
                    mime_type=Image.MIME.get(raw.format, mime),
                    media_info={
                        "width": width,
                        "height": height,
                        "format": raw.format,
                        "preview_error": "图片尺寸较大，未生成预览；原文件已完整保留。",
                    },
                    preview_path=None,
                    pixel_hash=None,
                )
            raw.load()
            image = ImageOps.exif_transpose(raw)
            kind = "image"
            mime = Image.MIME.get(raw.format, mime)
            info = dict(width=image.width, height=image.height, format=raw.format, mode=image.mode)
            pixel_hash = hashlib.sha256(str((image.mode, image.size)).encode() + image.tobytes()).hexdigest()
            thumb = image.convert("RGBA") if "A" in image.mode else image.convert("RGB")
            thumb.thumbnail((640, 640))
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            thumb.save(preview_path, "PNG")
            preview = preview_path.name
    except Image.DecompressionBombError:
        return dict(
            media_type="other",
            mime_type=mime,
            media_info={"preview_error": "图片超出安全解码范围，原文件已保留，可下载查看。"},
            preview_path=None,
            pixel_hash=None,
        )
    except (OSError, ValueError):
        try:
            with av.open(str(path)) as container:
                video = container.streams.video
                audio = container.streams.audio
                if video:
                    kind = "video"
                    stream = video[0]
                    info.update(
                        width=stream.width,
                        height=stream.height,
                        fps=float(stream.average_rate) if stream.average_rate else None,
                        frames=stream.frames,
                        codec=stream.codec_context.name,
                    )
                    for frame in container.decode(video=0):
                        thumb = frame.to_image()
                        thumb.thumbnail((640, 640))
                        thumb.save(preview_path, "PNG")
                        preview = preview_path.name
                        break
                elif audio:
                    kind = "audio"
                if container.duration:
                    info["duration"] = container.duration / av.time_base
                info["audio"] = [
                    dict(
                        codec=s.codec_context.name,
                        sample_rate=s.codec_context.sample_rate,
                        channels=len(s.codec_context.layout.channels),
                    )
                    for s in audio
                ]
                if mime == "application/octet-stream":
                    mime = "video/mp4" if video else "audio/mpeg"
        except (av.error.FFmpegError, OSError, ValueError):
            if name.lower().endswith((".txt", ".md", ".json", ".csv", ".yaml", ".yml", ".log")):
                kind, mime = "text", "text/plain"
                try:
                    with path.open("rb") as file:
                        info["text_preview"] = codecs.getincrementaldecoder("utf-8")().decode(
                            file.read(32768), final=False
                        )
                except UnicodeDecodeError:
                    info["text_preview"] = None
                    info["preview_error"] = "不是UTF-8文本，可下载原文件查看。"
    return dict(media_type=kind, mime_type=mime, media_info=info, preview_path=preview, pixel_hash=pixel_hash)
