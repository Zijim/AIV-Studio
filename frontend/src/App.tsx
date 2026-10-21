import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  ArrowDownToLine,
  ArrowUp,
  Archive,
  Check,
  ChevronDown,
  Circle,
  Clock3,
  Copy,
  FileText,
  Folder,
  FolderPlus,
  GitBranch,
  ImagePlus,
  Layers3,
  LoaderCircle,
  Maximize2,
  MessageSquare,
  MoreHorizontal,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Star,
  Trash2,
  Upload,
  Video,
  WandSparkles,
  X,
} from "lucide-react";
import {
  api,
  bytes,
  clock,
  eventCursor,
  type Data,
  labels,
  messageSchema,
  useWorkspace,
} from "./lib";
import { Button, Empty, Modal } from "./components/ui";

const activeStates = ["queued", "running", "saving"];
const executeKeys = new Map<string, string>();
const imageRoles: Record<string, string> = {
  auto: "自动判断用途",
  edit_source: "待编辑原图",
  subject_reference: "主体参考",
  style_reference: "风格参考",
  composition_reference: "构图参考",
  first_frame: "视频首帧",
};
function Thumb({ asset }: { asset: Data }) {
  return asset.preview_url && asset.presence !== "purged" ? (
    <img src={asset.preview_url} alt={asset.name} loading="lazy" />
  ) : (
    <div className="file-placeholder">
      {asset.media_type === "video" ? <Video /> : <FileText />}
      <span>{asset.media_type}</span>
    </div>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const navigate = useNavigate(),
    location = useLocation();
  const store = useWorkspace();
  const [modal, setModal] = useState<string | null>(null);
  const [focus, setFocus] = useState<Data | null>(null);
  const [tool, setTool] = useState<Data | null>(null);
  const [editingPlan, setEditingPlan] = useState<Data | null>(null);
  const [search, setSearch] = useState(""),
    [filter, setFilter] = useState("");
  const [trash, setTrash] = useState(false),
    [message, setMessage] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(""),
    [toast, setToast] = useState("");
  const [importResult, setImportResult] = useState<Data | null>(null);
  const [taskDetail, setTaskDetail] = useState<Data | null>(null);
  const files = useRef<HTMLInputElement>(null),
    folders = useRef<HTMLInputElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Data[]>("/projects"),
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api("/health"),
    refetchInterval: 15000,
  });
  const tools = useQuery({
    queryKey: ["tools"],
    queryFn: () => api<Data[]>("/tools"),
    refetchInterval: 30000,
  });
  const project = projects.data?.find((p) => p.id === store.projectId);
  const assets = useQuery({
    queryKey: ["assets", store.projectId, search, filter, trash, page],
    queryFn: () =>
      api(
        `/projects/${store.projectId}/assets?q=${encodeURIComponent(search)}&media_type=${filter}&removed=${trash}&offset=${page * 100}`,
      ),
    enabled: !!project,
  });
  const messages = useQuery({
    queryKey: ["messages", store.projectId],
    queryFn: () => api<Data[]>(`/projects/${store.projectId}/messages`),
    enabled: !!project,
  });
  const plans = useQuery({
    queryKey: ["plans", store.projectId],
    queryFn: () => api<Data[]>(`/projects/${store.projectId}/plans`),
    enabled: !!project,
  });
  const turns = useQuery({
    queryKey: ["turns", store.projectId],
    queryFn: () => api<Data[]>(`/projects/${store.projectId}/turns`),
    enabled: !!project,
    refetchInterval: 4000,
  });
  const planningTurn = turns.data?.find((t) => t.state === "planning");
  const tasks = useQuery({
    queryKey: ["tasks", store.projectId],
    queryFn: () => api<Data[]>(`/tasks?project_id=${store.projectId}`),
    enabled: !!project,
    refetchInterval: 5000,
  });
  const draft = plans.data?.find((p) => p.state === "draft");
  const allAssets: Data[] = assets.data?.items ?? [];
  const selectedAssets = useQuery({
    queryKey: ["selected-assets", store.projectId, store.selected],
    queryFn: () =>
      Promise.all(store.selected.map((id) => api(`/assets/${id}`))),
    enabled: !!project && !!store.selected.length,
  });
  const selected = store.selected.length ? (selectedAssets.data ?? []) : [];
  const imageCount = selected.filter((a) => a.media_type === "image").length;
  const visionLimit = health.data?.planner.vision_max_images ?? 6;
  const running =
    tasks.data?.filter((t) => activeStates.includes(t.state)) ?? [];
  const writable = !!project && !project.archived_at;

  function refresh() {
    queryClient.invalidateQueries();
  }
  async function action(
    name: string,
    operation: () => Promise<unknown>,
    success?: string,
  ) {
    setBusy(name);
    try {
      const result = await operation();
      refresh();
      if (success) setToast(success);
      return result;
    } catch (error) {
      setToast((error as Error).message);
      return undefined;
    } finally {
      setBusy("");
    }
  }
  useEffect(() => {
    const match = location.pathname.match(/^\/projects\/([^/]+)/);
    if (match && match[1] !== store.projectId) store.chooseProject(match[1]);
  }, [location.pathname]);
  useEffect(() => {
    setFocus(null);
    setTrash(false);
    setSearch("");
  }, [store.projectId]);
  useEffect(() => setPage(0), [store.projectId, search, filter, trash]);
  useEffect(() => {
    if (
      projects.data?.length &&
      !projects.data.some((p) => p.id === store.projectId)
    ) {
      store.chooseProject(projects.data[0].id);
      navigate("/projects/" + projects.data[0].id, { replace: true });
    }
  }, [projects.data]);
  useEffect(() => {
    const source = new EventSource(`/api/v1/events?after=${eventCursor}`);
    source.onmessage = (event) => {
      const update = JSON.parse(event.data);
      const type = update.type as string;
      if (type === "reset") {
        refresh();
        return;
      }
      if (
        type === "asset.saved" &&
        update.project_id === useWorkspace.getState().projectId &&
        Date.now() - Date.parse(update.created_at) < 10000
      ) {
        api(`/assets/${update.entity_id}`)
          .then((asset) => {
            if (
              asset.role === "final" &&
              asset.presence === "available" &&
              update.project_id === useWorkspace.getState().projectId
            ) {
              asset.project = asset.projects.find(
                (p: Data) => p.project_id === update.project_id,
              );
              setFocus(asset);
              useWorkspace.getState().chooseAsset(asset.id);
              setToast("新结果已归档");
            }
          })
          .catch(() => {});
      }
      const keys = type.startsWith("task.")
        ? ["tasks", "turns"]
        : type.startsWith("plan.")
          ? ["plans"]
          : type.startsWith("message.") || type.startsWith("turn.")
            ? ["messages", "plans", "turns"]
            : type.startsWith("project.")
              ? ["projects", "assets"]
              : ["assets", "detail"];
      keys.forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
    };
    return () => source.close();
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timeout = setTimeout(() => setToast(""), 6000);
    return () => clearTimeout(timeout);
  }, [toast]);
  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.data?.length, draft?.revision]);
  useEffect(() => {
    if (focus) {
      const fresh = allAssets.find((a) => a.id === focus.id);
      if (fresh) setFocus(fresh);
    }
  }, [assets.data]);

  async function importFiles(list: FileList | File[]) {
    if (!writable || !list.length) return;
    const accepted = Array.from(list);
    const results: Data[] = [];
    await action(
      "import",
      async () => {
        for (let i = 0; i < accepted.length; i += 50) {
          const data = new FormData();
          accepted
            .slice(i, i + 50)
            .forEach((f) =>
              data.append("files", f, f.webkitRelativePath || f.name),
            );
          const batch = await api(
            `/projects/${project!.id}/imports`,
            "POST",
            data,
          );
          results.push(...batch.items);
        }
        setImportResult({ items: results });
        setModal("import-result");
      },
      "素材已归档到当前项目",
    );
  }
  function openTool(value: Data, plan?: Data) {
    setTool(value);
    setEditingPlan(plan || null);
    setModal("tool");
  }
  async function execute(plan: Data) {
    const id = `${plan.id}:${plan.revision}`;
    if (!executeKeys.has(id)) executeKeys.set(id, crypto.randomUUID());
    await action("execute", async () => {
      try {
        return await api(`/plans/${plan.id}/execute`, "POST", {
          revision: plan.revision,
          plan_hash: plan.plan_hash,
          idempotency_key: executeKeys.get(id),
          accept_previous_context: false,
        });
      } catch (error) {
        if ((error as Data).code === "context_stale") {
          setEditingPlan(plan);
          setModal("stale");
          return;
        }
        throw error;
      }
    });
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (
      store.selected.length &&
      (selectedAssets.isPending || selectedAssets.isError)
    ) {
      setToast(
        selectedAssets.isError
          ? "无法获取所选素材，请重新选择后发送。"
          : "正在读取所选素材信息，请稍候。",
      );
      return;
    }
    if (imageCount > visionLimit) {
      setToast(`每轮最多读取 ${visionLimit} 张图片，请减少选择。`);
      return;
    }
    const parsed = messageSchema.safeParse(message);
    if (!parsed.success) {
      setToast(parsed.error.issues[0].message);
      return;
    }
    await action("message", async () => {
      await api(`/projects/${project!.id}/messages`, "POST", {
        content: parsed.data,
        selected_asset_ids: store.selected,
        image_roles: Object.fromEntries(
          Object.entries(store.imageRoles).filter(([id]) =>
            store.selected.includes(id),
          ),
        ),
        idempotency_key: crypto.randomUUID(),
      });
      setMessage("");
    });
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a
          className="brand"
          href="/"
          onClick={(e) => {
            e.preventDefault();
            setModal("about");
          }}
        >
          <div className="brand-mark">
            <Layers3 size={19} />
          </div>
          <strong>映作</strong>
          <span>AIV STUDIO</span>
        </a>
        <div className="topbar-middle">
          <span
            className={`status-dot ${health.data?.comfyui?.compatible ? "green" : ""}`}
          />
          {health.data?.comfyui?.compatible
            ? "ComfyUI 已连接"
            : "ComfyUI 未就绪"}
          <span className="top-separator" />
          <span>本地创作工作台</span>
        </div>
        <div className="top-actions">
          <Button
            title="项目与素材回收站"
            onClick={() => setModal("project-trash")}
          >
            <Trash2 size={16} />
          </Button>
          <Button onClick={() => setModal("settings")}>
            <Settings2 size={16} />
            <span>设置</span>
          </Button>
          <div className="avatar">我</div>
        </div>
      </header>
      <div className="workspace">
        <aside className="library-panel">
          <div className="project-switch">
            <span className="eyebrow">当前项目</span>
            <div className="project-row">
              <Folder size={19} />
              <select
                aria-label="切换项目"
                value={store.projectId}
                onChange={(e) => {
                  store.chooseProject(e.target.value);
                  setFocus(null);
                  navigate("/projects/" + e.target.value);
                }}
              >
                {!projects.data?.length && (
                  <option value="">创建第一个项目</option>
                )}
                {projects.data?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.archived_at ? " · 已归档" : ""}
                  </option>
                ))}
              </select>
              <Button
                aria-label="新建项目"
                onClick={() => setModal("new-project")}
              >
                <Plus size={17} />
              </Button>
            </div>
            {project && (
              <button
                className="project-description"
                onClick={() => setModal("edit-project")}
              >
                {project.goal || "添加项目目标与风格，让助手理解你的创作"}
                <MoreHorizontal size={15} />
              </button>
            )}
          </div>
          <div className="library-heading">
            <h2>
              {trash ? "已移除素材" : "项目素材"}{" "}
              <span>{assets.data?.total || 0}</span>
            </h2>
            <Button
              title={trash ? "返回素材" : "素材回收站"}
              onClick={() => setTrash(!trash)}
            >
              <Trash2 size={15} />
            </Button>
          </div>
          <div className="search-box">
            <Search size={15} />
            <input
              aria-label="搜索素材"
              placeholder="搜索名称、备注、描述…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="filter-row">
            {[
              ["", "全部"],
              ["image", "图片"],
              ["video", "视频"],
              ["text", "文本"],
            ].map(([value, label]) => (
              <button
                key={value}
                className={filter === value ? "active" : ""}
                onClick={() => setFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="asset-library">
            {allAssets.map((a) => (
              <button
                key={a.id}
                className={`asset-card ${store.selected.includes(a.id) ? "selected" : ""}`}
                onClick={(e) => {
                  store.chooseAsset(a.id, e.ctrlKey || e.metaKey || e.shiftKey);
                  setFocus(a);
                }}
                onDoubleClick={() => {
                  setFocus(a);
                  setModal("asset");
                }}
              >
                <div className="asset-thumb">
                  <Thumb asset={a} />
                  {tasks.data?.some((t) => t.best_asset_id === a.id) && (
                    <span className="best-badge" title="最佳候选">
                      <Star size={12} fill="currentColor" />
                    </span>
                  )}
                  {store.selected.includes(a.id) && (
                    <span className="selected-tick">
                      <Check size={12} />
                    </span>
                  )}
                  {a.media_type === "video" && (
                    <span className="media-badge">
                      <Play size={11} />
                      {a.media_info.duration?.toFixed(1)}s
                    </span>
                  )}
                  {a.presence !== "available" && (
                    <span className="missing-badge">{labels[a.presence]}</span>
                  )}
                </div>
                <div className="asset-caption">
                  <span title={a.name}>{a.name}</span>
                  <small>
                    {a.media_info.width
                      ? `${a.media_info.width} × ${a.media_info.height}`
                      : bytes(a.byte_size)}
                  </small>
                </div>
              </button>
            ))}
            {assets.isLoading && <LoaderCircle className="spin loader" />}
            {!allAssets.length && !assets.isLoading && (
              <div className="library-empty">
                {search
                  ? "没有匹配的素材"
                  : trash
                    ? "回收站为空"
                    : "作品和导入素材将在这里积累"}
              </div>
            )}
          </div>
          {(assets.data?.total || 0) > 100 && (
            <div className="pagination">
              <Button disabled={page === 0} onClick={() => setPage(page - 1)}>
                上一页
              </Button>
              <span>
                {page + 1} / {Math.ceil(assets.data!.total / 100)}
              </span>
              <Button
                disabled={(page + 1) * 100 >= assets.data!.total}
                onClick={() => setPage(page + 1)}
              >
                下一页
              </Button>
            </div>
          )}
          <div className="library-footer">
            <Button
              disabled={!writable || !!busy}
              onClick={() => files.current?.click()}
            >
              <Upload size={16} />
              导入素材
            </Button>
            <Button
              title="导入文件夹"
              disabled={!writable || !!busy}
              onClick={() => folders.current?.click()}
            >
              <FolderPlus size={16} />
            </Button>
            <Button
              title="跨项目素材库"
              disabled={!writable}
              onClick={() => setModal("reuse")}
            >
              <Copy size={16} />
            </Button>
            <Button
              title="导入ComfyUI既有产物"
              disabled={!writable}
              onClick={() => setModal("comfy-output")}
            >
              <ImagePlus size={16} />
            </Button>
          </div>
          <input
            ref={files}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) importFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <input
            ref={folders}
            type="file"
            multiple
            hidden
            {...({ webkitdirectory: "" } as any)}
            onChange={(e) => {
              if (e.target.files) importFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </aside>
        <main
          className="canvas-panel"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            importFiles(e.dataTransfer.files);
          }}
        >
          <div className="canvas-toolbar">
            <div>
              <span className="eyebrow">创作画布</span>
              <span className="canvas-title">
                {selected.length > 1
                  ? `已选择 ${selected.length} 份素材`
                  : focus?.name || "把想法变成画面"}
              </span>
            </div>
            <div className="canvas-buttons">
              {focus && (
                <>
                  <Button title="素材详情" onClick={() => setModal("asset")}>
                    <SlidersHorizontal size={16} />
                  </Button>
                  <Button title="来源关系" onClick={() => setModal("lineage")}>
                    <GitBranch size={16} />
                  </Button>
                  <Button title="全屏查看" onClick={() => setModal("preview")}>
                    <Maximize2 size={16} />
                  </Button>
                </>
              )}
            </div>
          </div>
          <div className="canvas-stage">
            {!project ? (
              <Empty icon={<FolderPlus size={36} />} title="从一个项目开始">
                <span>为每次创作留下一份可追溯的记录。</span>
                <Button
                  variant="primary"
                  onClick={() => setModal("new-project")}
                >
                  <Plus size={16} />
                  新建项目
                </Button>
              </Empty>
            ) : selected.length > 1 ? (
              <div className="contact-sheet">
                {selected.map((a) => (
                  <div
                    key={a.id}
                    onDoubleClick={() => {
                      store.chooseAsset(a.id);
                      setFocus(a);
                    }}
                  >
                    <Thumb asset={a} />
                    <span>{a.name}</span>
                  </div>
                ))}
              </div>
            ) : focus ? (
              <Media asset={focus} />
            ) : (
              <div className="canvas-welcome">
                <div className="welcome-symbol">
                  <WandSparkles size={38} />
                </div>
                <span className="eyebrow">每一个想法，都值得被看见</span>
                <h1>今天想创作什么？</h1>
                <p>
                  描述你的想法，或导入一张图片。
                  <br />
                  从第一帧开始，慢慢打磨属于你的作品。
                </p>
                <div className="welcome-actions">
                  <Button
                    variant="primary"
                    disabled={!writable}
                    onClick={() => {
                      const value = tools.data?.find(
                        (t) => t.id === "image.generate.zimage",
                      );
                      if (value) openTool(value);
                    }}
                  >
                    <Sparkles size={16} />
                    开始创作
                  </Button>
                  <Button
                    disabled={!writable}
                    onClick={() => files.current?.click()}
                  >
                    <ImagePlus size={16} />
                    导入图片
                  </Button>
                </div>
                <div className="canvas-hint">也可以将文件拖到这里</div>
              </div>
            )}
          </div>
          {focus && (
            <div className="canvas-info">
              <div>
                <span className="badge">{labels[focus.role]}</span>
                <span>
                  {focus.media_info.width
                    ? `${focus.media_info.width} × ${focus.media_info.height}`
                    : focus.media_type}
                </span>
                <span>{bytes(focus.byte_size)}</span>
                {focus.candidate_index !== null && (
                  <span>候选 {focus.candidate_index + 1}</span>
                )}
              </div>
              <div>
                <Button
                  title="下载原文件"
                  onClick={() =>
                    window.open(focus.content_url + "?download=true")
                  }
                >
                  <ArrowDownToLine size={16} />
                </Button>
                {trash ? (
                  <Button
                    onClick={() =>
                      action(
                        "restore",
                        () =>
                          api(
                            `/projects/${project!.id}/assets/${focus.id}/restore`,
                            "POST",
                          ),
                        "素材已恢复",
                      )
                    }
                  >
                    恢复
                  </Button>
                ) : (
                  <Button
                    title="从项目移除，可恢复"
                    disabled={!writable}
                    onClick={() =>
                      action("unlink", async () => {
                        await api(
                          `/projects/${project!.id}/assets/${focus.id}`,
                          "DELETE",
                        );
                        store.clear();
                        setFocus(null);
                      })
                    }
                  >
                    <Trash2 size={16} />
                  </Button>
                )}
              </div>
            </div>
          )}
          <div className="tool-strip">
            <span>创作工具</span>
            {tools.data?.map((t) => (
              <button
                key={t.id}
                disabled={!writable || t.availability !== "available"}
                title={t.issues?.join("；") || t.subtitle}
                onClick={() => openTool(t)}
              >
                {t.category === "video" ? (
                  <Video size={15} />
                ) : t.category === "upscale" ? (
                  <Maximize2 size={15} />
                ) : (
                  <WandSparkles size={15} />
                )}
                {t.name}
              </button>
            ))}
          </div>
        </main>
        <aside className="assistant-panel">
          <div className="assistant-heading">
            <div className="assistant-logo">
              <Sparkles size={17} />
            </div>
            <div>
              <h2>创作助手</h2>
              <span>DeepSeek V4.1 Flash</span>
            </div>
            <span className="assistant-status">
              {busy === "message" || planningTurn ? "思考中" : "单步创作"}
            </span>
          </div>
          <div className="conversation">
            {!messages.data?.length && (
              <div className="assistant-intro">
                <span className="eyebrow">一起把想法往前推进</span>
                <h3>我会为你准备创作方案。</h3>
                <p>
                  告诉我你想要的画面、风格，或希望对当前素材做什么调整。确认方案后开始生成。
                </p>
                <div className="suggestions">
                  {[
                    "生成一组电影感的自然风景",
                    "把选中的图片改成暖色调",
                    "让这张图片动起来",
                  ].map((text) => (
                    <button
                      key={text}
                      disabled={!writable}
                      onClick={() => setMessage(text)}
                    >
                      {text}
                      <ArrowUp size={13} />
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.data?.map((m) => (
              <div key={m.id} className={`chat-message ${m.role}`}>
                <span className="message-role">
                  {m.role === "user"
                    ? "你"
                    : m.role === "assistant"
                      ? "映作"
                      : "任务记录"}{" "}
                  <small>{clock(m.created_at)}</small>
                </span>
                <p>{m.content}</p>
                {!!m.details?.vision_images?.length && (
                  <div className="message-images">
                    {m.details.vision_images.map((image: Data) => (
                      <span
                        key={image.asset_id}
                        title={`素材 ${image.asset_id}`}
                      >
                        附图 {image.index} · {image.name} ·{" "}
                        {imageRoles[image.purpose]}
                        {image.frame === "first" && "（仅动画首帧）"}
                      </span>
                    ))}
                  </div>
                )}
                {!!m.details?.visual_observations?.length && (
                  <details className="visual-observations">
                    <summary>
                      模型图片观察 · {m.details.visual_observations.length} 张
                    </summary>
                    {m.details.visual_observations.map((item: Data) => (
                      <div key={item.asset_id}>
                        <strong>
                          {item.name} · {imageRoles[item.purpose]}
                        </strong>
                        <p>{item.observation}</p>
                        {item.uncertainties && (
                          <small>不确定之处：{item.uncertainties}</small>
                        )}
                        <small>
                          本轮读图尺寸 {item.sent_width} × {item.sent_height}
                        </small>
                      </div>
                    ))}
                  </details>
                )}
              </div>
            ))}
            {(busy === "message" || planningTurn) && (
              <div className="thinking">
                <LoaderCircle className="spin" size={16} />
                正在理解需求、准备方案…
                {planningTurn && (
                  <Button
                    disabled={!!busy}
                    onClick={() =>
                      action("abandon", () =>
                        api(`/turns/${planningTurn.id}/abandon`, "POST"),
                      )
                    }
                  >
                    放弃本轮
                  </Button>
                )}
              </div>
            )}
            {draft && (
              <div className="plan-card">
                <div className="plan-heading">
                  <span>
                    <WandSparkles size={15} />
                    {draft.tool_name}
                  </span>
                  <span>待确认 · v{draft.revision}</span>
                </div>
                <p>{draft.change_summary || "按以下参数执行一次创作。"}</p>
                <div className="plan-specs">
                  <span>{draft.candidate_count} 个候选</span>
                  {draft.parameters.width && (
                    <span>
                      {draft.parameters.width} × {draft.parameters.height}
                    </span>
                  )}
                  {draft.parameters.resolved_seconds && (
                    <span>
                      {draft.parameters.resolved_seconds.toFixed(2)} 秒 ·{" "}
                      {draft.parameters.resolved_frames} 帧
                    </span>
                  )}
                </div>
                {draft.inputs.length > 0 && (
                  <div className="plan-inputs">
                    {draft.inputs.map((input: Data) => (
                      <span key={input.slot}>
                        {
                          tools.data
                            ?.find((t) => t.id === draft.tool_id)
                            ?.inputs.find((i: Data) => i.slot === input.slot)
                            ?.label
                        }
                        ：
                        {allAssets.find((a) => a.id === input.asset_id)?.name ||
                          input.asset_id.slice(0, 8)}
                      </span>
                    ))}
                  </div>
                )}
                <details>
                  <summary>提示词与参数</summary>
                  <p className="prompt-text">
                    {draft.parameters.prompt ||
                      draft.parameters.instruction ||
                      `放大 ${draft.parameters.scale} 倍`}
                  </p>
                  <small>种子：{draft.seed_list.join("、")}</small>
                </details>
                <div className="plan-actions">
                  <Button
                    onClick={() => {
                      const t = tools.data?.find((t) => t.id === draft.tool_id);
                      if (t) openTool(t, draft);
                    }}
                  >
                    <SlidersHorizontal size={14} />
                    修改
                  </Button>
                  <Button
                    title="放弃方案"
                    disabled={!!busy}
                    onClick={() =>
                      action("dismiss", () =>
                        api(`/plans/${draft.id}/dismiss`, "POST", {
                          revision: draft.revision,
                        }),
                      )
                    }
                  >
                    <X size={15} />
                  </Button>
                  <Button
                    variant="primary"
                    disabled={!writable || !!busy}
                    onClick={() => execute(draft)}
                  >
                    <Play size={14} />
                    确认执行
                  </Button>
                </div>
              </div>
            )}
            <div ref={chatEnd} />
          </div>
          <form className="composer" onSubmit={send}>
            {store.selected.length > 0 && (
              <>
                <div className="context-selection">
                  <Layers3 size={13} />
                  已加入 {store.selected.length} 份素材
                  <Button
                    aria-label="取消素材选择"
                    onClick={store.clear}
                    type="button"
                  >
                    <X size={13} />
                  </Button>
                </div>
                <div className="selected-context-assets">
                  {selected.map((asset) => (
                    <div className="selected-context-asset" key={asset.id}>
                      <Thumb asset={asset} />
                      <div>
                        <span title={asset.name}>{asset.name}</span>
                        {asset.media_type === "image" ? (
                          <select
                            aria-label={`${asset.name} 的读图用途`}
                            value={store.imageRoles[asset.id] ?? "auto"}
                            disabled={!!planningTurn || busy === "message"}
                            onChange={(e) =>
                              store.setImageRole(asset.id, e.target.value)
                            }
                          >
                            {Object.entries(imageRoles).map(
                              ([value, label]) => (
                                <option key={value} value={value}>
                                  {label}
                                </option>
                              ),
                            )}
                          </select>
                        ) : (
                          <small>仅发送文件信息，尚不读取内容</small>
                        )}
                      </div>
                      <Button
                        type="button"
                        aria-label={`取消选择 ${asset.name}`}
                        onClick={() => store.chooseAsset(asset.id, true)}
                      >
                        <X size={12} />
                      </Button>
                    </div>
                  ))}
                </div>
                <div
                  className={`vision-notice ${imageCount > visionLimit ? "over-limit" : ""}`}
                >
                  {selectedAssets.isPending
                    ? "正在读取素材信息…"
                    : selectedAssets.isError
                      ? "素材信息读取失败，请重新选择。"
                      : imageCount
                        ? `本轮 ${imageCount} 张图片将发送至 DeepSeek 读图（上限 ${visionLimit} 张）`
                        : "本轮仅发送素材文字信息"}
                </div>
              </>
            )}
            <textarea
              aria-label="发送给创作助手"
              placeholder={
                !health.data?.planner.configured
                  ? "先在设置中配置 DeepSeek 官方 API key"
                  : "描述想法，选中图片或粘贴本工作台的图片链接…"
              }
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              disabled={
                !writable ||
                !!running.length ||
                !!planningTurn ||
                busy === "message"
              }
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
            />
            {message.includes("/api/v1/assets/") && (
              <div className="vision-notice">
                当前项目的本工作台图片链接会自动加入读图，无需重复选择素材。
              </div>
            )}
            <div className="composer-footer">
              <span>
                {running.length
                  ? "等待本次结果，再继续打磨"
                  : project?.archived_at
                    ? "项目已归档，解除归档后继续"
                    : "确认后执行 · 完成后等待你的反馈"}
              </span>
              <Button
                variant="primary"
                type="submit"
                aria-label="发送"
                disabled={
                  !writable ||
                  !!planningTurn ||
                  !!busy ||
                  !!running.length ||
                  imageCount > visionLimit ||
                  (!!store.selected.length &&
                    (selectedAssets.isPending || selectedAssets.isError)) ||
                  !message.trim() ||
                  !health.data?.planner.configured
                }
              >
                <ArrowUp size={17} />
              </Button>
            </div>
          </form>
        </aside>
      </div>
      <footer className="task-bar">
        <button onClick={() => store.setTaskOpen(!store.taskOpen)}>
          <Layers3 size={16} />
          <strong>任务队列</strong>
          {running.length > 0 && (
            <span className="count-dot">{running.length}</span>
          )}
          <span>
            {running.length
              ? running[0].tool_name + " · " + labels[running[0].state]
              : "准备就绪"}
          </span>
          <ChevronDown size={14} className={store.taskOpen ? "rotate" : ""} />
        </button>
        <div>
          <span className="status-dot green" />
          素材保存在本机
          <span className="top-separator" />
          {health.data?.storage &&
            bytes(health.data.storage.free_bytes) + " 可用"}
        </div>
      </footer>
      {store.taskOpen && (
        <div className="task-drawer">
          <div className="drawer-heading">
            <h2>任务与结果</h2>
            <span>候选串行执行，取消只影响本应用对应任务</span>
            <Button onClick={() => store.setTaskOpen(false)}>
              <X size={17} />
            </Button>
          </div>
          {tasks.data?.length ? (
            tasks.data.map((t) => (
              <div className="task-row" key={t.id}>
                <div className={`task-state ${t.state}`}>
                  {activeStates.includes(t.state) ? (
                    <LoaderCircle className="spin" size={17} />
                  ) : t.state === "completed" ? (
                    <Check size={17} />
                  ) : (
                    <Circle size={17} />
                  )}
                </div>
                <div className="task-name">
                  <strong>{t.tool_name}</strong>
                  <small>
                    {clock(t.created_at)} · {t.candidate_count} 个候选
                  </small>
                </div>
                <span className="task-label">{labels[t.state]}</span>
                <div className="task-progress">
                  {t.executions.find(
                    (e: Data) => !TERMINAL_UI.includes(e.execution_state),
                  )?.progress.stage ||
                    t.error?.message ||
                    "执行与保存记录已留存"}
                </div>
                <Button
                  onClick={() =>
                    action("detail", async () => {
                      setTaskDetail(await api(`/tasks/${t.id}`));
                      setModal("task");
                    })
                  }
                >
                  详情
                </Button>
                {activeStates.includes(t.state) && (
                  <Button
                    variant="danger"
                    disabled={!!busy || t.cancel_requested}
                    onClick={() =>
                      action("cancel", () =>
                        api(`/tasks/${t.id}/cancel`, "POST"),
                      )
                    }
                  >
                    取消
                  </Button>
                )}
              </div>
            ))
          ) : (
            <Empty icon={<Clock3 />} title="还没有任务">
              准备一个方案并确认执行，结果会自动归档。
            </Empty>
          )}
        </div>
      )}
      {toast && (
        <div className="toast" role="status">
          {toast}
          <button aria-label="关闭提示" onClick={() => setToast("")}>
            <X size={15} />
          </button>
        </div>
      )}
      {(modal === "new-project" || modal === "edit-project") && (
        <ProjectEditor
          project={modal === "edit-project" ? project : undefined}
          onClose={() => setModal(null)}
          onSave={(p) => {
            refresh();
            store.chooseProject(p.id);
            navigate("/projects/" + p.id);
            setModal(null);
          }}
          onAction={action}
        />
      )}
      {modal === "tool" && tool && (
        <ToolEditor
          tool={tool}
          assets={allAssets}
          selection={store.selected}
          plan={editingPlan}
          projectId={project!.id}
          busy={!!busy}
          onClose={() => setModal(null)}
          onSubmit={(body) =>
            action(
              "plan",
              async () => {
                await api(
                  editingPlan
                    ? `/plans/${editingPlan.id}`
                    : `/projects/${project!.id}/plans`,
                  editingPlan ? "PATCH" : "POST",
                  {
                    ...body,
                    ...(editingPlan ? { revision: editingPlan.revision } : {}),
                  },
                );
                setModal(null);
              },
              "方案已准备，请确认执行",
            )
          }
        />
      )}
      {modal === "stale" && editingPlan && (
        <Modal
          title="项目背景已改变"
          description="这份方案使用了修改前的项目背景。"
          onClose={() => setModal(null)}
        >
          <p className="muted">
            可以返回修改方案，使它使用当前背景；也可以明确采用之前准备的方案。
          </p>
          <div className="modal-actions">
            <Button
              onClick={() => {
                const t = tools.data?.find((t) => t.id === editingPlan.tool_id);
                if (t) openTool(t, editingPlan);
              }}
            >
              修改方案
            </Button>
            <Button
              variant="primary"
              onClick={() =>
                action("execute", async () => {
                  await api(`/plans/${editingPlan.id}/execute`, "POST", {
                    revision: editingPlan.revision,
                    plan_hash: editingPlan.plan_hash,
                    idempotency_key: crypto.randomUUID(),
                    accept_previous_context: true,
                  });
                  setModal(null);
                })
              }
            >
              采用原方案执行
            </Button>
          </div>
        </Modal>
      )}
      {modal === "settings" && (
        <SettingsDialog
          action={action}
          onClose={() => setModal(null)}
          busy={!!busy}
        />
      )}
      {modal === "reuse" && (
        <ReuseDialog
          projectId={project!.id}
          onClose={() => setModal(null)}
          action={action}
        />
      )}
      {modal === "comfy-output" && (
        <ComfyOutputDialog
          projectId={project!.id}
          action={action}
          onClose={() => setModal(null)}
          onImported={(batch) => {
            setImportResult(batch);
            setModal("import-result");
          }}
        />
      )}
      {modal === "asset" && focus && (
        <AssetDialog
          asset={focus}
          project={project!}
          onClose={() => setModal(null)}
          action={action}
          onLineage={() => setModal("lineage")}
        />
      )}
      {modal === "preview" && focus && (
        <Modal title={focus.name} onClose={() => setModal(null)} wide>
          <div className="full-preview">
            <Media asset={focus} />
          </div>
        </Modal>
      )}
      {modal === "lineage" && focus && (
        <LineageDialog asset={focus} onClose={() => setModal(null)} />
      )}
      {modal === "task" && taskDetail && (
        <TaskDialog
          task={taskDetail}
          onClose={() => setModal(null)}
          action={action}
          onPlan={(p) => {
            refresh();
            setModal(null);
            setToast("重做方案已准备，请确认执行");
            store.setTaskOpen(false);
          }}
        />
      )}
      {modal === "import-result" && (
        <Modal title="导入结果" onClose={() => setModal(null)}>
          <div className="import-results">
            {importResult?.items.map((i: Data, index: number) => (
              <div key={index}>
                <span>{i.name}</span>
                <span
                  className={
                    i.state === "saved" ? "success-text" : "error-text"
                  }
                >
                  {i.state === "saved" ? "已归档" : i.error}
                </span>
              </div>
            ))}
          </div>
        </Modal>
      )}
      {modal === "project-trash" && (
        <ProjectTrash onClose={() => setModal(null)} action={action} />
      )}
      {modal === "about" && (
        <Modal
          title="映作 · AIV Studio"
          description="个人创作助手 · 0.1.0"
          onClose={() => setModal(null)}
        >
          <p>
            项目、素材与执行记录保存在本机。助手每轮只准备一个操作，确认后使用现有
            ComfyUI 工作流执行。
          </p>
          <p className="muted">
            文字需求及所选素材的描述会发送给 DeepSeek 官方
            API。图片与视频生成由本机 ComfyUI 完成。
          </p>
        </Modal>
      )}
    </div>
  );
}

const TERMINAL_UI = [
  "succeeded",
  "failed",
  "cancelled",
  "unknown",
  "interrupted",
];
type Action = (
  name: string,
  operation: () => Promise<unknown>,
  success?: string,
) => Promise<unknown>;
function Media({ asset }: { asset: Data }) {
  if (asset.presence !== "available")
    return (
      <Empty icon={<FileText size={30} />} title={labels[asset.presence]}>
        可以在任务详情中补存已有输出，或重新导入原文件。
      </Empty>
    );
  if (asset.media_type === "image")
    return (
      <img className="canvas-image" src={asset.content_url} alt={asset.name} />
    );
  if (asset.media_type === "video")
    return (
      <video
        className="canvas-image"
        key={asset.id}
        src={asset.content_url}
        poster={asset.preview_url}
        controls
      />
    );
  if (asset.media_type === "audio")
    return (
      <div className="audio-preview">
        <Video size={30} />
        <h3>{asset.name}</h3>
        <audio src={asset.content_url} controls />
      </div>
    );
  if (asset.media_type === "text")
    return (
      <pre className="text-preview">
        {asset.media_info.text_preview || asset.media_info.preview_error}
      </pre>
    );
  return (
    <Empty icon={<FileText size={30} />} title={asset.name}>
      <Button onClick={() => window.open(asset.content_url)}>下载原文件</Button>
    </Empty>
  );
}

function ProjectEditor({
  project,
  onClose,
  onSave,
  onAction,
}: {
  project?: Data;
  onClose: () => void;
  onSave: (p: Data) => void;
  onAction: Action;
}) {
  const [loadedRevision] = useState(project?.revision);
  const { register, handleSubmit } = useForm({
    defaultValues: {
      name: project?.name || "",
      goal: project?.goal || "",
      style: project?.style || "",
    },
  });
  return (
    <Modal
      title={project ? "项目背景" : "新建创作项目"}
      description="写下目标和风格，作为每次创作的共同背景。"
      onClose={onClose}
    >
      <form
        onSubmit={handleSubmit(async (values) => {
          await onAction("project", async () => {
            const p = await api(
              project ? `/projects/${project.id}` : "/projects",
              project ? "PATCH" : "POST",
              { ...values, ...(project ? { revision: loadedRevision } : {}) },
            );
            onSave(p);
          });
        })}
      >
        <label>
          项目名称
          <input
            {...register("name", { required: true, maxLength: 120 })}
            required
            disabled={!!project?.archived_at}
            placeholder="例如：城市漫游短片"
          />
        </label>
        <label>
          创作目标
          <textarea
            {...register("goal")}
            rows={3}
            disabled={!!project?.archived_at}
            placeholder="作品要表达什么？用在哪里？"
          />
        </label>
        <label>
          风格与约束
          <textarea
            {...register("style")}
            rows={3}
            disabled={!!project?.archived_at}
            placeholder="色彩、画幅、视觉风格、希望避免的内容…"
          />
        </label>
        <div className="modal-actions">
          {project && (
            <>
              <Button
                type="button"
                onClick={() =>
                  onAction("archive", async () => {
                    const p = await api(
                      `/projects/${project.id}/archive`,
                      "POST",
                      { archived: !project.archived_at },
                    );
                    onSave(p);
                  })
                }
              >
                <Archive size={15} />
                {project.archived_at ? "解除归档" : "归档"}
              </Button>
              <Button
                variant="danger"
                type="button"
                onClick={() =>
                  onAction("remove-project", async () => {
                    await api(`/projects/${project.id}`, "DELETE");
                    onClose();
                  })
                }
              >
                <Trash2 size={15} />
                移除项目
              </Button>
            </>
          )}
          <Button
            variant="primary"
            type="submit"
            disabled={!!project?.archived_at}
          >
            保存背景
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ToolEditor({
  tool,
  assets,
  selection,
  plan,
  projectId,
  busy,
  onClose,
  onSubmit,
}: {
  tool: Data;
  assets: Data[];
  selection: string[];
  plan?: Data | null;
  projectId: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (body: Data) => void;
}) {
  const [params, setParams] = useState<Data>(() =>
    Object.fromEntries(
      tool.fields.map((f: Data) => [
        f.name,
        plan?.parameters[f.name] ?? f.default,
      ]),
    ),
  );
  const [inputs, setInputs] = useState<Data>(() =>
    Object.fromEntries(
      tool.inputs.map((i: Data, index: number) => [
        i.slot,
        plan?.inputs.find((r: Data) => r.slot === i.slot)?.asset_id ||
          selection[index] ||
          "",
      ]),
    ),
  );
  const [count, setCount] = useState(plan?.candidate_count || 1);
  const [summary, setSummary] = useState(plan?.change_summary || "");
  const wantedIds = [
    ...new Set([
      ...selection,
      ...(plan?.inputs || []).map((i: Data) => i.asset_id),
    ]),
  ].filter((id) => !assets.some((a) => a.id === id));
  const additional = useQuery({
    queryKey: ["tool-inputs", projectId, wantedIds],
    queryFn: () => Promise.all(wantedIds.map((id) => api(`/assets/${id}`))),
    enabled: wantedIds.length > 0,
  });
  const images = [
    ...assets,
    ...(additional.data || []).map(
      (a): Data => ({
        ...a,
        project: a.projects.find((p: Data) => p.project_id === projectId),
      }),
    ),
  ].filter(
    (a) =>
      a.media_type === "image" &&
      a.presence === "available" &&
      !a.project?.removed_at,
  );
  return (
    <Modal
      title={plan ? `修改方案 · ${tool.name}` : tool.name}
      description={`${tool.subtitle} · 参数确认后创建方案`}
      onClose={onClose}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit({
            tool_id: tool.id,
            parameters: params,
            inputs: tool.inputs.map((i: Data) => ({
              slot: i.slot,
              asset_id: inputs[i.slot],
            })),
            candidate_count: count,
            change_summary: summary,
            based_on_execution_id: plan?.based_on_execution_id || null,
          });
        }}
      >
        <div className="tool-form">
          {tool.inputs.map((input: Data) => (
            <label key={input.slot}>
              {input.label}
              <select
                required
                value={inputs[input.slot]}
                onChange={(e) =>
                  setInputs({ ...inputs, [input.slot]: e.target.value })
                }
              >
                <option value="">选择项目图片</option>
                {images.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
              {inputs[input.slot] && (
                <div className="input-preview">
                  <Thumb
                    asset={
                      images.find((a) => a.id === inputs[input.slot]) || {}
                    }
                  />
                </div>
              )}
            </label>
          ))}
          <div className="parameter-grid">
            {tool.fields.map((field: Data) => (
              <label
                className={field.type === "textarea" ? "wide-field" : ""}
                key={field.name}
              >
                {field.label}
                {field.type === "textarea" ? (
                  <textarea
                    required={field.required}
                    rows={field.name === "negative_prompt" ? 2 : 4}
                    maxLength={12000}
                    value={params[field.name] ?? ""}
                    onChange={(e) =>
                      setParams({ ...params, [field.name]: e.target.value })
                    }
                    placeholder={
                      field.name === "negative_prompt"
                        ? "可选"
                        : "具体描述主体、环境、动作和风格…"
                    }
                  />
                ) : field.type === "select" ? (
                  <select
                    value={params[field.name]}
                    onChange={(e) => {
                      const next: Data = {
                        ...params,
                        [field.name]: e.target.value,
                      };
                      if (
                        tool.id === "image.generate.qwen" &&
                        field.name === "preset" &&
                        ((params.width === 1024 && params.height === 1024) ||
                          (params.width === 1328 && params.height === 1328))
                      ) {
                        next.width = next.height =
                          e.target.value === "quality" ? 1328 : 1024;
                      }
                      setParams(next);
                    }}
                  >
                    {field.options.map((o: Data) => (
                      <option value={o.value} key={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="number"
                    min={field.min ?? 0}
                    max={field.max ?? 9007199254740991}
                    step={
                      tool.id === "image.generate.qwen" &&
                      ["width", "height"].includes(field.name)
                        ? 16
                        : (field.step ?? 1)
                    }
                    value={params[field.name] ?? ""}
                    placeholder={
                      field.type === "seed" ? "留空随机" : "跟随输入"
                    }
                    onChange={(e) =>
                      setParams({
                        ...params,
                        [field.name]:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  />
                )}
              </label>
            ))}
          </div>
          {tool.category === "generate" && (
            <label>
              候选数量
              <select
                value={count}
                onChange={(e) => setCount(Number(e.target.value))}
              >
                {[1, 2, 3, 4].map((n) => (
                  <option key={n} value={n}>
                    {n} 张 · 串行生成
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            这次想改变什么
            <textarea
              rows={2}
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="可选：记录本次改动，方便后续追溯"
            />
          </label>
          {tool.category === "video" && (
            <p className="muted">
              输出为24fps，实际时长按工作流帧数规则对齐；确认卡会显示实际秒数。改变比例会拉伸输入图片。
            </p>
          )}
        </div>
        <div className="modal-actions">
          <Button type="button" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" type="submit" disabled={busy}>
            <Check size={15} />
            保存方案
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function AssetDialog({
  asset,
  project,
  onClose,
  action,
  onLineage,
}: {
  asset: Data;
  project: Data;
  onClose: () => void;
  action: Action;
  onLineage: () => void;
}) {
  const detail = useQuery({
    queryKey: ["detail", asset.id],
    queryFn: () => api(`/assets/${asset.id}`),
  });
  const [note, setNote] = useState(asset.project?.note || ""),
    [tags, setTags] = useState((asset.project?.tags || []).join("，")),
    [usage, setUsage] = useState(asset.project?.usage || "material");
  const [loadedRevision] = useState(asset.project?.revision);
  return (
    <Modal title="素材详情" description={asset.name} onClose={onClose}>
      <div className="asset-detail-thumb">
        <Thumb asset={asset} />
      </div>
      <div className="info-grid">
        <span>
          类型<strong>{asset.media_type}</strong>
        </span>
        <span>
          状态<strong>{labels[asset.presence]}</strong>
        </span>
        <span>
          尺寸
          <strong>
            {asset.media_info.width
              ? `${asset.media_info.width} × ${asset.media_info.height}`
              : "—"}
          </strong>
        </span>
        <span>
          文件大小<strong>{bytes(asset.byte_size)}</strong>
        </span>
      </div>
      <label>
        在本项目中的用途
        <select value={usage} onChange={(e) => setUsage(e.target.value)}>
          <option value="material">创作素材</option>
          <option value="reference">参考素材</option>
          <option value="output">作品输出</option>
        </select>
      </label>
      <label>
        项目备注
        <textarea
          rows={3}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="保留了什么、还想调整什么…"
        />
      </label>
      <label>
        标签
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="用逗号分隔"
        />
      </label>
      {detail.data?.execution && (
        <details>
          <summary>生成参数与记录</summary>
          <pre className="json-view">
            {JSON.stringify(
              {
                execution_id: detail.data.execution.id,
                seed: detail.data.execution.seed,
                parameters: detail.data.execution.parameters,
                tool: detail.data.execution.tool_id,
                version: detail.data.execution.tool_version,
                prompt_id: detail.data.execution.prompt_id,
              },
              null,
              2,
            )}
          </pre>
        </details>
      )}
      <div className="modal-actions">
        <Button onClick={onLineage}>
          <GitBranch size={15} />
          来源关系
        </Button>
        {detail.data?.execution && asset.role === "final" && (
          <Button
            onClick={() =>
              action(
                "best",
                () =>
                  api(`/tasks/${detail.data!.execution.task_id}/best`, "POST", {
                    asset_id: asset.id,
                  }),
                "已标记为最佳候选",
              )
            }
          >
            <Star size={15} />
            最佳候选
          </Button>
        )}
        <Button
          variant="primary"
          disabled={!!project.archived_at}
          onClick={() =>
            action("note", async () => {
              await api(`/projects/${project.id}/assets/${asset.id}`, "PATCH", {
                note,
                tags: tags
                  .split(/[,，]/)
                  .map((s: string) => s.trim())
                  .filter(Boolean),
                usage,
                revision: loadedRevision,
              });
              onClose();
            })
          }
        >
          保存备注
        </Button>
      </div>
    </Modal>
  );
}

function LineageDialog({
  asset,
  onClose,
}: {
  asset: Data;
  onClose: () => void;
}) {
  const result = useQuery({
    queryKey: ["lineage", asset.id],
    queryFn: () => api(`/assets/${asset.id}/lineage`),
  });
  const nodes: Node[] = (result.data?.nodes || []).map(
    (a: Data, i: number) => ({
      id: a.id,
      position: { x: Math.floor(i / 3) * 280, y: (i % 3) * 170 },
      data: {
        label: (
          <div className="graph-card">
            <div>
              <Thumb asset={a} />
            </div>
            <strong>{a.name}</strong>
            <small>
              {labels[a.role]} · {labels[a.presence]}
            </small>
          </div>
        ),
      },
      style: {
        width: 220,
        borderColor: a.id === asset.id ? "#eea56b" : "#414247",
        background: "#242528",
        color: "#eee",
      },
    }),
  );
  const edges: Edge[] = (result.data?.edges || []).map((r: Data) => ({
    id: r.id,
    source: r.from_asset_id,
    target: r.to_asset_id,
    label: r.slot === "reference_image" ? "参考" : "输入",
    style: { stroke: "#bf906b" },
    labelStyle: { fill: "#ccc" },
    labelBgStyle: { fill: "#242528" },
  }));
  return (
    <Modal
      title="素材来源关系"
      description="查看输入图片、派生结果与参考关系，最多展开三层。"
      onClose={onClose}
      wide
    >
      <div className="graph-view">
        {result.isLoading ? (
          <LoaderCircle className="spin" />
        ) : (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            fitView
            colorMode="dark"
            nodesDraggable
          >
            <Background />
            <Controls />
          </ReactFlow>
        )}
      </div>
    </Modal>
  );
}

function ReuseDialog({
  projectId,
  onClose,
  action,
}: {
  projectId: string;
  onClose: () => void;
  action: Action;
}) {
  const [q, setQ] = useState("");
  const result = useQuery({
    queryKey: ["global-assets", q],
    queryFn: () => api("/assets?q=" + encodeURIComponent(q)),
  });
  const seen = new Set<string>();
  const items = (result.data?.items || []).filter((a: Data) => {
    if (seen.has(a.id)) return false;
    seen.add(a.id);
    return a.project.project_id !== projectId;
  });
  return (
    <Modal
      title="从其他项目复用"
      description="原文件和来源记录保持同一份，当前项目可以拥有自己的备注。"
      onClose={onClose}
      wide
    >
      <div className="search-box">
        <Search size={16} />
        <input
          autoFocus
          placeholder="搜索所有项目的素材"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      <div className="reuse-grid">
        {items.map((a: Data) => (
          <div key={a.id}>
            <div className="reuse-thumb">
              <Thumb asset={a} />
            </div>
            <span>{a.name}</span>
            <Button
              disabled={a.presence !== "available"}
              onClick={() =>
                action(
                  "attach",
                  () =>
                    api(`/projects/${projectId}/assets/${a.id}/attach`, "POST"),
                  "已添加到当前项目",
                )
              }
            >
              <Plus size={14} />
              复用
            </Button>
          </div>
        ))}
      </div>
      {!items.length && <Empty icon={<Copy />} title="暂无其他项目素材" />}
    </Modal>
  );
}

function TaskDialog({
  task,
  onClose,
  action,
  onPlan,
}: {
  task: Data;
  onClose: () => void;
  action: Action;
  onPlan: (p: Data) => void;
}) {
  const [current, setCurrent] = useState(task);
  return (
    <Modal
      title="任务详情"
      description={`${labels[current.state]} · ${clock(current.created_at)}`}
      onClose={onClose}
      wide
    >
      <div className="execution-list">
        {current.executions.map((e: Data) => (
          <div className="execution-card" key={e.id}>
            <div className="execution-heading">
              <strong>候选 {e.candidate_index + 1}</strong>
              <span>
                {labels[e.execution_state]} / {labels[e.storage_state]}
              </span>
            </div>
            <small>
              种子 {e.seed} · 工具 {e.tool_id} · v{e.tool_version}
            </small>
            {e.error && <p className="error-text">{e.error.message}</p>}
            <div className="artifact-list">
              {e.artifacts?.map((a: Data) => (
                <div key={a.id}>
                  <span>
                    {labels[a.role]} · {a.reference.filename}
                  </span>
                  <span>
                    {labels[a.state] || a.state}
                    {a.error ? " · " + a.error : ""}
                  </span>
                </div>
              ))}
            </div>
            <div className="execution-actions">
              {TERMINAL_UI.includes(e.execution_state) && (
                <Button
                  onClick={() =>
                    action(
                      "collect",
                      async () =>
                        setCurrent(
                          await api(`/executions/${e.id}/collect`, "POST"),
                        ),
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
            <details>
              <summary>请求、历史与输出证据</summary>
              <pre className="json-view">{JSON.stringify(e, null, 2)}</pre>
            </details>
          </div>
        ))}
      </div>
      <div className="modal-actions">
        <Button
          onClick={() =>
            action("task-refresh", async () =>
              setCurrent(await api(`/tasks/${task.id}`)),
            )
          }
        >
          <RefreshCw size={14} />
          刷新
        </Button>
        {!activeStates.includes(current.state) && (
          <Button
            variant="primary"
            onClick={() =>
              action("retry", async () =>
                onPlan(await api(`/tasks/${task.id}/retry`, "POST")),
              )
            }
          >
            准备重做方案
          </Button>
        )}
      </div>
    </Modal>
  );
}

function SettingsDialog({
  action,
  onClose,
  busy,
}: {
  action: Action;
  onClose: () => void;
  busy: boolean;
}) {
  const result = useQuery({
    queryKey: ["settings"],
    queryFn: () => api("/settings"),
  });
  const backups = useQuery({
    queryKey: ["backups"],
    queryFn: () => api<Data[]>("/backups"),
  });
  const [key, setKey] = useState(""),
    [values, setValues] = useState<Data>({}),
    [gc, setGc] = useState<Data | null>(null),
    [diagnostics, setDiagnostics] = useState<Data | null>(null);
  const restoreFile = useRef<HTMLInputElement>(null);
  const s = result.data;
  return (
    <Modal
      title="设置与本机状态"
      description="DeepSeek 官方模型、资源预算和素材维护。"
      onClose={onClose}
      wide
    >
      {!s ? (
        <LoaderCircle className="spin" />
      ) : (
        <div className="settings-content">
          <section>
            <h3>DeepSeek 官方 API</h3>
            <div className="settings-summary">
              <span
                className={`status-dot ${s.api_key_configured ? "green" : ""}`}
              />
              {s.api_key_configured ? "已配置密钥" : "尚未配置密钥"}
              <span className="badge">{s.llm_model}</span>
            </div>
            <label>
              官方 API Key
              <input
                type="password"
                autoComplete="new-password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="密钥保存到 Windows 凭据管理器"
              />
            </label>
            <div className="inline-actions">
              <Button
                variant="primary"
                disabled={!key || busy}
                onClick={() =>
                  action(
                    "key",
                    async () => {
                      await api("/settings/credentials", "PUT", {
                        api_key: key,
                      });
                      setKey("");
                    },
                    "密钥已保存",
                  )
                }
              >
                <Check size={14} />
                保存密钥
              </Button>
              <Button
                disabled={s.api_key_from_environment || busy}
                onClick={() =>
                  action(
                    "key",
                    () => api("/settings/credentials", "PUT", { api_key: "" }),
                    "已移除保存的密钥",
                  )
                }
              >
                移除密钥
              </Button>
            </div>
            {s.api_key_from_environment && (
              <small>
                当前进程使用 DEEPSEEK_API_KEY 环境变量；更改环境变量后需重启。
              </small>
            )}
            <p className="muted">
              助手会发送需求、项目背景、素材描述和选中图片的读图副本至
              DeepSeek。视频暂只发送文件信息。图像与视频生成在本机完成；密钥不写入工作流或备份。
            </p>
            <p className="muted">
              输入上限 {(s.max_input_tokens / 1024).toFixed(0)}K tokens，达到
              {((s.history_summary_threshold_tokens ?? 98304) / 1024).toFixed(
                0,
              )}
              K 时整理较早对话，输出上限{" "}
              {(s.max_output_tokens / 1024).toFixed(0)}K。
              原始聊天记录完整保存在本机，历史整理调用计入模型费用。
            </p>
            <div className="budget-card">
              <span>
                今日费用估算<strong>${s.usage.estimated_usd.toFixed(5)}</strong>
              </span>
              <span>
                预算<strong>${s.daily_budget_usd.toFixed(2)}</strong>
              </span>
              <span>
                模型调用<strong>{s.usage.call_count}</strong>
              </span>
            </div>
            <small>
              按高峰价格保守估算，实际账单以 DeepSeek 为准。未知费用调用：
              {s.usage.unknown_calls}。
            </small>
          </section>
          <section>
            <h3>预算与资源</h3>
            <div className="parameter-grid">
              {[
                ["daily_budget_usd", "每日模型预算（USD）", 0.01],
                ["turn_budget_usd", "每轮模型预算（USD）", 0.001],
                ["image_timeout_seconds", "图片执行超时（秒）", 1],
                ["video_timeout_seconds", "视频执行超时（秒）", 1],
                ["upscale_max_edge", "放大最长边（px）", 16],
                [
                  "auto_free_after_idle_seconds",
                  "空闲释放模型（秒，0关闭）",
                  1,
                ],
                ["max_outstanding_tasks", "队列最大任务数", 1],
              ].map(([name, label, step]) => (
                <label key={name as string}>
                  {label}
                  <input
                    type="number"
                    step={step as number}
                    value={values[name as string] ?? s[name as string]}
                    onChange={(e) =>
                      setValues({ ...values, [name]: Number(e.target.value) })
                    }
                  />
                </label>
              ))}
            </div>
            <Button
              disabled={busy}
              onClick={() =>
                action(
                  "settings",
                  async () => {
                    await api("/settings", "PATCH", {
                      ...values,
                      revision: s.revision,
                    });
                    setValues({});
                  },
                  "设置已保存",
                )
              }
            >
              保存资源设置
            </Button>
          </section>
          <section>
            <h3>本机连接与诊断</h3>
            <p className="muted">
              ComfyUI：{s.comfy_url}
              <br />
              素材目录：{s.data_dir}
            </p>
            <Button
              disabled={busy}
              onClick={() =>
                action("diagnostics", async () =>
                  setDiagnostics(await api("/diagnostics")),
                )
              }
            >
              <RefreshCw size={14} />
              核实本机状态
            </Button>
            {diagnostics && (
              <details open>
                <summary>
                  诊断信息 · 数据库 {diagnostics.application.database}
                </summary>
                <pre className="json-view">
                  {JSON.stringify(diagnostics, null, 2)}
                </pre>
              </details>
            )}
          </section>
          <section>
            <h3>备份与恢复</h3>
            <p className="muted">
              包含数据库、素材原文件、工具版本与执行记录。恢复到独立目录，校验完成后可修改配置启用。
            </p>
            <div className="inline-actions">
              <Button
                disabled={busy}
                onClick={() =>
                  action(
                    "backup",
                    async () => {
                      const b = await api("/backups", "POST");
                      window.open(`/api/v1/backups/${b.id}`);
                    },
                    "一致备份已完成",
                  )
                }
              >
                <ArrowDownToLine size={15} />
                创建完整备份
              </Button>
              <Button
                disabled={busy}
                onClick={() => restoreFile.current?.click()}
              >
                <Upload size={15} />
                恢复备份
              </Button>
            </div>
            <input
              type="file"
              accept=".zip"
              ref={restoreFile}
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file)
                  action("restore-backup", async () => {
                    const form = new FormData();
                    form.append("file", file);
                    const restored = await api(
                      "/backups/restore",
                      "POST",
                      form,
                    );
                    alert(restored.message + "\n" + restored.directory);
                  });
                e.target.value = "";
              }}
            />
            {backups.data?.map((b) => (
              <a
                className="backup-row"
                href={`/api/v1/backups/${b.id}`}
                key={b.id}
              >
                {clock(b.created_at)}
                <span>{bytes(b.data.bytes)} · 下载备份</span>
              </a>
            ))}
          </section>
          <section>
            <h3>永久文件清理</h3>
            <p className="muted">
              普通移除可以恢复。永久清理只处理已无项目或待执行方案引用的文件，清理后保留来源记录。
            </p>
            <Button
              disabled={busy}
              onClick={() =>
                action("gc-preview", async () =>
                  setGc(await api("/gc/preview", "POST")),
                )
              }
            >
              <Trash2 size={14} />
              预览可清理文件
            </Button>
            {gc && (
              <div className="gc-preview">
                <p>
                  {gc.data.files.length} 个文件，共 {bytes(gc.data.byte_size)}
                </p>
                {gc.data.files.map((f: Data) => (
                  <div key={f.blob_id}>
                    {f.assets.map((a: Data) => a.name).join("、")} ·{" "}
                    {bytes(f.bytes)}
                  </div>
                ))}
                <Button
                  variant="danger"
                  disabled={busy || !gc.data.files.length}
                  onClick={() =>
                    action(
                      "gc",
                      async () => {
                        await api(`/gc/${gc.id}/commit`, "POST", {
                          preview_hash: gc.data.hash,
                        });
                        setGc(null);
                      },
                      "已永久清理预览中的文件",
                    )
                  }
                >
                  确认永久删除以上原文件
                </Button>
              </div>
            )}
          </section>
        </div>
      )}
    </Modal>
  );
}

function ComfyOutputDialog({
  projectId,
  action,
  onClose,
  onImported,
}: {
  projectId: string;
  action: Action;
  onClose: () => void;
  onImported: (batch: Data) => void;
}) {
  const [q, setQ] = useState(""),
    [offset, setOffset] = useState(0),
    [selected, setSelected] = useState<string[]>([]);
  const result = useQuery({
    queryKey: ["comfy-output", q, offset],
    queryFn: () =>
      api(`/comfy-output-files?q=${encodeURIComponent(q)}&offset=${offset}`),
  });
  useEffect(() => setOffset(0), [q]);
  return (
    <Modal
      title="ComfyUI既有产物"
      description="从配置的本机output目录选择文件，复制归档到当前项目。此导入不会创建生成执行记录。"
      wide
      onClose={onClose}
    >
      <div className="search-box">
        <Search size={15} />
        <input
          placeholder="搜索输出文件名"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {result.error && <p className="error-text">{result.error.message}</p>}
      <div className="reuse-grid">
        {result.data?.items.map((f: Data) => (
          <button
            key={f.path}
            className={`output-file ${selected.includes(f.path) ? "selected" : ""}`}
            onClick={() =>
              setSelected(
                selected.includes(f.path)
                  ? selected.filter((p) => p !== f.path)
                  : [...selected, f.path],
              )
            }
          >
            <div className="reuse-thumb">
              {f.preview_url ? (
                <img src={f.preview_url} alt={f.name} loading="lazy" />
              ) : (
                <FileText size={28} />
              )}
            </div>
            <span title={f.path}>{f.name}</span>
            <small>
              {bytes(f.byte_size)} {selected.includes(f.path) ? "· 已选择" : ""}
            </small>
          </button>
        ))}
      </div>
      <div className="pagination">
        <Button
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          上一页
        </Button>
        <span>{result.data?.total || 0} 个文件</span>
        <Button
          disabled={!result.data?.next_cursor}
          onClick={() => setOffset(offset + 50)}
        >
          下一页
        </Button>
      </div>
      <div className="modal-actions">
        <span className="muted">已选择 {selected.length} 个文件</span>
        <Button
          variant="primary"
          disabled={!selected.length || selected.length > 50}
          onClick={() =>
            action(
              "import-output",
              async () => {
                const imported = await api("/imports/comfy-output", "POST", {
                  project_id: projectId,
                  paths: selected,
                });
                onImported(imported);
              },
              "既有产物已归档到当前项目",
            )
          }
        >
          导入所选文件
        </Button>
      </div>
    </Modal>
  );
}

function ProjectTrash({
  onClose,
  action,
}: {
  onClose: () => void;
  action: Action;
}) {
  const result = useQuery({
    queryKey: ["project-trash"],
    queryFn: () => api<Data[]>("/projects?removed=true"),
  });
  return (
    <Modal
      title="已移除项目"
      description="恢复项目会重新显示其素材引用；已永久清理的原文件无法随之恢复。"
      onClose={onClose}
    >
      {result.data?.map((p) => (
        <div className="backup-row" key={p.id}>
          <span>{p.name}</span>
          <Button
            onClick={() =>
              action(
                "restore-project",
                () => api(`/projects/${p.id}/restore`, "POST"),
                "项目已恢复",
              )
            }
          >
            恢复项目
          </Button>
        </div>
      ))}
      {!result.data?.length && (
        <Empty icon={<Trash2 />} title="没有已移除项目">
          项目内的已移除素材，可通过素材栏的回收站查看。
        </Empty>
      )}
    </Modal>
  );
}
