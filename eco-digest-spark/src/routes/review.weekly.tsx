import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { TopBar } from "@/components/top-bar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  REVIEW_STATUS_LABELS,
  useLatestBuildJob,
  useReviewPrincipal,
  useReviewWeeklyReport,
  useStartBuild,
  useWeeklyReport,
  useWeeklyReports,
  type ReviewStatus,
} from "@/lib/weekly-review";

export const Route = createFileRoute("/review/weekly")({
  component: WeeklyReviewPage,
});

function WeeklyReviewPage() {
  const [status, setStatus] = useState<ReviewStatus | "">("review_required");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: principal } = useReviewPrincipal();
  const { data, isLoading, isError, error } = useWeeklyReports(status);
  const reports = data?.reports ?? [];

  // 一覧が読み込まれたら先頭を選択。選択中のものが一覧から消えた場合も先頭へ切り替える
  useEffect(() => {
    if (reports.length === 0) setSelectedId(null);
    else if (!reports.some((r) => r.id === selectedId)) setSelectedId(reports[0].id);
  }, [reports, selectedId]);

  return (
    <>
      <TopBar breadcrumb={[{ label: "メールレビュー" }, { label: "週次メール" }]} />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">週次メールレビュー</h1>
          <p className="text-sm text-muted-foreground mt-2">
            週次メールのドラフトは自動送信されません。内容を確認のうえ、承認して送信/却下/修正してください。
          </p>
          {principal && (
            <p className={`text-xs mt-2 ${principal.ok ? "text-muted-foreground" : "text-destructive"}`}>
              {principal.ok
                ? `操作者: ${principal.displayName}（${principal.userId} / ${principal.authSource}）`
                : `認証済みレビュー担当者を解決できないため、承認・却下・修正保存は無効です。${principal.error ?? ""}`}
            </p>
          )}
        </section>

        <BuildPanel canRun={!!principal?.ok} onDone={(id) => { setStatus("review_required"); setSelectedId(id); }} />

        <div className="flex items-center gap-3">
          <Select value={status || "all"} onValueChange={(v) => setStatus(v === "all" ? "" : (v as ReviewStatus))}>
            <SelectTrigger className="w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(REVIEW_STATUS_LABELS) as ReviewStatus[]).map((s) => (
                <SelectItem key={s} value={s}>{REVIEW_STATUS_LABELS[s]}</SelectItem>
              ))}
              <SelectItem value="all">すべて</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-sm text-muted-foreground">{reports.length}件</span>
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">読み込み中…</p>}
        {isError && (
          <p className="text-sm text-destructive">
            週次ドラフトの取得に失敗しました: {(error as Error).message}
            （sql/2026-07-22_weekly_email_review_gate_schema.sql の適用が必要な可能性があります）
          </p>
        )}
        {!isLoading && !isError && reports.length === 0 && (
          <p className="text-sm text-muted-foreground">
            該当する週次ドラフトがありません。先に <code>python weekly_email_report.py build</code> を実行してください。
          </p>
        )}

        {reports.length > 0 && (
          <div className="grid grid-cols-[320px_1fr] gap-6 items-start">
            <ul className="space-y-2">
              {reports.map((r) => (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(r.id)}
                    className={`w-full text-left rounded-lg border p-3 transition-colors ${
                      r.id === selectedId ? "border-primary bg-accent" : "border-border hover:bg-accent/50"
                    }`}
                  >
                    <Badge variant="secondary">{REVIEW_STATUS_LABELS[r.reviewStatus] ?? r.reviewStatus}</Badge>
                    <div className="mt-2 text-sm font-medium line-clamp-2">{r.subject || "(件名なし)"}</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {r.periodStart}〜{r.periodEnd}　記事{r.articleCount}件
                    </div>
                  </button>
                </li>
              ))}
            </ul>
            <ReportDetail id={selectedId} canApprove={!!principal?.canApprove} />
          </div>
        )}
      </main>
    </>
  );
}

function BuildPanel({ canRun, onDone }: { canRun: boolean; onDone: (reportId: string) => void }) {
  const { data } = useLatestBuildJob();
  const start = useStartBuild();
  const [sinceDays, setSinceDays] = useState("7");
  const [force, setForce] = useState(false);
  const job = data?.job ?? null;
  const running = job?.state === "running" || start.isPending;

  // 実行中ジョブが完了したら、新しいドラフトを選択する
  const lastJob = useRef(job?.state);
  useEffect(() => {
    if (lastJob.current === "running" && job?.state === "done" && job.reportId) onDone(job.reportId);
    lastJob.current = job?.state;
  }, [job?.state, job?.reportId, onDone]);

  const run = () => {
    const n = Number(sinceDays);
    if (!Number.isInteger(n) || n < 1 || n > 31) return;
    if (!window.confirm(`直近${n}日分で週次ドラフトを生成します（LLMを呼び出すため数分かかります。メールは送信されません）。よろしいですか？`)) return;
    start.mutate({ sinceDays: n, forceGeoRerun: force });
  };

  return (
    <section className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="font-semibold">ドラフト生成</h2>
        <label className="text-sm text-muted-foreground flex items-center gap-2">
          対象期間（日）
          <Input type="number" min={1} max={31} value={sinceDays} onChange={(e) => setSinceDays(e.target.value)} className="w-20" />
        </label>
        <label className="text-sm text-muted-foreground flex items-center gap-2">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Geo Intelligenceを強制再実行
        </label>
        <Button disabled={!canRun || running} onClick={run}>{running ? "生成中…" : "ドラフトを生成"}</Button>
        <span className="text-xs text-muted-foreground">生成してもメールは送信されません。承認は下の一覧から行います。</span>
      </div>
      {start.isError && <p className="text-sm text-destructive">{(start.error as Error).message}</p>}
      {job && (
        <div className="space-y-1">
          <p className="text-sm">
            状態: {{ running: "生成中", done: "完了", error: "失敗", lost: "状態不明" }[job.state]}
            {job.error ? `（${job.error}）` : ""}
          </p>
          {(job.state === "running" || job.state !== "done") && job.log && (
            <pre className="max-h-48 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap">{job.log}</pre>
          )}
        </div>
      )}
    </section>
  );
}

function ReportDetail({ id, canApprove }: { id: string | null; canApprove: boolean }) {
  const { data: report, isLoading } = useWeeklyReport(id);
  const review = useReviewWeeklyReport();
  const [freeText, setFreeText] = useState("");
  const [jsonText, setJsonText] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  // 既定はテスト送信(テスト受信者だけへ送る)。誤って本番の宛先へ送らないよう、本番に送るときだけ外す
  const [testMode, setTestMode] = useState(true);

  // 別のドラフトを選んだ(または再読込された)ら、編集欄をその内容で初期化する
  useEffect(() => {
    if (report) {
      setJsonText(JSON.stringify(report.draftContent, null, 2));
      setFreeText("");
      setMessage(null);
    }
  }, [report]);

  if (!id || isLoading || !report) return <p className="text-sm text-muted-foreground">読み込み中…</p>;

  const rejected = report.reviewStatus === "rejected";
  const disabled = !canApprove || rejected || review.isPending;

  const submit = (action: "approved" | "rejected" | "review_required") => {
    let edited: Record<string, unknown> | undefined;
    try {
      const parsed = JSON.parse(jsonText) as Record<string, unknown>;
      // 変更がなければ本文は送らない(Streamlit版と同じ挙動)
      if (JSON.stringify(parsed) !== JSON.stringify(report.draftContent)) edited = parsed;
    } catch {
      setMessage({ kind: "error", text: "週次ドラフト本体のJSONが不正です。修正内容は保存されませんでした。" });
      return;
    }
    if (action === "approved") {
      const ask = testMode
        ? "テスト送信します。テスト受信者(is_test)だけへ送られ、本番の宛先には届きません。\n"
          + "ただし承認済み(送信済み)になるため、このドラフトは本番へ再送できません(本番送信は、ドラフトを作り直して行います)。\nよろしいですか？"
        : "本番の宛先へ、実際にメールが送信されます。取り消せません。よろしいですか？";
      if (!window.confirm(ask)) return;
    }
    review.mutate(
      { id: report.id, status: action, freeText, draftContent: edited, testMode },
      {
        onSuccess: () => setMessage({ kind: "ok", text: "保存しました" }),
        onError: (e) => setMessage({ kind: "error", text: `処理に失敗しました: ${(e as Error).message}` }),
      },
    );
  };

  return (
    <div className="space-y-5 min-w-0">
      <div>
        <h2 className="text-lg font-semibold">{report.subject}</h2>
        <p className="text-xs text-muted-foreground mt-1">
          report_id: {report.id}　記事数: {report.articleCount}件
          {report.reviewerId ? `　reviewer_id: ${report.reviewerId}` : ""}
          {report.sendStatus ? `　send_status: ${report.sendStatus}` : ""}
        </p>
        {report.status === "error" && (
          <p className="text-sm text-destructive mt-2">ドラフト生成中にエラーが発生しています: {report.errorMessage}</p>
        )}
      </div>

      {report.htmlBody ? (
        // メール本文は外部由来の文字列を含むため、スクリプト等を実行できないsandboxで表示する
        <iframe
          title="メール本文プレビュー"
          sandbox=""
          srcDoc={report.htmlBody}
          className="w-full h-[700px] rounded-lg border border-border bg-white"
        />
      ) : (
        <p className="text-sm text-muted-foreground">メール本文がまだ生成されていません。</p>
      )}

      <section className="space-y-3 border-t border-border pt-5">
        <h3 className="font-semibold">レビュー</h3>
        <div>
          <label className="text-sm text-muted-foreground">自由記述フィードバック</label>
          <Textarea value={freeText} onChange={(e) => setFreeText(e.target.value)} rows={2} />
        </div>
        <div>
          <label className="text-sm text-muted-foreground">週次ドラフト本体（修正する場合はJSONを直接編集）</label>
          <Textarea
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            rows={10}
            className="font-mono text-xs"
          />
        </div>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={testMode}
            onChange={(e) => setTestMode(e.target.checked)}
          />
          <span>
            テスト送信（テスト受信者だけへ送る）
            <span className="block text-xs text-muted-foreground">
              外すと本番の宛先へ送信されます。テスト送信でも承認済みになるため、同じドラフトは本番へ再送できません。
            </span>
          </span>
        </label>
        <div className="flex gap-3 flex-wrap">
          <Button disabled={disabled} onClick={() => submit("approved")}>
            {testMode ? "承認してテスト送信" : "承認して本番送信"}
          </Button>
          <Button variant="outline" disabled={disabled} onClick={() => submit("rejected")}>却下</Button>
          <Button variant="outline" disabled={disabled} onClick={() => submit("review_required")}>
            修正のみ保存（レビュー待ちのまま）
          </Button>
        </div>
        {message && (
          <p className={`text-sm ${message.kind === "ok" ? "text-green-600" : "text-destructive"}`}>{message.text}</p>
        )}
      </section>
    </div>
  );
}
