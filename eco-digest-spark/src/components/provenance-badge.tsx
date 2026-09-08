import type { Provenance } from "@/lib/api";
import { useLanguage } from "@/lib/i18n";

/**
 * Phase B: Department Intelligence AI — 出典(Provenance)バッジ。
 * Chat（クロスドメイン回答の出典）とSearch（Related Intelligenceの出典）で共通利用する。
 * 初期表示は1行の要約のみ、詳細（タイトル・問い合わせ内容・時点・参照情報）は
 * <details>展開時のみ表示する（新しいオーバーレイ/ドロワーは作らない）。
 * 値が判明していない項目（null）はそもそも表示しない（不明を不明のまま扱う）。
 */
export function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  const { t } = useLanguage();
  const departmentLabel =
    t(`provenance.department.${provenance.sourceDepartment}`) || provenance.sourceDepartment;
  const reuseLabel =
    provenance.reuseType !== "none" ? t(`provenance.reuseType.${provenance.reuseType}`) : null;

  const hasDetail =
    !!provenance.assessmentTitle ||
    !!provenance.requestQuestion ||
    !!provenance.asOf ||
    (provenance.references && provenance.references.length > 0);

  const summary = (
    <span className="text-[11px] text-muted-foreground">
      {departmentLabel}
      {provenance.confidence != null && ` · ${provenance.confidence}`}
      {reuseLabel && ` · ${reuseLabel}`}
    </span>
  );

  if (!hasDetail) {
    return (
      <div className="inline-flex rounded-md border border-border bg-muted/30 px-2 py-1">
        {summary}
      </div>
    );
  }

  return (
    <details className="rounded-md border border-border bg-muted/30 px-2 py-1">
      <summary className="cursor-pointer select-none list-none">{summary}</summary>
      <div className="mt-1.5 space-y-0.5 pl-0.5 text-[11px] text-muted-foreground">
        {provenance.assessmentTitle && (
          <div>
            {t("provenance.assessmentTitle")}: {provenance.assessmentTitle}
          </div>
        )}
        {provenance.requestQuestion && (
          <div>
            {t("provenance.requestQuestion")}: {provenance.requestQuestion}
          </div>
        )}
        {provenance.asOf && (
          <div>
            {t("provenance.asOf")}: {provenance.asOf}
          </div>
        )}
        {provenance.references && provenance.references.length > 0 && (
          <div>
            {t("provenance.references")}: {provenance.references.length}
          </div>
        )}
      </div>
    </details>
  );
}
