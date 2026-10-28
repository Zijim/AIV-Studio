"""Prepare bounded, ephemeral model inputs without changing stored originals."""

import base64
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import AppError

# DeepSeek's vision guide specifies an upper bound of 1024 tokens per image.
# Reserve that bound instead of treating base64 characters as text tokens.
IMAGE_TOKEN_RESERVE = 1024
MAX_IMAGE_BYTES = 1536 * 1024
MAX_DECODE_PIXELS = 50_000_000

# Match only our asset routes, including a link immediately followed by Chinese
# prose. Never fetch arbitrary URLs or infer attachments from past messages.
LOCAL_ASSET_LINK = re.compile(
    r"(?<![A-Za-z0-9_./:?=&%#@-])"
    r"(?P<origin>https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?)?"
    r"/api/v1/assets/(?P<asset_id>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
    r"/(?:preview|content)(?![A-Za-z0-9_./%~-])",
    re.IGNORECASE,
)


def linked_asset_ids(content, port, public_origin=""):
    ids = []
    for match in LOCAL_ASSET_LINK.finditer(content):
        origin = match["origin"]
        if origin:
            url = urlsplit(origin)
            try:
                actual_port = url.port or (443 if url.scheme.lower() == "https" else 80)
            except ValueError:
                continue
            # The Vite development page proxies the same /api routes.
            local = url.hostname in ("127.0.0.1", "localhost") and actual_port in (port, 5173)
            published = bool(public_origin and origin.lower() == public_origin)
            if not (local or published):
                continue
        asset_id = str(UUID(match["asset_id"]))
        if asset_id not in ids:
            ids.append(asset_id)
    return ids


IMAGE_ROLES = {
    "auto": "查看图片，结合本轮需求判断用途",
    "edit_source": "待编辑原图",
    "subject_reference": "主体参考，不代表视频首帧",
    "style_reference": "风格参考，不代表视频首帧",
    "scene_reference": "环境参考，不代表视频首帧",
    "composition_reference": "构图参考，不代表视频首帧",
    "first_frame": "视频首帧，生成将从这张画面开始",
    "last_frame": "视频尾帧，生成将在这张画面结束",
}


@dataclass(frozen=True)
class PreparedImage:
    metadata: dict
    data_url: str

    def content_parts(self):
        info = self.metadata
        return [
            {
                "type": "input_text",
                "text": f"附图 {info['index']}：asset_id={info['asset_id']}；"
                f"用途={info['purpose']}（{IMAGE_ROLES[info['purpose']]}）。身份以asset_id为准。",
            },
            {"type": "input_image", "image_url": self.data_url, "detail": "high"},
        ]


def prepare_image(path: Path, *, asset_id, blob_id, name, purpose, index, max_edge):
    try:
        with Image.open(path) as raw:
            if raw.width * raw.height > MAX_DECODE_PIXELS:
                raise AppError("vision_image_size", f"“{name}”超过5000万像素，请选择缩小后的图片再读图。")
            animated = getattr(raw, "is_animated", False)
            # Animated images intentionally contribute only their first frame.
            raw.seek(0)
            image = ImageOps.exif_transpose(raw)
            original_size = image.size
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            image = Image.alpha_composite(background, rgba).convert("RGB")
            for quality in (85, 75, 60):
                output = io.BytesIO()
                # Deliberately omit EXIF and other source metadata.
                image.save(output, "JPEG", quality=quality, optimize=True)
                payload = output.getvalue()
                if len(payload) <= MAX_IMAGE_BYTES:
                    break
            else:
                raise AppError("vision_image_size", f"“{name}”的读图副本仍然过大，请缩小图片后重试。")
            metadata = {
                "index": index,
                "asset_id": asset_id,
                "blob_id": blob_id,
                "name": name,
                "purpose": purpose,
                "original_width": original_size[0],
                "original_height": original_size[1],
                "sent_width": image.width,
                "sent_height": image.height,
                "mime_type": "image/jpeg",
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "frame": "first" if animated else "still",
                "transparency_background": "white",
                "estimated_image_tokens": IMAGE_TOKEN_RESERVE,
            }
            return PreparedImage(metadata, "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"))
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AppError("vision_image_decode", f"无法读取“{name}”的图片像素，请检查原文件或换成PNG/JPEG。") from exc


def request_audit(request):
    """Keep input identities/text for diagnostics, without duplicating image bytes in SQLite."""
    if isinstance(request.get("input"), str):
        return dict(request)
    items = []
    for item in request.get("input") or []:
        parts = [
            {"type": "input_image", "detail": part.get("detail", "auto"), "image_url": "[image bytes omitted]"}
            if part.get("type") == "input_image"
            else dict(part)
            for part in item.get("content", [])
        ]
        items.append({**item, "content": parts})
    return {**request, "input": items}
