import { useRef } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { ArticleCard } from "./article-card";
import type { Article } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function ArticleCarousel({
  articles,
  title,
  subtitle,
}: {
  articles: Article[];
  title: string;
  subtitle?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const scroll = (dir: 1 | -1) => {
    ref.current?.scrollBy({ left: dir * 640, behavior: "smooth" });
  };
  return (
    <section className="relative">
      <div className="flex items-end justify-between mb-4 px-1">
        <div>
          <h2 className="text-editorial text-3xl">{title}</h2>
          {subtitle && (
            <p className="text-sm text-muted-foreground mt-1">{subtitle}</p>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="icon" onClick={() => scroll(-1)} className="h-9 w-9 rounded-full">
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button variant="outline" size="icon" onClick={() => scroll(1)} className="h-9 w-9 rounded-full">
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div
        ref={ref}
        className="flex gap-5 overflow-x-auto pb-4 snap-x snap-mandatory scrollbar-none [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden"
      >
        {articles.map((a) => (
          <div
            key={a.id}
            className="snap-start shrink-0 w-[320px] xl:w-[360px]"
          >
            <ArticleCard article={a} />
          </div>
        ))}
      </div>
    </section>
  );
}