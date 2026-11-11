import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { API_BASE } from "@/lib/apiBase";

export type Lang = "ja" | "en";

export interface Category {
  id: string;
  label: string;
  labelJa: string;
  hue: string;
  count: number;
}

export interface Subcategory {
  id: string;
  label: string;
  labelJa: string;
  parent: string;
  count: number;
}

export interface ImportanceScoreEntry {
  id: string;
  score: number;
}

// Phase B: Department Intelligence AI — 出典(Provenance)。Chat/Search共通の形。
// sourceDepartmentは"sustainability"|"geopolitics"（将来"scm"|"pa"|"erm"等が増える想定）。
export interface Provenance {
  sourceDepartment: string;
  sourceAgent: string | null;
  sourceType: string;
  assessmentTitle: string | null;
  requestQuestion: string | null;
  confidence: string | number | null;
  reviewStatus: "human_reviewed" | "needs_review" | "ai_only";
  asOf: string | null;
  references: unknown[];
  externalCallId: string | null;
  reuseType: "none" | "exact_cache" | "retrieved";
}

// Search専用: Provenanceに加え、人間が直接読むためのIntelligence本体を持つ。
export interface CrossDomainIntelligenceItem {
  id: string;
  title: string | null;
  assessment: string | null;
  whyRelevant: string | null;
  asOf: string | null;
  provenance: Provenance;
}

export interface Article {
  id: string;
  title: string;
  summary: string;
  category: string; // 大テーマ(先頭1件、既存のアイコン・色・パンくず用)
  categories: string[]; // 記事が持つ大テーマ全件
  source: string;
  sector: string;
  publishedAt: string;
  importance: number; // 0-100（S/A/B/C/Dランクから変換した目安値。カード表示用）
  importanceLevel: string; // S/A/B/C/D
  importanceTotal: number | null; // 7項目合計の実点数（0-35）
  importanceBreakdown: ImportanceScoreEntry[]; // 評価項目ごとの点数（0-5）
  trending: boolean; // 重要度S・Aランクの記事を「注目」として扱う
  url: string;
  tags: string[];
  subThemes: string[];
  materialityCodes: string[];
  cover: string;
  body?: string[];
  likesCount: number;
  readsCount: number;
  provenance: Provenance;
}

export interface ImportanceCriterion {
  id: string;
  label: string;
  description: string;
  maxScore: number;
  bands: Record<string, string>;
}

const covers = [
  "linear-gradient(135deg,#2f4a34 0%,#8bb185 60%,#e6d9a6 100%)",
  "linear-gradient(135deg,#1c3a2e 0%,#3d6b4b 55%,#c9d9a3 100%)",
  "linear-gradient(135deg,#4a3b1f 0%,#a97c3f 50%,#e8d6a4 100%)",
  "linear-gradient(135deg,#22333b 0%,#5f8a8b 60%,#dfe7d4 100%)",
  "linear-gradient(135deg,#3b2f4a 0%,#7d6b9e 55%,#e0d4c6 100%)",
  "linear-gradient(135deg,#1f3a3a 0%,#4a8a7b 60%,#f0e6c8 100%)",
  "linear-gradient(135deg,#2e3a1f 0%,#7a9648 55%,#f3e9c1 100%)",
  "linear-gradient(135deg,#3a1f22 0%,#8a4a52 55%,#e8c8b0 100%)",
];

function coverFor(id: string): string {
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return covers[hash % covers.length];
}

function withCover(a: Omit<Article, "cover">): Article {
  return { ...a, cover: coverFor(a.id) };
}

export function relativeTime(iso: string, lang: Lang = "ja"): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3_600_000);
  if (lang === "en") {
    if (h < 1) return "just now";
    if (h < 24) return `${h}h ago`;
    const d = Math.floor(h / 24);
    if (d < 7) return `${d}d ago`;
    return new Date(iso).toLocaleDateString("en-US");
  }
  if (h < 1) return "たった今";
  if (h < 24) return `${h}時間前`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}日前`;
  return new Date(iso).toLocaleDateString("ja-JP");
}

const FALLBACK_CATEGORY: Category = {
  id: "その他",
  label: "Other",
  labelJa: "その他",
  hue: "oklch(0.5 0.02 150)",
  count: 0,
};

export function categoryMeta(categories: Category[], id: string): Category {
  return categories.find((c) => c.id === id) ?? { ...FALLBACK_CATEGORY, id, labelJa: id };
}

export function categoryLabel(cat: Category, lang: Lang): string {
  return lang === "en" ? cat.label : cat.labelJa;
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API error ${res.status}`);
  }
  return res.json();
}

export const categoriesQueryOptions = {
  queryKey: ["categories"] as const,
  queryFn: () => fetchJson<Category[]>("/api/categories"),
  staleTime: 5 * 60_000,
};

export function useCategories() {
  return useQuery(categoriesQueryOptions);
}

export const subcategoriesQueryOptions = {
  queryKey: ["subcategories"] as const,
  queryFn: () => fetchJson<Subcategory[]>("/api/subcategories"),
  staleTime: 5 * 60_000,
};

export function useSubcategories() {
  return useQuery(subcategoriesQueryOptions);
}

export const importanceRubricQueryOptions = {
  queryKey: ["importanceRubric"] as const,
  queryFn: () => fetchJson<ImportanceCriterion[]>("/api/importance-rubric"),
  staleTime: 60 * 60_000, // 評価基準そのものはめったに変わらない静的データ
};

export function useImportanceRubric() {
  return useQuery(importanceRubricQueryOptions);
}

export type ArticleSort = "importance" | "latest" | "likes" | "reads" | "trending";

// 絞り込み後の「全件」に対する集計（ページに依存しない。サイドバー・カテゴリ画面の件数表示用）
export interface ArticleStats {
  count: number;
  importantCount: number;
  trendingCount: number;
  regulatoryCount: number;
  avgImportance: number;
}

export interface ArticlesResult {
  total: number;
  filteredTotal: number;
  keywords: string[];
  matchedThemes: string[];
  articles: Article[];
  crossDomainIntelligence: CrossDomainIntelligenceItem[];
  // ページ分割（limit指定）のときだけ付く
  hasMore?: boolean;
  nextCursor?: string | null;
  stats?: ArticleStats;
}

export interface ArticlesParams {
  themes?: string[];
  q?: string;
  lang?: Lang;
  sinceDays?: number;
  sort?: ArticleSort; // 並べ替え（サーバー側で行う）
  minImportance?: number; // 重要度の下限（サーバー側で絞る）
  limit?: number; // 1回に取る件数。指定しないと従来どおり全件
}

// 1ページ分を取得する。cursor は「続き」の位置（最初のページは省略）
async function fetchArticlesPage(params: ArticlesParams, cursor?: string): Promise<ArticlesResult> {
  const usp = new URLSearchParams();
  const themes = (params.themes ?? []).join(",");
  if (themes) usp.set("themes", themes);
  if (params.q) usp.set("q", params.q);
  usp.set("lang", params.lang ?? "ja");
  if (params.sinceDays) usp.set("since_days", String(params.sinceDays));
  if (params.limit) usp.set("limit", String(params.limit));
  if (params.sort) usp.set("sort", params.sort);
  if (params.minImportance) usp.set("min_importance", String(params.minImportance));
  if (cursor) usp.set("cursor", cursor);
  const data = await fetchJson<Omit<ArticlesResult, "articles"> & { articles: Omit<Article, "cover">[] }>(
    `/api/articles?${usp.toString()}`,
  );
  return { ...data, articles: data.articles.map(withCover) };
}

function articlesKey(params: ArticlesParams) {
  return [
    "articles", (params.themes ?? []).join(","), params.q ?? "", params.lang ?? "ja",
    params.sinceDays, params.sort, params.minImportance, params.limit,
  ] as const;
}

export function articlesQueryOptions(params: ArticlesParams = {}) {
  return {
    queryKey: articlesKey(params),
    queryFn: (): Promise<ArticlesResult> => fetchArticlesPage(params),
    staleTime: 60_000,
  };
}

export function useArticles(params: ArticlesParams = {}) {
  return useQuery(articlesQueryOptions(params));
}

// 無限スクロール用。画面の下に近づいたら fetchNextPage() で続きを取る。
// limit は1回の件数（未指定は30件）。items は全ページを連結した記事一覧、先頭ページの集計は stats。
export function useArticlesInfinite(params: ArticlesParams = {}) {
  const limit = params.limit ?? 30;
  const query = useInfiniteQuery({
    queryKey: ["articles-infinite", ...articlesKey({ ...params, limit })] as const,
    queryFn: ({ pageParam }) => fetchArticlesPage({ ...params, limit }, pageParam || undefined),
    initialPageParam: "" as string,
    getNextPageParam: (last) => (last.hasMore && last.nextCursor ? last.nextCursor : undefined),
    staleTime: 60_000,
  });
  const pages = query.data?.pages ?? [];
  return {
    ...query,
    items: pages.flatMap((p) => p.articles),
    first: pages[0], // 総件数・集計・検索結果の付随情報は先頭ページから読む
  };
}

// 条件に合う記事を、最後のページまで全部取る（CSV出力用。画面に読み込んだ分ではなく全件を出すため）
export async function fetchAllArticles(params: ArticlesParams = {}): Promise<Article[]> {
  const all: Article[] = [];
  let cursor: string | undefined;
  for (let i = 0; i < 200; i++) {
    const page = await fetchArticlesPage({ ...params, limit: 100 }, cursor);
    all.push(...page.articles);
    if (!page.hasMore || !page.nextCursor) break;
    cursor = page.nextCursor;
  }
  return all;
}

// サイドバーなど、集計(件数)だけが欲しい画面用。記事は1件だけ取って、statsを使う
export function useArticleStats() {
  return useArticles({ limit: 1 });
}

export function articleQueryOptions(id: string, lang: Lang = "ja") {
  return {
    queryKey: ["article", id, lang] as const,
    queryFn: () => fetchJson<Omit<Article, "cover">>(`/api/articles/${id}?lang=${lang}`).then(withCover),
  };
}

export function useArticle(id: string | undefined, lang: Lang = "ja") {
  return useQuery({ ...articleQueryOptions(id ?? "", lang), enabled: !!id });
}

export interface ChatMsg {
  role: "user" | "assistant";
  content: string;
  sources?: string[];
  crossDomainIntelligence?: Provenance[];
}

export function useTranslateTexts(texts: string[], targetLang: Lang, enabled: boolean) {
  return useQuery({
    queryKey: ["translateTexts", targetLang, ...texts] as const,
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/translate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ texts, targetLang }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `API error ${res.status}`);
      }
      return res.json() as Promise<{ translations: string[] }>;
    },
    enabled: enabled && texts.length > 0,
    staleTime: Infinity,
  });
}

export function useChatSend() {
  return useMutation({
    mutationFn: async ({
      message,
      history,
      themes,
      lang,
    }: {
      message: string;
      history: ChatMsg[];
      themes?: string[];
      lang?: Lang;
    }) => {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          history: history.map(({ role, content }) => ({ role, content })),
          themes: themes ?? [],
          lang: lang ?? "ja",
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `API error ${res.status}`);
      }
      return res.json() as Promise<{
        reply: string;
        sources: string[];
        crossDomainIntelligence: Provenance[];
      }>;
    },
  });
}

// ─── いいね・読んだ（件数はDB保存、ON/OFF状態はブラウザのlocalStorageで管理） ───
// ログイン機能が無いため「誰が」いいねしたかは扱えない。件数は全ブラウザ共通の
// 実数（DB保存）、ボタンの点灯状態だけをこのブラウザ内のlocalStorageで覚えておく。
const LIKED_KEY = "verdant_liked_article_ids";
const READ_KEY = "verdant_read_article_ids";

function readLocalSet(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function writeLocalSet(key: string, set: Set<string>): void {
  localStorage.setItem(key, JSON.stringify([...set]));
}

function toggleLocalSet(key: string, id: string): boolean {
  const set = readLocalSet(key);
  const next = !set.has(id);
  if (next) set.add(id);
  else set.delete(id);
  writeLocalSet(key, set);
  return next;
}

export function isLikedLocally(id: string): boolean {
  return readLocalSet(LIKED_KEY).has(id);
}

export function isReadLocally(id: string): boolean {
  return readLocalSet(READ_KEY).has(id);
}

interface EngagementResult {
  likesCount: number;
  readsCount: number;
}

export function useEngagement() {
  const queryClient = useQueryClient();

  const applyResult = (id: string, data: EngagementResult) => {
    queryClient.setQueriesData<ArticlesResult | undefined>({ queryKey: ["articles"] }, (old) => {
      if (!old) return old;
      return {
        ...old,
        articles: old.articles.map((a) =>
          a.id === id ? { ...a, likesCount: data.likesCount, readsCount: data.readsCount } : a,
        ),
      };
    });
    queryClient.setQueriesData<Article | undefined>({ queryKey: ["article", id] }, (old) =>
      old ? { ...old, likesCount: data.likesCount, readsCount: data.readsCount } : old,
    );
  };

  const mutation = useMutation({
    mutationFn: async ({ id, liked, read }: { id: string; liked?: boolean; read?: boolean }) => {
      const res = await fetch(`${API_BASE}/api/articles/${id}/engagement`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ liked, read }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `API error ${res.status}`);
      }
      return res.json() as Promise<EngagementResult>;
    },
  });

  const toggleLike = (id: string) => {
    const liked = toggleLocalSet(LIKED_KEY, id);
    mutation.mutate({ id, liked }, { onSuccess: (data) => applyResult(id, data) });
    return liked;
  };

  const toggleRead = (id: string) => {
    const read = toggleLocalSet(READ_KEY, id);
    mutation.mutate({ id, read }, { onSuccess: (data) => applyResult(id, data) });
    return read;
  };

  return { toggleLike, toggleRead };
}

// いいね/既読の初期状態(localStorage)とトグル操作をまとめたフック。
// ArticleCardと記事詳細ページの両方で同じ配線が必要になるため共通化。
export function useArticleLikeRead(articleId: string) {
  const { toggleLike, toggleRead } = useEngagement();
  const [liked, setLiked] = useState(() => isLikedLocally(articleId));
  const [read, setRead] = useState(() => isReadLocally(articleId));

  return {
    liked,
    read,
    toggleLike: () => setLiked(toggleLike(articleId)),
    toggleRead: () => setRead(toggleRead(articleId)),
  };
}

// ─── 競合サステナビリティモニタリング ───────────────────────────────────
// バックエンドはapi_server.pyの/api/competitors/*（読み取り専用）。
// 実際のクロール・分類・変更検知はcompetitor_crawler.py等のPythonバッチが担い、
// PMOレビュー・承認はStreamlit(sustainability_expert_dashboard.py)で行う。
// この画面群はあくまで閲覧用。

export interface CompetitorChangedField {
  field: string;
  before: string | number | null;
  after: string | number | null;
}

export interface CompetitorChange {
  id: string;
  companyId: string;
  companyName: string;
  companyNameEn: string | null;
  companyCategory: string;
  recordType: "TARGET" | "KPI" | "ACTUAL" | "ESG_RATING";
  changeType: string | null;
  direction: "STRENGTHENED" | "WEAKENED" | "NEUTRAL" | null;
  summary: string;
  reasoningSummary: string | null;
  changedFields: CompetitorChangedField[];
  themes: string[];
  goalCategoryId: string | null;
  goalCategoryName: string | null;
  goalCategoryTheme: string | null;
  confidence: number | null;
  reviewRequired: boolean;
  sourceUrl: string | null;
  beforeSourceUrl: string | null;
  crossDocumentComparison: boolean;
  createdAt: string;
  sourceUpdatedAt: string | null;
}

export interface CompetitorInitiative {
  id: string;
  companyId: string;
  companyName: string;
  companyNameEn: string | null;
  companyCategory: string;
  title: string;
  summary: string;
  themes: string[];
  goalCategoryId: string | null;
  goalCategoryName: string | null;
  goalCategoryTheme: string | null;
  isNew: boolean;
  detectedAt: string;
  sourceUrl: string | null;
  body?: string[];
}

export interface CompetitorTarget {
  id: string;
  companyId: string;
  companyName: string;
  companyNameEn: string | null;
  companyCategory: string;
  isOwnCompany: boolean;
  recordType: "TARGET" | "KPI";
  title: string | null;
  themes: string[];
  goalCategoryId: string | null;
  goalCategoryName: string | null;
  goalCategoryTheme: string | null;
  targetValue: string | number | null;
  numericValue: string | number | null;
  unit: string | number | null;
  reductionRate: string | number | null;
  baseYear: string | number | null;
  targetYear: string | number | null;
  scope: string | number | null;
  boundary: string | number | null;
  targetRegion: string | number | null;
  targetMaterial: string | number | null;
  kpiDefinition: string | number | null;
  achievementStatus: string | number | null;
  sourceUrl: string | null;
  sourceUpdatedAt: string | null;
}

export interface CompetitorCompany {
  id: string;
  name: string;
  nameEn: string | null;
  category: string;
  displayOrder: number;
  isOwnCompany: boolean;
  targetCount: number;
  initiativeCount: number;
}

export interface CompetitorCompanyDetail {
  id: string;
  name: string;
  nameEn: string | null;
  category: string;
  targets: CompetitorTarget[];
  initiatives: CompetitorInitiative[];
  changeHistory: CompetitorChange[];
  sources: { id: string; url: string; type: string }[];
}

export interface CompetitorTrend {
  title: string;
  summary: string;
  relatedCompanyNames: string[];
  relatedThemes: string[];
  confidence: number;
}

export interface CompetitorOverview {
  reportMonth: string | null;
  summary: {
    monitoredCompanies: number;
    updatedCompanies: number;
    targetChangeCount: number;
    actualUpdateCount: number;
    initiativeCount: number;
    reviewPendingCount: number;
  };
  crossCompanyTrends: CompetitorTrend[];
  recentChanges: CompetitorChange[];
  topInitiatives: CompetitorInitiative[];
}

export const competitorOverviewQueryOptions = {
  queryKey: ["competitorOverview"] as const,
  queryFn: () => fetchJson<CompetitorOverview>("/api/competitors/overview"),
  staleTime: 60_000,
};

export function useCompetitorOverview() {
  return useQuery(competitorOverviewQueryOptions);
}

export interface CompetitorChangesParams {
  companyId?: string;
  theme?: string;
  recordType?: string;
  sinceDays?: number;
  dateField?: "createdAt" | "sourceUpdatedAt";
  sortDir?: "asc" | "desc";
  dateFrom?: string;
  dateTo?: string;
}

export function competitorChangesQueryOptions(params: CompetitorChangesParams = {}) {
  const usp = new URLSearchParams();
  if (params.companyId) usp.set("company_id", params.companyId);
  if (params.theme) usp.set("theme", params.theme);
  if (params.recordType) usp.set("record_type", params.recordType);
  usp.set("since_days", String(params.sinceDays ?? 90));
  usp.set("date_field", params.dateField ?? "createdAt");
  usp.set("sort_dir", params.sortDir ?? "desc");
  if (params.dateFrom) usp.set("date_from", params.dateFrom);
  if (params.dateTo) usp.set("date_to", params.dateTo);
  return {
    queryKey: ["competitorChanges", params.companyId ?? "", params.theme ?? "",
      params.recordType ?? "", params.sinceDays ?? 90, params.dateField ?? "createdAt",
      params.sortDir ?? "desc", params.dateFrom ?? "", params.dateTo ?? ""] as const,
    queryFn: () => fetchJson<{ changes: CompetitorChange[] }>(`/api/competitors/changes?${usp.toString()}`),
    staleTime: 60_000,
  };
}

export function useCompetitorChanges(params: CompetitorChangesParams = {}) {
  return useQuery(competitorChangesQueryOptions(params));
}

export function competitorTargetsQueryOptions(
  params: { companyId?: string; theme?: string; goalCategory?: string } = {},
) {
  const usp = new URLSearchParams();
  if (params.companyId) usp.set("company_id", params.companyId);
  if (params.theme) usp.set("theme", params.theme);
  if (params.goalCategory) usp.set("goal_category", params.goalCategory);
  return {
    queryKey: ["competitorTargets", params.companyId ?? "", params.theme ?? "",
      params.goalCategory ?? ""] as const,
    queryFn: () => fetchJson<{ targets: CompetitorTarget[] }>(`/api/competitors/targets?${usp.toString()}`),
    staleTime: 60_000,
  };
}

export function useCompetitorTargets(
  params: { companyId?: string; theme?: string; goalCategory?: string } = {},
) {
  return useQuery(competitorTargetsQueryOptions(params));
}

export function competitorInitiativesQueryOptions(
  params: { companyId?: string; theme?: string; isNew?: boolean; goalCategory?: string } = {},
) {
  const usp = new URLSearchParams();
  if (params.companyId) usp.set("company_id", params.companyId);
  if (params.theme) usp.set("theme", params.theme);
  if (params.isNew !== undefined) usp.set("is_new", String(params.isNew));
  if (params.goalCategory) usp.set("goal_category", params.goalCategory);
  return {
    queryKey: ["competitorInitiatives", params.companyId ?? "", params.theme ?? "",
      params.isNew ?? "", params.goalCategory ?? ""] as const,
    queryFn: () =>
      fetchJson<{ initiatives: CompetitorInitiative[] }>(`/api/competitors/initiatives?${usp.toString()}`),
    staleTime: 60_000,
  };
}

export function useCompetitorInitiatives(
  params: { companyId?: string; theme?: string; isNew?: boolean; goalCategory?: string } = {},
) {
  return useQuery(competitorInitiativesQueryOptions(params));
}

export function competitorInitiativeQueryOptions(id: string, lang: Lang = "ja") {
  return {
    queryKey: ["competitorInitiative", id, lang] as const,
    queryFn: () => fetchJson<CompetitorInitiative>(`/api/competitors/initiatives/${id}?lang=${lang}`),
  };
}

export function useCompetitorInitiative(id: string | undefined, lang: Lang = "ja") {
  return useQuery({ ...competitorInitiativeQueryOptions(id ?? "", lang), enabled: !!id });
}

export const competitorCompaniesQueryOptions = {
  queryKey: ["competitorCompanies"] as const,
  queryFn: () => fetchJson<{ companies: CompetitorCompany[] }>("/api/competitors/companies"),
  staleTime: 5 * 60_000,
};

export function useCompetitorCompanies() {
  return useQuery(competitorCompaniesQueryOptions);
}

export function competitorCompanyQueryOptions(companyId: string) {
  return {
    queryKey: ["competitorCompany", companyId] as const,
    queryFn: () => fetchJson<CompetitorCompanyDetail>(`/api/competitors/companies/${companyId}`),
  };
}

export function useCompetitorCompany(companyId: string | undefined) {
  return useQuery({ ...competitorCompanyQueryOptions(companyId ?? ""), enabled: !!companyId });
}

export const competitorUnreadCountQueryOptions = {
  queryKey: ["competitorUnreadCount"] as const,
  queryFn: () => fetchJson<{ count: number }>("/api/competitors/notifications/unread-count"),
  staleTime: 60_000,
  refetchInterval: 60_000,
};

export function useCompetitorUnreadCount() {
  return useQuery(competitorUnreadCountQueryOptions);
}
