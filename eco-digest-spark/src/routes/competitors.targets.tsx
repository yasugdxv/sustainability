import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { Download } from "lucide-react";
import { TopBar } from "@/components/top-bar";
import { useCompetitorTargets } from "@/lib/api";
import { companyDisplayName, COMPETITOR_THEMES, themeLabel, themeListLabel, useLanguage } from "@/lib/i18n";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { downloadCsv } from "@/lib/csv";

export const Route = createFileRoute("/competitors/targets")({
  component: CompetitorTargetsPage,
});

function CompetitorTargetsPage() {
  const { lang, t } = useLanguage();
  const [theme, setTheme] = useState("");
  const { data, isLoading } = useCompetitorTargets({ theme: theme || undefined });
  const targets = data?.targets ?? [];

  const exportCsv = () => {
    downloadCsv(
      `competitor_targets_${new Date().toISOString().slice(0, 10)}.csv`,
      [
        t("csv.col.company"), t("csv.col.theme"), t("csv.col.target"), t("csv.col.baseYear"),
        t("csv.col.targetYear"), t("csv.col.scope"), t("csv.col.kpiDefinition"),
        t("csv.col.achievementStatus"), t("csv.col.url"),
      ],
      targets.map((r) => [
        companyDisplayName(r.companyName, r.companyNameEn, lang),
        themeListLabel(r.themes, lang),
        r.title ?? r.targetValue,
        r.baseYear,
        r.targetYear,
        r.scope,
        r.kpiDefinition,
        r.achievementStatus,
        r.sourceUrl,
      ]),
    );
  };

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.competitorDashboard"), href: "/competitors/overview" },
          { label: t("nav.competitorTargets") },
        ]}
      />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{t("competitor.targets.title")}</h1>
          <p className="text-sm text-muted-foreground mt-2">{t("competitor.targets.subtitle")}</p>
        </section>

        <div className="flex items-center justify-between gap-3">
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
          <Button
            variant="outline"
            size="sm"
            className="rounded-full gap-1.5"
            onClick={exportCsv}
            disabled={targets.length === 0}
          >
            <Download className="h-3.5 w-3.5" />
            {t("csv.download")}
          </Button>
        </div>

        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : targets.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("competitor.targets.empty")}</p>
        ) : (
          <div className="card-paper rounded-lg">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("competitor.targets.company")}</TableHead>
                  <TableHead>{t("competitor.targets.theme")}</TableHead>
                  <TableHead>{t("competitor.targets.target")}</TableHead>
                  <TableHead>{t("competitor.targets.baseYear")}</TableHead>
                  <TableHead>{t("competitor.targets.targetYear")}</TableHead>
                  <TableHead>{t("competitor.targets.scope")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {targets.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="font-medium whitespace-nowrap">
                      {companyDisplayName(r.companyName, r.companyNameEn, lang)}
                    </TableCell>
                    <TableCell>{themeListLabel(r.themes, lang) || "—"}</TableCell>
                    <TableCell className="max-w-sm truncate" title={r.title ?? ""}>
                      {r.title ?? String(r.targetValue ?? "—")}
                    </TableCell>
                    <TableCell>{r.baseYear ?? "—"}</TableCell>
                    <TableCell>{r.targetYear ?? "—"}</TableCell>
                    <TableCell className="max-w-xs truncate" title={String(r.scope ?? "")}>
                      {r.scope ?? "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </main>
    </>
  );
}
