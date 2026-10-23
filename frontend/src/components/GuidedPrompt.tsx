import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy, FileCheck2, PencilLine } from "lucide-react";
import { api, type Data } from "../lib";
import { Button } from "./ui";

export function CreativeSummary({ plan }: { plan: Data }) {
  const meta = plan.prompt_metadata;
  if (!meta) return null;
  return (
    <div className="creative-summary">
      {meta.selection_reason_zh && (
        <p className="tool-selection-reason">{meta.selection_reason_zh}</p>
      )}
      {meta.creative_summary_zh && <p>{meta.creative_summary_zh}</p>}
      {!!meta.assumptions_zh?.length && (
        <div className="creative-assumptions">
          <span>本次补充</span>
          {meta.assumptions_zh.map((text: string, i: number) => (
            <p key={i}>{text}</p>
          ))}
        </div>
      )}
      {!!(
        meta.constraint_review?.kept?.length ||
        meta.constraint_review?.changed?.length
      ) && (
        <details className="constraint-review">
          <summary>保持项与本次修改</summary>
          {!!meta.constraint_review.kept?.length && (
            <p>保持：{meta.constraint_review.kept.join("；")}</p>
          )}
          {!!meta.constraint_review.changed?.length && (
            <p>修改：{meta.constraint_review.changed.join("；")}</p>
          )}
          <small>以上为助手的创作检查，生成效果请查看成片。</small>
        </details>
      )}
    </div>
  );
}

export function PlanInputs({ plan, tools }: { plan: Data; tools: Data[] }) {
  const ids: string[] = plan.inputs.map((input: Data) => input.asset_id);
  const assets = useQuery({
    queryKey: ["plan-input-preview", plan.id, plan.revision],
    queryFn: () => Promise.all(ids.map((id) => api(`/assets/${id}`))),
    enabled: !!ids.length,
  });
  if (!ids.length) return null;
  return (
    <div className="guided-inputs">
      {plan.inputs.map((input: Data) => {
        const asset = assets.data?.find((a) => a.id === input.asset_id);
        const label = plan.tool_id.startsWith("video.")
          ? "视频首帧"
          : (tools
              .find((t) => t.id === plan.tool_id)
              ?.inputs.find((i: Data) => i.slot === input.slot)?.label ??
            input.slot);
        return (
          <div className="guided-input" key={input.slot}>
            <img
              src={`/api/v1/assets/${input.asset_id}/preview`}
              alt={asset?.name ?? label}
              loading="lazy"
            />
            <div>
              <small>{label}</small>
              <span title={asset?.name}>
                {asset?.name ?? input.asset_id.slice(0, 8)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function PromptDetails({
  plan,
  onError,
}: {
  plan: Data;
  onError: (text: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const meta = plan.prompt_metadata;
  const text =
    plan.parameters.prompt ||
    plan.parameters.instruction ||
    `放大 ${plan.parameters.scale} 倍`;
  const guided = meta?.validation_status === "structure_validated";
  const parameterLabels: Record<string, string> = {
    width: "输出宽度",
    height: "输出高度",
    output_width: "请求宽度",
    output_height: "请求高度",
    duration_seconds: "请求时长（秒）",
    resolved_seconds: "实际时长（秒）",
    resolved_frames: "实际帧数",
    megapixels: "目标像素（MP）",
    aspect_ratio: "画面比例",
    preset: "生成档位",
    scale: "放大倍率",
    resolution: "目标分辨率",
    max_resolution: "最长边上限",
  };
  const parameterValues: Record<string, string> = {
    source: "保持原图比例",
    fast: "快速",
    quality: "质量",
  };
  const parameters = Object.entries(plan.parameters).filter(
    ([name]) =>
      !["prompt", "instruction", "negative_prompt", "seed"].includes(name),
  );
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      onError("复制失败，请展开原文手动选择复制。");
    }
  }
  return (
    <details className="guided-prompt">
      <summary>完整提示词与参数</summary>
      <div className="prompt-toolbar">
        <span className={`prompt-origin ${guided ? "validated" : ""}`}>
          {guided ? <FileCheck2 size={12} /> : <PencilLine size={12} />}
          {guided
            ? "结构校验通过"
            : meta?.generation_origin === "user_edited"
              ? "手动编辑"
              : "原始提示词"}
        </span>
        <Button onClick={copy} title="复制完整执行提示词">
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </Button>
      </div>
      {guided && meta.sections?.length ? (
        <>
          {meta.sections.map((section: Data, index: number) => (
            <div className="prompt-section" key={index}>
              <h4>{section.label}</h4>
              <p>{section.text}</p>
            </div>
          ))}
          <details className="prompt-raw">
            <summary>查看完整执行原文</summary>
            <p className="prompt-text">{text}</p>
          </details>
        </>
      ) : (
        <p className="prompt-text">{text}</p>
      )}
      {plan.parameters.negative_prompt && (
        <div className="prompt-section">
          <h4>负面提示词</h4>
          <p>{plan.parameters.negative_prompt}</p>
        </div>
      )}
      {!!parameters.length && (
        <dl className="prompt-parameters">
          {parameters.map(([name, value]) => (
            <div key={name}>
              <dt>{parameterLabels[name] ?? name}</dt>
              <dd>
                {value == null
                  ? "自动"
                  : name === "resolved_seconds" && typeof value === "number"
                    ? value.toFixed(3)
                    : (parameterValues[String(value)] ?? String(value))}
              </dd>
            </div>
          ))}
        </dl>
      )}
      <small>种子：{plan.seed_list.join("、")}</small>
      {meta?.guide_id && (
        <details className="prompt-provenance">
          <summary>生成来源</summary>
          <p>
            指引：{meta.guide_id} · {meta.guide_version}
          </p>
          <p>模式：{meta.task_mode}</p>
          <p className="prompt-hash">版本校验：{meta.effective_guide_hash}</p>
          {!guided && (
            <small>已保留原指引来源；手动文本未按该指引重新验证。</small>
          )}
        </details>
      )}
    </details>
  );
}
