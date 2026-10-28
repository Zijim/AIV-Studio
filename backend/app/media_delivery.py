"""Small derived previews and revalidated browser caching for stored media."""

import hashlib
import logging
import os
import secrets
from pathlib import Path

from PIL import Image
from starlette.responses import FileResponse, Response

# Keep the payload in the browser, but check that it still exists before reuse.
# In particular, permanent deletion must never be answered with a stale 304.
MEDIA_CACHE_CONTROL = "private, max-age=0, must-revalidate"
PREVIEW_VERSION = "thumb-v1"


def file_etag(path: Path, identity=""):
    stat = path.stat()
    value = f"{identity}:{path.name}:{stat.st_mtime_ns}:{stat.st_size}"
    return '"' + hashlib.sha256(value.encode()).hexdigest() + '"'


def small_preview(source: Path):
    """Derive existing PNG previews too; never rewrite the stored source."""
    fingerprint = file_etag(source, PREVIEW_VERSION).strip('"')
    target = source.with_name(f"{source.stem}.{PREVIEW_VERSION}.{fingerprint[:16]}.webp")
    if target.is_file():
        return target, "image/webp", '"' + fingerprint + '"'
    temporary = target.with_name(target.name + "." + secrets.token_hex(6) + ".partial")
    try:
        with Image.open(source) as image:
            image.thumbnail((320, 320), Image.Resampling.LANCZOS)
            image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
            image.save(temporary, "WEBP", quality=76, method=4)
        os.replace(temporary, target)
        return target, "image/webp", '"' + fingerprint + '"'
    except (OSError, ValueError):
        logging.getLogger("aiv.media").warning("缩略图转换暂时不可用，保留已有预览。")
        return (
            source,
            Image.MIME.get(Image.registered_extensions().get(source.suffix.lower()), "image/png"),
            file_etag(source),
        )
    finally:
        temporary.unlink(missing_ok=True)


def remove_preview_files(source: Path):
    """Remove this asset's managed derivatives along with its saved preview."""
    source.unlink(missing_ok=True)
    for derivative in source.parent.glob(f"{source.stem}.thumb-v*.*.webp"):
        derivative.unlink(missing_ok=True)


def media_response(request, path: Path, *, etag, media_type, filename=None, disposition="inline"):
    headers = {"Cache-Control": MEDIA_CACHE_CONTROL, "ETag": etag}
    # The caller has checked database state and file presence before this point.
    # Range transfers remain FileResponse's responsibility.
    incoming = request.headers.get("if-none-match", "")
    if any(value.strip().removeprefix("W/") in (etag, "*") for value in incoming.split(",")):
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type=disposition,
        headers=headers,
        stat_result=path.stat(),
    )
