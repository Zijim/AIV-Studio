import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
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
  ArrowLeft,
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
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Star,
  Tag,
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
  useAccess,
  useWorkspace,
} from "./lib";
import { Button, Empty, Modal } from "./components/ui";
import { ConversationSwitcher } from "./components/ConversationSwitcher";
import { AutonomousAssistant } from "./components/AutonomousAssistant";
import { ChatMessage } from "./components/ChatMessage";
import { ProgressiveImage } from "./components/ProgressiveImage";
import { TaskQueue, TaskAssetPreview } from "./components/TaskQueue";
import {
  CreativeSummary,
  PlanInputs,
  PromptDetails,
} from "./components/GuidedPrompt";

const activeStates = ["queued", "running", "saving"];
const executeKeys = new Map<string, string>();
const panelLimits = {
  leftMin: 210,
  leftMax: 460,
  rightMin: 280,
  rightMax: 560,
  centerMin: 320,
  handle: 8,
};

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function defaultPanelLayout() {
  return window.innerWidth >= 1600
    ? { left: 300, right: 400 }
    : { left: 270, right: 356 };
}

function fitPanelLayout(
  left: number,
  right: number,
  width = window.innerWidth,
) {
  const maxTotal = width - panelLimits.centerMin - panelLimits.handle * 2;
  let nextLeft = clamp(left, panelLimits.leftMin, panelLimits.leftMax);
  let nextRight = clamp(right, panelLimits.rightMin, panelLimits.rightMax);
  if (nextLeft + nextRight > maxTotal) {
    const reduceRight = Math.min(
      nextLeft + nextRight - maxTotal,
      Math.max(0, nextRight - panelLimits.rightMin),
    );
    nextRight -= reduceRight;
    const remaining = Math.max(0, nextLeft + nextRight - maxTotal);
    nextLeft -= Math.min(
      remaining,
      Math.max(0, nextLeft - panelLimits.leftMin),
    );
  }
  return { left: nextLeft, right: nextRight };
}

function readPanelLayout() {
  try {
    const stored = JSON.parse(
      window.localStorage.getItem("aiv-panel-layout") || "null",
    );
    if (
      stored &&
      Number.isFinite(stored.left) &&
      Number.isFinite(stored.right)
    ) {
      return fitPanelLayout(stored.left, stored.right);
    }
  } catch {
    // Ignore malformed local preferences and use the defaults.
  }
  return fitPanelLayout(defaultPanelLayout().left, defaultPanelLayout().right);
}
const imageRoles: Record<string, string> = {
  auto: "自动判断用途",
  edit_source: "待编辑原图",
  subject_reference: "主体参考",
  style_reference: "风格参考",
  composition_reference: "构图参考",
  first_frame: "视频首帧",
  last_frame: "视频尾帧",
  scene_reference: "环境参考",
};
function Thumb({ asset }: { asset: Data }) {
  return asset.preview_url && asset.presence !== "purged" ? (
    <img
      src={asset.preview_url}
      alt={asset.name}
      loading="lazy"
      decoding="async"
    />
  ) : (
    <div className="file-placeholder">
      {asset.media_type === "video" ? <Video /> : <FileText />}
      <span>{asset.media_type}</span>
    </div>
  );
}

function PanelResizer({
  side,
  value,
  onPointerDown,
  onNudge,
  onReset,
}: {
  side: "left" | "right";
  value: number;
  onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onNudge: (delta: number) => void;
  onReset: () => void;
}) {
  const min = side === "left" ? panelLimits.leftMin : panelLimits.rightMin;
  const max = side === "left" ? panelLimits.leftMax : panelLimits.rightMax;
  return (
    <div
      className={`panel-resizer ${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={side === "left" ? "调整素材栏宽度" : "调整助手栏宽度"}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      tabIndex={0}
      title="拖动调整宽度，双击恢复默认"
      onPointerDown={onPointerDown}
      onDoubleClick={onReset}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          onNudge(-16);
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          onNudge(16);
        } else if (event.key === "Home") {
          event.preventDefault();
          onReset();
        }
      }}
    >
      <span />
    </div>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const navigate = useNavigate(),
    location = useLocation();
  const store = useWorkspace();
  const access = useAccess();
  const [modal, setModal] = useState<string | null>(null);
  const [focus, setFocus] = useState<Data | null>(null);
  const [tool, setTool] = useState<Data | null>(null);
  const [editingPlan, setEditingPlan] = useState<Data | null>(null);
  const [search, setSearch] = useState(""),
    [filter, setFilter] = useState("");
  const [trash, setTrash] = useState(false);
  const [libraryTab, setLibraryTab] = useState<"all" | "key">("all");
  const [tagId, setTagId] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [tagPickerOpen, setTagPickerOpen] = useState(false);
  const [tagManagerOpen, setTagManagerOpen] = useState(false);
  const [newTagName, setNewTagName] = useState("");
  const [pendingTags, setPendingTags] = useState<string[]>([]);
  const [tagEditing, setTagEditing] = useState<Data | null>(null);
  const [newSwitchTag, setNewSwitchTag] = useState("");
  const [panelLayout, setPanelLayout] = useState(readPanelLayout);
  const appShellRef = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(""),
    [toast, setToast] = useState("");
  const [importResult, setImportResult] = useState<Data | null>(null);
  const [taskPreview, setTaskPreview] = useState<Data | null>(null);
  const [locatedAsset, setLocatedAsset] = useState<Data | null>(null);
  const [focusTaskId, setFocusTaskId] = useState<string | null>(null);
  const [returnTaskId, setReturnTaskId] = useState<string | null>(null);
  const libraryRef = useRef<HTMLDivElement>(null);
  const libraryScroll = useRef<Record<string, number>>({ all: 0, key: 0 });
  const lastAssetLink = useRef("");
  const tabProject = useRef("");
  const previousTab = useRef<"all" | "key">("all");
  const browsingTaskMedia = useRef(false);
  browsingTaskMedia.current = !!locatedAsset || !!taskPreview;
  const files = useRef<HTMLInputElement>(null),
    folders = useRef<HTMLInputElement>(null),
    keyFiles = useRef<HTMLInputElement>(null);
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
  const conversations = useQuery({
    queryKey: ["conversations", store.projectId],
    queryFn: () => api(`/projects/${store.projectId}/conversations`),
    enabled: !!project,
  });
  const conversationItems: Data[] = conversations.data?.items ?? [];
  const conversation = conversationItems.find(
    (c) => c.id === store.conversationId,
  );
  const cid = conversation?.id as string | undefined;
  const assistantMode = conversation?.mode ?? "assisted";
  const manualConversationId =
    assistantMode === "autonomous"
      ? conversationItems.find(
          (c) => c.is_default && (c.mode ?? "assisted") === "assisted",
        )?.id
      : cid;
  const message = cid ? (store.messageDrafts[cid] ?? "") : "";
  function setMessage(value: string) {
    if (cid) store.setMessageDraft(cid, value);
  }
  const facets = useQuery({
    queryKey: ["library-facets", store.projectId],
    queryFn: () => api(`/projects/${store.projectId}/library/facets`),
    enabled: !!project,
  });
  const tags: Data[] = facets.data?.tags ?? [];
  const assets = useQuery({
    queryKey: [
      "assets",
      store.projectId,
      search,
      filter,
      trash,
      page,
      libraryTab,
      tagId,
    ],
    queryFn: () => {
      const params = new URLSearchParams({
        q: search,
        media_type: filter,
        removed: String(trash),
        offset: String(page * 100),
      });
      if (!trash) {
        if (libraryTab === "key") params.set("curated", "true");
        if (tagId) params.set("tag_id", tagId);
      }
      return api(`/projects/${store.projectId}/assets?${params}`);
    },
    enabled: !!project,
  });
  const messages = useInfiniteQuery({
    queryKey: ["messages", store.projectId, cid],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api<Data[]>(
        `/projects/${store.projectId}/messages?conversation_id=${cid}${pageParam ? `&before=${pageParam}` : ""}`,
      ),
    getNextPageParam: (lastPage: Data[]) =>
      lastPage.length === 100 ? (lastPage[0]?.id as string) : undefined,
    enabled: !!project && !!cid,
  });
  const chatMessages = [...(messages.data?.pages ?? [])].reverse().flat();
  const plans = useQuery({
    queryKey: ["plans", store.projectId, cid],
    queryFn: () =>
      api<Data[]>(`/projects/${store.projectId}/plans?conversation_id=${cid}`),
    enabled: !!project && !!cid,
  });
  const turns = useQuery({
    queryKey: ["turns", store.projectId, cid],
    queryFn: () =>
      api<Data[]>(`/projects/${store.projectId}/turns?conversation_id=${cid}`),
    enabled: !!project && !!cid,
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
  const tagEditingAsset = tagEditing
    ? (allAssets.find((item) => item.id === tagEditing.id) ?? tagEditing)
    : null;
  const selectedAssets = useQuery({
    queryKey: ["selected-assets", store.projectId, store.selected],
    queryFn: () =>
      Promise.all(store.selected.map((id) => api(`/assets/${id}`))),
    enabled: !!project && !!store.selected.length,
  });
  const selected = store.selected.length ? (selectedAssets.data ?? []) : [];
  const trashCount = facets.data?.trash ?? 0;
  const focusRemoved = !!(focus?.project?.removed_at || focus?.project_removed);
  const imageCount = selected.filter((a) => a.media_type === "image").length;
  const visionLimit = health.data?.planner.vision_max_images ?? 6;
  const running =
    tasks.data?.filter((t) => activeStates.includes(t.state)) ?? [];
  const writable = !!project && !project.archived_at;
  const conversationWritable =
    writable && !!conversation && !conversation.archived_at;
  const conversationRunning = running.filter(
    (task) => task.conversation_id === cid,
  );

  function chooseConversation(value: Data) {
    queryClient.setQueryData(
      ["conversations", store.projectId],
      (old: Data | undefined) => ({
        ...old,
        items: old?.items?.some((item: Data) => item.id === value.id)
          ? old.items.map((item: Data) =>
              item.id === value.id ? { ...item, ...value } : item,
            )
          : [value, ...(old?.items ?? [])],
      }),
    );
    store.chooseConversation(value.id, value.mode ?? "assisted");
    setEditingPlan(null);
    setModal(null);
    navigate(`/projects/${store.projectId}?conversation=${value.id}`);
  }

  async function switchAssistantMode(mode: string) {
    if (mode === assistantMode) return;
    const remembered =
      store.conversationsByMode?.[`${store.projectId}:${mode}`];
    const target =
      conversationItems.find((c) => c.id === remembered && c.mode === mode) ??
      conversationItems.find(
        (c) => (c.mode ?? "assisted") === mode && !c.archived_at,
      );
    if (target) chooseConversation(target);
    else
      await action("assistant-mode", async () => {
        const value = await api(
          `/projects/${store.projectId}/conversations`,
          "POST",
          { mode, idempotency_key: crypto.randomUUID() },
        );
        chooseConversation(value);
      });
  }

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
    if (
      !project ||
      !conversations.data ||
      (location.pathname !== `/projects/${project.id}` &&
        location.pathname !== "/")
    )
      return;
    const requested = new URLSearchParams(location.search).get("conversation");
    const value =
      conversationItems.find((c) => c.id === requested) ??
      conversationItems.find((c) => c.id === store.conversationId) ??
      conversationItems.find((c) => !c.archived_at) ??
      conversationItems[0];
    if (!value) return;
    if (store.conversationId !== value.id)
      store.chooseConversation(value.id, value.mode ?? "assisted");
    if (requested !== value.id) {
      const params = new URLSearchParams(location.search);
      params.set("conversation", value.id);
      navigate(`/projects/${project.id}?${params}`, { replace: true });
    }
  }, [project?.id, conversations.data, location.search, location.pathname]);
  useEffect(() => {
    setFocus(null);
    setLocatedAsset(null);
    setTaskPreview(null);
    setFocusTaskId(null);
    setReturnTaskId(null);
    setTrash(false);
    setSearch("");
  }, [store.projectId]);
  useEffect(() => setPage(0), [store.projectId, search, filter, trash]);
  useEffect(() => {
    const workspace = useWorkspace.getState();
    for (const asset of selectedAssets.data ?? []) {
      if (
        workspace.selected.includes(asset.id) &&
        asset.projects?.some(
          (membership: Data) =>
            membership.project_id === workspace.projectId && membership.removed_at,
        )
      ) {
        workspace.chooseAsset(asset.id, true);
      }
    }
  }, [selectedAssets.data]);
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
      if (type === "asset.purged") {
        const id = update.entity_id;
        const purged = (asset: Data | null): Data | null =>
          asset && (asset.id === id || asset.asset_id === id)
            ? {
                ...asset,
                presence: "purged",
                preview_url: null,
                content_url: null,
                can_open: false,
                can_preview: false,
              }
            : asset;
        setFocus(purged);
        setLocatedAsset(purged);
        setTaskPreview((current) =>
          current ? { ...current, asset: purged(current.asset) } : current,
        );
      }
      if (type === "reset") {
        refresh();
        return;
      }
      if (
        type === "library.updated" &&
        update.payload?.action === "creative.delivered" &&
        update.project_id === useWorkspace.getState().projectId
      ) {
        const recycled = new Set<string>(update.payload.recycled_asset_ids);
        setPicked((current) => current.filter((id) => !recycled.has(id)));
        setFocus((current) =>
          current && !browsingTaskMedia.current && recycled.has(current.id)
            ? null
            : current,
        );
      }
      if (
        type === "asset.saved" &&
        update.project_id === useWorkspace.getState().projectId &&
        Date.now() - Date.parse(update.created_at) < 10000
      ) {
        api(`/assets/${update.entity_id}`)
          .then((asset) => {
            const membership = asset.projects.find(
              (p: Data) => p.project_id === update.project_id,
            );
            if (
              !browsingTaskMedia.current &&
              asset.role === "final" &&
              asset.presence === "available" &&
              membership &&
              !membership.removed_at &&
              asset.conversation_id ===
                useWorkspace.getState().conversationId &&
              update.project_id === useWorkspace.getState().projectId
            ) {
              asset.project = membership;
              setFocus(asset);
              const currentWorkspace = useWorkspace.getState();
              const owner = queryClient
                .getQueryData<Data>([
                  "conversations",
                  currentWorkspace.projectId,
                ])
                ?.items?.find(
                  (c: Data) => c.id === currentWorkspace.conversationId,
                );
              if (owner?.mode === "assisted")
                currentWorkspace.chooseAsset(asset.id);
              setToast("新结果已归档");
            }
          })
          .catch(() => {});
      }
      const keys = type.startsWith("task.")
        ? ["tasks", "task-detail", "turns", "conversations"]
        : type.startsWith("plan.")
          ? ["plans", "conversations"]
          : type.startsWith("message.") || type.startsWith("turn.")
            ? ["messages", "plans", "turns", "conversations"]
            : type.startsWith("agent.")
              ? ["agent-runs", "agent-activity", "conversations", "messages"]
              : type.startsWith("conversation.")
                ? ["conversations"]
                : type.startsWith("project.")
                  ? ["projects", "assets", "library-facets"]
                  : type.startsWith("library.")
                    ? ["assets", "library-facets", "detail", "selected-assets"]
                    : [
                        "assets",
                        "library-facets",
                        "detail",
                        "tasks",
                        "task-detail",
                      ];
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
    window.localStorage.setItem(
      "aiv-panel-layout",
      JSON.stringify(panelLayout),
    );
  }, [panelLayout]);
  useEffect(() => {
    const onResize = () =>
      setPanelLayout((current) => {
        const next = fitPanelLayout(current.left, current.right);
        return next.left === current.left && next.right === current.right
          ? current
          : next;
      });
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, []);
  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [cid, chatMessages.at(-1)?.id, draft?.revision]);
  useEffect(() => {
    if (focus) {
      const fresh = allAssets.find((a) => a.id === focus.id);
      if (fresh) setFocus(fresh);
    }
  }, [assets.data]);
  useEffect(() => {
    setPage(0);
  }, [search, filter, trash, libraryTab, tagId]);
  useEffect(() => {
    if (!project || tabProject.current === project.id) return;
    tabProject.current = project.id;
    const params = new URLSearchParams(location.search);
    setLibraryTab(params.get("tab") === "key" ? "key" : "all");
    setTagId(params.get("tag"));
    setPicked([]);
    setPicking(false);
    setTagPickerOpen(false);
    setTagManagerOpen(false);
  }, [project?.id]);
  useEffect(() => {
    if (!tagId || !facets.data) return;
    if (!tags.some((tag) => tag.id === tagId)) setTagId(null);
  }, [facets.data, tagId]);
  useEffect(() => {
    if (previousTab.current === libraryTab) return;
    previousTab.current = libraryTab;
    requestAnimationFrame(() => {
      if (libraryRef.current)
        libraryRef.current.scrollTop = libraryScroll.current[libraryTab] ?? 0;
    });
  }, [libraryTab]);

  function startPanelDrag(
    side: "left" | "right",
    event: ReactPointerEvent<HTMLDivElement>,
  ) {
    if (event.button !== 0 || window.innerWidth <= 850) return;
    event.preventDefault();
    const startX = event.clientX;
    const start = panelLayout;
    let latest = start;
    const maxTotal =
      window.innerWidth - panelLimits.centerMin - panelLimits.handle * 2;
    document.body.classList.add("resizing-panels");

    const move = (moveEvent: PointerEvent) => {
      if (side === "left") {
        const left = clamp(
          start.left + moveEvent.clientX - startX,
          panelLimits.leftMin,
          Math.min(panelLimits.leftMax, maxTotal - start.right),
        );
        latest = { left, right: start.right };
      } else {
        const right = clamp(
          start.right + startX - moveEvent.clientX,
          panelLimits.rightMin,
          Math.min(panelLimits.rightMax, maxTotal - start.left),
        );
        latest = { left: start.left, right };
      }
      // Update the CSS variables directly so the 3-column layout and the
      // canvas follow the pointer without re-rendering the whole workspace.
      appShellRef.current?.style.setProperty(
        "--left-width",
        `${latest.left}px`,
      );
      appShellRef.current?.style.setProperty(
        "--right-width",
        `${latest.right}px`,
      );
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      document.body.classList.remove("resizing-panels");
      setPanelLayout(latest);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  }

  function nudgePanel(side: "left" | "right", delta: number) {
    setPanelLayout((current) =>
      fitPanelLayout(
        side === "left" ? current.left + delta : current.left,
        side === "right" ? current.right + delta : current.right,
      ),
    );
  }

  function resetPanelLayout() {
    const defaults = defaultPanelLayout();
    setPanelLayout(fitPanelLayout(defaults.left, defaults.right));
  }

  function clearLocatedAsset() {
    setLocatedAsset(null);
    const params = new URLSearchParams(location.search);
    params.delete("asset");
    params.delete("from_task");
    navigate(`${location.pathname}?${params}`, { replace: true });
    requestAnimationFrame(() => {
      if (libraryRef.current)
        libraryRef.current.scrollTop = libraryScroll.current[libraryTab] ?? 0;
    });
  }

  function returnToTask() {
    clearLocatedAsset();
    setTaskPreview(null);
    store.setTaskOpen(true);
    setReturnTaskId(null);
  }

  async function locateTaskAsset(item: Data, task: Data) {
    const targetProject = store.projectId;
    await action("locate-asset", async () => {
      const asset = await api(`/assets/${item.asset_id}`);
      if (useWorkspace.getState().projectId !== targetProject) return;
      const membership = asset.projects?.find(
        (m: Data) => m.project_id === store.projectId,
      );
      if (!membership)
        throw new Error("此素材不属于当前项目，无法在本项目定位。");
      if (item.presence === "changed")
        throw new Error("素材版本与任务记录不一致，不能定位为原输入。");
      libraryScroll.current[libraryTab] = libraryRef.current?.scrollTop ?? 0;
      const value = {
        ...asset,
        project: membership,
        project_removed: !!membership.removed_at,
      };
      setLocatedAsset(value);
      setFocus(value);
      setReturnTaskId(task.id);
      setTaskPreview(null);
      store.setTaskOpen(false);
      const params = new URLSearchParams(location.search);
      params.set("asset", asset.id);
      params.set("from_task", task.id);
      params.delete("task");
      lastAssetLink.current = `${store.projectId}:${asset.id}:${task.id}:`;
      navigate(`${location.pathname}?${params}`, { replace: true });
    });
  }

  async function openSourceTask(taskId: string) {
    await action("source-task", async () => {
      const task = await api(`/tasks/${taskId}`);
      const params =
        task.project_id === store.projectId
          ? new URLSearchParams(location.search)
          : new URLSearchParams();
      params.delete("asset");
      params.delete("from_task");
      params.set("task", taskId);
      setModal(null);
      setReturnTaskId(null);
      if (task.project_id === store.projectId) {
        setFocusTaskId(taskId);
        setLocatedAsset(null);
        store.setTaskOpen(true);
        lastAssetLink.current = "";
      }
      navigate(`/projects/${task.project_id}?${params}`);
    });
  }

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const assetId = params.get("asset"),
      fromTask = params.get("from_task"),
      taskId = params.get("task");
    if (!assetId && !taskId) lastAssetLink.current = "";
    if (
      !project ||
      (!assetId && !taskId) ||
      location.pathname !== `/projects/${project.id}`
    )
      return;
    const key = `${project.id}:${assetId ?? ""}:${fromTask ?? ""}:${taskId ?? ""}`;
    if (lastAssetLink.current === key) return;
    lastAssetLink.current = key;
    let cancelled = false;
    let settled = false;
    (async () => {
      try {
        if (assetId) {
          const asset = await api(`/assets/${assetId}`);
          const membership = asset.projects?.find(
            (m: Data) => m.project_id === project.id,
          );
          if (!membership) throw new Error("链接中的素材不属于当前项目。");
          if (fromTask) {
            const task = await api(`/tasks/${fromTask}`);
            if (task.project_id !== project.id)
              throw new Error("来源任务不属于当前项目。");
            const related = Object.values(task.media ?? {}).flat() as Data[];
            const reference = related.find((item) => item.asset_id === assetId);
            if (!reference) throw new Error("此素材不属于链接中的来源任务。");
            if (reference.presence === "changed")
              throw new Error("素材版本与任务输入不一致。");
          }
          if (cancelled) return;
          libraryScroll.current[libraryTab] =
            libraryRef.current?.scrollTop ?? 0;
          const value = {
            ...asset,
            project: membership,
            project_removed: !!membership.removed_at,
          };
          setLocatedAsset(value);
          setFocus(value);
          setReturnTaskId(fromTask);
          store.setTaskOpen(false);
        } else if (taskId) {
          const task = await api(`/tasks/${taskId}`);
          if (task.project_id !== project.id)
            throw new Error("链接中的任务不属于当前项目。");
          if (cancelled) return;
          setFocusTaskId(taskId);
          setLocatedAsset(null);
          store.setTaskOpen(true);
        }
      } catch (error) {
        if (!cancelled) setToast((error as Error).message);
      } finally {
        settled = true;
      }
    })();
    return () => {
      cancelled = true;
      if (!settled && lastAssetLink.current === key) lastAssetLink.current = "";
    };
  }, [project?.id, location.search, location.pathname]);

  function chooseTab(value: "all" | "key") {
    if (locatedAsset) return;
    libraryScroll.current[libraryTab] = libraryRef.current?.scrollTop ?? 0;
    setLibraryTab(value);
    setTagId(null);
    setPage(0);
    setTagEditing(null);
    const params = new URLSearchParams(location.search);
    if (value === "key") params.set("tab", "key");
    else params.delete("tab");
    params.delete("tag");
    navigate(`${location.pathname}?${params}`, { replace: true });
  }

  function chooseTag(value: string | null) {
    if (locatedAsset) return;
    setTagId(value);
    setPage(0);
    setTagEditing(null);
    const params = new URLSearchParams(location.search);
    if (value) {
      params.set("tab", "key");
      params.set("tag", value);
    } else {
      params.delete("tag");
      if (libraryTab !== "key") params.delete("tab");
    }
    navigate(`${location.pathname}?${params}`, { replace: true });
  }

  function toggleTrash() {
    setTrash(!trash);
    setPage(0);
    setLibraryTab("all");
    setTagId(null);
    setPicking(false);
    setPicked([]);
    setTagPickerOpen(false);
    setTagManagerOpen(false);
    setTagEditing(null);
    const params = new URLSearchParams(location.search);
    params.delete("tab");
    params.delete("tag");
    navigate(`${location.pathname}?${params}`, { replace: true });
  }

  function restoreLibraryAsset(asset: Data) {
    action(
      "restore-asset",
      async () => {
        await api(
          `/projects/${store.projectId}/assets/${asset.id}/restore`,
          "POST",
        );
        setFocus((current) => (current?.id === asset.id ? null : current));
      },
      "素材已恢复",
    );
  }

  async function toggleKey(asset: Data) {
    if (!writable) return;
    const curated = !!asset.project?.curated;
    await action(
      curated ? "unkey" : "key",
      () =>
        api(
          curated
            ? `/projects/${project!.id}/key-assets/remove`
            : `/projects/${project!.id}/key-assets`,
          "POST",
          { asset_ids: [asset.id] },
        ),
      curated ? "已移出关键素材" : "已加入关键素材",
    );
  }

  function togglePicked(id: string) {
    setPicked((current) =>
      current.includes(id)
        ? current.filter((value) => value !== id)
        : [...current, id],
    );
  }

  async function addPicked(tagIds: string[] = []) {
    if (!picked.length) return;
    const ids = [...picked];
    await action(
      "key",
      () =>
        api(`/projects/${project!.id}/key-assets`, "POST", {
          asset_ids: ids,
          tag_ids: tagIds,
        }),
      `已加入 ${ids.length} 份关键素材`,
    );
    setPicked([]);
    setPicking(false);
    setTagPickerOpen(false);
    setPendingTags([]);
  }

  async function removePicked() {
    if (!picked.length) return;
    const ids = [...picked];
    await action(
      "unkey",
      () =>
        api(`/projects/${project!.id}/key-assets/remove`, "POST", {
          asset_ids: ids,
        }),
      `已移出 ${ids.length} 份关键素材`,
    );
    setPicked([]);
    setPicking(false);
    setTagPickerOpen(false);
    setPendingTags([]);
  }

  async function createTag(name: string) {
    const value = await action(
      "tag",
      () => api(`/projects/${project!.id}/tags`, "POST", { name }),
      "标签已创建",
    );
    return value as Data | undefined;
  }

  async function renameTag(id: string, name: string) {
    await action(
      "tag",
      () => api(`/projects/${project!.id}/tags/${id}`, "PATCH", { name }),
      "标签已重命名",
    );
  }

  async function recolorTag(id: string, color: string) {
    await action(
      "tag",
      () => api(`/projects/${project!.id}/tags/${id}`, "PATCH", { color }),
      "标签颜色已更新",
    );
  }

  async function deleteTag(id: string) {
    await action(
      "tag",
      () => api(`/projects/${project!.id}/tags/${id}`, "DELETE"),
      "标签已删除，素材仍保留",
    );
    if (tagId === id) setTagId(null);
  }

  async function assignTag(asset: Data, tag: Data) {
    const current = (asset.project?.tags ?? []).map((item: Data) => item.id);
    if (current.includes(tag.id)) return;
    await action(
      "tag",
      () =>
        api(`/projects/${project!.id}/assets/${asset.id}/tags`, "PUT", {
          tag_ids: [...current, tag.id],
        }),
      `已放入「${tag.name}」`,
    );
  }

  async function toggleAssetTag(asset: Data, tag: Data) {
    const current = (asset.project?.tags ?? []).map((item: Data) => item.id);
    const next = current.includes(tag.id)
      ? current.filter((id: string) => id !== tag.id)
      : [...current, tag.id];
    await action(
      "tag",
      () =>
        api(`/projects/${project!.id}/assets/${asset.id}/tags`, "PUT", {
          tag_ids: next,
        }),
      current.includes(tag.id)
        ? `已从「${tag.name}」移出`
        : `已放入「${tag.name}」`,
    );
  }

  async function createAndAssignTag(name: string) {
    if (!tagEditingAsset || !name.trim()) return;
    const asset = tagEditingAsset;
    const current = (asset.project?.tags ?? []).map((tag: Data) => tag.id);
    const existing = tags.find((tag) => tag.name === name.trim());
    await action(
      "tag",
      async () => {
        const tagId =
          existing?.id ??
          (
            await api(`/projects/${project!.id}/tags`, "POST", {
              name: name.trim(),
            })
          ).id;
        await api(`/projects/${project!.id}/assets/${asset.id}/tags`, "PUT", {
          tag_ids: [...current, tagId],
        });
      },
      existing ? `已加入「${name.trim()}」` : "标签已创建并加入",
    );
    setNewSwitchTag("");
  }

  async function importFiles(
    list: FileList | File[],
    target: "all" | "key" = "all",
  ) {
    if (!writable || !list.length) return;
    const accepted = Array.from(list);
    const maxBatchBytes = access.maxUploadBytes
      ? access.maxUploadBytes - 65536
      : Infinity;
    if (accepted.some((file) => file.size > maxBatchBytes)) {
      setToast("公开工作台暂不支持单个90MB及以上的文件，请压缩后上传。");
      return;
    }
    const batches: File[][] = [];
    let batchBytes = 0;
    for (const file of accepted) {
      const last = batches[batches.length - 1];
      if (
        !last ||
        last.length >= 50 ||
        batchBytes + file.size > maxBatchBytes
      ) {
        batches.push([file]);
        batchBytes = file.size;
      } else {
        last.push(file);
        batchBytes += file.size;
      }
    }
    const results: Data[] = [];
    await action(
      "import",
      async () => {
        for (const files of batches) {
          const data = new FormData();
          files.forEach((f) =>
            data.append("files", f, f.webkitRelativePath || f.name),
          );
          const batch = await api(
            `/projects/${project!.id}/imports`,
            "POST",
            data,
          );
          results.push(...batch.items);
        }
        const saved = results
          .filter((item) => item.state === "saved" && item.asset_id)
          .map((item) => item.asset_id);
        if (target === "key" && saved.length) {
          await api(`/projects/${project!.id}/key-assets`, "POST", {
            asset_ids: saved,
          });
        }
        setImportResult({ items: results });
        setModal("import-result");
      },
      target === "key" ? "已导入并加入关键素材" : "素材已归档到当前项目",
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
      !cid ||
      !conversationWritable ||
      planningTurn ||
      conversationRunning.length
    )
      return;
    const targetConversation = cid;
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
        conversation_id: targetConversation,
        selected_asset_ids: store.selected,
        image_roles: Object.fromEntries(
          Object.entries(store.imageRoles).filter(([id]) =>
            store.selected.includes(id),
          ),
        ),
        preferred_tool_id: store.toolPreferences?.[targetConversation] || null,
        idempotency_key: crypto.randomUUID(),
      });
      store.setMessageDraft(targetConversation, "");
    });
  }

  async function rewritePrompt(plan: Data) {
    if (!conversationWritable || planningTurn || conversationRunning.length)
      return;
    await action("rewrite", () =>
      api(`/projects/${project!.id}/messages`, "POST", {
        content:
          "请按当前要求和对应工具指引重新编写提示词，保持工具、输入和执行参数不变。",
        conversation_id: cid,
        preferred_tool_id: plan.tool_id,
        revise_plan: {
          plan_id: plan.id,
          revision: plan.revision,
          rewrite_only: true,
        },
        selected_asset_ids: [
          ...new Set(plan.inputs.map((input: Data) => input.asset_id)),
        ],
        image_roles: Object.fromEntries(
          plan.inputs.map((input: Data) => [input.asset_id, "auto"]),
        ),
        idempotency_key: crypto.randomUUID(),
      }),
    );
  }

  return (
    <div
      className="app-shell"
      ref={appShellRef}
      style={
        {
          "--left-width": `${panelLayout.left}px`,
          "--right-width": `${panelLayout.right}px`,
        } as CSSProperties
      }
    >
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
          <span
            title={
              access.publicAccess ? "项目、素材与对话由所有访客共享" : undefined
            }
          >
            {access.publicAccess ? "公开共享工作台" : "本地创作工作台"}
          </span>
        </div>
        <div className="top-actions">
          <Button
            title="项目与素材回收站"
            onClick={() => setModal("project-trash")}
          >
            <Trash2 size={16} />
          </Button>
          {access.canManage && (
            <Button onClick={() => setModal("settings")}>
              <Settings2 size={16} />
              <span>设置</span>
            </Button>
          )}
          <div
            className="avatar"
            title={
              access.publicAccess ? "访客 · 共用项目、素材和对话" : "本机管理员"
            }
          >
            {access.publicAccess ? "客" : "我"}
          </div>
        </div>
      </header>
      <div className="workspace">
        <aside className="library-panel">
          {access.publicAccess && (
            <p className="shared-notice" role="note">
              公开体验：项目、素材和对话对所有访客可见，请勿上传私人资料。
            </p>
          )}
          <div className="project-switch">
            <div className="project-section-heading">
              <span className="project-section-title">创作项目</span>
              <span id="project-selection-hint">先选项目，再创作</span>
            </div>
            <div className="project-row">
              <div
                className="project-select"
                title={
                  project
                    ? `当前项目：${project.name}，点击切换`
                    : "选择或新建一个项目后开始创作"
                }
              >
                <span className="project-select-icon" aria-hidden="true">
                  <Folder size={17} />
                </span>
                <span className="project-select-name" aria-hidden="true">
                  {project
                    ? `${project.name}${project.archived_at ? " · 已归档" : ""}`
                    : "选择创作项目"}
                </span>
                <span className="project-select-action" aria-hidden="true">
                  切换
                  <ChevronDown size={13} />
                </span>
                <select
                  aria-label="切换项目"
                  aria-describedby="project-selection-hint"
                  value={project?.id ?? ""}
                  disabled={projects.isPending || !projects.data?.length}
                  onChange={(e) => {
                    store.chooseProject(e.target.value);
                    setFocus(null);
                    navigate("/projects/" + e.target.value);
                  }}
                >
                  {!project && (
                    <option value="" disabled>
                      {projects.isPending
                        ? "正在加载项目…"
                        : projects.data?.length
                          ? "选择创作项目"
                          : "请先新建项目"}
                    </option>
                  )}
                  {projects.data?.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                      {p.archived_at ? " · 已归档" : ""}
                    </option>
                  ))}
                </select>
              </div>
              <Button
                className="project-create"
                aria-label="新建项目"
                title="新建项目"
                onClick={() => setModal("new-project")}
              >
                <Plus size={17} />
              </Button>
            </div>
            {project && (
              <button
                className="project-description"
                title={project.goal || "设置项目目标与风格"}
                onClick={() => setModal("edit-project")}
              >
                <span>{project.goal || "设置项目目标与风格"}</span>
                <MoreHorizontal size={15} />
              </button>
            )}
            {!project && (
              <span className="project-empty-hint">
                {projects.isPending
                  ? "正在加载项目…"
                  : projects.data?.length
                    ? "选择项目，素材与对话会在此归档"
                    : "点击 + 新建你的第一个项目"}
              </span>
            )}
          </div>
          {trash ? (
            <div className="library-heading">
              <h2>
                素材回收站 <span>{trashCount}</span>
              </h2>
              <Button
                title="返回素材"
                disabled={!!locatedAsset}
                onClick={toggleTrash}
              >
                <ArrowLeft size={14} aria-hidden="true" /> 返回素材
              </Button>
            </div>
          ) : (
            <div className="library-tabbar">
              <div
                className="library-tabs"
                role="tablist"
                aria-label="素材库视图"
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={libraryTab === "all"}
                  className={libraryTab === "all" ? "active" : ""}
                  disabled={!!locatedAsset}
                  onClick={() => chooseTab("all")}
                >
                  全部素材{" "}
                  <span>{facets.data?.all ?? (assets.data?.total || 0)}</span>
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={libraryTab === "key"}
                  className={libraryTab === "key" ? "active" : ""}
                  disabled={!!locatedAsset}
                  onClick={() => chooseTab("key")}
                >
                  关键素材 <span>{facets.data?.key ?? 0}</span>
                </button>
              </div>
              <Button
                title="素材回收站"
                aria-label={`素材回收站 ${trashCount} 份`}
                className="library-trash-entry"
                disabled={!project || !!locatedAsset}
                onClick={toggleTrash}
              >
                <Trash2 size={13} aria-hidden="true" /> 回收站{" "}
                <span>{trashCount}</span>
              </Button>
            </div>
          )}
          <div className="search-box">
            <Search size={15} />
            <input
              aria-label="搜索素材"
              disabled={!!locatedAsset}
              placeholder={
                trash
                  ? "搜索回收站…"
                  : libraryTab === "key"
                    ? "搜索关键素材、标签、备注…"
                    : "搜索名称、备注、描述…"
              }
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
                disabled={!!locatedAsset}
                onClick={() => setFilter(value)}
              >
                {label}
              </button>
            ))}
            {!trash && libraryTab === "all" && (
              <button
                type="button"
                className={`triage ${picking ? "active" : ""}`}
                disabled={!!locatedAsset}
                onClick={() => {
                  setPicking(!picking);
                  setPicked([]);
                  setTagPickerOpen(false);
                }}
              >
                {picking ? "退出整理" : "整理"}
              </button>
            )}
          </div>
          {!trash && libraryTab === "key" && (
            <>
              <div className="tag-strip" aria-label="关键素材标签">
                <button
                  type="button"
                  className={`tag-chip ${!tagId ? "active" : ""}`}
                  disabled={!!locatedAsset}
                  onClick={() => chooseTag(null)}
                >
                  全部 <em>{facets.data?.key ?? 0}</em>
                </button>
                {tags.map((tag) => (
                  <button
                    key={tag.id}
                    type="button"
                    className={`tag-chip ${tagId === tag.id ? "active" : ""}`}
                    disabled={!!locatedAsset}
                    title={`${tag.name} · ${tag.count} 份${
                      writable ? "，可拖入素材" : ""
                    }`}
                    onClick={() => chooseTag(tagId === tag.id ? null : tag.id)}
                    onDragOver={(e) => {
                      e.preventDefault();
                      e.dataTransfer.dropEffect = "copy";
                    }}
                    onDrop={(e) => {
                      e.preventDefault();
                      const id = e.dataTransfer.getData("text/aiv-asset");
                      const asset = allAssets.find((item) => item.id === id);
                      if (asset) assignTag(asset, tag);
                    }}
                  >
                    <i style={{ background: tag.color }} />
                    {tag.name} <em>{tag.count}</em>
                  </button>
                ))}
                <button
                  type="button"
                  className="tag-chip tag-add"
                  title="新建或管理标签"
                  disabled={!!locatedAsset}
                  onClick={() => setTagManagerOpen((value) => !value)}
                >
                  <Plus size={12} />
                </button>
              </div>
              {tagManagerOpen && (
                <TagManager
                  tags={tags}
                  busy={!!busy}
                  onCreate={async (name) => {
                    const tag = await createTag(name);
                    if (tag) setTagId(tag.id);
                  }}
                  onRename={renameTag}
                  onColor={recolorTag}
                  onDelete={deleteTag}
                  onClose={() => setTagManagerOpen(false)}
                />
              )}
            </>
          )}
          {!trash && tagEditingAsset && (
            <div className="tag-switch-panel">
              <div className="tag-switch-head">
                <span title={tagEditingAsset.name}>
                  调整标签 · {tagEditingAsset.name}
                </span>
                <Button
                  aria-label="关闭标签调整"
                  onClick={() => setTagEditing(null)}
                >
                  <X size={13} />
                </Button>
              </div>
              <div className="tag-switch-options">
                {tags.map((tag) => {
                  const on = (tagEditingAsset.project?.tags ?? []).some(
                    (item: Data) => item.id === tag.id,
                  );
                  return (
                    <button
                      key={tag.id}
                      type="button"
                      className={`tag-choice ${on ? "on" : ""}`}
                      disabled={!!busy}
                      title={
                        on ? `从「${tag.name}」移出` : `放入「${tag.name}」`
                      }
                      onClick={() => toggleAssetTag(tagEditingAsset, tag)}
                    >
                      <i style={{ background: tag.color }} />
                      {tag.name}
                    </button>
                  );
                })}
                {!tags.length && <small>还没有标签，先在下面新建一个。</small>}
              </div>
              <div className="tag-picker-create">
                <input
                  aria-label="新建标签并加入"
                  value={newSwitchTag}
                  placeholder="新建标签并加入，如：场景/街道"
                  onChange={(e) => setNewSwitchTag(e.target.value)}
                />
                <Button
                  disabled={!newSwitchTag.trim() || !!busy}
                  onClick={() => createAndAssignTag(newSwitchTag)}
                >
                  新建并加入
                </Button>
              </div>
            </div>
          )}
          {locatedAsset && (
            <div className="located-asset-panel">
              <div className="located-asset-notice">
                <strong>已定位关联素材</strong>
                <span>原筛选、分页和附图选择已保留</span>
              </div>
              <button
                className="asset-card located-asset-card"
                onClick={() => setModal("preview")}
                aria-label={`预览已定位素材：${locatedAsset.name}`}
              >
                <div className="asset-thumb">
                  <Thumb asset={locatedAsset} />
                </div>
                <div className="asset-caption">
                  <span>{locatedAsset.name}</span>
                  <small>
                    {locatedAsset.project_removed
                      ? "已从项目移除 · 只读查看"
                      : "当前定位"}
                  </small>
                </div>
              </button>
              <Button onClick={clearLocatedAsset}>返回原列表</Button>
              {returnTaskId && <Button onClick={returnToTask}>返回任务</Button>}
            </div>
          )}
          <div
            className="asset-library"
            ref={libraryRef}
            hidden={!!locatedAsset}
          >
            {allAssets.map((a) => {
              const curated = !!a.project?.curated;
              const assetTags: Data[] = a.project?.tags ?? [];
              const selected =
                !trash &&
                (picking ? picked.includes(a.id) : store.selected.includes(a.id));
              return (
                <div
                  key={a.id}
                  role="button"
                  tabIndex={0}
                  className={`asset-card ${selected ? "selected" : ""} ${
                    curated ? "curated" : ""
                  } ${picking ? "picking" : ""}`}
                  draggable={writable && !picking && !trash}
                  onDragStart={(e) => {
                    e.dataTransfer.setData("text/aiv-asset", a.id);
                    e.dataTransfer.effectAllowed = "copy";
                  }}
                  onClick={(e) => {
                    if (trash) {
                      setFocus(a);
                      setModal("preview");
                      return;
                    }
                    if (picking) {
                      togglePicked(a.id);
                      return;
                    }
                    store.chooseAsset(
                      a.id,
                      e.ctrlKey || e.metaKey || e.shiftKey,
                    );
                    setFocus(a);
                  }}
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) {
                      return;
                    }
                    e.preventDefault();
                    if (picking) togglePicked(a.id);
                    else {
                      setFocus(a);
                      setModal(trash ? "preview" : "asset");
                    }
                  }}
                  onDoubleClick={() => {
                    if (picking) return;
                    setFocus(a);
                    setModal(trash ? "preview" : "asset");
                  }}
                >
                  <div className="asset-thumb">
                    <Thumb asset={a} />
                    {!trash && (
                      <button
                        type="button"
                        className={`curate-toggle ${curated ? "on" : ""}`}
                        title={curated ? "移出关键素材" : "加入关键素材"}
                        aria-label={curated ? "移出关键素材" : "加入关键素材"}
                        disabled={!writable}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleKey(a);
                        }}
                      >
                        <Star
                          size={12}
                          fill={curated ? "currentColor" : "none"}
                        />
                      </button>
                    )}
                    {selected && (
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
                      <span className="missing-badge">
                        {labels[a.presence]}
                      </span>
                    )}
                  </div>
                  <div className="asset-caption">
                    <span title={a.name}>{a.name}</span>
                    <small className="asset-meta">
                      {a.best && <b className="best-label">最佳</b>}
                      {a.origin === "imported"
                        ? "导入"
                        : a.kind === "preparation"
                          ? "准备画面"
                          : a.kind === "intermediate"
                            ? "中间"
                            : "生成"}
                      {a.media_info.width
                        ? ` · ${a.media_info.width}×${a.media_info.height}`
                        : ""}
                    </small>
                    {assetTags.length > 0 && (
                      <span
                        className="asset-tags"
                        title={assetTags
                          .map((tag: Data) => tag.name)
                          .join("、")}
                      >
                        {assetTags.slice(0, 4).map((tag: Data) => (
                          <i key={tag.id} style={{ background: tag.color }} />
                        ))}
                        {assetTags.length > 4 && (
                          <em>+{assetTags.length - 4}</em>
                        )}
                      </span>
                    )}
                    <div className="asset-actions">
                      {trash && writable && (
                        <button
                          type="button"
                          className="tag-edit restore-asset"
                          aria-label={`恢复素材 ${a.name}`}
                          disabled={!!busy || a.presence === "purged"}
                          onClick={(e) => {
                            e.stopPropagation();
                            restoreLibraryAsset(a);
                          }}
                        >
                          <RefreshCw size={11} aria-hidden="true" />
                          恢复
                        </button>
                      )}
                      {!trash && writable && (
                        <button
                          type="button"
                          className="tag-edit"
                          title="重命名这份素材"
                          onClick={(e) => {
                            e.stopPropagation();
                            setFocus(a);
                            setModal("asset");
                          }}
                        >
                          <Pencil size={11} />
                          改名
                        </button>
                      )}
                      {!trash && (libraryTab === "key" || curated) && (
                        <button
                          type="button"
                          className="tag-edit"
                          title="直接调整这份素材的标签"
                          onClick={(e) => {
                            e.stopPropagation();
                            setTagEditing(a);
                          }}
                        >
                          <Tag size={11} />
                          {assetTags.length ? "换标签" : "打标签"}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
            {assets.isLoading && <LoaderCircle className="spin loader" />}
            {!allAssets.length && !assets.isLoading && (
              <div className="library-empty">
                {search ? (
                  "没有匹配的素材"
                ) : trash ? (
                  "回收站为空"
                ) : libraryTab === "key" ? (
                  <div className="key-empty">
                    <span>把人物、场景、道具的关键图放进来</span>
                    <small>选中素材后点亮星标，或直接从全部素材整理。</small>
                    <div>
                      <Button onClick={() => chooseTab("all")}>
                        去全部素材挑选
                      </Button>
                      <Button
                        disabled={!writable}
                        onClick={() => keyFiles.current?.click()}
                      >
                        导入关键素材
                      </Button>
                    </div>
                  </div>
                ) : (
                  "作品和导入素材将在这里积累"
                )}
              </div>
            )}
          </div>
          {(assets.data?.total || 0) > 100 && (
            <div className="pagination" hidden={!!locatedAsset}>
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
          {!trash && (picking || picked.length > 0) && (
            <div className="batch-bar">
              <div className="batch-summary">
                <strong>已选 {picked.length} 份</strong>
                <span>{picking ? "点击卡片勾选" : "批量整理"}</span>
              </div>
              <div className="batch-actions">
                <Button
                  disabled={!picked.length || !writable}
                  onClick={() => addPicked([])}
                >
                  加入关键
                </Button>
                <Button
                  disabled={!picked.length || !writable}
                  onClick={() => setTagPickerOpen(!tagPickerOpen)}
                >
                  打标签
                </Button>
                {libraryTab === "key" && (
                  <Button
                    disabled={!picked.length || !writable}
                    onClick={removePicked}
                  >
                    移出
                  </Button>
                )}
                <Button
                  onClick={() => {
                    setPicked([]);
                    setPicking(false);
                    setTagPickerOpen(false);
                    setPendingTags([]);
                  }}
                >
                  取消
                </Button>
              </div>
              {tagPickerOpen && (
                <div className="tag-picker">
                  <span>选择标签（可多选）</span>
                  <div className="tag-options">
                    {tags.map((tag) => (
                      <label key={tag.id}>
                        <input
                          type="checkbox"
                          checked={pendingTags.includes(tag.id)}
                          onChange={(e) =>
                            setPendingTags(
                              e.target.checked
                                ? [...pendingTags, tag.id]
                                : pendingTags.filter((id) => id !== tag.id),
                            )
                          }
                        />
                        <i style={{ background: tag.color }} />
                        {tag.name}
                      </label>
                    ))}
                    {!tags.length && <small>还没有标签，先新建一个。</small>}
                  </div>
                  <div className="tag-picker-create">
                    <input
                      aria-label="新标签名称"
                      value={newTagName}
                      placeholder="新标签名称"
                      onChange={(e) => setNewTagName(e.target.value)}
                    />
                    <Button
                      disabled={!newTagName.trim() || !!busy}
                      onClick={async () => {
                        const tag = await createTag(newTagName.trim());
                        if (tag) {
                          setPendingTags([...pendingTags, tag.id]);
                          setNewTagName("");
                        }
                      }}
                    >
                      新建
                    </Button>
                  </div>
                  <div className="tag-picker-actions">
                    <Button
                      onClick={() => {
                        setTagPickerOpen(false);
                        setPendingTags([]);
                      }}
                    >
                      取消
                    </Button>
                    <Button
                      variant="primary"
                      disabled={!picked.length || !writable}
                      onClick={() => addPicked(pendingTags)}
                    >
                      加入并打标签
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
          {!trash && (
            <div className="library-footer">
              {libraryTab === "key" ? (
                <Button
                  disabled={!writable || !!busy}
                  onClick={() => keyFiles.current?.click()}
                >
                  <Upload size={16} />
                  导入关键素材
                </Button>
              ) : (
                <>
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
                </>
              )}
              <Button
                title="跨项目素材库"
                disabled={!writable}
                onClick={() => setModal("reuse")}
              >
                <Copy size={16} />
              </Button>
              {access.canManage && (
                <Button
                  title="导入ComfyUI既有产物"
                  disabled={!writable}
                  onClick={() => setModal("comfy-output")}
                >
                  <ImagePlus size={16} />
                </Button>
              )}
            </div>
          )}
          <input
            ref={files}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) importFiles(e.target.files, "all");
              e.target.value = "";
            }}
          />
          <input
            ref={keyFiles}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) importFiles(e.target.files, "key");
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
              if (e.target.files) importFiles(e.target.files, "all");
              e.target.value = "";
            }}
          />
        </aside>
        <PanelResizer
          side="left"
          value={panelLayout.left}
          onPointerDown={(event) => startPanelDrag("left", event)}
          onNudge={(delta) => nudgePanel("left", delta)}
          onReset={resetPanelLayout}
        />
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
                {!locatedAsset && selected.length > 1
                  ? `已选择 ${selected.length} 份素材`
                  : focus?.name || "把想法变成画面"}
              </span>
            </div>
            <div className="canvas-buttons">
              {returnTaskId && (
                <Button onClick={returnToTask}>
                  <Layers3 size={14} />
                  返回任务
                </Button>
              )}
              {focus && (
                <>
                  {!focusRemoved && (
                    <>
                      <Button
                        title={
                          focus.project?.curated ? "移出关键素材" : "加入关键素材"
                        }
                        disabled={!writable}
                        onClick={() => toggleKey(focus)}
                      >
                        <Star
                          size={16}
                          fill={focus.project?.curated ? "currentColor" : "none"}
                        />
                      </Button>
                      <Button title="素材详情" onClick={() => setModal("asset")}>
                        <SlidersHorizontal size={16} />
                      </Button>
                    </>
                  )}
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
            ) : !locatedAsset && selected.length > 1 ? (
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
              <div className="canvas-drop-hint">
                <div className="canvas-drop-icon" aria-hidden="true">
                  <WandSparkles size={28} strokeWidth={1.6} />
                </div>
                <p className="canvas-drop-title">将文件拖入</p>
                <p className="canvas-drop-description">也可以直接描述想法</p>
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
                {focusRemoved ? (
                  <Button
                    disabled={!writable || !!busy || focus.presence === "purged"}
                    onClick={() => restoreLibraryAsset(focus)}
                  >
                    恢复素材
                  </Button>
                ) : !trash && (
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
                disabled={
                  !conversationWritable || t.availability !== "available"
                }
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
        <PanelResizer
          side="right"
          value={panelLayout.right}
          onPointerDown={(event) => startPanelDrag("right", event)}
          onNudge={(delta) => nudgePanel("right", delta)}
          onReset={resetPanelLayout}
        />
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
              {assistantMode === "autonomous"
                ? "自主执行"
                : busy === "message" || planningTurn
                  ? "思考中"
                  : "自动规划"}
            </span>
          </div>
          <div
            className="assistant-mode-tabs"
            role="group"
            aria-label="创作助手模式"
          >
            <button
              aria-pressed={assistantMode === "assisted"}
              disabled={busy === "assistant-mode"}
              onClick={() => switchAssistantMode("assisted")}
            >
              辅助创作
            </button>
            <button
              aria-pressed={assistantMode === "autonomous"}
              disabled={busy === "assistant-mode"}
              onClick={() => switchAssistantMode("autonomous")}
            >
              自主创作
            </button>
          </div>
          {conversationItems
            .filter((c) => c.agent_run && c.id !== cid)
            .map((c) => (
              <button
                key={c.id}
                className="agent-return"
                onClick={() => chooseConversation(c)}
              >
                {c.title} · Agent{" "}
                {c.agent_run.state === "paused"
                  ? "已暂停"
                  : c.agent_run.state === "needs_attention"
                    ? "需要处理"
                    : "运行中"}{" "}
                · 返回
              </button>
            ))}
          <ConversationSwitcher
            projectId={store.projectId}
            items={conversationItems.filter(
              (c) => (c.mode ?? "assisted") === assistantMode,
            )}
            mode={assistantMode}
            current={conversation}
            limits={conversations.data?.context_limits}
            writable={writable}
            onChoose={chooseConversation}
            onRefresh={refresh}
            onError={setToast}
          />
          {assistantMode === "autonomous" && cid ? (
            <AutonomousAssistant
              key={cid}
              projectId={store.projectId}
              conversationId={cid}
              writable={conversationWritable}
              configured={!!health.data?.planner.configured}
              selected={selected}
              messages={chatMessages}
              earlierMessages={
                messages.hasNextPage
                  ? {
                      loading: messages.isFetchingNextPage,
                      load: () => void messages.fetchNextPage(),
                    }
                  : undefined
              }
              onError={setToast}
              onOpenAsset={(id) => {
                action("open-agent-output", async () => {
                  const asset = await api(`/assets/${id}`);
                  const value = {
                    ...asset,
                    project: asset.projects?.find(
                      (p: Data) => p.project_id === store.projectId,
                    ),
                  };
                  setFocus(value);
                  setLocatedAsset(value);
                  setModal("preview");
                });
              }}
            />
          ) : (
            <>
              <div className="conversation">
                {(conversations.isError || messages.isError) && (
                  <Empty icon={<MessageSquare />} title="对话暂时无法加载">
                    <Button
                      onClick={() => {
                        conversations.refetch();
                        messages.refetch();
                      }}
                    >
                      重新加载
                    </Button>
                  </Empty>
                )}
                {messages.hasNextPage && (
                  <Button
                    className="load-earlier-messages"
                    disabled={messages.isFetchingNextPage}
                    onClick={() => messages.fetchNextPage()}
                  >
                    {messages.isFetchingNextPage
                      ? "正在加载…"
                      : "加载更早的消息"}
                  </Button>
                )}
                {conversation &&
                  !chatMessages.length &&
                  !messages.isPending && (
                    <div className="assistant-intro">
                      <span className="eyebrow">一起把想法往前推进</span>
                      <h3>我会为你准备创作方案。</h3>
                      <p>
                        这是一个独立的对话。告诉我你想要的画面、风格，或选择项目素材继续打磨；其他对话的聊天记录不会带入。
                      </p>
                      <div className="suggestions">
                        {[
                          "生成一组电影感的自然风景",
                          "把选中的图片改成暖色调",
                          "让这张图片动起来",
                        ].map((text) => (
                          <button
                            key={text}
                            disabled={!conversationWritable}
                            onClick={() => setMessage(text)}
                          >
                            {text}
                            <ArrowUp size={13} />
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                {chatMessages.map((m) => (
                  <ChatMessage
                    key={m.id}
                    role={m.role}
                    content={m.content}
                    createdAt={m.created_at}
                  >
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
                      <details className="visual-observations message-details">
                        <summary>
                          模型图片观察 · {m.details.visual_observations.length}{" "}
                          张
                          <ChevronDown size={13} aria-hidden="true" />
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
                              本轮读图尺寸 {item.sent_width} ×{" "}
                              {item.sent_height}
                            </small>
                          </div>
                        ))}
                      </details>
                    )}
                  </ChatMessage>
                ))}
                {(busy === "message" || planningTurn) && (
                  <div className="thinking">
                    <LoaderCircle className="spin" size={16} />
                    {planningTurn?.stage === "composing"
                      ? "正在编写创作提示词…"
                      : planningTurn?.stage === "validating"
                        ? "正在检查方案…"
                        : "正在理解需求、选择工具…"}
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
                        {draft.prompt_metadata?.continuation
                          ? "生成视频"
                          : draft.tool_name}
                      </span>
                      <span>待确认 · v{draft.revision}</span>
                    </div>
                    <p>{draft.change_summary || "按以下参数执行一次创作。"}</p>
                    <CreativeSummary plan={draft} />
                    <div className="plan-specs">
                      <span>{draft.candidate_count} 个候选</span>
                      {(
                        draft.prompt_metadata?.continuation?.parameters ??
                        draft.parameters
                      ).width && (
                        <span>
                          {
                            (
                              draft.prompt_metadata?.continuation?.parameters ??
                              draft.parameters
                            ).width
                          }{" "}
                          ×{" "}
                          {
                            (
                              draft.prompt_metadata?.continuation?.parameters ??
                              draft.parameters
                            ).height
                          }
                        </span>
                      )}
                      {(
                        draft.prompt_metadata?.continuation?.parameters ??
                        draft.parameters
                      ).resolved_seconds && (
                        <span>
                          {(
                            draft.prompt_metadata?.continuation?.parameters ??
                            draft.parameters
                          ).resolved_seconds.toFixed(2)}{" "}
                          秒 ·{" "}
                          {
                            (
                              draft.prompt_metadata?.continuation?.parameters ??
                              draft.parameters
                            ).resolved_frames
                          }{" "}
                          帧
                        </span>
                      )}
                    </div>
                    <PlanInputs plan={draft} tools={tools.data ?? []} />
                    <PromptDetails
                      key={`${draft.id}-${draft.revision}`}
                      plan={draft}
                      onError={setToast}
                    />
                    <div className="plan-actions">
                      <Button
                        title="按专用指引重新编写，保留执行参数"
                        disabled={
                          !conversationWritable ||
                          !!busy ||
                          !!planningTurn ||
                          !!conversationRunning.length ||
                          !tools.data?.find((t) => t.id === draft.tool_id)
                            ?.assistant_guidance?.available ||
                          draft.tool_id === "image.upscale.seedvr2"
                        }
                        onClick={() => rewritePrompt(draft)}
                      >
                        <RefreshCw size={14} />
                        改写提示词
                      </Button>
                      <Button
                        disabled={
                          !!planningTurn ||
                          !!busy ||
                          !!draft.prompt_metadata?.continuation
                        }
                        title={
                          draft.prompt_metadata?.continuation
                            ? "通过对话修改整项视频任务"
                            : "修改参数"
                        }
                        onClick={() => {
                          const t = tools.data?.find(
                            (t) => t.id === draft.tool_id,
                          );
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
                        disabled={
                          !conversationWritable ||
                          !!busy ||
                          !!planningTurn ||
                          !!conversationRunning.length
                        }
                        onClick={() => execute(draft)}
                      >
                        <Play size={14} />
                        确认生成
                      </Button>
                    </div>
                  </div>
                )}
                <div ref={chatEnd} />
              </div>
              <form className="composer" onSubmit={send}>
                <details className="composer-advanced">
                  <summary>
                    {store.toolPreferences?.[cid ?? ""]
                      ? "高级设置 · 已限定工具"
                      : "高级设置 · 自动选择生成方式"}
                  </summary>
                  <label className="composer-tool-choice">
                    <WandSparkles size={13} />
                    <span>工具</span>
                    <select
                      aria-label="助手工具选择"
                      value={store.toolPreferences?.[cid ?? ""] ?? ""}
                      disabled={
                        !conversationWritable || !!planningTurn || !!busy
                      }
                      onChange={(event) => {
                        if (cid)
                          store.setToolPreference(cid, event.target.value);
                      }}
                    >
                      <option value="">自动选择</option>
                      {(tools.data ?? []).map((t) => (
                        <option
                          key={t.id}
                          value={t.id}
                          disabled={!t.assistant_guidance?.available}
                        >
                          {t.name}
                          {!t.assistant_guidance?.available
                            ? " · 当前不可用"
                            : ""}
                        </option>
                      ))}
                    </select>
                    {cid && store.toolPreferences?.[cid] && (
                      <button
                        type="button"
                        aria-label="恢复自动选择工具"
                        disabled={!!planningTurn || !!busy}
                        onClick={() => store.setToolPreference(cid, "")}
                      >
                        <X size={12} />
                      </button>
                    )}
                  </label>
                </details>
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
                            ? `本轮 ${imageCount} 张图片将发送至模型服务读图（上限 ${visionLimit} 张）`
                            : "本轮仅发送素材文字信息"}
                    </div>
                  </>
                )}
                <textarea
                  aria-label="发送给创作助手"
                  placeholder={
                    !health.data?.planner.configured
                      ? "先在设置中配置模型 API Key"
                      : "描述想法，选中图片或粘贴本工作台的图片链接…"
                  }
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  disabled={
                    !conversationWritable ||
                    !!conversationRunning.length ||
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
                    {conversationRunning.length
                      ? "等待本次结果，再继续打磨"
                      : project?.archived_at
                        ? "项目已归档，解除归档后继续"
                        : conversation?.archived_at
                          ? "对话已归档，恢复后继续"
                          : "确认后执行 · 完成后等待你的反馈"}
                  </span>
                  <Button
                    variant="primary"
                    type="submit"
                    aria-label="发送"
                    disabled={
                      !conversationWritable ||
                      !!planningTurn ||
                      !!busy ||
                      !!conversationRunning.length ||
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
            </>
          )}
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
      <TaskQueue
        open={store.taskOpen}
        tasks={tasks.data ?? []}
        tools={tools.data ?? []}
        conversations={conversationItems}
        focusedId={focusTaskId}
        onClose={() => {
          store.setTaskOpen(false);
          setFocusTaskId(null);
        }}
        onConversation={(value) => {
          chooseConversation(value);
          store.setTaskOpen(false);
        }}
        action={action}
        onPlan={(p) => {
          const owner = conversationItems.find(
            (c) => c.id === p.conversation_id,
          );
          if (owner) chooseConversation(owner);
          refresh();
          setToast("重做方案已准备，请确认执行");
          store.setTaskOpen(false);
        }}
        onPreview={(asset, task) => setTaskPreview({ asset, task })}
        onLocate={locateTaskAsset}
      />
      {taskPreview && (
        <TaskAssetPreview
          key={taskPreview.asset.key}
          asset={taskPreview.asset}
          task={taskPreview.task}
          onClose={() => setTaskPreview(null)}
          onLocate={locateTaskAsset}
        />
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
                    conversation_id:
                      editingPlan?.conversation_id ?? manualConversationId,
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
      {modal === "settings" && access.canManage && (
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
      {modal === "comfy-output" && access.canManage && (
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
          tags={tags}
          onClose={() => setModal(null)}
          action={action}
          onLineage={() => setModal("lineage")}
          onSourceTask={openSourceTask}
        />
      )}
      {modal === "preview" && focus && (
        <Modal title={focus.name} onClose={() => setModal(null)} wide>
          <div className="full-preview">
            <Media asset={focus} />
          </div>
          {focus.presence === "available" && focus.content_url && (
            <div className="modal-actions">
              <a
                className="btn"
                href={focus.content_url + "?download=true"}
                download
              >
                <ArrowDownToLine size={14} aria-hidden="true" />
                下载原文件
              </a>
            </div>
          )}
        </Modal>
      )}
      {modal === "lineage" && focus && (
        <LineageDialog asset={focus} onClose={() => setModal(null)} />
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
            项目、素材与执行记录保存在本机。助手自动选择生成方式，整项任务确认一次后使用现有
            ComfyUI 工作流执行。
          </p>
          <p className="muted">
            文字需求及所选素材的描述会发送给当前配置的模型服务。图片与视频生成由本机
            ComfyUI 完成。
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
  const [cachedFailed, setCachedFailed] = useState(false);
  useEffect(() => setCachedFailed(false), [asset.id]);
  if (asset.presence !== "available" && asset.preview_url && !cachedFailed)
    return (
      <div className="cached-media-preview">
        <p>原文件不可用，仅显示已保存的预览</p>
        <img
          src={asset.preview_url}
          alt={asset.name}
          onError={() => setCachedFailed(true)}
        />
      </div>
    );
  if (asset.presence !== "available")
    return (
      <Empty icon={<FileText size={30} />} title={labels[asset.presence]}>
        可以在任务详情中补存已有输出，或重新导入原文件。
      </Empty>
    );
  if (asset.media_type === "image")
    return (
      <ProgressiveImage
        className="canvas-image"
        src={asset.content_url}
        previewSrc={asset.preview_url}
        alt={asset.name}
      />
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
            inputs: tool.inputs
              .filter((i: Data) => inputs[i.slot])
              .map((i: Data) => ({
                slot: i.slot,
                asset_id: inputs[i.slot],
              })),
            candidate_count: count,
            change_summary: summary,
            based_on_execution_id: plan?.based_on_execution_id || null,
            ...(plan?.prompt_metadata ? { prompt_mode: "manual" } : {}),
          });
        }}
      >
        <div className="tool-form">
          {plan?.prompt_metadata && (
            <p className="manual-prompt-notice">
              此处为手动编辑，保存后保留指引来源并标记为手动方案。需要按专用指引改写时，请使用方案卡的“改写提示词”。
            </p>
          )}
          {tool.inputs.map((input: Data) => (
            <label key={input.slot}>
              {input.label}
              <select
                required={input.required !== false}
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
                    required={field.required}
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
                      field.type === "seed"
                        ? "留空随机"
                        : field.required
                          ? "必填"
                          : "跟随输入"
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
              输出为24fps，实际时长按工作流帧数规则对齐。参考图不限制视频画面比例；关键帧模式会按目标画面调整输入。
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
  tags,
  onClose,
  action,
  onLineage,
  onSourceTask,
}: {
  asset: Data;
  project: Data;
  tags: Data[];
  onClose: () => void;
  action: Action;
  onLineage: () => void;
  onSourceTask: (taskId: string) => void;
}) {
  const detail = useQuery({
    queryKey: ["detail", asset.id],
    queryFn: () => api(`/assets/${asset.id}`),
  });
  const [name, setName] = useState(asset.project?.display_name || asset.name),
    [note, setNote] = useState(asset.project?.note || ""),
    [tagIds, setTagIds] = useState<string[]>(
      (asset.project?.tags || []).map((tag: Data) => tag.id),
    ),
    [newTag, setNewTag] = useState(""),
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
        素材名称（仅本项目）
        <input
          maxLength={120}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={asset.original_name || asset.name}
        />
        <small className="muted">
          只改本项目里的显示名，原文件名保留为{" "}
          {asset.original_name || asset.name}；留空恢复原名。
        </small>
      </label>
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
      <fieldset className="tag-field">
        <legend>关键素材标签</legend>
        <p className="muted">
          勾选标签会把这份素材纳入本项目关键素材库；标签只属于当前项目。
        </p>
        <div className="tag-options">
          {tags.map((tag) => (
            <label key={tag.id}>
              <input
                type="checkbox"
                checked={tagIds.includes(tag.id)}
                onChange={(e) =>
                  setTagIds(
                    e.target.checked
                      ? [...tagIds, tag.id]
                      : tagIds.filter((id) => id !== tag.id),
                  )
                }
              />
              <i style={{ background: tag.color }} />
              {tag.name}
            </label>
          ))}
          {!tags.length && <small>还没有标签，可以在下面新建。</small>}
        </div>
        <div className="tag-picker-create">
          <input
            aria-label="新标签名称"
            value={newTag}
            placeholder="新建标签，如：人物/小雨"
            onChange={(e) => setNewTag(e.target.value)}
          />
          <Button
            disabled={!newTag.trim()}
            onClick={async () => {
              const created = (await action(
                "tag",
                () =>
                  api(`/projects/${project.id}/tags`, "POST", {
                    name: newTag.trim(),
                  }),
                "标签已创建",
              )) as Data | undefined;
              if (created) {
                setTagIds([...tagIds, created.id]);
                setNewTag("");
              }
            }}
          >
            新建
          </Button>
        </div>
      </fieldset>
      {detail.data?.execution && (
        <Button onClick={() => onSourceTask(detail.data!.execution.task_id)}>
          <Layers3 size={14} />
          来源任务 · 候选 {detail.data.execution.candidate_index + 1}
        </Button>
      )}
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
        <Button
          disabled={!!project.archived_at || !!asset.project?.removed_at}
          onClick={() =>
            action(
              asset.project?.curated ? "unkey" : "key",
              () =>
                api(
                  asset.project?.curated
                    ? `/projects/${project.id}/key-assets/remove`
                    : `/projects/${project.id}/key-assets`,
                  "POST",
                  { asset_ids: [asset.id] },
                ),
              asset.project?.curated ? "已移出关键素材" : "已加入关键素材",
            )
          }
        >
          <Star
            size={15}
            fill={asset.project?.curated ? "currentColor" : "none"}
          />
          {asset.project?.curated ? "移出关键素材" : "加入关键素材"}
        </Button>
        <Button onClick={onLineage}>
          <GitBranch size={15} />
          来源关系
        </Button>
        {detail.data?.execution &&
          asset.role === "final" &&
          !asset.source_info?.internal_preparation &&
          !(
            detail.data.execution.configuration?.step_count > 1 &&
            detail.data.execution.configuration?.step_index === 0
          ) && (
            <Button
              disabled={!!project.archived_at || !!asset.project?.removed_at}
              onClick={() =>
                action(
                  "best",
                  () =>
                    api(
                      `/tasks/${detail.data!.execution.task_id}/best`,
                      "POST",
                      {
                        asset_id: asset.id,
                      },
                    ),
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
          disabled={!!project.archived_at || !!asset.project?.removed_at}
          onClick={() =>
            action("note", async () => {
              await api(`/projects/${project.id}/assets/${asset.id}`, "PATCH", {
                name,
                note,
                usage,
                revision: loadedRevision,
              });
              await api(
                `/projects/${project.id}/assets/${asset.id}/tags`,
                "PUT",
                { tag_ids: tagIds },
              );
              onClose();
            })
          }
        >
          保存备注与标签
        </Button>
      </div>
    </Modal>
  );
}

function TagManager({
  tags,
  busy,
  onCreate,
  onRename,
  onColor,
  onDelete,
  onClose,
}: {
  tags: Data[];
  busy: boolean;
  onCreate: (name: string) => void;
  onRename: (id: string, name: string) => void;
  onColor: (id: string, color: string) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  return (
    <div className="tag-manager">
      <div className="tag-manager-head">
        <strong>标签管理</strong>
        <Button aria-label="关闭标签管理" onClick={onClose}>
          <X size={13} />
        </Button>
      </div>
      <div className="tag-manager-list">
        {tags.map((tag) => (
          <TagRow
            key={`${tag.id}:${tag.name}:${tag.color}`}
            tag={tag}
            busy={busy}
            onRename={onRename}
            onColor={onColor}
            onDelete={onDelete}
          />
        ))}
        {!tags.length && <small>还没有标签，先建一个人物或场景。</small>}
      </div>
      <form
        className="tag-manager-create"
        onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim() || busy) return;
          onCreate(name.trim());
          setName("");
        }}
      >
        <input
          aria-label="新标签名称"
          placeholder="新标签，如：人物/小雨"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button type="submit" disabled={!name.trim() || busy}>
          <Plus size={13} />
          新建
        </Button>
      </form>
      <small className="tag-manager-hint">
        拖一张素材到标签上即可放入；删除标签不会删除素材。
      </small>
    </div>
  );
}

function TagRow({
  tag,
  busy,
  onRename,
  onColor,
  onDelete,
}: {
  tag: Data;
  busy: boolean;
  onRename: (id: string, name: string) => void;
  onColor: (id: string, color: string) => void;
  onDelete: (id: string) => void;
}) {
  const [name, setName] = useState(tag.name);
  const [color, setColor] = useState(tag.color);
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="tag-row">
      <input
        className="tag-color"
        type="color"
        aria-label={`${tag.name}的颜色`}
        value={color}
        disabled={busy}
        onChange={(e) => setColor(e.target.value)}
        onBlur={() => {
          if (color !== tag.color) onColor(tag.id, color);
        }}
      />
      <input
        className="tag-name-input"
        aria-label={`${tag.name}的名称`}
        value={name}
        disabled={busy}
        onChange={(e) => setName(e.target.value)}
        onBlur={() => {
          const value = name.trim();
          if (value && value !== tag.name) onRename(tag.id, value);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
      <span title={`${tag.count} 份关键素材`}>{tag.count}</span>
      {confirming ? (
        <span className="tag-row-confirm">
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => onDelete(tag.id)}
          >
            删除
          </Button>
          <Button onClick={() => setConfirming(false)}>取消</Button>
        </span>
      ) : (
        <Button
          title="删除标签"
          aria-label={`删除标签 ${tag.name}`}
          disabled={busy}
          onClick={() => setConfirming(true)}
        >
          <Trash2 size={13} />
        </Button>
      )}
    </div>
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
      description="对话模型、资源预算和素材维护。"
      onClose={onClose}
      wide
    >
      {!s ? (
        <LoaderCircle className="spin" />
      ) : (
        <div className="settings-content">
          <section>
            <h3>对话模型 API</h3>
            <div className="settings-summary">
              <span
                className={`status-dot ${s.api_key_configured ? "green" : ""}`}
              />
              {s.api_key_configured ? "已配置密钥" : "尚未配置密钥"}
              <span className="badge">{s.llm_model}</span>
            </div>
            <label>
              API Key
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
                当前进程使用 {s.api_key_environment} 环境变量，更改后需重启。
              </small>
            )}
            <p className="muted">
              助手会将需求、项目背景、素材描述和选中图片的读图副本发送至当前模型服务。视频暂只发送文件信息。图像与视频生成在本机完成，密钥存入
              Windows 凭据管理器。
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
              按 DeepSeek 高峰价格估算，实际账单以当前服务商为准。未知费用调用：
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
