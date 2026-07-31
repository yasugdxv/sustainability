import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

const API_BASE = "http://127.0.0.1:8000";

export type Lang = "ja" | "en";

export interface Category {
  id: string;
  label: string;
  labelJa: string;
  hue: string;
  count: number;
}

export interface Article {
  id: string;
  title: string;
  summary: string;
  category: string;
  source: string;
  sector: string;
  publishedAt: string;
  importance: number; // 0-100（S/A/B/C/Dランクから変換した目安値）
  importanceLevel: string; // S/A/B/C/D
  trending: boolean; // 重要度S・Aランクの記事を「注目」として扱う
  url: string;
  tags: string[];
  materialityCodes: string[];
  cover: string;
  body?: string[];
  likesCount: number;
  readsCount: number;
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

export interface ArticlesResult {
  total: number;
  filteredTotal: number;
  keywords: string[];
  matchedThemes: string[];
  articles: Article[];
}

export function articlesQueryOptions(params: { themes?: string[]; q?: string; lang?: Lang } = {}) {
  const themes = (params.themes ?? []).join(",");
  const q = params.q ?? "";
  const lang = params.lang ?? "ja";
  return {
    queryKey: ["articles", themes, q, lang] as const,
    queryFn: async (): Promise<ArticlesResult> => {
      const usp = new URLSearchParams();
      if (themes) usp.set("themes", themes);
      if (q) usp.set("q", q);
      usp.set("lang", lang);
      const data = await fetchJson<Omit<ArticlesResult, "articles"> & { articles: Omit<Article, "cover">[] }>(
        `/api/articles?${usp.toString()}`,
      );
      return { ...data, articles: data.articles.map(withCover) };
    },
    staleTime: 60_000,
  };
}

export function useArticles(params: { themes?: string[]; q?: string; lang?: Lang } = {}) {
  return useQuery(articlesQueryOptions(params));
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
      return res.json() as Promise<{ reply: string; sources: string[] }>;
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
  confidence: number | null;
  reviewRequired: boolean;
  sourceUrl: string | null;
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
  isNew: boolean;
  detectedAt: string;
  sourceUrl: string | null;
}

export interface CompetitorTarget {
  id: string;
  companyId: string;
  companyName: string;
  companyNameEn: string | null;
  companyCategory: string;
  recordType: "TARGET" | "KPI";
  title: string | null;
  themes: string[];
  targetValue: string | number | null;
  baseYear: string | number | null;
  targetYear: string | number | null;
  scope: string | number | null;
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

export function competitorTargetsQueryOptions(params: { companyId?: string; theme?: string } = {}) {
  const usp = new URLSearchParams();
  if (params.companyId) usp.set("company_id", params.companyId);
  if (params.theme) usp.set("theme", params.theme);
  return {
    queryKey: ["competitorTargets", params.companyId ?? "", params.theme ?? ""] as const,
    queryFn: () => fetchJson<{ targets: CompetitorTarget[] }>(`/api/competitors/targets?${usp.toString()}`),
    staleTime: 60_000,
  };
}

export function useCompetitorTargets(params: { companyId?: string; theme?: string } = {}) {
  return useQuery(competitorTargetsQueryOptions(params));
}

export function competitorInitiativesQueryOptions(
  params: { companyId?: string; theme?: string; isNew?: boolean } = {},
) {
  const usp = new URLSearchParams();
  if (params.companyId) usp.set("company_id", params.companyId);
  if (params.theme) usp.set("theme", params.theme);
  if (params.isNew !== undefined) usp.set("is_new", String(params.isNew));
  return {
    queryKey: ["competitorInitiatives", params.companyId ?? "", params.theme ?? "",
      params.isNew ?? ""] as const,
    queryFn: () =>
      fetchJson<{ initiatives: CompetitorInitiative[] }>(`/api/competitors/initiatives?${usp.toString()}`),
    staleTime: 60_000,
  };
}

export function useCompetitorInitiatives(params: { companyId?: string; theme?: string; isNew?: boolean } = {}) {
  return useQuery(competitorInitiativesQueryOptions(params));
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
