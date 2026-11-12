import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ArrowUp,
  Check,
  ChevronDown,
  Copy,
  FileText,
  Link2,
  LoaderCircle,
  Maximize2,
  Pause,
  Play,
  Square,
  X,
} from "lucide-react";
import { api, type Data, messageSchema, useWorkspace } from "../lib";
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

export function AutonomousAssistant({
  projectId,
  conversationId,
  writable,
  configured,
  selected,
  messages,
  onError,
  onOpenAsset,
}: {
  projectId: string;
  conversationId: string;
  writable: boolean;
  configured: boolean;
  selected: Data[];
  messages: Data[];
  onError: (message: string) => void;
  onOpenAsset: (id: string) => void;
}) {
  const store = useWorkspace();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const useKeys = store.agentKeyAssets?.[conversationId] ?? true;
  const end = useRef<HTMLDivElement>(null);
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
    enabled: !!run,
    refetchInterval: active ? 2000 : false,
  });
  const calls: Data[] = activity.data?.items ?? [];
  const question = calls.find(
    (c) => c.name === "ask_user" && c.state === "waiting",
  );
  const draft = store.messageDrafts[conversationId] ?? "";
  useEffect(() => {
    setAnswer("");
    submission.current = null;
  }, [conversationId, question?.id]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
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
      <div className="conversation autonomous-conversation">
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
        {messages.map((m) => (
          <ChatMessage
            key={m.id}
            role={m.role}
            content={m.content}
            createdAt={m.created_at}
          >
            {!!m.details?.asset_ids?.length && (
              <div className="agent-deliverables">
                {m.details.asset_ids.map((id: string, i: number) => (
                  <AgentArtifact
                    key={id}
                    id={id}
                    index={i}
                    onOpen={onOpenAsset}
                    onError={onError}
                  />
                ))}
              </div>
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
        ))}
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
            {run.stop_reason && (
              <p className="agent-stop">{run.stop_reason.message}</p>
            )}
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
            {!!calls.length && (
              <details className="agent-activity message-details">
                <summary>
                  查看执行活动 · {calls.length}
                  <ChevronDown size={13} aria-hidden="true" />
                </summary>
                {calls.map((call) => (
                  <div key={call.id}>
                    <span>{toolNames[call.name] ?? call.name}</span>
                    <small>
                      {call.state === "completed"
                        ? "完成"
                        : call.state === "failed"
                          ? "未执行成功"
                          : call.state === "waiting"
                            ? "等待中"
                            : "处理中"}
                    </small>
                    {call.result?.error && <p>{call.result.error.message}</p>}
                  </div>
                ))}
              </details>
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
        <div ref={end} />
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
  const openLabel = `查看${value?.media_type === "text" ? "文档" : "成果"} ${index + 1}`;
  const referenced = useWorkspace((s) => s.selected.includes(id));
  return (
    <div className="agent-artifact">
      {value?.media_type === "image" && (
        <button
          className="agent-artifact-preview"
          type="button"
          aria-label={`预览${name}`}
          onClick={() => onOpen(id)}
        >
          <img
            src={value.preview_url ?? value.content_url}
            alt={name}
            width={value.media_info?.width}
            height={value.media_info?.height}
            loading="lazy"
            decoding="async"
          />
          <span className="agent-artifact-expand" aria-hidden="true">
            <Maximize2 size={13} />
          </span>
        </button>
      )}
      {value?.media_type === "video" && (
        <video
          src={value.content_url}
          poster={value.preview_url}
          controls
          preload="metadata"
          aria-label={name}
        />
      )}
      {value?.media_type === "text" && (
        <details className="agent-artifact-document">
          <summary>
            <FileText size={15} aria-hidden="true" />
            <span>{name}</span>
            <ChevronDown size={13} aria-hidden="true" />
          </summary>
          <pre>{value.media_info?.text_preview}</pre>
        </details>
      )}
      {asset.isPending && (
        <div className="agent-artifact-status" role="status">
          <LoaderCircle size={14} className="spin" aria-hidden="true" />
          正在加载成果…
        </div>
      )}
      {asset.isError && (
        <div className="agent-artifact-status">成果预览暂不可用</div>
      )}
      <div className="agent-artifact-actions">
        <Button
          className="agent-artifact-reference"
          type="button"
          aria-pressed={referenced}
          onClick={() => useWorkspace.getState().chooseAsset(id, true)}
        >
          {referenced ? (
            <Check size={13} aria-hidden="true" />
          ) : (
            <Link2 size={13} aria-hidden="true" />
          )}
          {referenced ? "已引用" : "引用此成果"}
        </Button>
        <Button type="button" aria-label={openLabel} onClick={() => onOpen(id)}>
          <Maximize2 size={12} aria-hidden="true" />
          查看
        </Button>
        {value?.media_type === "text" && (
          <Button
            type="button"
            onClick={async () => {
              try {
                const response = await fetch(`/api/v1/assets/${id}/content`);
                if (!response.ok) throw new Error("读取文档失败");
                await navigator.clipboard.writeText(await response.text());
              } catch (error) {
                onError((error as Error).message);
              }
            }}
          >
            <Copy size={13} aria-hidden="true" />
            复制正文
          </Button>
        )}
        <a
          className="btn"
          href={`/api/v1/assets/${id}/content?download=true`}
          download
        >
          <ArrowDownToLine size={13} aria-hidden="true" />
          下载
        </a>
      </div>
    </div>
  );
}
