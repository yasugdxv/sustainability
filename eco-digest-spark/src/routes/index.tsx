import { createFileRoute } from "@tanstack/react-router";
import { TopBar } from "@/components/top-bar";
import { ArticleCarousel } from "@/components/article-carousel";
import { categoryLabel, useArticles, useCategories } from "@/lib/api";
import { useLanguage } from "@/lib/i18n";
import { Globe2 } from "lucide-react";

export const Route = createFileRoute("/")({
  component: Dashboard,
});

function Dashboard() {
  const { lang, t } = useLanguage();
  const { data: result, isLoading } = useArticles({ lang });
  const { data: categories = [] } = useCategories();
  const articles = result?.articles ?? [];

  const top = [...articles].sort((a, b) => b.importance - a.importance);
  const carousel = top.slice(0, 20);

  const today = new Date().toLocaleDateString(lang === "en" ? "en-US" : "ja-JP", {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });

  if (isLoading) {
    return (
      <>
        <TopBar breadcrumb={[{ label: t("nav.dashboard") }]} />
        <main className="flex-1 px-8 py-8 max-w-[1440px] w-full mx-auto">
          <p className="text-muted-foreground">...</p>
        </main>
      </>
    );
  }

  return (
    <>
      <TopBar breadcrumb={[{ label: t("nav.dashboard") }]} />

      <main className="flex-1 px-8 py-8 max-w-[1440px] w-full mx-auto space-y-10">
        {/* Editorial masthead */}
        <section className="border-b border-border pb-5">
          <div className="text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
            {today}
          </div>
          <h1 className="text-editorial text-5xl mt-2">
            {t("home.title")}
          </h1>
          <p className="text-sm text-muted-foreground mt-2 max-w-2xl">
            {t("home.subtitle", { count: articles.length })}
          </p>
        </section>

        {articles.length === 0 && (
          <p className="text-muted-foreground">{t("home.empty")}</p>
        )}

        {/* Netflix carousel */}
        {carousel.length > 0 && (
          <ArticleCarousel
            title={t("home.carouselTitle")}
            subtitle={t("home.carouselSubtitle")}
            articles={carousel}
          />
        )}

        {/* Categories quick access */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <Globe2 className="h-4 w-4" />
            <h2 className="text-editorial text-3xl">{t("home.categoriesTitle")}</h2>
          </div>
          <div className="grid grid-cols-4 gap-3">
            {categories.map((c) => (
              <a
                key={c.id}
                href={`/category/${encodeURIComponent(c.id)}`}
                className="group card-paper rounded-lg p-4 hover:border-primary/40 transition-colors"
              >
                <div className="flex items-center justify-between">
                  <span
                    className="h-3 w-3 rounded-full"
                    style={{ backgroundColor: c.hue }}
                  />
                  <span className="text-xs text-muted-foreground tabular-nums">
                    {t("unit.articleCount", { count: c.count })}
                  </span>
                </div>
                <div className="text-editorial text-xl mt-3 group-hover:text-primary">
                  {categoryLabel(c, lang)}
                </div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground mt-0.5">
                  {lang === "en" ? c.labelJa : c.label}
                </div>
              </a>
            ))}
          </div>
        </section>
      </main>
    </>
  );
}
