import { Link } from "@tanstack/react-router";
import {
  TrendingUp,
  Heart,
  BookOpen,
  Droplet,
  Flame,
  Package,
  Wheat,
  Leaf,
  Users,
  HeartPulse,
  UserCog,
  Megaphone,
  Newspaper,
  type LucideIcon,
} from "lucide-react";
import {
  categoryLabel,
  categoryMeta,
  relativeTime,
  useArticleLikeRead,
  useCategories,
  type Article,
} from "@/lib/api";
import { useLanguage } from "@/lib/i18n";

// サムネイル画像を持たないため、カテゴリごとの大きめアイコンを添えて
// 単色グラデーションだけの「空っぽ感」を和らげる（実写画像の代用ではなく装飾）
const CATEGORY_ICON: Record<string, LucideIcon> = {
  水: Droplet,
  "気候変動・GHG": Flame,
  容器包装: Package,
  原料調達: Wheat,
  生物多様性: Leaf,
  人権: Users,
  健康: HeartPulse,
  人的資本: UserCog,
  責任あるマーケティング: Megaphone,
};

function categoryIcon(category: string): LucideIcon {
  return CATEGORY_ICON[category] ?? Newspaper;
}

function ImportanceDial({ value }: { value: number }) {
  const color =
    value >= 90
      ? "oklch(0.55 0.2 28)"
      : value >= 80
        ? "oklch(0.62 0.15 55)"
        : "oklch(0.55 0.1 155)";
  return (
    <div className="flex items-center gap-1.5">
      <div className="relative h-8 w-8">
        <svg viewBox="0 0 36 36" className="h-8 w-8 -rotate-90">
          <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" strokeOpacity="0.12" strokeWidth="3" />
          <circle
            cx="18" cy="18" r="15" fill="none"
            stroke={color} strokeWidth="3" strokeLinecap="round"
            strokeDasharray={`${(value / 100) * 94.2} 94.2`}
          />
        </svg>
        <span className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold tabular-nums">
          {value}
        </span>
      </div>
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
        Impact
      </span>
    </div>
  );
}

function EngagementRow({ article, size = "sm" }: { article: Article; size?: "sm" | "md" }) {
  const { liked, read, toggleLike, toggleRead } = useArticleLikeRead(article.id);
  const iconSize = size === "md" ? "h-4 w-4" : "h-3.5 w-3.5";

  const onLike = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    toggleLike();
  };
  const onRead = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    toggleRead();
  };

  return (
    <div className="flex items-center gap-3 text-[11px] text-muted-foreground shrink-0">
      <button
        onClick={onLike}
        className={`inline-flex items-center gap-1 transition-colors ${liked ? "text-destructive" : "hover:text-destructive"}`}
      >
        <Heart className={`${iconSize} ${liked ? "fill-current" : ""}`} />
        {article.likesCount}
      </button>
      <button
        onClick={onRead}
        className={`inline-flex items-center gap-1 transition-colors ${read ? "text-primary" : "hover:text-primary"}`}
      >
        <BookOpen className={`${iconSize} ${read ? "fill-current" : ""}`} />
        {article.readsCount}
      </button>
    </div>
  );
}

export function ArticleCard({
  article,
  variant = "default",
}: {
  article: Article;
  variant?: "default" | "hero" | "compact" | "list";
}) {
  const { data: categories = [] } = useCategories();
  const { lang, t } = useLanguage();
  const cat = categoryMeta(categories, article.category);
  const Icon = categoryIcon(article.category);

  if (variant === "list") {
    return (
      <Link
        to="/article/$id"
        params={{ id: article.id }}
        className="group grid grid-cols-[180px_1fr_auto] gap-5 items-start p-5 border-b border-border hover:bg-muted/40 transition-colors"
      >
        <div
          className="aspect-[4/3] rounded-md relative overflow-hidden"
          style={{ background: article.cover }}
        >
          <div className="absolute inset-0 flex items-center justify-center">
            <Icon className="h-10 w-10 text-white/30" strokeWidth={1.5} />
          </div>
          <span
            className="absolute top-2 left-2 text-[10px] uppercase tracking-wider text-white/95 px-1.5 py-0.5 rounded"
            style={{ backgroundColor: cat.hue }}
          >
            {categoryLabel(cat, lang)}
          </span>
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground uppercase tracking-wider">
            <span>{article.source}</span>
            <span>·</span>
            <span>{relativeTime(article.publishedAt, lang)}</span>
            {article.trending && (
              <span className="ml-1 inline-flex items-center gap-0.5 text-destructive normal-case tracking-normal">
                <TrendingUp className="h-3 w-3" /> {t("card.trending")}
              </span>
            )}
          </div>
          <h3 className="text-editorial text-2xl mt-2 leading-tight group-hover:text-primary transition-colors">
            {article.title}
          </h3>
          <p className="text-sm text-muted-foreground mt-2 leading-relaxed line-clamp-2">
            {article.summary}
          </p>
          <div className="flex items-center gap-4 mt-3">
            <EngagementRow article={article} />
            {article.sector && (
              <span className="text-xs text-muted-foreground/70">{article.sector}</span>
            )}
          </div>
        </div>
        <div className="pt-1">
          <ImportanceDial value={article.importance} />
        </div>
      </Link>
    );
  }

  if (variant === "hero") {
    return (
      <Link
        to="/article/$id"
        params={{ id: article.id }}
        className="group relative block overflow-hidden rounded-xl card-paper"
      >
        <div
          className="aspect-[16/8] w-full relative"
          style={{ background: article.cover }}
        >
          <div className="absolute inset-0 flex items-center justify-center">
            <Icon className="h-20 w-20 text-white/15" strokeWidth={1.5} />
          </div>
          <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-black/20 to-transparent" />
          <div className="absolute top-5 left-5 flex items-center gap-2">
            <span
              className="text-[10px] uppercase tracking-[0.2em] text-white px-2 py-1 rounded"
              style={{ backgroundColor: cat.hue }}
            >
              {categoryLabel(cat, lang)}
            </span>
            {article.trending && (
              <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-white bg-destructive/90 px-2 py-1 rounded">
                <TrendingUp className="h-3 w-3" /> {t("card.trending")}
              </span>
            )}
          </div>
          <div className="absolute bottom-0 left-0 right-0 p-6 text-white">
            <div className="text-[11px] uppercase tracking-[0.2em] text-white/80">
              {article.source}
            </div>
            <h2 className="text-editorial text-4xl md:text-5xl mt-2 max-w-3xl group-hover:opacity-90">
              {article.title}
            </h2>
            <p className="text-sm text-white/85 mt-3 max-w-2xl leading-relaxed">
              {article.summary}
            </p>
          </div>
        </div>
      </Link>
    );
  }

  // default card (used in carousel & grids)
  return (
    <Link
      to="/article/$id"
      params={{ id: article.id }}
      className="group block card-paper rounded-lg overflow-hidden h-full transition-transform hover:-translate-y-0.5"
    >
      <div
        className="aspect-[16/9] relative"
        style={{ background: article.cover }}
      >
        <div className="absolute inset-0 flex items-center justify-center">
          <Icon className="h-12 w-12 text-white/25" strokeWidth={1.5} />
        </div>
        <span
          className="absolute top-3 left-3 text-[10px] uppercase tracking-[0.15em] text-white px-1.5 py-0.5 rounded"
          style={{ backgroundColor: cat.hue }}
        >
          {categoryLabel(cat, lang)}
        </span>
        {article.trending && (
          <span className="absolute top-3 right-3 inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-white bg-black/60 backdrop-blur px-1.5 py-0.5 rounded">
            <TrendingUp className="h-3 w-3" />
          </span>
        )}
      </div>
      <div className="p-4">
        <div className="flex items-center justify-between text-[11px] text-muted-foreground uppercase tracking-wider">
          <span className="truncate">{article.source}</span>
          <span className="tabular-nums shrink-0 ml-2">
            <span className="text-primary font-semibold">{article.importance}</span>
            <span className="text-muted-foreground/60">/100</span>
          </span>
        </div>
        <h3 className="text-editorial text-xl mt-2 leading-snug line-clamp-3 group-hover:text-primary">
          {article.title}
        </h3>
        <p className="text-xs text-muted-foreground mt-2 leading-relaxed line-clamp-2">
          {article.summary}
        </p>
        <div className="flex items-center justify-between mt-3 pt-3 border-t border-border/70">
          <EngagementRow article={article} />
          <span className="text-[11px] text-muted-foreground">{relativeTime(article.publishedAt, lang)}</span>
        </div>
      </div>
    </Link>
  );
}
