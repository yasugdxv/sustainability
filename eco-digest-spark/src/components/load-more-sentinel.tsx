import { useEffect, useRef } from "react";

// 無限スクロール用の目印。画面の下に近づいたら（見えたら）onVisible を呼んで、続きを読み込む。
// 先読みのため、画面の下端の400px手前で発火する。自動で読み込めない環境のために、ボタンも置く。
export function LoadMoreSentinel({
  hasMore,
  loading,
  onVisible,
  label = "もっと見る",
}: {
  hasMore: boolean;
  loading: boolean;
  onVisible: () => void;
  label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !hasMore || loading || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onVisible();
      },
      { rootMargin: "400px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasMore, loading, onVisible]);

  if (!hasMore) return null;
  return (
    <div ref={ref} className="flex justify-center py-8">
      <button
        type="button"
        onClick={onVisible}
        disabled={loading}
        className="px-5 py-2 rounded-full border border-border text-sm text-muted-foreground hover:bg-muted transition-colors disabled:opacity-60"
      >
        {loading ? "..." : label}
      </button>
    </div>
  );
}
