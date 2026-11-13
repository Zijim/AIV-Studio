import math

from PIL import Image, ImageOps


def prepare_local_edit_source(source, destination):
    # Use the same orientation as the Agent's image preview for both crop and composite.
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image).convert("RGBA")
        oriented.save(destination, format="PNG", compress_level=1)
        return oriented.size


def local_edit_geometry(params, source_size):
    width, height = source_size
    x, y = params["region_x"], params["region_y"]
    right, bottom = x + params["region_width"], y + params["region_height"]
    left_px, top_px = min(width - 1, round(x * width)), min(height - 1, round(y * height))
    right_px = max(left_px + 1, min(width, round(right * width)))
    bottom_px = max(top_px + 1, min(height, round(bottom * height)))
    mask_width, mask_height = right_px - left_px, bottom_px - top_px

    # Shift the crop inward at an edge so the model still sees nearby context.
    context_edge = max(256, 4 * max(mask_width, mask_height))
    crop_width, crop_height = min(width, context_edge), min(height, context_edge)
    crop_x = max(0, min(width - crop_width, (left_px + right_px - crop_width) // 2))
    crop_y = max(0, min(height - crop_height, (top_px + bottom_px - crop_height) // 2))
    scale = 512 / max(crop_width, crop_height)

    return {
        "source_width": width,
        "source_height": height,
        "crop_x": crop_x,
        "crop_y": crop_y,
        "crop_width": crop_width,
        "crop_height": crop_height,
        "mask_x": left_px - crop_x,
        "mask_y": top_px - crop_y,
        "mask_width": mask_width,
        "mask_height": mask_height,
        "feather": min(4, (min(mask_width, mask_height) - 1) // 2),
        "sample_width": max(16, math.floor(crop_width * scale / 16 + 0.5) * 16),
        "sample_height": max(16, math.floor(crop_height * scale / 16 + 0.5) * 16),
    }
