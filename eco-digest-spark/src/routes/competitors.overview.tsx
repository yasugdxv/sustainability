import { createFileRoute, Link } from "@tanstack/react-router";
import { TopBar } from "@/components/top-bar";
import { useCompetitorOverview } from "@/lib/api";
import { companyDisplayName, useLanguage } from "@/lib/i18n";
import { TrendingUp, Target, Sparkles, ArrowRight } from "lucide-react";

export const Route = createFileRoute("/competitors/overview")({
  component: CompetitorOverviewPage,
});

const STATS = [
  { key: "monitoredCompanies", labelKey: "competitor.overview.monitoredCompanies" },
  { key: "updatedCompanies", labelKey: "competitor.overview.updatedCompanies" },
  { key: "targetChangeCount", labelKey: "competitor.overview.targetChanges" },
  { key: "actualUpdateCount", labelKey: "competitor.overview.actualUpdates" },
  { key: "initiativeCount", labelKey: "competitor.overview.initiatives" },
  { key: "reviewPendingCount", labelKey: "competitor.overview.reviewPending" },
] as const;

function CompetitorOverviewPage() {
  const { lang, t } = useLanguage();
  const { data, isLoading } = useCompetitorOverview();

  return (
    <>
      <TopBar breadcrumb={[{ label: t("nav.competitorDashboard") }, { label: t("nav.competitorOverview") }]} />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-10">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{t("competitor.overview.title")}</h1>
          <p className="text-sm text-muted-foreground mt-2 max-w-2xl">
            {t("competitor.overview.subtitle", { count: data?.summary.monitoredCompanies ?? 20 })}
          </p>
        </section>

        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : !data?.reportMonth ? (
          <p className="text-muted-foreground">{t("competitor.overview.noReport")}</p>
        ) : (
          <>
            <section className="grid grid-cols-3 md:grid-cols-6 gap-3">
              {STATS.map((s) => (
                <div key={s.key} className="card-paper rounded-lg p-4">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{t(s.labelKey)}</div>
                  <div className="text-editorial text-3xl mt-1">{data.summary[s.key]}</div>
                </div>
              ))}
            </section>

            <section>
              <div className="flex items-center gap-2 mb-4">
                <Sparkles className="h-4 w-4" />
                <h2 className="text-editorial text-2xl">{t("competitor.overview.trendsTitle")}</h2>
              </div>
              {data.crossCompanyTrends.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("competitor.overview.trendsEmpty")}</p>
              ) : (
                <div className="grid grid-cols-3 gap-4">
                  {data.crossCompanyTrends.map((trend, i) => (
                    <div key={i} className="card-paper rounded-lg p-4">
                      <div className="text-editorial text-lg">{trend.title}</div>
                      <p className="text-sm text-muted-foreground mt-2 leading-relaxed">{trend.summary}</p>
                      <div className="flex flex-wrap gap-1 mt-3">
                        {trend.relatedCompanyNames.map((name) => (
                          <span
                            key={name}
                            className="text-[10px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground"
                          >
                            {name}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <div className="grid grid-cols-2 gap-8">
              <section>
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-2">
                    <Target className="h-4 w-4" />
                    <h2 className="text-editorial text-2xl">{t("competitor.overview.recentChangesTitle")}</h2>
                  </div>
                  <Link
                    to="/competitors/changes"
                    className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
                  >
                    <ArrowRight className="h-3 w-3" />
                  </Link>
                </div>
                <div className="space-y-3">
                  {data.recentChanges.map((c) => (
                    <div key={c.id} className="card-paper rounded-lg p-4">
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {companyDisplayName(c.companyName, c.companyNameEn, lang)}
                      </div>
                      <p className="text-sm mt-1 leading-relaxed">{c.summary}</p>
                    </div>
                  ))}
                </div>
              </section>

              <section>
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-2">
                    <TrendingUp className="h-4 w-4" />
                    <h2 className="text-editorial text-2xl">{t("competitor.overview.topInitiativesTitle")}</h2>
                  </div>
                  <Link
                    to="/competitors/initiatives"
                    className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
                  >
                    <ArrowRight className="h-3 w-3" />
                  </Link>
                </div>
                <div className="space-y-3">
                  {data.topInitiatives.map((ini) => (
                    <div key={ini.id} className="card-paper rounded-lg p-4">
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {companyDisplayName(ini.companyName, ini.companyNameEn, lang)}
                      </div>
                      <p className="text-sm font-medium mt-1">{ini.title}</p>
                      <p className="text-sm text-muted-foreground mt-1 leading-relaxed">{ini.summary}</p>
                    </div>
                  ))}
                </div>
              </section>
            </div>
          </>
        )}
      </main>
    </>
  );
}
