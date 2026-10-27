"""Video conditioning semantics shared by planning, validation and execution."""

VIDEO_MODES = {
    "video.generate.minimax_t2v": "video_t2va",
    "video.generate.minimax_ref2v": "video_ref2va",
    "video.generate.minimax_i2v": "video_i2va",
    "video.generate.minimax_fl2v": "video_fl2va",
    "video.generate.minimax_l2v": "video_l2va",
}
FRAME_MODES = {"video_i2va", "video_fl2va", "video_l2va"}
REFERENCE_PURPOSES = {"subject_reference", "style_reference", "scene_reference", "composition_reference"}
INPUT_PURPOSES = {
    "source_image": "first_frame",
    "first_frame": "first_frame",
    "last_frame": "last_frame",
}


def check_input_slots(tool, slots):
    slots = list(slots)
    allowed = {item["slot"] for item in tool["inputs"]}
    required = {item["slot"] for item in tool["inputs"] if item.get("required", True)}
    if len(slots) != len(set(slots)) or not required <= set(slots) <= allowed:
        raise ValueError("输入槽位缺失、重复或不属于当前工具")
    if tool["id"] == "video.generate.minimax_ref2v":
        ordered = [item["slot"] for item in tool["inputs"]]
        if set(slots) != set(ordered[: len(slots)]):
            raise ValueError("参考图片须按编号连续绑定，编号与提示词标签一一对应")


def source_info(tool_id, assets):
    if tool_id == "video.generate.minimax_t2v" or tool_id == "video.generate.minimax_ref2v":
        return None
    source = assets.get("source_image") or assets.get("first_frame") or assets.get("last_frame")
    return source["media_info"] if source else None


def input_purpose(tool_id, slot, inferred="auto"):
    if tool_id == "video.generate.minimax_ref2v":
        return inferred
    if tool_id in VIDEO_MODES:
        return INPUT_PURPOSES.get(slot, inferred)
    return inferred
