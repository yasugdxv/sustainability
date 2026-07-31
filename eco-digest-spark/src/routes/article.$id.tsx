import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import { useState } from "react";
import { Bookmark, Share2, Sparkles, ArrowLeft, ExternalLink, TrendingUp, Heart, BookOpen } from "lucide-react";
import { TopBar } from "@/components/top-bar";
import {
  articleQueryOptions,
  categoryLabel,
  categoryMeta,
  relativeTime,
  useArticle,
  useArticleLikeRead,
  useArticles,
  useCategories,
} from "@/lib/api";
import { useLanguage } from "@/lib/i18n";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/article/$id")({
  loader: async ({ params, context }) => {
    const article = await context.queryClient
      .ensureQueryData(articleQueryOptions(params.id))
      .catch(() => null);
    if (!article) throw notFound();
    return { article };
  },
  component: ArticleDetail,
  notFoundComponent: () => (
    <div className="p-16 text-center text-muted-foreground">Article not found</div>
  ),
});

function ArticleDetail() {
  const { article: loaderArticle } = Route.useLoaderData();
  const { id } = Route.useParams();
  const { lang, t } = useLanguage();
  // ローダーで取得済みのデータをreact-queryキャッシュ経由で再利用しつつ、以後の変化にも追随させる
  const { data: article = loaderArticle } = useArticle(id, lang);
  const { data: categories = [] } = useCategories();
  const cat = categoryMeta(categories, article.category);
  const [saved, setSaved] = useState(false);
  const { liked, read, toggleLike, toggleRead } = useArticleLikeRead(article.id);

  const { data: relatedResult } = useArticles({ themes: [article.category], lang });
  const related = (relatedResult?.articles ?? []).filter((a) => a.id !== article.id).slice(0, 3);

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: categoryLabel(cat, lang), href: `/category/${cat.id}` },
          { label: article.source },
        ]}
      />

      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8">
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground mb-6"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> {t("article.backToDashboard")}
        </Link>

        <div className="grid grid-cols-[1fr_320px] gap-12">
          {/* Article body */}
          <article>
            <div className="flex items-center gap-2 text-xs">
              <span
                className="text-[10px] uppercase tracking-[0.2em] text-white px-2 py-1 rounded"
                style={{ backgroundColor: cat.hue }}
              >
                {categoryLabel(cat, lang)}
              </span>
              {article.trending && (
                <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-destructive">
                  <TrendingUp className="h-3 w-3" /> {t("card.trending")}
                </span>
              )}
              <span className="text-muted-foreground">{relativeTime(article.publishedAt, lang)}</span>
            </div>

            <h1 className="text-editorial text-5xl mt-4 leading-[1.05]">
              {article.title}
            </h1>

            <div className="flex items-center gap-4 mt-5 pb-6 border-b border-border text-sm">
              <div className="flex items-center gap-2">
                <div className="h-9 w-9 rounded-full bg-gradient-to-br from-primary to-accent-leaf" />
                <div>
                  <div className="text-foreground font-medium">{article.source}</div>
                  {article.sector && (
                    <div className="text-xs text-muted-foreground">{article.sector}</div>
                  )}
                </div>
              </div>
              <div className="flex-1" />
              {article.url && (
                <a
                  href={article.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-primary hover:underline inline-flex items-center gap-1"
                >
                  <ExternalLink className="h-3.5 w-3.5" /> {t("article.readOriginal")}
                </a>
              )}
            </div>

            {/* AI summary */}
            <section className="mt-6 card-paper rounded-lg p-5 border-l-4 border-l-primary">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.18em] text-primary">
                <Sparkles className="h-3 w-3" /> {t("article.aiSummary")}
              </div>
              <p className="text-sm mt-2 leading-relaxed text-foreground">
                {article.summary}
              </p>
              <div className="mt-4 grid grid-cols-2 gap-3 text-xs">
                {article.sector && (
                  <div>
                    <div className="text-muted-foreground uppercase tracking-wider text-[10px]">{t("article.sourceCategory")}</div>
                    <div className="mt-0.5 font-medium">{article.sector}</div>
                  </div>
                )}
                <div>
                  <div className="text-muted-foreground uppercase tracking-wider text-[10px]">{t("article.importanceScore")}</div>
                  <div className="mt-0.5 font-medium text-primary">
                    {t("article.importanceScoreValue", { score: article.importance, level: article.importanceLevel })}
                  </div>
                </div>
              </div>
            </section>

            <div className="prose prose-sm max-w-none mt-8 space-y-5 text-[15px] leading-[1.8] text-foreground/90">
              {(article.body ?? []).map((p: string, i: number) => (
                <p
                  key={i}
                  className="first:first-letter:text-editorial first:first-letter:text-5xl first:first-letter:float-left first:first-letter:mr-2 first:first-letter:leading-none first:first-letter:mt-1"
                >
                  {p}
                </p>
              ))}
              {(!article.body || article.body.length === 0) && (
                <p className="text-muted-foreground">{t("article.noBody")}</p>
              )}
            </div>

            {/* Actions bar */}
            <div className="mt-10 pt-6 border-t border-border flex items-center gap-2">
              <Button
                variant={liked ? "default" : "outline"}
                onClick={toggleLike}
                className="rounded-full"
              >
                <Heart className={`h-4 w-4 ${liked ? "fill-current" : ""}`} />
                {article.likesCount.toLocaleString()} {t("article.likes")}
              </Button>
              <Button
                variant={read ? "default" : "outline"}
                onClick={toggleRead}
                className="rounded-full"
              >
                <BookOpen className="h-4 w-4" />
                {read ? t("article.readDone") : t("article.read")} · {article.readsCount.toLocaleString()}
              </Button>
              <Button
                variant={saved ? "default" : "outline"}
                onClick={() => setSaved((v) => !v)}
                size="icon"
                className="rounded-full"
              >
                <Bookmark className={`h-4 w-4 ${saved ? "fill-current" : ""}`} />
              </Button>
              <Button variant="outline" size="icon" className="rounded-full">
                <Share2 className="h-4 w-4" />
              </Button>
            </div>
          </article>

          {/* Sidebar */}
          <aside className="space-y-6">
            <div className="card-paper rounded-lg p-5">
              <div className="text-[10px] uppercase tracking-[0.18em] text-primary flex items-center gap-1.5">
                <Sparkles className="h-3 w-3" /> {t("nav.sustainaAI")}
              </div>
              <p className="text-sm mt-2 text-muted-foreground leading-relaxed">
                {t("article.askAI")}
              </p>
              <Link
                to="/chat"
                className="mt-3 block w-full text-left text-xs px-3 py-2 rounded-md border border-border hover:bg-muted transition-colors"
              >
                {t("article.askAILink")}
              </Link>
            </div>

            {related.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground mb-3">
                  {t("article.related")}
                </div>
                <div className="space-y-3">
                  {related.map((r) => (
                    <Link
                      key={r.id}
                      to="/article/$id"
                      params={{ id: r.id }}
                      className="group block border-b border-border pb-3 last:border-0"
                    >
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {r.source} · {relativeTime(r.publishedAt, lang)}
                      </div>
                      <h4 className="text-editorial text-base leading-snug mt-1 group-hover:text-primary">
                        {r.title}
                      </h4>
                    </Link>
                  ))}
                </div>
              </div>
            )}
          </aside>
        </div>
      </main>
    </>
  );
}
