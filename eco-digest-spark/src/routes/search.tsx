import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { Download, Search as SearchIcon, SlidersHorizontal } from "lucide-react";
import { TopBar } from "@/components/top-bar";
import { ArticleCard } from "@/components/article-card";
import { categoryLabel, categoryMeta, useArticles, useCategories } from "@/lib/api";
import { searchSuggestions, useLanguage } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { downloadCsv } from "@/lib/csv";

export const Route = createFileRoute("/search")({
  component: SearchPage,
});

const SORT_IDS = ["importance", "latest", "trending", "likes", "reads"] as const;

function SearchPage() {
  const { lang, t } = useLanguage();
  const [inputQ, setInputQ] = useState("");
  const [submittedQ, setSubmittedQ] = useState("");
  const [sort, setSort] = useState<(typeof SORT_IDS)[number]>("importance");
  const [selectedCats, setSelectedCats] = useState<string[]>([]);
  const [minImportance, setMinImportance] = useState(0);

  const { data: categories = [] } = useCategories();
  const { data: result, isFetching } = useArticles({ themes: selectedCats, q: submittedQ, lang });
  const articles = result?.articles ?? [];

  const filtered = articles
    .filter((a) => a.importance >= minImportance)
    .sort((a, b) => {
      if (sort === "importance") return b.importance - a.importance;
      if (sort === "trending") return Number(b.trending) - Number(a.trending);
      if (sort === "likes") return b.likesCount - a.likesCount;
      if (sort === "reads") return b.readsCount - a.readsCount;
      return +new Date(b.publishedAt) - +new Date(a.publishedAt);
    });

  const toggleCat = (id: string) =>
    setSelectedCats((prev) =>
      prev.includes(id) ? prev.filter((c) => c !== id) : [...prev, id],
    );

  const submit = (q: string) => {
    setInputQ(q);
    setSubmittedQ(q);
  };

  const exportCsv = () => {
    downloadCsv(
      `articles_${new Date().toISOString().slice(0, 10)}.csv`,
      [
        t("csv.col.title"), t("csv.col.category"), t("csv.col.source"), t("csv.col.sector"),
        t("csv.col.publishedAt"), t("csv.col.importance"), t("csv.col.tags"), t("csv.col.url"),
      ],
      filtered.map((a) => [
        a.title,
        categoryLabel(categoryMeta(categories, a.category), lang),
        a.source,
        a.sector,
        a.publishedAt,
        a.importanceLevel,
        a.tags.join(lang === "en" ? ", " : "・"),
        a.url,
      ]),
    );
  };

  return (
    <>
      <TopBar breadcrumb={[{ label: t("nav.search"), href: "/search" }]} />

      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8">
        {/* Natural language search */}
        <div className="card-paper rounded-2xl p-2 pl-5 flex items-center gap-3">
          <SearchIcon className="h-5 w-5 text-muted-foreground shrink-0" />
          <input
            value={inputQ}
            onChange={(e) => setInputQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit(inputQ);
            }}
            placeholder={t("search.placeholder")}
            className="flex-1 bg-transparent outline-none text-lg py-3 placeholder:text-muted-foreground/60"
          />
          <Button className="rounded-xl h-11 px-6" onClick={() => submit(inputQ)}>
            {t("search.submit")}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-2 mt-3 text-xs text-muted-foreground pl-2">
          <span>{t("search.suggestionsLabel")}</span>
          {searchSuggestions(lang).map((s) => (
            <button
              key={s}
              onClick={() => submit(s)}
              className="px-2.5 py-1 rounded-full border border-border hover:bg-muted transition-colors"
            >
              {s}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-[240px_1fr] gap-8 mt-8">
          {/* Filters sidebar */}
          <aside className="space-y-6">
            <div>
              <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground flex items-center gap-1.5 mb-3">
                <SlidersHorizontal className="h-3 w-3" /> {t("search.filters")}
              </div>
              <div className="text-xs font-medium text-foreground mb-2">{t("sidebar.categories")}</div>
              <div className="space-y-1.5">
                {categories.map((c) => {
                  const active = selectedCats.includes(c.id);
                  return (
                    <button
                      key={c.id}
                      onClick={() => toggleCat(c.id)}
                      className={`w-full flex items-center gap-2 text-left text-sm px-2.5 py-1.5 rounded-md transition-colors ${
                        active ? "bg-primary/10 text-primary" : "hover:bg-muted"
                      }`}
                    >
                      <span
                        className="h-2 w-2 rounded-full"
                        style={{ backgroundColor: c.hue }}
                      />
                      <span className="flex-1">{categoryLabel(c, lang)}</span>
                      <span className="text-[10px] text-muted-foreground tabular-nums">
                        {c.count}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <div className="text-xs font-medium text-foreground mb-2">{t("search.importance")}</div>
              <div className="px-1">
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={minImportance}
                  onChange={(e) => setMinImportance(Number(e.target.value))}
                  className="w-full accent-primary"
                />
                <div className="flex justify-between text-[10px] text-muted-foreground mt-1">
                  <span>0</span>
                  <span>{t("search.importanceMin", { value: minImportance })}</span>
                  <span>100</span>
                </div>
              </div>
            </div>
          </aside>

          {/* Results */}
          <div>
            <div className="flex items-baseline justify-between mb-4">
              <div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  {t("search.resultsLabel")}
                </div>
                <h2 className="text-editorial text-2xl mt-0.5">
                  {submittedQ
                    ? t("search.resultsWithQuery", { query: submittedQ, count: filtered.length })
                    : t("search.resultsCount", { count: filtered.length })}
                </h2>
              </div>
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1 border border-border rounded-full p-1 bg-muted/30">
                  {SORT_IDS.map((id) => (
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
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full gap-1.5"
                  onClick={exportCsv}
                  disabled={filtered.length === 0}
                >
                  <Download className="h-3.5 w-3.5" />
                  {t("csv.download")}
                </Button>
              </div>
            </div>

            {isFetching && (
              <p className="text-sm text-muted-foreground mb-4">{t("search.loading")}</p>
            )}

            {submittedQ && result && (
              <div className="card-paper rounded-lg p-4 mb-6 border-l-4 border-l-primary">
                <div className="text-[10px] uppercase tracking-[0.18em] text-primary flex items-center gap-1.5">
                  ✦ {t("search.aiInterpretation")}
                </div>
                <p className="text-sm mt-1.5 leading-relaxed">
                  {result.keywords.length > 0 && (
                    <>
                      {t("search.keywords")}: <strong>{result.keywords.join(" / ")}</strong>
                      {result.matchedThemes.length > 0 && "　"}
                    </>
                  )}
                  {result.matchedThemes.length > 0 && (
                    <>
                      {t("search.matchedThemes")}: <strong>{result.matchedThemes.join(" / ")}</strong>
                    </>
                  )}
                  {result.keywords.length === 0 && result.matchedThemes.length === 0 && (
                    t("search.noKeywords")
                  )}
                </p>
              </div>
            )}

            <div className="card-paper rounded-lg overflow-hidden">
              {filtered.map((a) => (
                <ArticleCard key={a.id} article={a} variant="list" />
              ))}
              {filtered.length === 0 && !isFetching && (
                <p className="p-6 text-sm text-muted-foreground">{t("search.empty")}</p>
              )}
            </div>
          </div>
        </div>
      </main>
    </>
  );
}
