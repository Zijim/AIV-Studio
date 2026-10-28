import { useState } from "react";

type Props = {
  src: string;
  previewSrc?: string | null;
  alt: string;
  className?: string;
};

export function ProgressiveImage(props: Props) {
  return (
    <ImageLayers key={`${props.src}|${props.previewSrc ?? ""}`} {...props} />
  );
}

function ImageLayers({ src, previewSrc, alt, className = "" }: Props) {
  const [previewReady, setPreviewReady] = useState(false);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [originalState, setOriginalState] = useState<
    "loading" | "ready" | "failed"
  >("loading");
  const [attempt, setAttempt] = useState(0);
  // Let the much smaller preview finish first instead of competing with the original.
  const startOriginal = !previewSrc || previewReady || previewFailed;
  const ready = originalState === "ready";

  function retry() {
    setOriginalState("loading");
    setPreviewFailed(false);
    setPreviewReady(false);
    setAttempt((value) => value + 1);
  }

  return (
    <div
      className={`progressive-image ${ready ? "is-ready" : ""} ${className}`}
      aria-busy={originalState === "loading"}
    >
      {previewSrc && !previewFailed && (
        <img
          key={`preview-${attempt}`}
          className="progressive-image-preview"
          src={previewSrc}
          alt={ready ? "" : alt}
          aria-hidden={ready}
          decoding="async"
          fetchPriority="high"
          onLoad={() => setPreviewReady(true)}
          onError={() => setPreviewFailed(true)}
        />
      )}
      {startOriginal && (
        <img
          key={`original-${attempt}`}
          className="progressive-image-original"
          src={src}
          alt={ready ? alt : ""}
          aria-hidden={!ready}
          decoding="async"
          fetchPriority="low"
          onLoad={async (event) => {
            const image = event.currentTarget;
            try {
              await image.decode();
              if (image.isConnected) setOriginalState("ready");
            } catch {
              if (image.isConnected) setOriginalState("failed");
            }
          }}
          onError={() => setOriginalState("failed")}
        />
      )}
      {!ready && (
        <div className="image-loading-status" role="status">
          {originalState === "failed" ? (
            <>
              <span>
                {previewReady
                  ? "高清图加载失败，已保留预览"
                  : "图片暂时无法加载"}
              </span>
              <button type="button" onClick={retry}>
                重试
              </button>
            </>
          ) : (
            <span>
              {previewReady || !previewSrc || previewFailed
                ? "正在加载高清图…"
                : "正在加载预览…"}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
