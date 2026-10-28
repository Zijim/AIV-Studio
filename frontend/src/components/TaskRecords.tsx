import { useEffect, useState } from "react";
import { type Data } from "../lib";

const terminal = ["succeeded", "failed", "cancelled", "interrupted", "unknown"];
const parameterNames: Record<string, string> = {
  prompt: "生成提示词",
  instruction: "编辑指令",
  negative_prompt: "负面提示词",
  width: "实际宽度（px）",
  height: "实际高度（px）",
  output_width: "输出宽度（px）",
  output_height: "输出高度（px）",
  seed: "随机种子",
  preset: "生成档位",
  scale: "放大倍率",
  resolution: "目标短边（px）",
  max_resolution: "最长边上限（px）",
  duration_seconds: "请求视频时长（秒）",
  resolved_seconds: "实际视频时长（秒）",
  resolved_frames: "实际帧数",
  megapixels: "目标像素（MP）",
  aspect_ratio: "画面比例",
  ref_image_size: "参考图尺寸",
};
const configNames: Record<string, string> = {
  comfy_url: "执行服务",
  timeout_seconds: "超时限制（秒）",
  template_hash: "工作流校验值",
  step_index: "步骤序号（从 0 开始）",
  step_label: "步骤名称",
  step_count: "步骤总数",
  depends_on_execution_id: "依赖执行",
  input_slot: "承接素材槽位",
  resolved_dependency_asset_id: "承接素材",
};
const valueNames: Record<string, string> = {
  fast: "快速",
  quality: "质量",
  source: "跟随输入",
  match: "匹配输出尺寸",
  max: "最大尺寸",
};

export function duration(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${hours ? `${hours} 小时 ` : ""}${hours || minutes ? `${minutes} 分 ` : ""}${total % 60} 秒`;
}

function timestamp(value: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—";
}

function useNow(live: boolean) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  return now;
}

function elapsed(timing: Data, now: number): number | null {
  return timing.is_running && timing.started_at
    ? Math.max(0, (now - Date.parse(timing.started_at)) / 1000)
    : (timing.elapsed_seconds ?? null);
}

export function TaskTiming({
  task,
  compact = false,
}: {
  task: Data;
  compact?: boolean;
}) {
  const executions: Data[] = task.executions ?? [];
  const live = executions.some((e) => e.timing?.is_running);
  const now = useNow(live);
  const timings = executions.map((e) => e.timing ?? {});
  const starts = timings
    .map((t) => t.started_at)
    .filter(Boolean)
    .sort();
  const finishes = timings
    .map((t) => t.finished_at)
    .filter(Boolean)
    .sort();
  const allTerminal =
    executions.length > 0 &&
    executions.every((e) => terminal.includes(e.execution_state));
  const incomplete = timings.some((t) => t.incomplete);
  const knownDuration = timings.some(
    (t) => t.started_at && elapsed(t, now) !== null,
  );
  const unresolvedEnd = executions.some(
    (e) =>
      ["unknown", "interrupted"].includes(e.execution_state) &&
      !e.timing?.finished_at,
  );
  const seconds = timings.reduce((sum, t) => sum + (elapsed(t, now) ?? 0), 0);
  const approximate = timings.some(
    (t) => t.started_at && t.source === "observed",
  );
  const text =
    incomplete && !knownDuration
      ? "暂无完整记录"
      : !starts.length
        ? "尚未开始"
        : `${incomplete ? "已知 " : ""}${approximate ? "约 " : ""}${duration(seconds)}`;
  return (
    <dl className={`task-timing ${compact ? "compact" : ""}`}>
      <div>
        <dt>开始时间</dt>
        <dd>{timestamp(starts[0])}</dd>
      </div>
      <div>
        <dt>结束时间</dt>
        <dd>
          {allTerminal && !unresolvedEnd
            ? timestamp(finishes.at(-1) ?? task.finished_at)
            : "—"}
        </dd>
      </div>
      <div>
        <dt>累计执行耗时{live ? " · 计时中" : ""}</dt>
        <dd>
          {text}
          {incomplete && starts.length > 0 ? "（记录不完整）" : ""}
        </dd>
      </div>
    </dl>
  );
}

export function ExecutionTiming({ execution }: { execution: Data }) {
  const timing = execution.timing ?? {};
  const now = useNow(!!timing.is_running);
  const seconds = elapsed(timing, now);
  return (
    <>
      <dl className="task-timing">
        <div>
          <dt>开始时间</dt>
          <dd>{timestamp(timing.started_at)}</dd>
        </div>
        <div>
          <dt>结束时间</dt>
          <dd>{timestamp(timing.finished_at)}</dd>
        </div>
        <div>
          <dt>执行耗时{timing.is_running ? " · 计时中" : ""}</dt>
          <dd>
            {!timing.started_at && !timing.incomplete
              ? "尚未开始"
              : seconds === null
                ? "暂无完整记录"
                : !timing.started_at
                  ? "尚未开始"
                  : `${timing.source === "observed" ? "约 " : ""}${duration(seconds)}`}
          </dd>
        </div>
      </dl>
      {timing.incomplete && (
        <p className="timing-note">执行时间记录不完整，无法确认完整耗时。</p>
      )}
    </>
  );
}

export function executionLabel(execution: Data) {
  return execution.configuration?.step_label
    ? `步骤 ${execution.configuration.step_index + 1} · ${execution.configuration.step_label}`
    : `候选 ${execution.candidate_index + 1}`;
}

export function parameterSummary(execution: Data) {
  const p = execution?.parameters ?? {};
  return [
    p.width && p.height ? `${p.width} × ${p.height}` : null,
    p.output_width && p.output_height
      ? `${p.output_width} × ${p.output_height}`
      : null,
    p.resolved_seconds != null
      ? `视频 ${Number(p.resolved_seconds.toFixed(2))} 秒`
      : null,
    p.scale != null ? `放大 ${p.scale} 倍` : null,
    p.preset ? (valueNames[p.preset] ?? p.preset) : null,
    execution?.seed != null ? `种子 ${execution.seed}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function displayValue(key: string, value: unknown) {
  if (value === null || value === undefined) return "自动";
  if (value === "") return "未设置";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  if (["preset", "aspect_ratio", "ref_image_size"].includes(key))
    return valueNames[String(value)] ?? String(value);
  return String(value);
}

export function ExecutionParameters({
  execution,
  tools,
}: {
  execution: Data;
  tools: Data[];
}) {
  const tool = tools.find(
    (t) => t.id === execution.tool_id && t.version === execution.tool_version,
  );
  const params = { ...execution.parameters, seed: execution.seed };
  const configuration = Object.entries(execution.configuration ?? {}).filter(
    ([key]) => key !== "prompt_generation",
  );
  return (
    <div className="execution-parameters">
      <p className="execution-tool">
        {tool?.name ?? execution.tool_id} · v{execution.tool_version}
      </p>
      <dl className="execution-parameter-grid">
        {Object.entries(params).map(([key, value]) => (
          <div
            key={key}
            className={
              ["prompt", "instruction", "negative_prompt"].includes(key) ||
              typeof value === "object"
                ? "full-parameter"
                : ""
            }
          >
            <dt>
              {parameterNames[key] ??
                tool?.fields?.find((f: Data) => f.name === key)?.label ??
                key}
            </dt>
            <dd>{displayValue(key, value)}</dd>
          </div>
        ))}
      </dl>
      {!!execution.inputs?.length && (
        <details className="execution-config">
          <summary>输入素材（{execution.inputs.length}）</summary>
          <dl className="execution-parameter-grid">
            {execution.inputs.map((input: Data) => (
              <div className="full-parameter" key={input.slot}>
                <dt>
                  {tool?.inputs?.find((i: Data) => i.slot === input.slot)
                    ?.label ?? input.slot}
                </dt>
                <dd>
                  素材 {input.asset_id}
                  {input.blob_id ? `\n文件校验值 ${input.blob_id}` : ""}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {!!configuration.length && (
        <details className="execution-config">
          <summary>执行配置</summary>
          <dl className="execution-parameter-grid">
            {configuration.map(([key, value]) => (
              <div key={key}>
                <dt>{configNames[key] ?? key}</dt>
                <dd>{displayValue(key, value)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  );
}
