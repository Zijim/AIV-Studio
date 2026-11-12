import { ListChecks, Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { clock } from "../lib";

export function ChatMessage({
  role,
  content,
  createdAt,
  children,
}: {
  role: string;
  content: string;
  createdAt?: string;
  children?: ReactNode;
}) {
  const kind = role === "user" || role === "assistant" ? role : "system";
  const label =
    kind === "user" ? "你" : kind === "assistant" ? "创作助手" : "执行记录";
  const Icon = kind === "assistant" ? Sparkles : ListChecks;

  return (
    <article className={`chat-message ${kind}`} aria-label={label}>
      <div className="message-role">
        {kind !== "user" && (
          <span className="message-icon" aria-hidden="true">
            <Icon size={13} />
          </span>
        )}
        <span>{label}</span>
        {createdAt && <time dateTime={createdAt}>{clock(createdAt)}</time>}
      </div>
      <div className="message-bubble">
        <p className="message-content">{content}</p>
        {children}
      </div>
    </article>
  );
}
