import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import { Sparkles, ArrowLeft, ExternalLink } from "lucide-react";
import { TopBar } from "@/components/top-bar";
import {
  competitorInitiativeQueryOptions,
  useCompetitorInitiative,
} from "@/lib/api";
import { companyDisplayName, themeLabel, useLanguage } from "@/lib/i18n";

export const Route = createFileRoute("/competitors/initiatives/$id")({
  loader: async ({ params, context }) => {
    const initiative = await context.queryClient
      .ensureQueryData(competitorInitiativeQueryOptions(params.id))
      .catch(() => null);
    if (!initiative) throw notFound();
    return { initiative };
  },
  component: CompetitorInitiativeDetail,
  notFoundComponent: () => (
    <div className="p-16 text-center text-muted-foreground">Initiative not found</div>
  ),
});

function CompetitorInitiativeDetail() {
  const { initiative: loaderInitiative } = Route.useLoaderData();
  const { id } = Route.useParams();
  const { lang, t } = useLanguage();
  const { data: initiative = loaderInitiative } = useCompetitorInitiative(id, lang);

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.competitorDashboard"), href: "/competitors/overview" },
          { label: t("nav.competitorInitiatives"), href: "/competitors/initiatives" },
        ]}
      />
      <main className="flex-1 max-w-[900px] w-full mx-auto px-8 py-8">
        <Link
          to="/competitors/initiatives"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground mb-6"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> {t("article.backToDashboard")}
        </Link>

        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {companyDisplayName(initiative.companyName, initiative.companyNameEn, lang)}
        </div>
        <h1 className="text-editorial text-4xl mt-2 leading-[1.1]">{initiative.title}</h1>

        <div className="flex flex-wrap gap-1.5 mt-4">
          {initiative.themes.map((th: string) => (
            <span key={th} className="text-[11px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
              {themeLabel(th, lang)}
            </span>
          ))}
          {initiative.goalCategoryName && (
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-primary/10 text-primary">
              {initiative.goalCategoryName}
            </span>
          )}
        </div>

        <section className="mt-6 card-paper rounded-lg p-5 border-l-4 border-l-primary">
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] text-primary">
            <Sparkles className="h-3 w-3" /> {t("article.aiSummary")}
          </div>
          <p className="text-sm mt-2 leading-relaxed text-foreground">{initiative.summary}</p>
        </section>

        <div className="prose prose-sm max-w-none mt-8 space-y-5 text-[15px] leading-[1.8] text-foreground/90">
          {(initiative.body ?? []).map((p: string, i: number) => (
            <p key={i}>{p}</p>
          ))}
          {(!initiative.body || initiative.body.length === 0) && (
            <p className="text-muted-foreground">{t("article.noBody")}</p>
          )}
        </div>

        {initiative.sourceUrl && (
          <a
            href={initiative.sourceUrl}
            target="_blank"
            rel="noreferrer"
            className="mt-8 inline-flex items-center gap-1 text-xs text-primary hover:underline"
          >
            <ExternalLink className="h-3.5 w-3.5" /> {t("competitor.readOriginal")}
          </a>
        )}
      </main>
    </>
  );
}
