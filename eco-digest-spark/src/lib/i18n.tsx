import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Lang } from "@/lib/api";

const LANG_KEY = "verdant_lang";

const STRINGS: Record<Lang, Record<string, string>> = {
  ja: {
    "nav.dashboard": "ダッシュボード",
    "nav.search": "検索",
    "nav.categories": "カテゴリ",
    "nav.sustainaAI": "サスティナAI",
    "nav.weeklyApproval": "週次メール承認",

    "sidebar.categories": "カテゴリ",
    "sidebar.competitorMonitoring": "競合モニタリング",
    "nav.competitorDashboard": "競合データダッシュボード",
    "nav.competitorOverview": "競合モニタリング概要",
    "nav.competitorChanges": "目標変更サマリー",
    "nav.competitorTargets": "目標比較",
    "nav.competitorInitiatives": "取組事例",
    "nav.competitorCompanies": "企業別",

    "competitor.overview.title": "競合モニタリング概要",
    "competitor.overview.subtitle": "競合{count}社のサステナビリティ目標・実績・取組を継続的に監視しています。",
    "competitor.overview.monitoredCompanies": "監視対象企業",
    "competitor.overview.updatedCompanies": "当月更新企業",
    "competitor.overview.targetChanges": "目標・KPI変更",
    "competitor.overview.actualUpdates": "実績更新",
    "competitor.overview.initiatives": "取組事例",
    "competitor.overview.reviewPending": "確認中",
    "competitor.overview.trendsTitle": "競合横断の注目動向",
    "competitor.overview.trendsEmpty": "今月分のデータがまだありません。",
    "competitor.overview.recentChangesTitle": "最新の目標変更",
    "competitor.overview.topInitiativesTitle": "今月の主な取組",
    "competitor.overview.noReport": "月次レポートがまだ生成されていません。",

    "competitor.changes.title": "目標変更サマリー",
    "competitor.changes.subtitle": "競合各社の目標・KPI・ESG評価の変更を一覧できます。",
    "competitor.changes.empty": "該当する変更はまだありません。",
    "competitor.changes.before": "変更前",
    "competitor.changes.after": "変更後",
    "competitor.changes.reviewBadge": "要確認",
    "competitor.changes.createdAt": "取得日",
    "competitor.changes.sourceUpdatedAt": "掲載日",
    "competitor.changes.sourceUpdatedAtUnknown": "掲載日不明",
    "competitor.changes.sortLabel": "並び替え",
    "competitor.changes.sort.createdAtDesc": "取得日が新しい順",
    "competitor.changes.sort.createdAtAsc": "取得日が古い順",
    "competitor.changes.sort.sourceUpdatedAtDesc": "掲載日が新しい順",
    "competitor.changes.sort.sourceUpdatedAtAsc": "掲載日が古い順",
    "competitor.changes.dateFrom": "期間(開始)",
    "competitor.changes.dateTo": "期間(終了)",

    "competitor.targets.title": "目標比較",
    "competitor.targets.subtitle": "競合各社の現行目標・KPIを一覧で比較できます。",
    "competitor.targets.empty": "該当する目標はまだありません。",
    "competitor.targets.company": "企業",
    "competitor.targets.theme": "テーマ",
    "competitor.targets.target": "目標",
    "competitor.targets.baseYear": "基準年",
    "competitor.targets.targetYear": "目標年",
    "competitor.targets.scope": "対象範囲",

    "competitor.initiatives.title": "取組事例",
    "competitor.initiatives.subtitle": "競合各社の新規・更新取組事例を一覧できます。",
    "competitor.initiatives.empty": "該当する取組事例はまだありません。",
    "competitor.initiatives.new": "新規",
    "competitor.initiatives.updated": "更新",

    "competitor.companies.title": "企業別",
    "competitor.companies.subtitle": "監視対象の競合企業一覧です。",
    "competitor.companies.empty": "監視対象企業がまだ登録されていません。",
    "competitor.companies.targetCount": "目標・KPI",
    "competitor.companies.initiativeCount": "取組事例",
    "competitor.companies.detailTargets": "目標・KPI",
    "competitor.companies.detailInitiatives": "取組事例",
    "competitor.companies.detailHistory": "変更履歴",
    "competitor.companies.detailSources": "情報源",
    "competitor.companies.noSources": "登録されている情報源はまだありません。",
    "competitor.companies.notFound": "企業が見つかりませんでした。",

    "competitor.filter.allThemes": "すべてのテーマ",
    "competitor.filter.allCompanies": "すべての企業",
    "competitor.readOriginal": "原典を見る",
    "competitor.viewCompany": "企業ページを見る",

    "competitor.recordType.TARGET": "目標",
    "competitor.recordType.KPI": "KPI指標",
    "competitor.recordType.ACTUAL": "実績",
    "competitor.recordType.ESG_RATING": "ESG評価",

    "competitor.changeType.NEW_TARGET": "新規設定",
    "competitor.changeType.SUCCESSOR_TARGET": "後継目標への切替",
    "competitor.changeType.SEPARATE_TARGET": "別目標を新設",
    "competitor.changeType.TARGET_WITHDRAWN": "撤回",
    "competitor.changeType.REMOVED_FROM_PAGE": "ページから削除",
    "competitor.changeType.SCOPE_EXPANDED": "対象範囲の拡大",
    "competitor.changeType.SCOPE_NARROWED": "対象範囲の縮小",
    "competitor.changeType.SUBSTANTIVE_CHANGE": "内容を実質的に変更",
    "competitor.changeType.WORDING_ONLY": "表現の変更のみ",
    "competitor.changeType.SIMPLE_REPUBLISH": "同一内容の再掲載",

    "competitor.direction.STRENGTHENED": "強化",
    "competitor.direction.WEAKENED": "後退",
    "competitor.direction.NEUTRAL": "中立",

    "csv.download": "CSVダウンロード",
    "csv.col.title": "タイトル",
    "csv.col.category": "カテゴリ",
    "csv.col.source": "情報源",
    "csv.col.sector": "業界",
    "csv.col.publishedAt": "公開日",
    "csv.col.importance": "重要度",
    "csv.col.url": "URL",
    "csv.col.tags": "タグ",
    "csv.col.company": "企業",
    "csv.col.theme": "テーマ",
    "csv.col.target": "目標",
    "csv.col.baseYear": "基準年",
    "csv.col.targetYear": "目標年",
    "csv.col.scope": "対象範囲",
    "csv.col.kpiDefinition": "KPI定義",
    "csv.col.achievementStatus": "達成状況",
    "csv.col.summary": "概要",
    "csv.col.status": "状態",
    "csv.col.detectedAt": "検知日",

    "sidebar.briefingTitle": "今日のブリーフィング",
    "sidebar.briefingText": "直近30日で重要記事 {important} 本、うち規制関連が {regulatory} 本。要約を確認しますか？",

    "lang.ja": "日本語",
    "lang.en": "English",

    "home.title": "今日のサスティナビリティ・インテリジェンス",
    "home.subtitle": "直近30日で収集・AI分析した記事 {count} 本を規制・企業動向・評価機関の視点でお届けします。",
    "home.empty": "直近30日の記事がまだありません。",
    "home.carouselTitle": "重要記事ピックアップ",
    "home.carouselSubtitle": "重要度ランクの高い記事",
    "home.categoriesTitle": "カテゴリで探す",
    "unit.articleCount": "{count} 本",

    "card.trending": "注目",

    "category.articleCount": "記事数",
    "category.trendingCount": "注目記事(S/A)",
    "category.avgImportance": "平均重要度",
    "category.listTitle": "{name} の記事一覧",
    "category.empty": "該当する記事がまだありません。",

    "sort.importance": "重要度順",
    "sort.latest": "新着順",
    "sort.trending": "トレンド",
    "sort.likes": "いいね順",
    "sort.reads": "読んだ順",

    "search.placeholder": "例:「EUのCSRD対応で日本企業が今すぐやるべきこと」",
    "search.submit": "AIで検索",
    "search.suggestionsLabel": "候補:",
    "search.filters": "絞り込み",
    "search.importance": "重要度",
    "search.importanceMin": "{value} 以上",
    "search.resultsLabel": "検索結果",
    "search.resultsWithQuery": "「{query}」に関連する {count} 本",
    "search.resultsCount": "{count} 本",
    "search.aiInterpretation": "AI 検索の解釈",
    "search.keywords": "抽出キーワード",
    "search.matchedThemes": "該当テーマ",
    "search.noKeywords": "この検索条件に一致するキーワード・テーマは抽出されませんでした。",
    "search.loading": "検索中...",
    "search.empty": "該当する記事が見つかりませんでした。",

    "chat.newConversation": "新しい会話",
    "chat.history": "会話履歴",
    "chat.historyEmpty": "まだ会話履歴はありません。",
    "chat.translating": "翻訳中...",
    "chat.footerDesc": "記事DBと当社公式コンテキストを横断分析。回答は根拠となった資料名を添えて表示されます。",
    "chat.headerSubtitle": "記事DB・当社公式コンテキスト連携",
    "chat.seedMessage": "こんにちは。サスティナビリティ・アナリストAIです。規制動向、企業事例、当社の方針との関係など、記事DB・当社公式コンテキストから根拠付きでお答えします。",
    "chat.inputPlaceholder": "規制、業界、当社の取り組みについて質問…",
    "chat.disclaimer": "AI回答は記事DB・当社公式コンテキストを根拠にしますが、最終判断は専門家に確認してください",
    "chat.fabLabel": "サスティナAIに聞く",
    "chat.thinking": "回答を作成中...",
    "chat.suggestion1": "当社の水に関する目標を教えて",
    "chat.suggestion2": "TNFD対応の優先度が高い業界を教えて",
    "chat.suggestion3": "今週のScope 3関連の重要トピックを要約",

    "article.backToDashboard": "ダッシュボードへ戻る",
    "article.readOriginal": "原文を読む",
    "article.aiSummary": "AI 要約",
    "article.sourceCategory": "情報源カテゴリ",
    "article.importanceScore": "重要度スコア",
    "article.importanceScoreValue": "{score} / 100（{level}ランク）",
    "article.noBody": "（本文取得なし）",
    "article.likes": "いいね",
    "article.read": "読んだ",
    "article.readDone": "読了済み",
    "article.saved": "保存済み",
    "article.saveForLater": "あとで読む",
    "article.askAI": "この記事について、自社への影響や次のアクションを分析させることができます。",
    "article.askAILink": "→ サスティナAIに質問する",
    "article.related": "関連記事",
  },
  en: {
    "nav.dashboard": "Dashboard",
    "nav.search": "Search",
    "nav.categories": "Categories",
    "nav.sustainaAI": "Sustaina AI",
    "nav.weeklyApproval": "Weekly Email Approval",

    "sidebar.categories": "Categories",
    "sidebar.competitorMonitoring": "Competitor Monitoring",
    "nav.competitorDashboard": "Competitor Data Dashboard",
    "nav.competitorOverview": "Monitoring Overview",
    "nav.competitorChanges": "Target Change Summary",
    "nav.competitorTargets": "Target Comparison",
    "nav.competitorInitiatives": "Initiatives",
    "nav.competitorCompanies": "By Company",

    "competitor.overview.title": "Competitor Monitoring Overview",
    "competitor.overview.subtitle": "Continuously tracking the sustainability targets, results, and initiatives of {count} competitors.",
    "competitor.overview.monitoredCompanies": "Monitored Companies",
    "competitor.overview.updatedCompanies": "Updated This Month",
    "competitor.overview.targetChanges": "Target/KPI Changes",
    "competitor.overview.actualUpdates": "Actual Updates",
    "competitor.overview.initiatives": "Initiatives",
    "competitor.overview.reviewPending": "Pending Review",
    "competitor.overview.trendsTitle": "Cross-Company Trends",
    "competitor.overview.trendsEmpty": "No data for this month yet.",
    "competitor.overview.recentChangesTitle": "Latest Target Changes",
    "competitor.overview.topInitiativesTitle": "This Month's Key Initiatives",
    "competitor.overview.noReport": "No monthly report has been generated yet.",

    "competitor.changes.title": "Target Change Summary",
    "competitor.changes.subtitle": "Browse target, KPI, and ESG rating changes across competitors.",
    "competitor.changes.empty": "No changes found.",
    "competitor.changes.before": "Before",
    "competitor.changes.after": "After",
    "competitor.changes.reviewBadge": "Needs Review",
    "competitor.changes.createdAt": "Captured",
    "competitor.changes.sourceUpdatedAt": "Published",
    "competitor.changes.sourceUpdatedAtUnknown": "Publish date unknown",
    "competitor.changes.sortLabel": "Sort by",
    "competitor.changes.sort.createdAtDesc": "Captured (newest first)",
    "competitor.changes.sort.createdAtAsc": "Captured (oldest first)",
    "competitor.changes.sort.sourceUpdatedAtDesc": "Published (newest first)",
    "competitor.changes.sort.sourceUpdatedAtAsc": "Published (oldest first)",
    "competitor.changes.dateFrom": "From",
    "competitor.changes.dateTo": "To",

    "competitor.targets.title": "Target Comparison",
    "competitor.targets.subtitle": "Compare current targets and KPIs across competitors.",
    "competitor.targets.empty": "No targets found.",
    "competitor.targets.company": "Company",
    "competitor.targets.theme": "Theme",
    "competitor.targets.target": "Target",
    "competitor.targets.baseYear": "Base Year",
    "competitor.targets.targetYear": "Target Year",
    "competitor.targets.scope": "Scope",

    "competitor.initiatives.title": "Initiatives",
    "competitor.initiatives.subtitle": "Browse new and updated competitor initiatives.",
    "competitor.initiatives.empty": "No initiatives found.",
    "competitor.initiatives.new": "New",
    "competitor.initiatives.updated": "Updated",

    "competitor.companies.title": "By Company",
    "competitor.companies.subtitle": "List of monitored competitors.",
    "competitor.companies.empty": "No monitored companies yet.",
    "competitor.companies.targetCount": "Targets/KPIs",
    "competitor.companies.initiativeCount": "Initiatives",
    "competitor.companies.detailTargets": "Targets & KPIs",
    "competitor.companies.detailInitiatives": "Initiatives",
    "competitor.companies.detailHistory": "Change History",
    "competitor.companies.detailSources": "Sources",
    "competitor.companies.noSources": "No sources registered yet.",
    "competitor.companies.notFound": "Company not found.",

    "competitor.filter.allThemes": "All Themes",
    "competitor.filter.allCompanies": "All Companies",
    "competitor.readOriginal": "View source",
    "competitor.viewCompany": "View company page",

    "competitor.recordType.TARGET": "Target",
    "competitor.recordType.KPI": "KPI",
    "competitor.recordType.ACTUAL": "Actual",
    "competitor.recordType.ESG_RATING": "ESG Rating",

    "competitor.changeType.NEW_TARGET": "New",
    "competitor.changeType.SUCCESSOR_TARGET": "Successor target",
    "competitor.changeType.SEPARATE_TARGET": "New separate target",
    "competitor.changeType.TARGET_WITHDRAWN": "Withdrawn",
    "competitor.changeType.REMOVED_FROM_PAGE": "Removed from page",
    "competitor.changeType.SCOPE_EXPANDED": "Scope expanded",
    "competitor.changeType.SCOPE_NARROWED": "Scope narrowed",
    "competitor.changeType.SUBSTANTIVE_CHANGE": "Substantive change",
    "competitor.changeType.WORDING_ONLY": "Wording only",
    "competitor.changeType.SIMPLE_REPUBLISH": "Republished",

    "competitor.direction.STRENGTHENED": "Strengthened",
    "competitor.direction.WEAKENED": "Weakened",
    "competitor.direction.NEUTRAL": "Neutral",

    "csv.download": "Download CSV",
    "csv.col.title": "Title",
    "csv.col.category": "Category",
    "csv.col.source": "Source",
    "csv.col.sector": "Sector",
    "csv.col.publishedAt": "Published At",
    "csv.col.importance": "Importance",
    "csv.col.url": "URL",
    "csv.col.tags": "Tags",
    "csv.col.company": "Company",
    "csv.col.theme": "Theme",
    "csv.col.target": "Target",
    "csv.col.baseYear": "Base Year",
    "csv.col.targetYear": "Target Year",
    "csv.col.scope": "Scope",
    "csv.col.kpiDefinition": "KPI Definition",
    "csv.col.achievementStatus": "Achievement Status",
    "csv.col.summary": "Summary",
    "csv.col.status": "Status",
    "csv.col.detectedAt": "Detected At",

    "sidebar.briefingTitle": "Today's Briefing",
    "sidebar.briefingText": "{important} important articles in the last 30 days, {regulatory} of them regulatory. Review the summary?",

    "lang.ja": "日本語",
    "lang.en": "English",

    "home.title": "Today's Sustainability Intelligence",
    "home.subtitle": "{count} articles collected and AI-analyzed over the last 30 days, from a regulatory, corporate-activity, and ratings-agency perspective.",
    "home.empty": "No articles in the last 30 days yet.",
    "home.carouselTitle": "Top Picks",
    "home.carouselSubtitle": "Articles with the highest importance rank",
    "home.categoriesTitle": "Browse by Category",
    "unit.articleCount": "{count}",

    "card.trending": "Trending",

    "category.articleCount": "Articles",
    "category.trendingCount": "Trending (S/A)",
    "category.avgImportance": "Avg. Importance",
    "category.listTitle": "Articles in {name}",
    "category.empty": "No articles yet.",

    "sort.importance": "Importance",
    "sort.latest": "Latest",
    "sort.trending": "Trending",
    "sort.likes": "Likes",
    "sort.reads": "Reads",

    "search.placeholder": "e.g. \"What Japanese companies should do now about EU CSRD\"",
    "search.submit": "AI Search",
    "search.suggestionsLabel": "Try:",
    "search.filters": "Filters",
    "search.importance": "Importance",
    "search.importanceMin": "{value}+",
    "search.resultsLabel": "Results",
    "search.resultsWithQuery": "{count} articles related to \"{query}\"",
    "search.resultsCount": "{count} articles",
    "search.aiInterpretation": "AI search interpretation",
    "search.keywords": "Keywords",
    "search.matchedThemes": "Matched themes",
    "search.noKeywords": "No keywords or themes could be extracted from this query.",
    "search.loading": "Searching...",
    "search.empty": "No matching articles found.",

    "chat.newConversation": "New conversation",
    "chat.history": "History",
    "chat.historyEmpty": "No conversations yet.",
    "chat.translating": "Translating...",
    "chat.footerDesc": "Analyzes across the article database and our official context. Answers are shown with their source documents.",
    "chat.headerSubtitle": "Connected to article DB & official context",
    "chat.seedMessage": "Hello, I'm the Sustainability Analyst AI. I can answer questions about regulatory trends, corporate case studies, and how they relate to our company's policies — grounded in the article database and our official context.",
    "chat.inputPlaceholder": "Ask about regulations, industries, or our initiatives…",
    "chat.disclaimer": "AI answers are grounded in the article DB and our official context, but please confirm final decisions with an expert",
    "chat.fabLabel": "Ask Sustaina AI",
    "chat.thinking": "Generating a response...",
    "chat.suggestion1": "What are our water-related targets?",
    "chat.suggestion2": "Which industries should prioritize TNFD alignment?",
    "chat.suggestion3": "Summarize this week's key Scope 3 topics",

    "article.backToDashboard": "Back to dashboard",
    "article.readOriginal": "Read original",
    "article.aiSummary": "AI Summary",
    "article.sourceCategory": "Source category",
    "article.importanceScore": "Importance score",
    "article.importanceScoreValue": "{score} / 100 (Rank {level})",
    "article.noBody": "(No article body available)",
    "article.likes": "Likes",
    "article.read": "Mark as read",
    "article.readDone": "Read",
    "article.saved": "Saved",
    "article.saveForLater": "Save for later",
    "article.askAI": "Ask the AI to analyze this article's impact on us and suggest next actions.",
    "article.askAILink": "→ Ask Sustaina AI",
    "article.related": "Related articles",
  },
};

const CATEGORY_DESCRIPTIONS: Record<Lang, Record<string, string>> = {
  ja: {
    水: "水資源の利用・涵養、水ストレス、TCFD/TNFD水関連開示に関する動向。",
    "気候変動・GHG": "GHG排出、パリ協定NDC、気候リスク、脱炭素技術に関する動向を追跡します。",
    容器包装: "容器包装の資源循環、拡大生産者責任、リサイクル素材に関するトピック。",
    原料調達: "原料サプライチェーンの持続可能性、トレーサビリティ、調達方針に関する動向。",
    生物多様性: "TNFD、自然資本、森林・海洋、生物多様性クレジットに関する動向。",
    人権: "人権デューデリジェンス、労働慣行、サプライチェーン人権対応に関するトピック。",
    健康: "responsible drinking、健康・ウェルネス、飲料業界の健康関連規制動向。",
    人的資本: "DEI、人材育成、従業員エンゲージメントに関する動向。",
    責任あるマーケティング: "責任あるマーケティング、広告規制、グリーンウォッシュ対応に関する動向。",
  },
  en: {
    水: "Water use and replenishment, water stress, and TCFD/TNFD water-related disclosure trends.",
    "気候変動・GHG": "GHG emissions, Paris Agreement NDCs, climate risk, and decarbonization technology.",
    容器包装: "Packaging circularity, extended producer responsibility, and recycled materials.",
    原料調達: "Raw-material supply chain sustainability, traceability, and sourcing policy.",
    生物多様性: "TNFD, natural capital, forests/oceans, and biodiversity credits.",
    人権: "Human rights due diligence, labor practices, and supply-chain human rights response.",
    健康: "Responsible drinking, health & wellness, and beverage-industry health regulation.",
    人的資本: "DEI, talent development, and employee engagement.",
    責任あるマーケティング: "Responsible marketing, advertising regulation, and greenwashing response.",
  },
};

export function categoryDescription(id: string, lang: Lang): string {
  return CATEGORY_DESCRIPTIONS[lang][id] ?? "";
}

// 競合モニタリングの9テーマ。api_server.pyのTHEME_METAと同じ日本語キー・英語ラベル。
const THEME_LABELS_EN: Record<string, string> = {
  水: "Water",
  "気候変動・GHG": "Climate & GHG",
  容器包装: "Packaging",
  原料調達: "Raw Materials",
  生物多様性: "Biodiversity",
  人権: "Human Rights",
  健康: "Health",
  人的資本: "Human Capital",
  責任あるマーケティング: "Responsible Marketing",
};

// 競合モニタリングの9テーマ一覧（フィルタ用セレクトボックス等で共通利用）
export const COMPETITOR_THEMES = Object.keys(THEME_LABELS_EN);

export function themeLabel(theme: string, lang: Lang): string {
  return lang === "en" ? THEME_LABELS_EN[theme] ?? theme : theme;
}

export function themeListLabel(themes: string[], lang: Lang): string {
  return themes.map((th) => themeLabel(th, lang)).join(lang === "en" ? ", " : "・");
}

// competitor_companiesのindustry_category（固定7分類）の英語ラベル。
const COMPANY_CATEGORY_LABELS_EN: Record<string, string> = {
  ビール: "Beer",
  蒸留酒: "Spirits",
  清涼飲料: "Soft Drinks",
  "日本・総合": "Japan / Diversified",
  ボトラー: "Bottler",
  FMCG: "FMCG",
  自社: "Own Company",
};

export function companyCategoryLabel(category: string, lang: Lang): string {
  return lang === "en" ? COMPANY_CATEGORY_LABELS_EN[category] ?? category : category;
}

export function companyDisplayName(name: string, nameEn: string | null | undefined, lang: Lang): string {
  return lang === "en" && nameEn ? nameEn : name;
}

export function recordTypeLabel(recordType: string, lang: Lang): string {
  return t(lang, `competitor.recordType.${recordType}`);
}

export function changeTypeLabel(changeType: string | null, lang: Lang): string {
  if (!changeType) return "";
  return t(lang, `competitor.changeType.${changeType}`);
}

export function directionLabel(direction: string | null, lang: Lang): string {
  if (!direction) return "";
  return t(lang, `competitor.direction.${direction}`);
}

export function chatSuggestions(lang: Lang): string[] {
  return [t(lang, "chat.suggestion1"), t(lang, "chat.suggestion2"), t(lang, "chat.suggestion3")];
}

export function searchSuggestions(lang: Lang): string[] {
  return lang === "en"
    ? [
        "Leading examples of TNFD alignment",
        "State-level laws replacing the US SEC rule",
        "Consumer goods companies succeeding on Scope 3 cuts",
      ]
    : ["TNFD対応の先進事例", "Scope 3 削減の取り組み", "容器包装のリサイクル規制"];
}

function format(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return Object.entries(vars).reduce(
    (acc, [k, v]) => acc.replaceAll(`{${k}}`, String(v)),
    template,
  );
}

function t(lang: Lang, key: string, vars?: Record<string, string | number>): string {
  const template = STRINGS[lang][key] ?? STRINGS.ja[key] ?? key;
  return format(template, vars);
}

interface LanguageContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

function readInitialLang(): Lang {
  try {
    const stored = localStorage.getItem(LANG_KEY);
    return stored === "en" ? "en" : "ja";
  } catch {
    return "ja";
  }
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readInitialLang);

  const setLang = (next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem(LANG_KEY, next);
    } catch {
      // localStorageが使えない環境では言語切り替えは今回のセッション内のみ有効
    }
  };

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const value = useMemo<LanguageContextValue>(
    () => ({ lang, setLang, t: (key, vars) => t(lang, key, vars) }),
    [lang],
  );

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage(): LanguageContextValue {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLanguage must be used within LanguageProvider");
  return ctx;
}
