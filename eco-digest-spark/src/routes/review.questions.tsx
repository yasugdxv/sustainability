import { createFileRoute } from "@tanstack/react-router";
import { TopBar } from "@/components/top-bar";
import { Badge } from "@/components/ui/badge";
import { useDecisionInsights, useStrategicQuestions } from "@/lib/admin";

export const Route = createFileRoute("/review/questions")({
  component: QuestionsPage,
});

const LIFECYCLE: Record<string, string> = {
  draft: "下書き（未embedded）",
  embedded: "週次メールに埋め込み済み（承認待ち）",
  open: "回答受付中",
  closed: "クローズ済み",
};

function Json({ value }: { value: unknown }) {
  return <pre className="max-h-72 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap">{JSON.stringify(value, null, 2)}</pre>;
}

function QuestionsPage() {
  const q = useStrategicQuestions();
  const ins = useDecisionInsights();
  const questions = q.data?.questions ?? [];
  const errored = questions.filter((x) => x.insightStatus === "error");

  return (
    <>
      <TopBar breadcrumb={[{ label: "メールレビュー" }, { label: "戦略質問・組織判断ナレッジ" }]} />
      <main className="flex-1 max-w-[1100px] w-full mx-auto px-8 py-8 space-y-8">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">戦略質問・組織判断ナレッジ</h1>
          <p className="text-sm text-muted-foreground mt-2">
            閲覧専用です。戦略質問の承認は、週次メールレビュー画面のJSON編集にバンドルされています。
          </p>
        </section>

        <section className="space-y-3">
          <h2 className="text-xl font-semibold">戦略質問の履歴</h2>
          {q.isError && <p className="text-sm text-destructive">取得に失敗しました: {(q.error as Error).message}（sql/2026-09-01_strategic_question_schema.sql の適用が必要な可能性があります）</p>}
          {errored.length > 0 && (
            <div className="rounded-lg border border-destructive/50 p-3 text-sm space-y-1">
              <p>Decision Insight生成が失敗したまま残っている質問が{errored.length}件あります。python generate_strategic_question.py insights を再実行してください。</p>
              {errored.map((x) => <p key={x.id} className="text-xs text-muted-foreground">{x.periodStart} {x.title}: {x.insightError}</p>)}
            </div>
          )}
          {!q.isLoading && !q.isError && questions.length === 0 && (
            <p className="text-sm text-muted-foreground">まだ戦略質問がありません。python generate_strategic_question.py build を実行してください。</p>
          )}
          {questions.map((x) => (
            <details key={x.id} className="rounded-lg border border-border p-4">
              <summary className="cursor-pointer text-sm font-medium">
                <Badge variant="secondary" className="mr-2">{LIFECYCLE[x.status] ?? x.status}</Badge>
                {x.periodStart}　[{x.theme}] {x.title}
              </summary>
              <div className="mt-3 space-y-3">
                <p className="text-xs text-muted-foreground">question_id: {x.id}　対立軸: {x.dimensionLabel}（{x.dimensionKey}）</p>
                <p className="font-medium">{x.questionText}</p>
                <ul className="text-sm space-y-1">
                  {x.options.map((o) => (
                    <li key={o.code}>{o.code}. {o.label}{o.isStatusQuo ? "（現状維持寄り）" : ""}: {o.description}</li>
                  ))}
                </ul>
                {x.aggregate && (
                  <p className="text-xs text-muted-foreground">
                    回答数: {x.aggregate.totalResponses} / 配信数: {x.aggregate.deliveryCount} / 回答率: {(x.aggregate.responseRate * 100).toFixed(1)}%
                  </p>
                )}
                <details><summary className="cursor-pointer text-sm">分析の根拠（Signal→示唆→当社戦略関連→競合比較→対立軸）</summary><Json value={x.analysis} /></details>
                <details><summary className="cursor-pointer text-sm">他の候補・ランキング根拠</summary><Json value={x.candidates} /></details>
              </div>
            </details>
          ))}
        </section>

        <section className="space-y-3">
          <h2 className="text-xl font-semibold">組織判断ナレッジ一覧（Decision Insight）</h2>
          <p className="text-xs text-muted-foreground">いずれも「観測された傾向」であり、会社の正式方針ではありません。</p>
          {ins.isError && <p className="text-sm text-destructive">取得に失敗しました: {(ins.error as Error).message}</p>}
          {!ins.isLoading && !ins.isError && (ins.data?.insights.length ?? 0) === 0 && (
            <p className="text-sm text-muted-foreground">蓄積された組織判断ナレッジがまだありません。</p>
          )}
          {(ins.data?.insights ?? []).map((i, idx) => (
            <details key={i.id ?? idx} className="rounded-lg border border-border p-4">
              <summary className="cursor-pointer text-sm font-medium">
                <Badge variant={i.publishable ? "default" : "secondary"} className="mr-2">
                  {i.publishable ? "掲載条件を満たす" : "回答数/回答率が閾値未満（非掲載）"}
                </Badge>
                [{i.theme}] {i.dimensionLabel}
              </summary>
              <div className="mt-3 space-y-3 text-sm">
                <p className="text-xs text-muted-foreground">
                  observed_at: {i.observedAt}　回答数: {i.responseCount} / 配信数: {i.deliveryCount}　confidence: {i.confidence}
                </p>
                <p className="rounded border border-border bg-muted/40 p-2 text-xs">{i.guardrailLabel}</p>
                <p>{i.observedTendency}</p>
                <Json value={i.distribution} />
                <p><b>理由の要約</b>: {i.reasoningSummary}</p>
                {i.representativeReasoning.length > 0 && (
                  <div><b>代表的な理由づけパターン</b><ul className="list-disc pl-5">{i.representativeReasoning.map((r, k) => <li key={k}>{r}</li>)}</ul></div>
                )}
              </div>
            </details>
          ))}
        </section>
      </main>
    </>
  );
}
