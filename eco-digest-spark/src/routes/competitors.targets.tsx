import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Download } from "lucide-react";
import { TopBar } from "@/components/top-bar";
import { CompetitorTarget, useCompetitorTargets } from "@/lib/api";
import { companyDisplayName, COMPETITOR_THEMES, themeLabel, useLanguage } from "@/lib/i18n";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { downloadCsv } from "@/lib/csv";

export const Route = createFileRoute("/competitors/targets")({
  component: CompetitorTargetsPage,
});

interface CategoryRow {
  goalCategoryId: string;
  goalCategoryName: string;
  theme: string;
  ownTargets: CompetitorTarget[];
  competitorTargets: CompetitorTarget[];
}

interface CompanyColumn {
  id: string;
  name: string;
  nameEn: string | null;
  count: number;
}

function targetLine(r: CompetitorTarget): string {
  const text = r.title ?? String(r.targetValue ?? "—");
  return r.targetYear ? `${text}（${r.targetYear}）` : text;
}

// タイトルに具体的な数値が含まれない場合でも、対象範囲・地域・原料・指標の定義が
// structured_fieldsにあれば内容が空に見えないよう補足情報として表示する
function targetDetail(r: CompetitorTarget): string | null {
  const parts = [
    r.reductionRate, r.scope, r.boundary, r.targetRegion, r.targetMaterial,
    r.kpiDefinition, r.achievementStatus,
  ]
    .filter((v): v is string | number => v !== null && v !== "")
    .map(String);
  return parts.length > 0 ? parts.join(" ・ ") : null;
}

function TargetCell({ items }: { items: CompetitorTarget[] }) {
  if (items.length === 0) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="space-y-2.5">
      {items.map((r) => (
        <div key={r.id}>
          <div className="text-sm leading-relaxed">{targetLine(r)}</div>
          {targetDetail(r) && (
            <div className="text-xs text-muted-foreground mt-0.5">{targetDetail(r)}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function CompetitorTargetsPage() {
  const { lang, t } = useLanguage();
  const [theme, setTheme] = useState("");
  const [goalCategory, setGoalCategory] = useState("");
  // 列数が多く横に長い表になるため、下側の横スクロールバーだけだと存在に気づきにくい・
  // 手前まで戻すのが面倒という声を受け、表の上にも横スクロールバーを複製して同期させる
  const topScrollRef = useRef<HTMLDivElement>(null);
  const bodyScrollRef = useRef<HTMLDivElement>(null);
  const [scrollWidth, setScrollWidth] = useState(0);
  const syncingRef = useRef<"top" | "body" | null>(null);
  // テーマ・目標カテゴリのどちらもここではクライアント側で絞り込む（APIのthemeパラメータは
  // レコードの自由付与themes配列を見るため、目標カテゴリが属する主要テーマとは基準がずれる）
  const { data, isLoading } = useCompetitorTargets();
  const allTargets = data?.targets ?? [];

  // 目標カテゴリ単位で行を作り、サントリー自身の目標がある行だけを「目標比較」の対象にする
  // （サントリーの目標カテゴリに、対応する競合各社の目標を並べる形式）。
  // テーマは目標カテゴリ自体が属する1つのテーマ（goalCategoryTheme）を使う。個々のレコードの
  // themes配列は最大1〜3個の自由付与タグで目標カテゴリの所属テーマとは別物のため、
  // ここで束ねると1カテゴリに複数テーマが表示されてしまい混乱するため使わない
  const rows = useMemo(() => {
    const byCategory = new Map<string, CategoryRow>();
    for (const r of allTargets) {
      if (!r.goalCategoryId) continue;
      let row = byCategory.get(r.goalCategoryId);
      if (!row) {
        row = {
          goalCategoryId: r.goalCategoryId,
          goalCategoryName: r.goalCategoryName ?? r.goalCategoryId,
          theme: r.goalCategoryTheme ?? "",
          ownTargets: [],
          competitorTargets: [],
        };
        byCategory.set(r.goalCategoryId, row);
      }
      (r.isOwnCompany ? row.ownTargets : row.competitorTargets).push(r);
    }
    return [...byCategory.values()]
      .filter((row) => row.ownTargets.length > 0)
      .sort((a, b) => a.goalCategoryId.localeCompare(b.goalCategoryId));
  }, [allTargets]);

  const themeFilteredRows = theme ? rows.filter((row) => row.theme === theme) : rows;
  const goalCategoryOptions = useMemo(
    () => themeFilteredRows.map((row) => [row.goalCategoryId, row.goalCategoryName] as const),
    [themeFilteredRows],
  );
  const visibleRows = goalCategory
    ? themeFilteredRows.filter((row) => row.goalCategoryId === goalCategory)
    : themeFilteredRows;

  // 競合各社を1社1列にする。列に出す企業は現在の絞り込み結果に登場する企業だけとし、
  // 登場するカテゴリ数が多い（比較の効くデータが多い）企業から先に並べる
  const competitorColumns = useMemo(() => {
    const byCompany = new Map<string, CompanyColumn>();
    for (const row of visibleRows) {
      for (const r of row.competitorTargets) {
        const existing = byCompany.get(r.companyId);
        if (existing) existing.count += 1;
        else byCompany.set(r.companyId, { id: r.companyId, name: r.companyName, nameEn: r.companyNameEn, count: 1 });
      }
    }
    return [...byCompany.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [visibleRows]);

  // 表の実際の幅（列数・内容で変わる）に合わせて、上部の複製スクロールバーの幅を揃える
  useEffect(() => {
    setScrollWidth(bodyScrollRef.current?.scrollWidth ?? 0);
  }, [visibleRows, competitorColumns]);

  const syncScroll = (from: "top" | "body") => (e: React.UIEvent<HTMLDivElement>) => {
    if (syncingRef.current && syncingRef.current !== from) return;
    syncingRef.current = from;
    const other = from === "top" ? bodyScrollRef.current : topScrollRef.current;
    if (other) other.scrollLeft = e.currentTarget.scrollLeft;
    syncingRef.current = null;
  };

  const exportCsv = () => {
    downloadCsv(
      `competitor_targets_${new Date().toISOString().slice(0, 10)}.csv`,
      [
        t("csv.col.theme"), t("competitor.targets.goalCategory"), t("competitor.targets.ownTargets"),
        ...competitorColumns.map((c) => companyDisplayName(c.name, c.nameEn, lang)),
      ],
      visibleRows.map((row) => [
        themeLabel(row.theme, lang),
        row.goalCategoryName,
        row.ownTargets.map(targetLine).join(" / "),
        ...competitorColumns.map((c) =>
          row.competitorTargets.filter((r) => r.companyId === c.id).map(targetLine).join(" / "),
        ),
      ]),
    );
  };

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.competitorDashboard"), href: "/competitors/overview" },
          { label: t("nav.competitorTargets") },
        ]}
      />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        <section className="border-b border-border pb-5">
          <h1 className="text-editorial text-4xl">{t("competitor.targets.title")}</h1>
          <p className="text-sm text-muted-foreground mt-2">{t("competitor.targets.subtitle")}</p>
        </section>

        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Select
              value={theme || "all"}
              onValueChange={(v) => {
                setTheme(v === "all" ? "" : v);
                setGoalCategory("");
              }}
            >
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
            <Select value={goalCategory || "all"} onValueChange={(v) => setGoalCategory(v === "all" ? "" : v)}>
              <SelectTrigger className="w-56">
                <SelectValue placeholder={t("competitor.filter.allGoalCategories")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("competitor.filter.allGoalCategories")}</SelectItem>
                {goalCategoryOptions.map(([id, name]) => (
                  <SelectItem key={id} value={id}>{name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="rounded-full gap-1.5"
            onClick={exportCsv}
            disabled={visibleRows.length === 0}
          >
            <Download className="h-3.5 w-3.5" />
            {t("csv.download")}
          </Button>
        </div>

        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : visibleRows.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("competitor.targets.empty")}</p>
        ) : (
          <div className="card-paper rounded-lg">
            {/* 下側のスクロールバーだけだと存在に気づきにくいため、同じ幅の複製バーを表の上にも出し
                横スクロール位置を相互に同期させる */}
            <div ref={topScrollRef} onScroll={syncScroll("top")} className="overflow-x-auto border-b border-border">
              <div style={{ width: scrollWidth, height: 1 }} />
            </div>
            <div ref={bodyScrollRef} onScroll={syncScroll("body")} className="overflow-x-auto">
            {/* table-fixedで列幅を明示値どおりに固定しないと、内容量に応じてブラウザが列幅を
                自動調整してしまい、sticky列のleftオフセット計算（幅の想定）とズレて重なって見える */}
            <Table className="table-fixed">
              <TableHeader>
                <TableRow>
                  <TableHead className="sticky left-0 z-10 bg-card w-28 break-words">
                    {t("competitor.targets.theme")}
                  </TableHead>
                  <TableHead className="sticky left-28 z-10 bg-card w-52 break-words">
                    {t("competitor.targets.goalCategory")}
                  </TableHead>
                  <TableHead className="w-64 break-words">
                    {t("competitor.targets.ownTargets")}
                  </TableHead>
                  {competitorColumns.map((c) => (
                    <TableHead key={c.id} className="w-56 break-words">
                      {companyDisplayName(c.name, c.nameEn, lang)}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleRows.map((row) => (
                  <TableRow key={row.goalCategoryId}>
                    <TableCell className="sticky left-0 z-10 bg-card align-top w-28 break-words">
                      {row.theme ? themeLabel(row.theme, lang) : "—"}
                    </TableCell>
                    <TableCell className="sticky left-28 z-10 bg-card align-top font-medium w-52 break-words">
                      {row.goalCategoryName}
                    </TableCell>
                    <TableCell className="align-top w-64 break-words">
                      <TargetCell items={row.ownTargets} />
                    </TableCell>
                    {competitorColumns.map((c) => (
                      <TableCell key={c.id} className="align-top w-56 break-words">
                        <TargetCell items={row.competitorTargets.filter((r) => r.companyId === c.id)} />
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </div>
          </div>
        )}
      </main>
    </>
  );
}
