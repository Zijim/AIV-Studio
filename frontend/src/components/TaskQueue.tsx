import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ArrowRight,
  Check,
  Circle,
  ChevronDown,
  FileText,
  Image,
  Layers3,
  LoaderCircle,
  Maximize2,
  Minimize2,
  Play,
  RefreshCw,
  X,
} from "lucide-react";
import { api, clock, labels, type Data } from "../lib";
import { Button, Modal } from "./ui";
import {
  ExecutionIdentity,
  ExecutionParameters,
  ExecutionTiming,
  TaskIdentity,
  TaskTiming,
  executionLabel,
} from "./TaskRecords";

type Action = (
  name: string,
  operation: () => Promise<unknown>,
  success?: string,
) => Promise<unknown>;
type MediaAction = (asset: Data, task: Data) => void;
const active = ["queued", "running", "saving"];
const terminal = ["succeeded", "failed", "cancelled", "interrupted", "unknown"];

export function mediaStatus(asset: Data) {
  if (asset.presence === "changed") return "素材版本不一致";
  if (asset.presence === "unsaved")
    return asset.storage_state === "pending" ? "等待保存" : "尚未保存";
  if (asset.presence !== "available")
    return asset.preview_url
      ? "原文件不可用 · 仅预览"
      : (labels[asset.presence] ?? "素材不可用");
  return asset.project_removed ? "已从项目移除" : "";
}

function AssetCard({
  asset,
  index,
  output,
  task,
  onPreview,
  onLocate,
}: {
  asset: Data;
  index: number;
  output: boolean;
  task: Data;
  onPreview: MediaAction;
  onLocate: MediaAction;
}) {
  const [broken, setBroken] = useState(false);
  useEffect(
    () => setBroken(false),
    [asset.preview_url, asset.presence, asset.storage_state],
  );
  const title = output
    ? `${asset.is_intermediate && asset.role === "final" ? "中间产物" : "产物"} ${index + 1} · ${asset.step_label ?? `候选 ${asset.candidate_index + 1}`}`
    : `输入 ${index + 1} · ${(asset.usage_labels ?? [asset.label]).join(" / ")}`;
  const info = asset.media_info ?? {};
  const status = mediaStatus(asset);
  return (
    <article
      className="task-asset-card"
      data-asset-id={asset.asset_id ?? undefined}
    >
      <button
        className="task-asset-thumb"
        disabled={!asset.can_preview}
        onClick={() => onPreview(asset, task)}
        aria-label={`预览${title}：${asset.name}`}
      >
        {asset.preview_url && !broken ? (
          <img
            src={asset.preview_url}
            alt={asset.name}
            loading="lazy"
            onError={() => setBroken(true)}
          />
        ) : (
          <div className="task-media-placeholder">
            {asset.media_type === "video" ? (
              <Play />
            ) : asset.media_type === "image" ? (
              <Image />
            ) : (
              <FileText />
            )}
            <span>{status || "暂无缩略图"}</span>
          </div>
        )}
        <span className="task-asset-type">
          {asset.media_type === "video"
            ? `视频${info.duration ? ` · ${info.duration.toFixed(1)}s` : ""}`
            : output
              ? asset.role === "final"
                ? asset.is_intermediate
                  ? "中间产物"
                  : "最终产物"
                : "其他输出"
              : "输入参考"}
        </span>
        {asset.asset_id && task.best_asset_id === asset.asset_id && (
          <span className="task-best">最佳候选</span>
        )}
      </button>
      <strong className="task-asset-title">{title}</strong>
      <span className="task-asset-filename" title={asset.name}>
        {asset.name}
      </span>
      <small>
        {info.width ? `${info.width} × ${info.height}` : ""}
        {status ? ` ${status}` : ""}
      </small>
      <div className="task-asset-actions">
        <button
          disabled={!asset.can_preview}
          onClick={() => onPreview(asset, task)}
        >
          预览{asset.media_type === "video" ? " / 播放" : ""}
        </button>
        <button
          disabled={
            !asset.asset_id ||
            asset.presence === "changed" ||
            !asset.can_preview
          }
          onClick={() => onLocate(asset, task)}
        >
          定位素材
        </button>
      </div>
      {asset.error && <small className="error-text">{asset.error}</small>}
    </article>
  );
}

function AssetGroup({
  items,
  output,
  title,
  empty,
  task,
  limit,
  onAll,
  onPreview,
  onLocate,
}: {
  items: Data[];
  output: boolean;
  title: string;
  empty: string;
  task: Data;
  limit?: number;
  onAll?: () => void;
  onPreview: MediaAction;
  onLocate: MediaAction;
}) {
  return (
    <section className="task-media-group">
      <div className="task-media-heading">
        <h4>
          {title} <span>{items.length}</span>
        </h4>
        {limit && items.length > limit && (
          <button onClick={onAll}>查看全部 {items.length} 份</button>
        )}
      </div>
      {items.length ? (
        <div className="task-media-grid">
          {items.slice(0, limit).map((asset, index) => (
            <AssetCard
              key={asset.key}
              {...{ asset, index, output, task, onPreview, onLocate }}
            />
          ))}
        </div>
      ) : (
        <div className="task-media-empty">{empty}</div>
      )}
    </section>
  );
}

function emptyOutput(task: Data) {
  if (task.state === "queued") return "等待生成";
  if (task.state === "running") return "生成中 · 产物保存后会显示在这里";
  if (task.state === "saving") return "已生成，等待保存";
  if (task.state === "needs_attention")
    return "请核实执行与保存状态，暂无已确认的正式产物";
  return "本次暂无正式产物记录";
}

function LiveProgress({ task }: { task: Data }) {
  if (!active.includes(task.state)) return null;
  const execution = task.executions.find(
    (e: Data) => !terminal.includes(e.execution_state),
  );
  const progress = execution?.progress ?? {};
  const sampling =
    typeof progress.value === "number" &&
    typeof progress.max === "number" &&
    progress.max > 0;
  return (
    <div className="task-live-progress">
      <span>{progress.stage || labels[task.state]}</span>
      {sampling && (
        <>
          <small>
            当前节点 {progress.value} / {progress.max}
          </small>
          <progress
            aria-label="当前节点执行进度"
            value={progress.value}
            max={progress.max}
          />
        </>
      )}
    </div>
  );
}

function MediaFlow({
  task,
  media,
  detailed = false,
  onAll,
  onPreview,
  onLocate,
}: {
  task: Data;
  media: Data;
  detailed?: boolean;
  onAll?: () => void;
  onPreview: MediaAction;
  onLocate: MediaAction;
}) {
  return (
    <div className="task-media-flow">
      <AssetGroup
        items={media.inputs ?? []}
        output={false}
        title="输入参考"
        empty="无参考素材 · 文字生成"
        limit={detailed ? undefined : 3}
        {...{ task, onAll, onPreview, onLocate }}
      />
      <ArrowRight className="task-flow-arrow" size={18} aria-label="生成" />
      <AssetGroup
        items={media.outputs ?? []}
        output
        title="生成产物"
        empty={emptyOutput(task)}
        limit={detailed ? undefined : 3}
        {...{ task, onAll, onPreview, onLocate }}
      />
    </div>
  );
}

export function TaskAssetPreview({
  asset,
  task,
  onClose,
  onLocate,
}: {
  asset: Data;
  task: Data;
  onClose: () => void;
  onLocate: MediaAction;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <Modal
      title={asset.label === "最终产物" ? "任务产物预览" : "任务素材预览"}
      description={asset.name}
      wide
      onClose={onClose}
    >
      <div className="task-media-viewer">
        {failed ? (
          <p>文件暂时无法读取，请检查素材状态。</p>
        ) : asset.can_open && asset.media_type === "video" ? (
          <video
            src={asset.content_url}
            poster={asset.preview_url ?? undefined}
            controls
            onError={() => setFailed(true)}
          />
        ) : asset.can_open && asset.media_type === "audio" ? (
          <audio
            src={asset.content_url}
            controls
            onError={() => setFailed(true)}
          />
        ) : asset.preview_url ||
          (asset.can_open && asset.media_type === "image") ? (
          <img
            src={
              asset.can_open && asset.media_type === "image"
                ? asset.content_url
                : asset.preview_url
            }
            alt={asset.name}
            onError={() => setFailed(true)}
          />
        ) : (
          <p>该素材没有可用的图片预览。</p>
        )}
      </div>
      <dl className="task-preview-info">
        <div>
          <dt>本次用途</dt>
          <dd>{(asset.usage_labels ?? [asset.label]).join(" / ")}</dd>
        </div>
        <div>
          <dt>素材编号</dt>
          <dd>{asset.asset_id ?? "尚未归档"}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{mediaStatus(asset) || "已保存，可查看原文件"}</dd>
        </div>
      </dl>
      <div className="modal-actions">
        <Button onClick={onClose}>返回任务</Button>
        <Button variant="primary" onClick={() => onLocate(asset, task)}>
          定位到项目素材
        </Button>
      </div>
    </Modal>
  );
}

function TaskDetails({
  task,
  tools,
  action,
  onPlan,
  onPreview,
  onLocate,
}: {
  task: Data;
  tools: Data[];
  action: Action;
  onPlan: (plan: Data) => void;
  onPreview: MediaAction;
  onLocate: MediaAction;
}) {
  const detail = useQuery({
    queryKey: ["task-detail", task.id],
    queryFn: () => api(`/tasks/${task.id}`),
    refetchInterval: 5000,
  });
  const current = detail.data ?? task;
  return (
    <div className="task-expanded-details">
      {detail.isError && (
        <p className="error-text">详情刷新失败，正在显示已加载的记录。</p>
      )}
      {(current.media?.intermediates?.length ?? 0) > 0 && (
        <AssetGroup
          items={current.media.intermediates}
          output
          title="中间素材 · 不计最终产物"
          empty=""
          task={current}
          {...{ onPreview, onLocate }}
        />
      )}
      {current.executions.map((e: Data) => (
        <section className="task-step-card" key={e.id}>
          <div className="execution-heading">
            <strong>{executionLabel(e)}</strong>
            <span>
              {labels[e.execution_state]} / {labels[e.storage_state]}
            </span>
          </div>
          {e.configuration?.depends_on_execution_id && (
            <p className="timing-note">
              承接前一步生成的素材 → {e.configuration.step_label ?? "下一步"}
            </p>
          )}
          <ExecutionIdentity execution={e} compact />
          <MediaFlow
            task={current}
            media={e.media ?? {}}
            detailed
            {...{ onPreview, onLocate }}
          />
          <ExecutionTiming execution={e} />
          {e.error && <p className="error-text">{e.error.message}</p>}
          <details className="task-parameters">
            <summary>完整参数与执行配置</summary>
            <ExecutionParameters execution={e} tools={tools} />
          </details>
          {!!e.media?.other_outputs?.length && (
            <details className="task-parameters">
              <summary>其他输出（{e.media.other_outputs.length}）</summary>
              <AssetGroup
                items={e.media.other_outputs}
                output
                title="其他输出"
                empty=""
                task={current}
                {...{ onPreview, onLocate }}
              />
            </details>
          )}
          <div className="execution-actions">
            {terminal.includes(e.execution_state) && (
              <Button
                onClick={() =>
                  action(
                    "collect",
                    () => api(`/executions/${e.id}/collect`, "POST"),
                    "已核实并补存现有输出",
                  )
                }
              >
                <ArrowDownToLine size={14} />
                补存输出
              </Button>
            )}
            {["unknown", "interrupted"].includes(e.execution_state) && (
              <Button
                onClick={() =>
                  action(
                    "reconcile",
                    () => api(`/executions/${e.id}/reconcile`, "POST"),
                    "正在重新核实服务器记录",
                  )
                }
              >
                <RefreshCw size={14} />
                核实状态
              </Button>
            )}
          </div>
          <details className="task-parameters">
            <summary>请求、历史与输出证据</summary>
            <pre className="json-view">{JSON.stringify(e, null, 2)}</pre>
          </details>
        </section>
      ))}
      {!active.includes(current.state) && (
        <Button
          onClick={() =>
            action("retry", async () =>
              onPlan(await api(`/tasks/${current.id}/retry`, "POST")),
            )
          }
        >
          准备重做方案
        </Button>
      )}
    </div>
  );
}

export function TaskQueue({
  open,
  tasks,
  tools,
  conversations,
  onConversation,
  onClose,
  action,
  onPlan,
  onPreview,
  onLocate,
  focusedId,
}: {
  open: boolean;
  tasks: Data[];
  tools: Data[];
  conversations: Data[];
  onConversation: (conversation: Data) => void;
  onClose: () => void;
  action: Action;
  onPlan: (plan: Data) => void;
  onPreview: MediaAction;
  onLocate: MediaAction;
  focusedId: string | null;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [large, setLarge] = useState(false);
  const panel = useRef<HTMLElement>(null);
  function scrollToTask(id: string) {
    requestAnimationFrame(() => {
      const container = panel.current;
      const target = container?.querySelector<HTMLElement>(
        `[data-task-id="${CSS.escape(id)}"]`,
      );
      if (container && target)
        container.scrollTo({
          top: Math.max(
            0,
            target.offsetTop -
              (container.querySelector("header")?.offsetHeight ?? 0) -
              12,
          ),
        });
    });
  }
  function toggleSize() {
    const container = panel.current;
    const top =
      (container?.getBoundingClientRect().top ?? 0) +
      (container?.querySelector("header")?.offsetHeight ?? 0);
    const anchor = [
      ...(container?.querySelectorAll<HTMLElement>("[data-task-id]") ?? []),
    ].find((item) => item.getBoundingClientRect().bottom > top);
    setLarge(!large);
    if (anchor?.dataset.taskId) scrollToTask(anchor.dataset.taskId);
  }
  const focus = useQuery({
    queryKey: ["task-detail", focusedId],
    queryFn: () => api(`/tasks/${focusedId}`),
    enabled: !!focusedId && open && !tasks.some((t) => t.id === focusedId),
  });
  const rows =
    focus.data && !tasks.some((t) => t.id === focus.data.id)
      ? [focus.data, ...tasks]
      : tasks;
  useEffect(() => {
    if (focusedId) setExpanded(focusedId);
  }, [focusedId]);
  useEffect(() => {
    if (open && focusedId) scrollToTask(focusedId);
  }, [focusedId, focus.data?.id]);
  return (
    <section
      ref={panel}
      className={`task-drawer media-task-drawer ${large ? "expanded" : ""}`}
      hidden={!open}
      aria-label="任务与结果"
    >
      <header className="drawer-heading">
        <div>
          <h2>
            <Layers3 size={18} />
            任务与结果 <span>{rows.length}</span>
          </h2>
          <p>输入参考与最终产物按任务关联，点击缩略图预览</p>
        </div>
        <div className="task-panel-actions">
          <Button
            aria-label={large ? "缩小队列" : "展开队列"}
            onClick={toggleSize}
          >
            {large ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
          </Button>
          <Button aria-label="关闭任务队列" onClick={onClose}>
            <X size={18} />
          </Button>
        </div>
      </header>
      {focus.isError && (
        <p className="error-text">无法载入指定任务，请检查记录是否仍存在。</p>
      )}
      {rows.length ? (
        rows.map((task) => (
          <article
            key={task.id}
            data-task-id={task.id}
            className={`task-result-card ${focusedId === task.id ? "task-highlighted" : ""}`}
          >
            <header className="task-result-heading">
              <div className="task-result-title">
                <span className={`task-state ${task.state}`}>
                  {active.includes(task.state) ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : task.state === "completed" ? (
                    <Check size={16} />
                  ) : (
                    <Circle size={16} />
                  )}
                </span>
                <div>
                  <h3>{task.title || task.tool_name || "创作任务"}</h3>
                  <small>
                    {clock(task.created_at)} ·{" "}
                    {task.executions.some(
                      (e: Data) => e.configuration?.step_count > 1,
                    )
                      ? `${task.executions.length} 个步骤`
                      : `${task.candidate_count} 个候选`}
                  </small>
                </div>
              </div>
              <div className="task-result-status">
                <span>{labels[task.state]}</span>
                <small>
                  已保存{" "}
                  {task.media?.outputs?.filter(
                    (a: Data) => a.storage_state === "saved",
                  ).length ?? 0}{" "}
                  / 产物 {task.media?.outputs?.length ?? 0} 份
                </small>
              </div>
            </header>
            <TaskIdentity task={task} />
            <LiveProgress task={task} />
            <MediaFlow
              task={task}
              media={task.media ?? {}}
              onAll={() => setExpanded(task.id)}
              {...{ onPreview, onLocate }}
            />
            <TaskTiming task={task} compact />
            <footer className="task-result-actions">
              <Button
                aria-expanded={expanded === task.id}
                onClick={() => {
                  setExpanded(expanded === task.id ? null : task.id);
                  scrollToTask(task.id);
                }}
              >
                <ChevronDown
                  className={expanded === task.id ? "rotate" : ""}
                  size={14}
                />
                {expanded === task.id ? "收起详情" : "任务详情与全部素材"}
                {task.media?.intermediates?.length
                  ? ` · 中间素材 ${task.media.intermediates.length}`
                  : ""}
              </Button>
              {task.conversation_id && (
                <Button
                  onClick={() => {
                    const c = conversations.find(
                      (c) => c.id === task.conversation_id,
                    );
                    if (c) onConversation(c);
                  }}
                >
                  返回所属对话
                </Button>
              )}
              {active.includes(task.state) && (
                <Button
                  variant="danger"
                  disabled={task.cancel_requested}
                  onClick={() =>
                    action("cancel", () =>
                      api(`/tasks/${task.id}/cancel`, "POST"),
                    )
                  }
                >
                  {task.cancel_requested ? "正在取消" : "取消任务"}
                </Button>
              )}
            </footer>
            {expanded === task.id && (
              <TaskDetails
                {...{ task, tools, action, onPlan, onPreview, onLocate }}
              />
            )}
          </article>
        ))
      ) : (
        <div className="task-media-empty">
          还没有任务。确认执行方案后，可在这里查看输入和产物。
        </div>
      )}
    </section>
  );
}
