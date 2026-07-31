import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { Download } from "lucide-react";
import { TopBar } from "@/components/top-bar";
import { useCompetitorCompanies, useCompetitorInitiatives } from "@/lib/api";
import { companyDisplayName, COMPETITOR_THEMES, themeLabel, themeListLabel, useLanguage } from "@/lib/i18n";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { downloadCsv } from "@/lib/csv";

export const Route = createFileRoute("/competitors/initiatives")({
  component: CompetitorInitiativesPage,
});

function CompetitorInitiativesPage() {
  const { lang, t } = useLanguage();
  const [companyId, setCompanyId] = useState("");
  const [theme, setTheme] = useState("");
  const { data: companiesData } = useCompetitorCompanies();
  const { data, isLoading } = useCompetitorInitiatives({
    companyId: companyId || undefined,
    theme: theme || undefined,
  });
  const companies = companiesData?.companies ?? [];
  const initiatives = data?.initiatives ?? [];

  const exportCsv = () => {
    downloadCsv(
      `competitor_initiatives_${new Date().toISOString().slice(0, 10)}.csv`,
      [
        t("csv.col.company"), t("csv.col.theme"), t("csv.col.title"), t("csv.col.summary"),
        t("csv.col.status"), t("csv.col.detectedAt"), t("csv.col.url"),
      ],
      initiatives.map((ini) => [
        companyDisplayName(ini.companyName, ini.companyNameEn, lang),
        themeListLabel(ini.themes, lang),
        ini.title,
        ini.summary,
        ini.isNew ? t("competitor.initiatives.new") : t("competitor.initiatives.updated"),
        ini.detectedAt,
        ini.sourceUrl,
      ]),
    );
  };

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.competitorDashboard"), href: "/competitors/overview" },
          { label: t("nav.competitorInitiatives") },
        ]}
      />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{t("competitor.initiatives.title")}</h1>
          <p className="text-sm text-muted-foreground mt-2">{t("competitor.initiatives.subtitle")}</p>
        </section>

        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
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
          </div>
          <Button
            variant="outline"
            size="sm"
            className="rounded-full gap-1.5"
            onClick={exportCsv}
            disabled={initiatives.length === 0}
          >
            <Download className="h-3.5 w-3.5" />
            {t("csv.download")}
          </Button>
        </div>

        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : initiatives.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("competitor.initiatives.empty")}</p>
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {initiatives.map((ini) => (
              <div key={ini.id} className="card-paper rounded-lg p-4">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    {companyDisplayName(ini.companyName, ini.companyNameEn, lang)}
                  </span>
                  <Badge variant={ini.isNew ? "default" : "secondary"}>
                    {ini.isNew ? t("competitor.initiatives.new") : t("competitor.initiatives.updated")}
                  </Badge>
                </div>
                <div className="text-editorial text-lg mt-2">{ini.title}</div>
                <p className="text-sm text-muted-foreground mt-2 leading-relaxed">{ini.summary}</p>
                <div className="flex flex-wrap gap-1 mt-3">
                  {ini.themes.map((th) => (
                    <span key={th} className="text-[10px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
                      {themeLabel(th, lang)}
                    </span>
                  ))}
                </div>
                {ini.sourceUrl && (
                  <a
                    href={ini.sourceUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs text-muted-foreground hover:text-foreground mt-3 inline-block"
                  >
                    {t("competitor.readOriginal")}
                  </a>
                )}
              </div>
            ))}
          </div>
        )}
      </main>
    </>
  );
}
