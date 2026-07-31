import { createFileRoute, Link, Outlet, useRouterState } from "@tanstack/react-router";
import { TopBar } from "@/components/top-bar";
import { useCompetitorCompanies } from "@/lib/api";
import { companyCategoryLabel, companyDisplayName, useLanguage } from "@/lib/i18n";
import { Building2 } from "lucide-react";

export const Route = createFileRoute("/competitors/companies")({
  component: CompetitorCompaniesPage,
});

function CompetitorCompaniesPage() {
  const { lang, t } = useLanguage();
  const { data, isLoading } = useCompetitorCompanies();
  const companies = data?.companies ?? [];
  const pathname = useRouterState({ select: (r) => r.location.pathname });

  // /competitors/companies/$companyId(企業詳細)は本ファイルの子ルートとして登録されるため、
  // 詳細ページ表示中は一覧を出さずOutletのみ描画する(一覧と詳細が二重に表示されるのを防ぐ)。
  if (pathname !== "/competitors/companies") {
    return <Outlet />;
  }

  return (
    <>
      <TopBar breadcrumb={[{ label: t("nav.competitorDashboard"), href: "/competitors/overview" }, { label: t("nav.competitorCompanies") }]} />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{t("competitor.companies.title")}</h1>
          <p className="text-sm text-muted-foreground mt-2">{t("competitor.companies.subtitle")}</p>
        </section>

        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : companies.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("competitor.companies.empty")}</p>
        ) : (
          <div className="grid grid-cols-4 gap-4">
            {companies.map((c) => (
              <Link
                key={c.id}
                to="/competitors/companies/$companyId"
                params={{ companyId: c.id }}
                className="card-paper rounded-lg p-4 hover:bg-sidebar-accent/20 transition-colors"
              >
                <div className="flex items-center gap-2 text-muted-foreground">
                  <Building2 className="h-4 w-4" />
                  <span className="text-[10px] uppercase tracking-wider">{companyCategoryLabel(c.category, lang)}</span>
                </div>
                <div className="text-editorial text-lg mt-2">{companyDisplayName(c.name, c.nameEn, lang)}</div>
                <div className="flex items-center gap-4 mt-3 text-xs text-muted-foreground">
                  <span>{c.targetCount} {t("competitor.companies.targetCount")}</span>
                  <span>{c.initiativeCount} {t("competitor.companies.initiativeCount")}</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  );
}
