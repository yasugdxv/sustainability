import { createFileRoute, notFound } from "@tanstack/react-router";
import { TopBar } from "@/components/top-bar";
import { ArticleCard } from "@/components/article-card";
import { categoriesQueryOptions, categoryLabel, useArticles } from "@/lib/api";
import { categoryDescription, useLanguage } from "@/lib/i18n";
import { useState } from "react";

export const Route = createFileRoute("/category/$slug")({
  loader: async ({ params, context }) => {
    const categories = await context.queryClient.ensureQueryData(categoriesQueryOptions);
    const cat = categories.find((c) => c.id === params.slug);
    if (!cat) throw notFound();
    return { cat };
  },
  component: CategoryPage,
  notFoundComponent: () => (
    <div className="p-16 text-center text-muted-foreground">Category not found</div>
  ),
});

function CategoryPage() {
  const { cat } = Route.useLoaderData();
  const { lang, t } = useLanguage();
  const [sort, setSort] = useState<"importance" | "latest" | "likes" | "reads">("importance");
  const { data: result } = useArticles({ themes: [cat.id], lang });
  const articles = [...(result?.articles ?? [])].sort((a, b) => {
    if (sort === "importance") return b.importance - a.importance;
    if (sort === "likes") return b.likesCount - a.likesCount;
    if (sort === "reads") return b.readsCount - a.readsCount;
    return +new Date(b.publishedAt) - +new Date(a.publishedAt);
  });
  const featured = articles[0];
  const rest = articles.slice(1);
  const trendingCount = articles.filter((a) => a.trending).length;
  const avgImportance = Math.round(
    articles.reduce((s, a) => s + a.importance, 0) / (articles.length || 1),
  );
  const label = categoryLabel(cat, lang);

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.categories") },
          { label },
        ]}
      />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8">
        {/* Category header */}
        <section
          className="rounded-2xl p-8 text-white relative overflow-hidden"
          style={{
            background: `linear-gradient(135deg, ${cat.hue} 0%, oklch(0.28 0.05 155) 100%)`,
          }}
        >
          <div className="max-w-3xl">
            <div className="text-[10px] uppercase tracking-[0.25em] text-white/70">
              {t("nav.categories")} · {cat.label}
            </div>
            <h1 className="text-editorial text-5xl mt-2">{label}</h1>
            <p className="mt-3 text-white/85 leading-relaxed">
              {categoryDescription(cat.id, lang)}
            </p>
            <div className="flex items-center gap-6 mt-6 text-sm">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-white/60">{t("category.articleCount")}</div>
                <div className="text-editorial text-2xl">{articles.length}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-white/60">{t("category.trendingCount")}</div>
                <div className="text-editorial text-2xl">{trendingCount}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-white/60">{t("category.avgImportance")}</div>
                <div className="text-editorial text-2xl">{avgImportance}</div>
              </div>
            </div>
          </div>
        </section>

        {/* Sort */}
        <div className="flex items-center justify-between mt-8 mb-5">
          <h2 className="text-editorial text-2xl">{t("category.listTitle", { name: label })}</h2>
          <div className="flex items-center gap-1 border border-border rounded-full p-1 bg-muted/30">
            {(["importance", "latest", "likes", "reads"] as const).map((id) => (
              <button
                key={id}
                onClick={() => setSort(id)}
                className={`text-xs px-3 py-1.5 rounded-full transition-colors ${
                  sort === id
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {t(`sort.${id}`)}
              </button>
            ))}
          </div>
        </div>

        {articles.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("category.empty")}</p>
        )}

        {featured && (
          <div className="mb-8">
            <ArticleCard article={featured} variant="hero" />
          </div>
        )}

        <div className="grid grid-cols-3 gap-5">
          {rest.map((a) => (
            <ArticleCard key={a.id} article={a} />
          ))}
        </div>
      </main>
    </>
  );
}
