import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  Check,
  ChevronDown,
  Copy,
  FileText,
  Film,
  Link2,
  ListChecks,
  LoaderCircle,
  Maximize2,
  Pause,
  Play,
  Square,
  WandSparkles,
  X,
} from "lucide-react";
import { api, clock, type Data, messageSchema, useWorkspace } from "../lib";
import { ChatMessage } from "./ChatMessage";
import { Button } from "./ui";

const terminal = ["completed", "cancelled", "failed"];
const states: Record<string, string> = {
  queued: "准备开始",
  running: "正在创作",
  waiting_task: "等待生成结果",
  waiting_user: "需要你的补充",
  paused: "已暂停",
  needs_attention: "需要处理",
  cancelling: "正在取消",
  completed: "已完成",
  cancelled: "已取消",
  failed: "执行失败",
};
const toolNames: Record<string, string> = {
  set_resource_scope: "确认本次素材范围",
  get_project_context: "了解创作背景",
  search_resources: "查找已开放素材",
  read_resource: "阅读素材",
  inspect_media: "观察媒体效果",
  list_capabilities: "查看可用能力",
  get_capability: "阅读创作指引",
  preview_creation: "核对创作配置",
  invoke_creation: "调用创作工具",
  get_tasks: "查看执行结果",
  wait_for_tasks: "等待生成",
  write_document: "保存创作文档",
  update_plan: "更新创作计划",
  ask_user: "请求必要补充",
  finish_run: "检查并交付",
};
const taskStates: Record<string, string> = {
  queued: "排队",
  running: "生成中",
  saving: "保存中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  needs_attention: "需要处理",
};
const scopeSources: Record<string, string> = {
  selected: "指定",
  key: "关键素材",
  run_output: "本次成果",
  document: "文档",
  other: "其他",
};
const argumentLabels: Record<string, string> = {
  output_type: "输出类型",
  query: "查询",
  media_type: "媒体类型",
  asset_id: "素材",
  format: "格式",
  kind: "结束类别",
  message: "说明",
  mode: "素材模式",
  user_quote: "用户原文",
  start_seconds: "开始秒",
  end_seconds: "结束秒",
  sample_count: "采样数",
  require_audio: "需要音轨",
  name: "名称",
  parent_asset_id: "父文档",
  note: "说明",
  category: "类别",
  question: "问题",
  reason: "原因",
  offset: "偏移",
  length: "长度",
  limit: "数量",
};
const mediaLabels: Record<string, string> = {
  image: "图片",
  video: "视频",
  audio: "音频",
  text: "文本",
};
const planStates: Record<string, string> = {
  pending: "待执行",
  running: "进行中",
  completed: "已完成",
};

type ToolActivityCounts = { total: number; failed: number };

export function AutonomousAssistant({
  projectId,
  conversationId,
  writable,
  configured,
  selected,
  messages,
  earlierMessages,
  onError,
  onOpenAsset,
}: {
  projectId: string;
  conversationId: string;
  writable: boolean;
  configured: boolean;
  selected: Data[];
  messages: Data[];
  earlierMessages?: { loading: boolean; load: () => void };
  onError: (message: string) => void;
  onOpenAsset: (id: string) => void;
}) {
  const store = useWorkspace();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const useKeys = store.agentKeyAssets?.[conversationId] ?? true;
  const conversationPane = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  // Preserve a submission identity across network errors; edits produce a new one.
  const submission = useRef<{ payload: string; key: string } | null>(null);
  const runs = useQuery({
    queryKey: ["agent-runs", projectId, conversationId],
    queryFn: () =>
      api(
        `/projects/${projectId}/agent-runs?conversation_id=${conversationId}`,
      ),
    enabled: !!conversationId,
    refetchInterval: 2000,
  });
  const run: Data | undefined =
    runs.data?.items?.find((r: Data) => !terminal.includes(r.state)) ??
    runs.data?.items?.[0];
  const active = !!run && !terminal.includes(run.state);
  const activity = useQuery({
    queryKey: ["agent-activity", run?.id],
    queryFn: () => api(`/agent-runs/${run!.id}/activity`),
    enabled: active,
    refetchInterval: active ? 2000 : false,
  });
  const calls: Data[] = activity.data?.items ?? [];
  const taskList = useQuery({
    queryKey: ["agent-tasks", run?.id],
    queryFn: () =>
      api<Data[]>(`/tasks?project_id=${projectId}&agent_run_id=${run!.id}`),
    enabled: !!run,
    refetchInterval: active ? 2000 : false,
  });
  const taskItems: Data[] = taskList.data ?? [];
  const question = calls.find(
    (c) => c.name === "ask_user" && c.state === "waiting",
  );
  const activityByRun = new Map<string, ToolActivityCounts>();
  const submissionByCall = new Map<string, string>();
  for (const message of messages) {
    if (message.agent_run_id && message.agent_activity) {
      activityByRun.set(message.agent_run_id, message.agent_activity);
    }
    if (message.agent_call_id) {
      submissionByCall.set(message.agent_call_id, message.content);
    }
  }
  if (active && run && activity.data) {
    activityByRun.set(run.id, {
      total: calls.length,
      failed: calls.filter((call) => call.state === "failed").length,
    });
  }
  const shownActivity = new Set<string>();
  const draft = store.messageDrafts[conversationId] ?? "";
  useEffect(() => {
    setAnswer("");
    submission.current = null;
  }, [conversationId, question?.id]);
  useEffect(() => {
    const pane = conversationPane.current;
    if (pane && followLatest.current) pane.scrollTop = pane.scrollHeight;
  }, [messages.at(-1)?.id, run?.state]);
  async function perform(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      await client.invalidateQueries();
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!writable || active || busy) return;
    const parsed = messageSchema.safeParse(draft);
    if (!parsed.success) return onError(parsed.error.issues[0].message);
    const payload = {
      conversation_id: conversationId,
      content: parsed.data,
      selected_asset_ids: store.selected,
      image_roles: store.imageRoles,
      use_key_assets: useKeys,
      parent_run_id: run?.id ?? null,
    };
    const serialized = JSON.stringify(payload);
    if (submission.current?.payload !== serialized)
      submission.current = { payload: serialized, key: crypto.randomUUID() };
    await perform(async () => {
      await api(`/projects/${projectId}/agent-runs`, "POST", {
        ...payload,
        idempotency_key: submission.current!.key,
      });
      store.setMessageDraft(conversationId, "");
      submission.current = null;
    });
  }
  function control(action: string) {
    return perform(() =>
      api(`/agent-runs/${run!.id}/${action}`, "POST", {
        control_version: run!.control_version,
      }),
    );
  }
  return (
    <>
      <div
        className="conversation autonomous-conversation"
        ref={conversationPane}
        onScroll={({ currentTarget }) => {
          followLatest.current =
            currentTarget.scrollHeight -
              currentTarget.scrollTop -
              currentTarget.clientHeight <
            48;
        }}
      >
        {earlierMessages && (
          <Button
            className="load-earlier-messages"
            disabled={earlierMessages.loading}
            onClick={earlierMessages.load}
          >
            {earlierMessages.loading ? "正在加载…" : "加载更早的消息"}
          </Button>
        )}
        {!messages.length && (
          <div className="assistant-intro">
            <h3>说出目标，交给 Agent 推进</h3>
            <p>
              自主选择现有创作工具、等待结果、检查并交付。仅使用你指定的素材、项目关键素材和本次新成果，也可以无素材开始。
            </p>
          </div>
        )}
        {runs.isError && (
          <p role="alert">
            创作状态加载失败。
            <Button onClick={() => runs.refetch()}>重试</Button>
          </p>
        )}
        {messages.flatMap((m) => {
          const runId = m.agent_run_id;
          const counts = activityByRun.get(runId);
          const showSummary = !!counts?.total && !shownActivity.has(runId);
          const summary = showSummary ? (
            <AgentActivity
              key={`activity-${runId}`}
              runId={runId}
              counts={counts}
              submissions={submissionByCall}
              onOpen={onOpenAsset}
            />
          ) : null;
          if (showSummary) shownActivity.add(runId);
          if (counts?.total && m.agent_call_id) return summary;
          const bubble = (
            <ChatMessage
              key={m.id}
              role={m.role}
              content={m.content}
              createdAt={m.created_at}
            >
              {!!m.details?.asset_ids?.length && (
                <AgentDeliverables
                  ids={m.details.asset_ids}
                  onOpen={onOpenAsset}
                  onError={onError}
                />
              )}
              {!!m.details?.checks?.length && (
                <details className="message-details">
                  <summary>
                    目标检查依据 <ChevronDown size={13} aria-hidden="true" />
                  </summary>
                  <div className="message-details-content">
                    {m.details.checks.map((check: Data, i: number) => (
                      <p key={i}>
                        {check.observation}
                        {check.coverage && <small>{check.coverage}</small>}
                      </p>
                    ))}
                  </div>
                </details>
              )}
              {!!m.details?.limitations?.length && (
                <details className="message-details">
                  <summary>
                    检查范围与限制 <ChevronDown size={13} aria-hidden="true" />
                  </summary>
                  <div className="message-details-content">
                    {m.details.limitations.map((text: string, i: number) => (
                      <p key={i}>{text}</p>
                    ))}
                  </div>
                </details>
              )}
            </ChatMessage>
          );
          if (!summary) return bubble;
          return m.role === "user" ? [bubble, summary] : [summary, bubble];
        })}
        {run && (
          <section
            className="agent-run-card"
            data-state={run.state}
            aria-label="自主创作状态"
            aria-live="polite"
          >
            <div className="agent-run-heading">
              <strong>{states[run.state] ?? run.state}</strong>
              <span>
                {run.completion?.kind === "reply"
                  ? "对话回复"
                  : `${run.execution_count} / ${run.limits.executions} 次创作`}
              </span>
            </div>
            <p className="agent-timing">
              总历时{" "}
              {Math.max(
                0,
                Math.round(
                  (Date.parse(run.finished_at ?? new Date().toISOString()) -
                    Date.parse(run.created_at)) /
                    60000,
                ),
              )}{" "}
              分钟 · 自主推进 {Math.round(run.active_ms / 1000)} 秒
            </p>
            <AgentScopeSummary run={run} onOpen={onOpenAsset} />
            {!!run.work_plan?.items?.length && (
              <ol className="agent-plan">
                {run.work_plan.items.map((item: Data) => (
                  <li key={item.id}>
                    <span>
                      {item.state === "completed" ? (
                        <Check size={13} />
                      ) : active && item.state === "running" ? (
                        <LoaderCircle size={13} className="spin" />
                      ) : (
                        "○"
                      )}
                    </span>
                    {item.text}
                  </li>
                ))}
              </ol>
            )}
            <AgentStageResults
              key={run.id}
              tasks={taskItems}
              documents={run.documents ?? []}
              active={active}
              onOpen={onOpenAsset}
              onError={onError}
            />
            {run.stop_reason && (
              <p className="agent-stop">{run.stop_reason.message}</p>
            )}
            <AgentRework run={run} />
            {active && (
              <div className="agent-controls">
                {run.state === "paused" || run.state === "needs_attention" ? (
                  <Button
                    disabled={busy || !writable}
                    onClick={() => control("resume")}
                  >
                    <Play size={13} />
                    恢复
                  </Button>
                ) : (
                  <Button
                    disabled={busy || !writable || run.state === "cancelling"}
                    onClick={() => control("pause")}
                  >
                    <Pause size={13} />
                    暂停
                  </Button>
                )}
                <Button
                  disabled={busy || !writable || run.state === "cancelling"}
                  onClick={() => control("cancel")}
                >
                  <Square size={13} />
                  取消本次创作
                </Button>
              </div>
            )}
            {run.state === "paused" && (
              <small>已提交的生成继续保存；恢复后 Agent 才继续推进。</small>
            )}
          </section>
        )}
        {question && run?.state === "waiting_user" && (
          <form
            className="agent-answer"
            onSubmit={(event) => {
              event.preventDefault();
              const payload = {
                call_id: question.call_id,
                answer,
                control_version: run.control_version,
                selected_asset_ids: question.question?.allow_assets
                  ? store.selected
                  : [],
              };
              const serialized = JSON.stringify(payload);
              if (submission.current?.payload !== serialized)
                submission.current = {
                  payload: serialized,
                  key: crypto.randomUUID(),
                };
              perform(() =>
                api(`/agent-runs/${run.id}/answers`, "POST", {
                  ...payload,
                  idempotency_key: submission.current!.key,
                }),
              );
            }}
          >
            {question.question?.options?.map((option: string) => (
              <Button
                key={option}
                type="button"
                onClick={() => setAnswer(option)}
              >
                {option}
              </Button>
            ))}
            <label>
              回答当前澄清问题
              <textarea
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                maxLength={8000}
              />
            </label>
            {question.question?.allow_assets && (
              <small>
                回答会附带当前明确选中的 {store.selected.length} 份素材。
              </small>
            )}
            <Button
              type="submit"
              variant="primary"
              disabled={!answer.trim() || busy}
            >
              补充后继续
            </Button>
          </form>
        )}
      </div>
      <form className="composer" onSubmit={submit}>
        <div className="agent-scope">
          <label>
            <input
              type="checkbox"
              checked={useKeys}
              disabled={active}
              onChange={(e) =>
                store.setAgentKeyAssets(conversationId, e.target.checked)
              }
            />
            开放项目关键素材
          </label>
          <small>不读取全部项目素材</small>
        </div>
        {!!store.selected.length && (
          <div className="selected-context-assets">
            {store.selected.map((id) => (
              <div className="selected-context-asset" key={id}>
                <span>
                  {selected.find((a) => a.id === id)?.name ?? "已指定素材"}
                </span>
                <Button
                  type="button"
                  disabled={active}
                  aria-label="取消指定素材"
                  onClick={() => store.chooseAsset(id, true)}
                >
                  <X size={12} />
                </Button>
              </div>
            ))}
          </div>
        )}
        <textarea
          aria-label="发送给自主创作 Agent"
          value={draft}
          onChange={(e) =>
            store.setMessageDraft(conversationId, e.target.value)
          }
          disabled={!writable || busy}
          placeholder={
            active
              ? "任务结束或取消后可发送新要求，草稿会保留"
              : "描述想完成的作品，或先讨论想法…"
          }
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              !active
            ) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div className="composer-footer">
          <span>
            {active
              ? "运行中仅接受当前必要澄清"
              : "自主设计与执行 · 检查后直接交付"}
          </span>
          <Button
            type="submit"
            variant="primary"
            aria-label="发送创作目标"
            disabled={
              !writable || !configured || active || busy || !draft.trim()
            }
          >
            <ArrowUp size={17} />
          </Button>
        </div>
      </form>
    </>
  );
}

function AgentScopeSummary({
  run,
  onOpen,
}: {
  run: Data;
  onOpen: (id: string) => void;
}) {
  const summary: Data | undefined = run.scope_summary;
  if (!summary) return null;
  const samples: Data[] = summary.samples ?? [];
  return (
    <details className="agent-scope-summary">
      <summary>
        <span>本次可用素材</span>
        <small>
          {summary.pending ? "范围确认中" : `共 ${summary.available} 份`}
        </small>
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      <div className="agent-scope-counts">
        <span>
          指定 <b>{summary.selected ?? 0}</b>
        </span>
        <span>
          关键素材 <b>{summary.key_assets ?? 0}</b>
        </span>
        <span>
          本次成果 <b>{summary.run_outputs ?? 0}</b>
        </span>
        <span>
          文档 <b>{summary.documents ?? 0}</b>
        </span>
        {!!summary.excluded && (
          <span>
            排除 <b>{summary.excluded}</b>
          </span>
        )}
      </div>
      <small>
        {summary.key_assets_enabled ? "已开放项目关键素材" : "已关闭项目关键素材"}
        {" · 不读取项目全库"}
        {summary.mode === "only" ? " · 仅使用白名单" : ""}
        {summary.mode === "none" ? " · 无参考素材" : ""}
        {" · 来源类别可能重叠"}
      </small>
      {samples.length > 0 && (
        <div className="agent-scope-items">
          {samples.map((item: Data) => (
            <button
              key={item.asset_id}
              type="button"
              onClick={() => onOpen(item.asset_id)}
              title={item.name}
            >
              <span>{item.name}</span>
              <small>{scopeSources[item.source] ?? item.source}</small>
            </button>
          ))}
        </div>
      )}
      {summary.available > samples.length && (
        <small>仅列出最近 {samples.length} 份</small>
      )}
    </details>
  );
}

function AgentStageResults({
  tasks,
  documents,
  active,
  onOpen,
  onError,
}: {
  tasks: Data[];
  documents: Data[];
  active: boolean;
  onOpen: (id: string) => void;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!tasks.length && !documents.length) {
    return active ? (
      <p className="agent-stage-empty">正在等待首个阶段成果…</p>
    ) : null;
  }
  return (
    <details
      className="agent-stage"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>阶段成果</span>
        <small>
          {tasks.length} 个任务 · {documents.length} 份文档
        </small>
        <ChevronDown
          className="agent-stage-chevron"
          size={14}
          aria-hidden="true"
        />
      </summary>
      {open && (
        <div className="agent-stage-content">
          {tasks.map((task: Data) => (
            <article className="agent-stage-task" key={task.id}>
              <header>
                <strong>{task.title || task.tool_name || "创作任务"}</strong>
                <small data-state={task.state}>
                  {taskStates[task.state] ?? task.state}
                </small>
              </header>
              <p className="agent-stage-meta">
                {task.tool_name || "创作工具"}
                {task.candidate_count ? ` · ${task.candidate_count} 个候选` : ""}
                {task.error ? ` · ${errorText(task.error)}` : ""}
              </p>
              <StageMedia media={task.media ?? {}} onOpen={onOpen} />
            </article>
          ))}
          {documents.length > 0 && (
            <div className="agent-stage-documents">
              {documents.map((doc: Data, index: number) => (
                <AgentArtifact
                  key={doc.asset_id}
                  id={doc.asset_id}
                  index={index}
                  onOpen={onOpen}
                  onError={onError}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </details>
  );
}

function StageMedia({
  media,
  onOpen,
}: {
  media: Data;
  onOpen: (id: string) => void;
}) {
  const items: Data[] = [];
  const seen = new Set<string>();
  for (const group of ["outputs", "intermediates"]) {
    for (const item of media[group] ?? []) {
      const key = item.asset_id ?? item.key;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      items.push(item);
    }
  }
  if (!items.length) {
    return <p className="agent-stage-meta">尚无已保存产物</p>;
  }
  return (
    <div className="agent-stage-media">
      {items.map((item: Data) => {
        const assetId: string | null = item.asset_id ?? null;
        const unavailable =
          !assetId || ["missing", "purged", "unsaved"].includes(item.presence);
        const label = item.is_intermediate ? "中间产物" : item.label || "产物";
        return (
          <button
            key={item.key ?? assetId}
            type="button"
            className="agent-stage-item"
            disabled={unavailable}
            onClick={() => assetId && onOpen(assetId)}
            title={item.name}
          >
            <StageThumb item={item} />
            <span>{item.name || "未命名产物"}</span>
            <small>
              {label}
              {typeof item.candidate_index === "number"
                ? ` · 候选 ${item.candidate_index + 1}`
                : ""}
              {item.step_label ? ` · ${item.step_label}` : ""}
              {unavailable ? " · 未保存" : ""}
            </small>
          </button>
        );
      })}
    </div>
  );
}

function StageThumb({ item }: { item: Data }) {
  const source = item.preview_url ?? item.content_url;
  if (item.media_type === "image" && source) {
    return <img src={source} alt={item.name ?? "阶段产物"} loading="lazy" />;
  }
  if (item.media_type === "video") {
    return item.preview_url ? (
      <img src={item.preview_url} alt={item.name ?? "视频"} loading="lazy" />
    ) : (
      <Film size={22} aria-hidden="true" />
    );
  }
  if (item.media_type === "text") {
    return <FileText size={22} aria-hidden="true" />;
  }
  return (
    <span className="agent-stage-thumb-missing">
      {item.presence === "unsaved" ? "待保存" : "无预览"}
    </span>
  );
}

function AgentRework({ run }: { run: Data }) {
  const requirements: Data[] = run.work_plan?.requirements ?? [];
  const issues: Record<string, Data> = run.issues ?? {};
  const limit = Number(run.limits?.issue_reworks ?? 0);
  const used = Object.values(issues).reduce(
    (sum, issue) => sum + (Number(issue?.count) || 0),
    0,
  );
  if (!requirements.length) return null;
  return (
    <details className="agent-rework">
      <summary>
        <span>执行与返工</span>
        <small>
          {run.execution_count}/{run.limits?.executions ?? "-"} 次创作 · 返工{" "}
          {used} 次
        </small>
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      <ul>
        {requirements.map((requirement: Data) => {
          const issue = issues[requirement.id];
          const history: Data[] = issue?.history ?? [];
          return (
            <li key={requirement.id}>
              <div>
                <span>{requirement.text}</span>
                <small>
                  {issue ? `返工 ${issue.count}/${limit}` : "未返工"}
                </small>
              </div>
              {history.length > 0 && (
                <details>
                  <summary>返工记录 · {history.length}</summary>
                  {history.map((entry: Data, index: number) => (
                    <p key={index}>
                      {entry.problem}
                      {entry.reason ? `（${entry.reason}）` : ""}
                    </p>
                  ))}
                </details>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function AgentActivity({
  runId,
  counts,
  submissions,
  onOpen,
}: {
  runId: string;
  counts: ToolActivityCounts;
  submissions: ReadonlyMap<string, string>;
  onOpen: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [creationOpen, setCreationOpen] = useState(false);
  const history = useQuery({
    queryKey: ["agent-activity", runId],
    queryFn: () => api(`/agent-runs/${runId}/activity`),
    enabled: open || creationOpen,
  });
  const calls: Data[] | undefined = history.data?.items;
  const creationCalls = calls?.filter((call) => call.creation_tool_name);
  const loadStatus = history.isError ? (
    <p>
      执行记录暂不可用
      <Button onClick={() => history.refetch()}>重试</Button>
    </p>
  ) : (
    <p role="status">正在加载执行记录…</p>
  );
  return (
    <div className="agent-activity-group">
      <details
        className="agent-activity"
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>
          <ListChecks size={15} aria-hidden="true" />
          <span>工具调用 {counts.total} 次</span>
          {counts.failed > 0 && <small>{counts.failed} 次失败</small>}
          <ChevronDown className="agent-activity-chevron" size={15} aria-hidden="true" />
        </summary>
        {open && (
          <div className="agent-activity-list">
            {calls
              ? calls.map((call) => (
                  <AgentCallRow
                    key={call.id}
                    runId={runId}
                    call={call}
                    submission={submissions.get(call.id)}
                    onOpen={onOpen}
                  />
                ))
              : loadStatus}
          </div>
        )}
      </details>
      <details
        className="agent-activity"
        onToggle={(event) => setCreationOpen(event.currentTarget.open)}
      >
        <summary>
          <WandSparkles size={15} aria-hidden="true" />
          <span>创作工具调用</span>
          <ChevronDown className="agent-activity-chevron" size={15} aria-hidden="true" />
        </summary>
        {creationOpen && (
          <div className="agent-activity-list">
            {!creationCalls && loadStatus}
            {creationCalls?.length === 0 && <p>本轮未调用创作工具。</p>}
            {!!creationCalls?.length && (
              <ul className="agent-creation-tools">
                {creationCalls.map((call) => (
                  <li key={call.id}>{call.creation_tool_name}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </details>
    </div>
  );
}

function AgentCallRow({
  runId,
  call,
  submission,
  onOpen,
}: {
  runId: string;
  call: Data;
  submission?: string;
  onOpen: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ["agent-call", runId, call.id],
    queryFn: () => api(`/agent-runs/${runId}/calls/${call.id}`),
    enabled: open,
    staleTime: 30000,
  });
  return (
    <details
      className="agent-call"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        {call.created_at && (
          <time dateTime={call.created_at}>{clock(call.created_at)}</time>
        )}
        <span>{submission ?? toolNames[call.name] ?? call.name}</span>
        <small>{callState(call.state)}</small>
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      {call.result?.error && (
        <p className="agent-call-error">{call.result.error.message}</p>
      )}
      {open && (
        <div className="agent-call-detail">
          {detail.isPending ? (
            <small>正在加载参数…</small>
          ) : detail.isError ? (
            <small>参数加载失败</small>
          ) : (
            <AgentCallDetail call={detail.data} onOpen={onOpen} />
          )}
        </div>
      )}
    </details>
  );
}

function PlanCallDetail({ args }: { args: Data }) {
  const requirements: Data[] = Array.isArray(args.requirements)
    ? args.requirements
    : [];
  const items: Data[] = Array.isArray(args.items) ? args.items : [];
  const targets: Data[] = Array.isArray(args.creation_targets)
    ? args.creation_targets
    : [];
  return (
    <div className="agent-call-plan">
      {args.output_type && (
        <p>
          <span>输出类型</span>
          <b>{mediaLabels[args.output_type] ?? args.output_type}</b>
        </p>
      )}
      {requirements.length > 0 && (
        <section className="agent-call-section">
          <h5>目标要求 · {requirements.length}</h5>
          <ul>
            {requirements.map((requirement: Data) => (
              <li key={requirement.id}>
                <b>{requirement.id}</b>
                <span>{requirement.text}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {items.length > 0 && (
        <section className="agent-call-section">
          <h5>计划步骤 · {items.length}</h5>
          <ul>
            {items.map((item: Data) => (
              <li key={item.id}>
                <small data-state={item.state}>
                  {planStates[item.state] ?? item.state}
                </small>
                <span>{item.text}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {targets.length > 0 && (
        <section className="agent-call-section">
          <h5>生产目标 · {targets.length}</h5>
          <ul>
            {targets.map((target: Data) => (
              <li key={target.id}>
                <b>{target.id}</b>
                <span>
                  {target.text}
                  {target.media_type
                    ? `（${mediaLabels[target.media_type] ?? target.media_type}）`
                    : ""}
                  {Array.isArray(target.requirement_ids)
                    ? ` · ${target.requirement_ids.join("、")}`
                    : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {args.note && (
        <p>
          <span>说明</span>
          <b>{args.note}</b>
        </p>
      )}
    </div>
  );
}

function AgentCallDetail({
  call,
  onOpen,
}: {
  call: Data;
  onOpen: (id: string) => void;
}) {
  const args: Data =
    call.arguments && typeof call.arguments === "object" ? call.arguments : {};
  const result: Data = call.result ?? {};
  const data: Data = result.data ?? {};
  const parameters: Data =
    args.parameters && typeof args.parameters === "object" ? args.parameters : {};
  const scalarArgs = Object.entries(args).filter(([key, value]) => {
    if (
      [
        "capability_id",
        "version",
        "operation_id",
        "purpose",
        "candidate_count",
        "parameters",
        "inputs",
        "checks",
        "rework",
        "prompt_document",
        "requirements",
        "items",
        "creation_targets",
        "note",
        "content",
      ].includes(key)
    ) {
      return false;
    }
    return ["string", "number", "boolean"].includes(typeof value) && value !== "";
  });
  return (
    <div className="agent-call-body">
      {call.name === "update_plan" && <PlanCallDetail args={args} />}
      {args.capability_id && (
        <p>
          <span>能力</span>
          <b>
            {args.capability_id}
            {args.version ? ` · ${args.version}` : ""}
          </b>
        </p>
      )}
      {args.operation_id && (
        <p>
          <span>生产目标</span>
          <b>{args.operation_id}</b>
        </p>
      )}
      {!!args.candidate_count && (
        <p>
          <span>候选</span>
          <b>{args.candidate_count}</b>
        </p>
      )}
      {args.purpose && (
        <p>
          <span>目的</span>
          <b>{args.purpose}</b>
        </p>
      )}
      {Array.isArray(args.inputs) && args.inputs.length > 0 && (
        <p>
          <span>输入</span>
          <b>
            {args.inputs
              .map((ref: Data) => ref.slot ?? ref.asset_id)
              .join("、")}
          </b>
        </p>
      )}
      {call.name !== "update_plan" && scalarArgs.length > 0 && (
        <div className="agent-call-fields">
          {scalarArgs.map(([key, value]) => (
            <p key={key}>
              <span>{argumentLabels[key] ?? key}</span>
              <b>{String(value)}</b>
            </p>
          ))}
        </div>
      )}
      {Object.keys(parameters).length > 0 && (
        <details>
          <summary>执行参数</summary>
          <pre>{JSON.stringify(parameters, null, 2)}</pre>
        </details>
      )}
      {data.task_id && (
        <p>
          <span>任务</span>
          <b>{data.task_id}</b>
        </p>
      )}
      {typeof data.asset_id === "string" && (
        <p>
          <span>素材</span>
          <button type="button" onClick={() => onOpen(data.asset_id)}>
            查看 {data.asset_id}
          </button>
        </p>
      )}
      {data.evidence_call_id && (
        <p>
          <span>证据</span>
          <b>{data.evidence_call_id}</b>
        </p>
      )}
      {Array.isArray(args.checks) && args.checks.length > 0 && (
        <ul className="agent-call-checks">
          {args.checks.map((check: Data, index: number) => (
            <li key={index}>
              <span>{check.requirement_id}</span>
              <small>{check.outcome === "passed" ? "通过" : check.outcome}</small>
              <p>{check.observation}</p>
            </li>
          ))}
        </ul>
      )}
      {result.error && (
        <p className="agent-call-error">
          {result.error.code}：{result.error.message}
        </p>
      )}
      <details>
        <summary>原始参数与结果（JSON）</summary>
        <pre>{JSON.stringify({ arguments: args, result }, null, 2)}</pre>
      </details>
    </div>
  );
}

function callState(state: string) {
  if (state === "completed") return "完成";
  if (state === "failed") return "未执行成功";
  if (state === "waiting") return "等待中";
  return "处理中";
}

function errorText(error: Data | string | null | undefined) {
  if (!error) return "";
  return typeof error === "string" ? error : error.message ?? "";
}

function AgentDeliverables({
  ids,
  onOpen,
  onError,
}: {
  ids: string[];
  onOpen: (id: string) => void;
  onError: (text: string) => void;
}) {
  const gallery = useRef<HTMLDivElement>(null);
  const galleryId = useId();
  const [columns, setColumns] = useState(1);
  const [showAll, setShowAll] = useState(false);
  useLayoutEffect(() => {
    const node = gallery.current;
    if (!node) return;
    function updateColumns(width: number) {
      // Each card needs 136px and adjacent cards have a 10px gap.
      setColumns(width >= 428 ? 3 : width >= 282 ? 2 : 1);
    }
    updateColumns(node.clientWidth);
    const observer = new ResizeObserver(([entry]) =>
      updateColumns(entry.contentRect.width),
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const visibleCount = columns * 3;
  const visibleIds = showAll ? ids : ids.slice(0, visibleCount);
  return (
    <div className="agent-deliverables" ref={gallery}>
      <div
        className="agent-artifact-grid"
        id={galleryId}
        style={{
          gridTemplateColumns: `repeat(${Math.min(columns, ids.length)}, minmax(0, 210px))`,
        }}
      >
        {visibleIds.map((id, index) => (
          <AgentArtifact
            key={id}
            id={id}
            index={index}
            onOpen={onOpen}
            onError={onError}
          />
        ))}
      </div>
      {ids.length > visibleCount && (
        <Button
          className="agent-show-all"
          type="button"
          aria-controls={galleryId}
          aria-expanded={showAll}
          onClick={() => setShowAll((current) => !current)}
        >
          {showAll ? "收起成果" : `查看全部 ${ids.length} 项`}
          <ChevronDown size={13} aria-hidden="true" />
        </Button>
      )}
    </div>
  );
}

function AgentArtifact({
  id,
  index,
  onOpen,
  onError,
}: {
  id: string;
  index: number;
  onOpen: (id: string) => void;
  onError: (text: string) => void;
}) {
  const asset = useQuery({
    queryKey: ["agent-artifact", id],
    queryFn: () => api(`/assets/${id}`),
  });
  const value = asset.data;
  const name = value?.name || `成果 ${index + 1}`;
  const isDocument = value?.media_type === "text";
  const isVideo = value?.media_type === "video";
  const previewSrc =
    value?.preview_url ??
    (value?.media_type === "image" ? value.content_url : undefined);
  const referenced = useWorkspace((s) => s.selected.includes(id));
  return (
    <div className="agent-artifact">
      {isDocument ? (
        <details className="agent-artifact-document">
          <summary title={name}>
            <FileText size={15} aria-hidden="true" />
            <span>{name}</span>
            <ChevronDown size={13} aria-hidden="true" />
          </summary>
          <pre>
            {value.media_info?.text_preview || value.media_info?.preview_error}
          </pre>
        </details>
      ) : (
        <button
          className="agent-artifact-preview"
          type="button"
          aria-label={`预览${name}`}
          disabled={!value}
          onClick={() => onOpen(id)}
        >
          {previewSrc ? (
            <img src={previewSrc} alt="" loading="lazy" decoding="async" />
          ) : (
            <span
              className="agent-artifact-status"
              role={asset.isPending ? "status" : undefined}
            >
              {asset.isPending ? (
                <>
                  <LoaderCircle size={14} className="spin" aria-hidden="true" />
                  正在加载成果…
                </>
              ) : isVideo ? (
                <Film size={24} aria-hidden="true" />
              ) : (
                <span>成果预览暂不可用</span>
              )}
            </span>
          )}
          {value && (
            <span className="agent-artifact-expand" aria-hidden="true">
              {isVideo ? <Play size={13} /> : <Maximize2 size={13} />}
            </span>
          )}
        </button>
      )}
      <div className="agent-artifact-actions">
        {!isDocument && (
          <span className="agent-artifact-name" title={name}>
            {name}
          </span>
        )}
        <Button
          className="agent-artifact-reference"
          type="button"
          disabled={value?.presence !== "available"}
          aria-label={`${referenced ? "取消引用" : "引用"}${name}`}
          aria-pressed={referenced}
          onClick={() => useWorkspace.getState().chooseAsset(id, true)}
        >
          {referenced ? (
            <Check size={13} aria-hidden="true" />
          ) : (
            <Link2 size={13} aria-hidden="true" />
          )}
          {referenced ? "已引用" : "引用"}
        </Button>
        {isDocument && (
          <>
            <Button
              type="button"
              aria-label={`查看${name}`}
              title="查看文档"
              onClick={() => onOpen(id)}
            >
              <Maximize2 size={13} aria-hidden="true" />
            </Button>
            <Button
              type="button"
              aria-label={`复制${name}正文`}
              title="复制正文"
              disabled={value.presence !== "available"}
              onClick={async () => {
                try {
                  const response = await fetch(value.content_url);
                  if (!response.ok) throw new Error("读取文档失败");
                  await navigator.clipboard.writeText(await response.text());
                } catch (error) {
                  onError((error as Error).message);
                }
              }}
            >
              <Copy size={13} aria-hidden="true" />
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
