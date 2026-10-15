import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { TopBar } from "@/components/top-bar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useCandidateAction, useCandidates, type CandidateStatus, type SourceCandidate } from "@/lib/admin";
import { useReviewPrincipal } from "@/lib/weekly-review";

export const Route = createFileRoute("/review/candidates")({
  component: CandidatesPage,
});

const STATUS_LABELS: Record<CandidateStatus, string> = { unconfirmed: "未確認", active: "有効", all: "すべて" };

function CandidatesPage() {
  const [status, setStatus] = useState<CandidateStatus>("unconfirmed");
  const [query, setQuery] = useState("");
  const { data: principal } = useReviewPrincipal();
  const { data, isLoading, isError, error } = useCandidates(status);
  const q = query.trim().toLowerCase();
  const candidates = (data?.candidates ?? []).filter(
    (c) => !q || c.url.toLowerCase().includes(q) || (c.pageTitle ?? "").toLowerCase().includes(q),
  );

  return (
    <>
      <TopBar breadcrumb={[{ label: "メールレビュー" }, { label: "競合クロール候補管理" }]} />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">競合クロール候補管理</h1>
          <p className="text-sm text-muted-foreground mt-2">
            サステナビリティ起点ページから自動発見された候補URLです。種別を確認・修正し、問題なければ有効化して監視対象に追加してください。
          </p>
          {principal && !principal.ok && (
            <p className="text-xs mt-2 text-destructive">
              認証済みレビュー担当者を解決できないため、有効化・無効化は無効です。{principal.error ?? ""}
            </p>
          )}
        </section>

        <div className="flex items-center gap-3 flex-wrap">
          <Select value={status} onValueChange={(v) => setStatus(v as CandidateStatus)}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>
              {(Object.keys(STATUS_LABELS) as CandidateStatus[]).map((s) => (
                <SelectItem key={s} value={s}>{STATUS_LABELS[s]}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input className="w-72" placeholder="URL・タイトルで絞り込み" value={query} onChange={(e) => setQuery(e.target.value)} />
          <span className="text-sm text-muted-foreground">{candidates.length}件</span>
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">読み込み中…</p>}
        {isError && <p className="text-sm text-destructive">取得に失敗しました: {(error as Error).message}</p>}
        {!isLoading && !isError && candidates.length === 0 && (
          <p className="text-sm text-muted-foreground">候補URLがありません。先に python competitor_crawler.py を実行してください。</p>
        )}

        <ul className="space-y-3">
          {candidates.map((c) => (
            <CandidateCard key={c.id} candidate={c} urlTypes={data?.urlTypes ?? []} canAct={!!principal?.canApprove} />
          ))}
        </ul>
      </main>
    </>
  );
}

function CandidateCard({ candidate: c, urlTypes, canAct }: { candidate: SourceCandidate; urlTypes: string[]; canAct: boolean }) {
  const action = useCandidateAction();
  const [urlType, setUrlType] = useState(c.urlType);
  const [error, setError] = useState<string | null>(null);
  const run = (a: "activate" | "deactivate") =>
    action.mutate({ id: c.id, action: a, urlType }, { onError: (e) => setError((e as Error).message), onSuccess: () => setError(null) });

  return (
    <li className="rounded-lg border border-border p-4 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant={c.isActive ? "default" : "secondary"}>{c.isActive ? "有効" : "未確認"}</Badge>
        <Badge variant="outline">{c.urlType}</Badge>
        <span className="text-sm font-medium">{c.pageTitle || c.url}</span>
      </div>
      <a href={c.url} target="_blank" rel="noreferrer noopener" className="text-xs text-primary break-all underline">{c.url}</a>
      <p className="text-xs text-muted-foreground">
        リンク元source_id: {c.discoveredFromSourceId ?? "—"}　最終確認日時: {c.lastCheckedAt ?? "—"}
        クロール可否: {c.isCrawlable ? "○" : "×（直近チェック時に取得失敗）"}
      </p>
      <div className="flex items-center gap-3 flex-wrap">
        {c.isActive ? (
          <Button variant="outline" size="sm" disabled={!canAct || action.isPending} onClick={() => run("deactivate")}>無効化</Button>
        ) : (
          <>
            <Select value={urlType} onValueChange={setUrlType}>
              <SelectTrigger className="w-56"><SelectValue /></SelectTrigger>
              <SelectContent>
                {urlTypes.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button size="sm" disabled={!canAct || action.isPending} onClick={() => run("activate")}>有効化して監視対象に追加</Button>
            <span className="text-xs text-muted-foreground">種別は自動推測です。誤っていれば修正してから有効化してください。</span>
          </>
        )}
      </div>
      {error && <p className="text-sm text-destructive">処理に失敗しました: {error}</p>}
    </li>
  );
}
