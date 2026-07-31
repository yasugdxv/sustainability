import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { TopBar } from "@/components/top-bar";
import { relativeTime, useCompetitorChanges, useCompetitorCompanies } from "@/lib/api";
import {
  changeTypeLabel,
  companyDisplayName,
  COMPETITOR_THEMES,
  directionLabel,
  recordTypeLabel,
  themeLabel,
  useLanguage,
} from "@/lib/i18n";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";

type SortOption = "createdAtDesc" | "createdAtAsc" | "sourceUpdatedAtDesc" | "sourceUpdatedAtAsc";

const SORT_OPTIONS: { value: SortOption; dateField: "createdAt" | "sourceUpdatedAt"; sortDir: "asc" | "desc" }[] = [
  { value: "createdAtDesc", dateField: "createdAt", sortDir: "desc" },
  { value: "createdAtAsc", dateField: "createdAt", sortDir: "asc" },
  { value: "sourceUpdatedAtDesc", dateField: "sourceUpdatedAt", sortDir: "desc" },
  { value: "sourceUpdatedAtAsc", dateField: "sourceUpdatedAt", sortDir: "asc" },
];

export const Route = createFileRoute("/competitors/changes")({
  component: CompetitorChangesPage,
});

function CompetitorChangesPage() {
  const { lang, t } = useLanguage();
  const [companyId, setCompanyId] = useState("");
  const [theme, setTheme] = useState("");
  const [sort, setSort] = useState<SortOption>("createdAtDesc");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const sortOption = SORT_OPTIONS.find((s) => s.value === sort) ?? SORT_OPTIONS[0];
  const { data: companiesData } = useCompetitorCompanies();
  const { data, isLoading } = useCompetitorChanges({
    companyId: companyId || undefined,
    theme: theme || undefined,
    dateField: sortOption.dateField,
    sortDir: sortOption.sortDir,
    dateFrom: dateFrom || undefined,
    dateTo: dateTo || undefined,
  });
  const companies = companiesData?.companies ?? [];
  const changes = data?.changes ?? [];

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.competitorDashboard"), href: "/competitors/overview" },
          { label: t("nav.competitorChanges") },
        ]}
      />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{t("competitor.changes.title")}</h1>
          <p className="text-sm text-muted-foreground mt-2">{t("competitor.changes.subtitle")}</p>
        </section>

        <div className="flex items-center gap-3 flex-wrap">
          <Select value={companyId || "all"} onValueChange={(v) => setCompanyId(v === "all" ? "" : v)}>
            <SelectTrigger className="w-56">
              <SelectValue placeholder={t("competitor.filter.allCompanies")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("competitor.filter.allCompanies")}</SelectItem>
              {companies.map((c) => (
                <SelectItem key={c.id} value={c.id}>{companyDisplayName(c.name, c.nameEn, lang)}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={theme || "all"} onValueChange={(v) => setTheme(v === "all" ? "" : v)}>
            <SelectTrigger className="w-48">
              <SelectValue placeholder={t("competitor.filter.allThemes")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("competitor.filter.allThemes")}</SelectItem>
              {COMPETITOR_THEMES.map((th) => (
                <SelectItem key={th} value={th}>{themeLabel(th, lang)}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(v) => setSort(v as SortOption)}>
            <SelectTrigger className="w-56">
              <SelectValue placeholder={t("competitor.changes.sortLabel")} />
            </SelectTrigger>
            <SelectContent>
              {SORT_OPTIONS.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {t(`competitor.changes.sort.${s.value}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span>{t("competitor.changes.dateFrom")}</span>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2 text-xs"
            />
            <span>{t("competitor.changes.dateTo")}</span>
            <input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2 text-xs"
            />
          </div>
        </div>

        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : changes.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("competitor.changes.empty")}</p>
        ) : (
          <div className="space-y-4">
            {changes.map((c) => (
              <div key={c.id} className="card-paper rounded-lg p-5">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium">{companyDisplayName(c.companyName, c.companyNameEn, lang)}</span>
                    <Badge variant="outline">{recordTypeLabel(c.recordType, lang)}</Badge>
                    {c.changeType && <Badge variant="secondary">{changeTypeLabel(c.changeType, lang)}</Badge>}
                    {c.direction && <Badge variant="outline">{directionLabel(c.direction, lang)}</Badge>}
                    {c.reviewRequired && (
                      <Badge variant="destructive">{t("competitor.changes.reviewBadge")}</Badge>
                    )}
                  </div>
                  {c.sourceUrl && (
                    <a
                      href={c.sourceUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs text-muted-foreground hover:text-foreground"
                    >
                      {t("competitor.readOriginal")}
                    </a>
                  )}
                </div>
                <div className="flex items-center gap-3 text-xs text-muted-foreground mt-2">
                  <span>
                    {t("competitor.changes.createdAt")}: {relativeTime(c.createdAt, lang)}
                  </span>
                  <span>
                    {t("competitor.changes.sourceUpdatedAt")}:{" "}
                    {c.sourceUpdatedAt
                      ? relativeTime(c.sourceUpdatedAt, lang)
                      : t("competitor.changes.sourceUpdatedAtUnknown")}
                  </span>
                </div>
                <p className="text-sm mt-3 leading-relaxed">{c.summary}</p>
                {c.changedFields.length > 0 && (
                  <div className="grid grid-cols-2 gap-4 mt-3 text-xs text-muted-foreground border-t border-border pt-3">
                    <div>
                      <div className="font-medium text-foreground mb-1">{t("competitor.changes.before")}</div>
                      {c.changedFields.map((f, i) => (
                        <div key={i}>{f.field}: {String(f.before ?? "—")}</div>
                      ))}
                    </div>
                    <div>
                      <div className="font-medium text-foreground mb-1">{t("competitor.changes.after")}</div>
                      {c.changedFields.map((f, i) => (
                        <div key={i}>{f.field}: {String(f.after ?? "—")}</div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </main>
    </>
  );
}
