import { create } from "zustand";
import { persist } from "zustand/middleware";
import { z } from "zod";

export type Data = Record<string, any>;
let csrf = "";
export let eventCursor = 0;
export async function bootstrap() {
  const response = await fetch("/api/v1/session");
  if (!response.ok) throw new Error("无法连接本地服务");
  const session = await response.json();
  csrf = session.csrf_token;
  eventCursor = session.event_cursor;
}
export async function api<T = Data>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const form = body instanceof FormData;
  const response = await fetch("/api/v1" + path, {
    method,
    headers: {
      ...(form ? {} : { "Content-Type": "application/json" }),
      ...(method === "GET" ? {} : { "X-AIV-CSRF": csrf }),
    },
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error?.message || "请求失败") as Error & {
      code?: string;
      details?: Data;
    };
    error.code = value.error?.code;
    error.details = value.error?.details;
    throw error;
  }
  return value;
}
export const messageSchema = z
  .string()
  .trim()
  .min(1, "请先描述你的想法")
  .max(16000, "描述过长");
export const useWorkspace = create(
  persist<{
    projectId: string;
    selected: string[];
    imageRoles: Record<string, string>;
    taskOpen: boolean;
    chooseProject: (id: string) => void;
    chooseAsset: (id: string, additive?: boolean) => void;
    setImageRole: (id: string, role: string) => void;
    clear: () => void;
    setTaskOpen: (value: boolean) => void;
  }>(
    (set) => ({
      projectId: "",
      selected: [],
      imageRoles: {},
      taskOpen: false,
      chooseProject: (id) =>
        set({ projectId: id, selected: [], imageRoles: {} }),
      chooseAsset: (id, additive) =>
        set((s) => {
          const selected = additive
            ? s.selected.includes(id)
              ? s.selected.filter((x) => x !== id)
              : [...s.selected, id]
            : [id];
          return {
            selected,
            imageRoles: Object.fromEntries(
              Object.entries(s.imageRoles).filter(([key]) =>
                selected.includes(key),
              ),
            ),
          };
        }),
      setImageRole: (id, role) =>
        set((s) => ({
          imageRoles: {
            ...Object.fromEntries(
              Object.entries(s.imageRoles).map(([key, value]) => [
                key,
                role === "first_frame" && value === "first_frame"
                  ? "auto"
                  : value,
              ]),
            ),
            [id]: role,
          },
        })),
      clear: () => set({ selected: [], imageRoles: {} }),
      setTaskOpen: (value) => set({ taskOpen: value }),
    }),
    {
      name: "aiv-workspace",
      partialize: (s) => ({ ...s, selected: [], imageRoles: {} }),
    },
  ),
);
export const bytes = (n: number) =>
  n >= 1024 ** 3
    ? (n / 1024 ** 3).toFixed(1) + " GB"
    : n >= 1024 ** 2
      ? (n / 1024 ** 2).toFixed(1) + " MB"
      : (n / 1024).toFixed(1) + " KB";
export const clock = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
export const labels: Record<string, string> = {
  queued: "等待执行",
  preparing: "准备素材",
  waiting_external: "等待外部任务",
  submitting: "正在提交",
  submission_unknown: "核实提交",
  running: "执行中",
  saving: "保存中",
  completed: "已完成",
  succeeded: "执行成功",
  failed: "失败",
  cancelled: "已取消",
  cancel_requested: "正在取消",
  needs_attention: "需要处理",
  unknown: "结果未知",
  interrupted: "执行中断",
  pending: "待保存",
  partial: "部分保存",
  missing: "文件缺失",
  complete: "保存完整",
  none: "暂无输出",
  draft: "待确认",
  consumed: "已执行",
  superseded: "已有新方案",
  dismissed: "已放弃",
  imported: "导入素材",
  final: "最终结果",
  preview: "预览",
  intermediate: "中间产物",
  available: "可用",
  purged: "已永久清理",
};
