import { useEffect, useState } from "react";
import { TopBar } from "@/components/top-bar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useReviewPrincipal } from "@/lib/weekly-review";
import {
  REVIEW_FILTER_LABELS,
  useReviewAction,
  useReviewItem,
  useReviewItems,
  type ReviewFilter,
  type ReviewItem,
  type ReviewKind,
} from "@/lib/review";

// 日次ダイジェスト／月次レポート共通の「一覧→本文プレビュー→承認して送信/却下」画面
export function ReviewPage({ kind, title, description, emptyHint }: {
  kind: ReviewKind;
  title: string;
  description: string;
  emptyHint: string;
}) {
  const [filter, setFilter] = useState<ReviewFilter | "">("pending");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: principal } = useReviewPrincipal();
  const { data, isLoading, isError, error } = useReviewItems(kind, filter);
  const items = data?.items ?? [];

  useEffect(() => {
    if (items.length === 0) setSelectedId(null);
    else if (!items.some((i) => i.id === selectedId)) setSelectedId(items[0].id);
  }, [items, selectedId]);

  return (
    <>
      <TopBar breadcrumb={[{ label: "メールレビュー" }, { label: title }]} />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{title}</h1>
          <p className="text-sm text-muted-foreground mt-2">{description}</p>
          {principal && (
            <p className={`text-xs mt-2 ${principal.ok ? "text-muted-foreground" : "text-destructive"}`}>
              {principal.ok
                ? `操作者: ${principal.displayName}（${principal.userId} / ${principal.authSource}）`
                : `認証済みレビュー担当者を解決できないため、承認・却下は無効です。${principal.error ?? ""}`}
            </p>
          )}
        </section>

        <div className="flex items-center gap-3">
          <Select value={filter || "all"} onValueChange={(v) => setFilter(v === "all" ? "" : (v as ReviewFilter))}>
            <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
            <SelectContent>
              {(Object.keys(REVIEW_FILTER_LABELS) as ReviewFilter[]).map((s) => (
                <SelectItem key={s} value={s}>{REVIEW_FILTER_LABELS[s]}</SelectItem>
              ))}
              <SelectItem value="all">すべて</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-sm text-muted-foreground">{items.length}件</span>
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">読み込み中…</p>}
        {isError && <p className="text-sm text-destructive">取得に失敗しました: {(error as Error).message}</p>}
        {!isLoading && !isError && items.length === 0 && <p className="text-sm text-muted-foreground">{emptyHint}</p>}

        {items.length > 0 && (
          <div className="grid grid-cols-[320px_1fr] gap-6 items-start">
            <ul className="space-y-2">
              {items.map((i) => (
                <li key={i.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(i.id)}
                    className={`w-full text-left rounded-lg border p-3 transition-colors ${
                      i.id === selectedId ? "border-primary bg-accent" : "border-border hover:bg-accent/50"
                    }`}
                  >
                    <Badge variant="secondary">{statusLabel(i)}</Badge>
                    <div className="mt-2 text-sm font-medium line-clamp-2">{i.subject || "(件名なし)"}</div>
                    <div className="mt-1 text-xs text-muted-foreground">{i.dateLabel}</div>
                  </button>
                </li>
              ))}
            </ul>
            <ReviewDetail kind={kind} id={selectedId} canApprove={!!principal?.canApprove} />
          </div>
        )}
      </main>
    </>
  );
}

function statusLabel(i: ReviewItem): string {
  if (i.auto) return "自動配信済み";
  return REVIEW_FILTER_LABELS[i.status as ReviewFilter] ?? i.rawStatus;
}

function ReviewDetail({ kind, id, canApprove }: { kind: ReviewKind; id: string | null; canApprove: boolean }) {
  const { data: item, isLoading } = useReviewItem(kind, id);
  const action = useReviewAction(kind);
  const [freeText, setFreeText] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    setFreeText("");
    setMessage(null);
  }, [id]);

  if (!id || isLoading || !item) return <p className="text-sm text-muted-foreground">読み込み中…</p>;

  const disabled = !canApprove || action.isPending;
  const submit = (act: "approve" | "reject") => {
    if (act === "approve" && !window.confirm("承認すると、宛先へ実際にメールが送信されます。よろしいですか？")) return;
    action.mutate(
      { id: item.id, action: act, freeText },
      {
        onSuccess: () => setMessage({ kind: "ok", text: act === "approve" ? "承認・送信しました" : "却下しました" }),
        onError: (e) => setMessage({ kind: "error", text: `処理に失敗しました: ${(e as Error).message}` }),
      },
    );
  };

  return (
    <div className="space-y-5 min-w-0">
      <div>
        <h2 className="text-lg font-semibold">{item.subject}</h2>
        <p className="text-xs text-muted-foreground mt-1">
          id: {item.id}　{item.meta}
          {item.reviewerId ? `　reviewer: ${item.reviewerId}` : ""}
          {item.sendStatus ? `　send_status: ${item.sendStatus}` : ""}
        </p>
      </div>

      {item.htmlBody ? (
        // メール本文は外部由来の文字列を含むため、スクリプト等を実行できないsandboxで表示する
        <iframe title="メール本文プレビュー" sandbox="" srcDoc={item.htmlBody}
          className="w-full h-[700px] rounded-lg border border-border bg-white" />
      ) : (
        <p className="text-sm text-muted-foreground">メール本文がまだ生成されていません。</p>
      )}

      {item.reviewable && (
        <section className="space-y-3 border-t border-border pt-5">
          <h3 className="font-semibold">レビュー</h3>
          <div>
            <label className="text-sm text-muted-foreground">自由記述フィードバック</label>
            <Textarea value={freeText} onChange={(e) => setFreeText(e.target.value)} rows={2} />
          </div>
          <div className="flex gap-3">
            <Button disabled={disabled} onClick={() => submit("approve")}>承認して送信</Button>
            <Button variant="outline" disabled={disabled} onClick={() => submit("reject")}>却下</Button>
          </div>
          {message && (
            <p className={`text-sm ${message.kind === "ok" ? "text-green-600" : "text-destructive"}`}>{message.text}</p>
          )}
        </section>
      )}
    </div>
  );
}
