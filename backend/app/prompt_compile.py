"""Deterministic compilation and structural checks, not a visual quality score."""

import re

from .prompt_guides import content_hash

I2VA_INSTRUCTION = (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
)
COMPILER_VERSION = "1.0.0"


def compile_document(tool_id, document, brief, resolved, bindings):
    if tool_id == "image.upscale.seedvr2":
        if document is not None:
            raise ValueError("放大工具没有创作提示词，prompt_document必须为null")
        return {}, []
    if document is None:
        raise ValueError("提出方案时必须填写当前工具的prompt_document")
    sections = []
    if tool_id.startswith("image.generate"):
        values = {"prompt": document.positive_prompt, "negative_prompt": document.negative_prompt}
        sections = [
            {"label": "画面描述", "text": document.positive_prompt},
            {"label": "避免出现", "text": document.negative_prompt},
        ]
    elif tool_id.startswith("image.edit"):
        values = {"instruction": document.instruction, "negative_prompt": document.negative_prompt}
        sections = [
            {"label": "编辑指令", "text": document.instruction},
            {"label": "避免出现", "text": document.negative_prompt},
        ]
    else:
        shots = document.shots
        if shots[0].index != 1 or shots[0].cut_at_seconds is not None:
            raise ValueError("第一镜头必须index=1、cut_at_seconds=null")
        previous = 0
        previous_ms = 0
        parts = []
        for index, shot in enumerate(shots, 1):
            if shot.index != index:
                raise ValueError("镜头编号必须从1连续递增")
            if re.search(
                r"\[Shot\s+\d+\]|integrated_multimodal_description:|subject_definitions:", shot.description, re.I
            ):
                raise ValueError("镜头description不应重复分节、Shot标签或全参考格式")
            prefix = f"[Shot {index}] "
            if index > 1:
                cut = shot.cut_at_seconds
                if cut is None or not previous < cut < (resolved["resolved_frames"] - 1) / 24:
                    raise ValueError("后续切镜时间必须递增并早于最后一帧")
                milliseconds = round(cut * 1000)
                if milliseconds <= previous_ms or milliseconds / 1000 >= (resolved["resolved_frames"] - 1) / 24:
                    raise ValueError("格式化后的切镜时间必须递增并早于最后一帧")
                prefix += f"At {milliseconds // 60000:02}:{milliseconds % 60000 / 1000:06.3f}, "
                previous = cut
                previous_ms = milliseconds
            parts.append(prefix + shot.description.strip())
        body = "\n".join(parts)
        audio = brief.audio_policy
        if audio.silence and (
            document.overall_soundscape.strip() != "N/A" or document.non_diegetic_music.strip() != "N/A"
        ):
            raise ValueError("完全静音要求两个声音字段都为N/A")
        if audio.music is not True and document.non_diegetic_music.strip() != "N/A":
            raise ValueError("未要求配乐时non_diegetic_music必须为N/A")
        if brief.camera_policy in ("static", "unspecified"):
            if len(shots) != 1:
                raise ValueError("默认/固定镜头不能擅自增加切镜")
            movement = r"\b(?:push(?:es|ing)?\s+in|pull(?:s|ing)?\s+out|zoom(?:s|ing)?|pan(?:s|ning)?|truck(?:s|ing)?|tilt(?:s|ing)?|orbit(?:s|ing)?)\b"
            if re.search(r"\b(?:camera|shot|lens)\s+(?:\w+\s+){0,4}" + movement, body, re.I) or re.search(
                "推镜|拉镜|变焦|摇镜|横移镜头", body
            ):
                raise ValueError("固定镜头正文含有相机运动，请修复或澄清")
        dialogue = re.findall(r"<d>\[([^\]]+)\]\s?(.*?)</d>", body, re.S)
        protected_dialogue = [item.text for item in brief.literal_texts if item.kind in ("dialogue", "lyrics")]
        if audio.silence and dialogue:
            raise ValueError("静音视频不能包含对白")
        if any(text not in protected_dialogue for _, text in dialogue):
            raise ValueError("正文新增或改写了用户未提供的台词/歌词")
        if protected_dialogue and sorted(text for _, text in dialogue) != sorted(protected_dialogue):
            raise ValueError("台词/歌词必须逐字保留且不得无故重复")
        sections = [
            {"label": "画面与动作", "text": body},
            {"label": "环境与动作声音", "text": document.overall_soundscape},
            {"label": "背景配乐", "text": document.non_diegetic_music},
        ]
        values = {
            "prompt": f"{I2VA_INSTRUCTION}\n\nintegrated_multimodal_description: {body}\n\noverall_soundscape: {document.overall_soundscape.strip()}\n\nnon_diegetic_music: {document.non_diegetic_music.strip()}"
        }
    text = values.get("prompt") or values.get("instruction") or ""
    allowed_labels = {binding["label"] for binding in bindings if binding.get("label")}
    used_labels = {f"<{kind} {number}>" for kind, number in re.findall(r"<(Picture|Video|Audio)\s+(\d+)>", text)}
    if not used_labels <= allowed_labels:
        raise ValueError("提示词引用了执行工具没有接收的图片/视频/音频标签")
    if re.search(r"<Subject\s+\d+>", text):
        raise ValueError("当前工具不使用全参考Subject标签")
    for literal in brief.literal_texts:
        if literal.text not in text:
            raise ValueError("提示词遗漏或改写了用户要求的原文")
    if len(text) > 12000:
        raise ValueError("编译后提示词超过12000字符，不能截断保存")
    return values, [section for section in sections if section["text"]]


def generation_metadata(guide, route, composition, bindings, params, sections):
    text = params.get("prompt") or params.get("instruction") or ""
    import hashlib

    return {
        "generation_origin": "agent_guided",
        "validation_status": "structure_validated",
        "guide_id": guide["id"],
        "guide_version": guide["version"],
        "effective_guide_hash": guide["effective_hash"],
        "compiler_version": COMPILER_VERSION,
        "task_mode": route.task_mode,
        "selection_reason_zh": route.selection_reason_zh or composition.selection_reason_zh,
        "creative_summary_zh": composition.creative_summary_zh,
        "assumptions_zh": composition.assumptions_zh,
        "creative_brief": composition.creative_brief.model_dump(),
        "constraint_review": composition.constraint_review.model_dump(),
        "asset_binding_map": bindings,
        "prompt_document": composition.prompt_document.model_dump() if composition.prompt_document else None,
        "sections": sections,
        "compiled_prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def metadata_digest(metadata):
    return content_hash(metadata) if metadata else None
