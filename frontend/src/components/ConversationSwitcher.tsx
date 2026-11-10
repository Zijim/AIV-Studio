import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Archive,
  ArchiveRestore,
  Check,
  ChevronDown,
  LoaderCircle,
  MessageSquare,
  Pencil,
  Plus,
  Search,
} from "lucide-react";
import { api, clock, type Data } from "../lib";
import { Button, Modal } from "./ui";

export function ConversationSwitcher({
  projectId,
  items,
  current,
  limits,
  writable,
  onChoose,
  onRefresh,
  onError,
  mode = "assisted",
}: {
  projectId: string;
  items: Data[];
  current?: Data;
  limits?: Data;
  writable: boolean;
  onChoose: (conversation: Data) => void;
  onRefresh: () => void;
  onError: (message: string) => void;
  mode?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [archived, setArchived] = useState(false);
  const [busy, setBusy] = useState("");
  const [renaming, setRenaming] = useState<Data | null>(null);
  const [title, setTitle] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const inputLimit = Math.round((limits?.max_input_tokens ?? 131072) / 1024);
  const threshold = Math.round(
    (limits?.summary_threshold_tokens ?? 98304) / 1024,
  );
  useEffect(() => {
    if (!open) return;
    function outside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    function escape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  useEffect(() => {
    setOpen(false);
    setRenaming(null);
    setQuery("");
  }, [projectId]);

  async function create() {
    setBusy("create");
    try {
      const conversation = await api(
        `/projects/${projectId}/conversations`,
        "POST",
        { idempotency_key: crypto.randomUUID(), mode },
      );
      onChoose(conversation);
      setOpen(false);
      onRefresh();
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy("");
    }
  }
  async function archive(item: Data, value: boolean) {
    setBusy(item.id);
    try {
      const changed = await api(
        `/projects/${projectId}/conversations/${item.id}/archive`,
        "POST",
        { archived: value, revision: item.revision },
      );
      onRefresh();
      if (item.id === current?.id) onChoose(changed);
    } catch (error) {
      onError((error as Error).message);
      onRefresh();
    } finally {
      setBusy("");
    }
  }
  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!renaming || !title.trim()) return;
    setBusy("rename");
    try {
      const changed = await api(
        `/projects/${projectId}/conversations/${renaming.id}`,
        "PATCH",
        { title: title.trim(), revision: renaming.revision },
      );
      if (changed.id === current?.id) onChoose(changed);
      setRenaming(null);
      onRefresh();
    } catch (error) {
      onError((error as Error).message);
      onRefresh();
    } finally {
      setBusy("");
    }
  }
  const visible = items.filter(
    (item) =>
      !!item.archived_at === archived &&
      `${item.title} ${item.preview ?? ""}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  return (
    <>
      <div className="conversation-controls" ref={root}>
        <div className="conversation-bar">
          <button
            className="conversation-trigger"
            ref={trigger}
            aria-label="切换对话"
            aria-expanded={open}
            aria-controls="conversation-picker"
            onClick={() => {
              setOpen(!open);
              setArchived(!!current?.archived_at);
            }}
          >
            <MessageSquare size={15} />
            <span>{current?.title ?? "正在加载对话…"}</span>
            {current?.archived_at && <small>已归档</small>}
            <ChevronDown size={13} />
          </button>
          <Button
            className="new-conversation"
            title="开启独立的新对话"
            aria-label="新建对话"
            disabled={!writable || !!busy}
            onClick={create}
          >
            {busy === "create" ? (
              <LoaderCircle size={14} className="spin" />
            ) : (
              <Plus size={14} />
            )}
            新对话
          </Button>
        </div>
        <div className="conversation-scope">
          <span>
            {mode === "autonomous"
              ? "指定素材与关键素材 · 自主执行"
              : "项目素材共享 · 对话记忆独立"}
          </span>
          <span
            title={
              mode === "autonomous"
                ? "自主创作保留目标与执行事实，按需整理工具记录。"
                : `每个对话独立使用 ${inputLimit}K 输入预算，达到 ${threshold}K 自动整理较早记录。原始消息保留；每日费用预算共用。${current?.summarized_message_count ? ` 已整理 ${current.summarized_message_count} 条早期消息。` : ""}`
            }
          >
            {inputLimit}K 上下文
          </span>
        </div>
        {open && (
          <div
            className="conversation-picker"
            id="conversation-picker"
            role="dialog"
            aria-label="项目对话"
          >
            <div className="conversation-picker-heading">
              <strong>项目对话</strong>
              <span>{items.length} 个</span>
            </div>
            <label className="conversation-search">
              <Search size={14} />
              <input
                autoFocus
                aria-label="搜索对话"
                placeholder="搜索对话"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <div
              className="conversation-filters"
              role="group"
              aria-label="对话状态筛选"
            >
              <button
                aria-pressed={!archived}
                onClick={() => setArchived(false)}
              >
                进行中 <span>{items.filter((i) => !i.archived_at).length}</span>
              </button>
              <button aria-pressed={archived} onClick={() => setArchived(true)}>
                已归档 <span>{items.filter((i) => i.archived_at).length}</span>
              </button>
            </div>
            <div className="conversation-list">
              {visible.map((item) => (
                <div
                  className={`conversation-row ${item.id === current?.id ? "current" : ""}`}
                  key={item.id}
                >
                  <a
                    className="conversation-choice"
                    href={`/projects/${projectId}?conversation=${item.id}`}
                    aria-current={item.id === current?.id ? "page" : undefined}
                    onClick={(event) => {
                      if (
                        event.ctrlKey ||
                        event.metaKey ||
                        event.shiftKey ||
                        event.altKey
                      )
                        return;
                      event.preventDefault();
                      onChoose(item);
                      setOpen(false);
                    }}
                  >
                    <span className="conversation-row-title">
                      <span>{item.title}</span>
                      {item.id === current?.id && <Check size={13} />}
                      {item.planning && (
                        <LoaderCircle size={12} className="spin" />
                      )}
                      {item.running && <small>执行中</small>}
                    </span>
                    <p>{item.preview || "空白对话，开始一个新的想法"}</p>
                    <small>
                      {clock(item.updated_at)} · {item.message_count ?? 0}{" "}
                      条消息
                    </small>
                  </a>
                  <div className="conversation-row-actions">
                    <Button
                      title="重命名对话"
                      aria-label={`重命名 ${item.title}`}
                      disabled={!writable || !!busy}
                      onClick={() => {
                        setRenaming(item);
                        setTitle(item.title);
                        setOpen(false);
                      }}
                    >
                      <Pencil size={13} />
                    </Button>
                    <Button
                      title={
                        item.archived_at ? "恢复对话" : "归档对话，保留全部记录"
                      }
                      aria-label={`${item.archived_at ? "恢复" : "归档"} ${item.title}`}
                      disabled={
                        !writable ||
                        !!busy ||
                        item.planning ||
                        item.running ||
                        !!item.agent_run
                      }
                      onClick={() => archive(item, !item.archived_at)}
                    >
                      {item.archived_at ? (
                        <ArchiveRestore size={13} />
                      ) : (
                        <Archive size={13} />
                      )}
                    </Button>
                  </div>
                </div>
              ))}
              {!visible.length && (
                <div className="conversation-list-empty">
                  {query
                    ? "没有匹配的对话"
                    : archived
                      ? "还没有归档的对话"
                      : "没有进行中的对话，开启一个新想法吧"}
                </div>
              )}
            </div>
            <p className="conversation-picker-note">
              新对话从空白记忆开始，已有对话随时可切回。
            </p>
          </div>
        )}
      </div>
      {current?.archived_at && (
        <div className="conversation-archived">
          <Archive size={14} />
          <span>此对话已归档，记录保留</span>
          <Button
            disabled={!writable || !!busy}
            onClick={() => archive(current, false)}
          >
            恢复对话
          </Button>
        </div>
      )}
      {renaming && (
        <Modal title="重命名对话" onClose={() => setRenaming(null)}>
          <form onSubmit={rename}>
            <label className="field-label">
              对话名称
              <input
                aria-label="对话名称"
                autoFocus
                required
                maxLength={120}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
            <div className="modal-actions">
              <Button type="button" onClick={() => setRenaming(null)}>
                取消
              </Button>
              <Button
                type="submit"
                variant="primary"
                disabled={!title.trim() || !!busy}
              >
                保存名称
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
