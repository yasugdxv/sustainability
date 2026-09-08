import { useImportanceRubric, type ImportanceScoreEntry } from "@/lib/api";
import { useLanguage } from "@/lib/i18n";

// 重要度スコアの内訳表示（7項目・各0-5点）。評価基準（ラベル・スコア別の判定文）は
// /api/importance-rubric から取得し、記事側は点数の実データ（0-5）のみ持つ。
export function ImportanceBreakdown({ breakdown }: { breakdown: ImportanceScoreEntry[] }) {
  const { t } = useLanguage();
  const { data: rubric = [] } = useImportanceRubric();
  const rubricById = new Map(rubric.map((r) => [r.id, r]));

  if (breakdown.length === 0 || rubric.length === 0) return null;

  return (
    <div className="space-y-2.5">
      {breakdown
        .filter((b) => rubricById.has(b.id))
        .sort((a, b) => rubric.findIndex((r) => r.id === a.id) - rubric.findIndex((r) => r.id === b.id))
        .map((b) => {
          const c = rubricById.get(b.id)!;
          const bandText = c.bands[String(b.score)];
          return (
            <div key={b.id} title={c.description}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs text-foreground">{c.label}</span>
                <span className="text-xs font-medium tabular-nums text-primary shrink-0">
                  {b.score} / {c.maxScore}
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-muted mt-1 overflow-hidden">
                <div
                  className="h-full rounded-full bg-primary/70"
                  style={{ width: `${(b.score / c.maxScore) * 100}%` }}
                />
              </div>
              {bandText && (
                <p className="text-[11px] text-muted-foreground mt-1 leading-snug">{bandText}</p>
              )}
            </div>
          );
        })}
      <p className="text-[11px] text-muted-foreground/80 pt-1">{t("article.importanceBreakdownHint")}</p>
    </div>
  );
}
