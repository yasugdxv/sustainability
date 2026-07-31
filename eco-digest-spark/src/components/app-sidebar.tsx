import { Link, useNavigate, useRouterState } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import {
  Compass,
  Search,
  Sparkles,
  Leaf,
  LayoutDashboard,
  Building2,
  ChevronRight,
  ChevronDown,
} from "lucide-react";
import { categoryLabel, useArticles, useCategories } from "@/lib/api";
import { useLanguage } from "@/lib/i18n";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarFooter,
} from "@/components/ui/sidebar";

const COMPETITOR_DASHBOARD_BASE = "/competitors/overview";
const COMPETITOR_SUB_ROUTES = [
  { key: "nav.competitorOverview", url: "/competitors/overview" },
  { key: "nav.competitorChanges", url: "/competitors/changes" },
  { key: "nav.competitorTargets", url: "/competitors/targets" },
  { key: "nav.competitorInitiatives", url: "/competitors/initiatives" },
] as const;
const COMPETITOR_COMPANIES_URL = "/competitors/companies";
const COMPETITOR_EXPANDED_KEY = "verdant_competitor_menu_expanded";

export function AppSidebar() {
  const pathname = useRouterState({ select: (r) => r.location.pathname });
  const navigate = useNavigate();
  const isActive = (url: string) => pathname === url.split("?")[0];
  const { lang, t } = useLanguage();
  const primary = [
    { title: t("nav.dashboard"), url: "/", icon: Compass },
    { title: t("nav.search"), url: "/search", icon: Search },
  ];
  const { data: categories = [] } = useCategories();

  // 「競合データダッシュボード」配下のいずれかを表示中なら親を強制展開する。
  // それ以外は、ユーザーが手動で開閉した状態をセッション中(sessionStorage)維持する。
  const isOnCompetitorSubRoute = COMPETITOR_SUB_ROUTES.some((r) => pathname === r.url);
  const [manuallyExpanded, setManuallyExpanded] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem(COMPETITOR_EXPANDED_KEY) === "true";
    } catch {
      return false;
    }
  });
  const competitorExpanded = isOnCompetitorSubRoute || manuallyExpanded;

  useEffect(() => {
    try {
      sessionStorage.setItem(COMPETITOR_EXPANDED_KEY, String(manuallyExpanded));
    } catch {
      // sessionStorageが使えない環境では開閉状態はこのセッション内のみ有効
    }
  }, [manuallyExpanded]);
  // 件数計算にのみ使うため、表示言語に関わらず常に日本語タグ名で取得する
  const { data: articlesResult } = useArticles();
  const articles = articlesResult?.articles ?? [];
  const importantCount = articles.filter((a) => a.importanceLevel === "S" || a.importanceLevel === "A").length;
  const regulatoryCount = articles.filter((a) =>
    a.tags.some((t) => ["情報開示", "地政学・マクロ規制環境", "エンフォースメント・訴訟"].includes(t)),
  ).length;

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-4 pt-5 pb-3">
        <Link to="/" className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-sidebar-primary text-sidebar-primary-foreground">
            <Leaf className="h-4.5 w-4.5" strokeWidth={2.2} />
          </div>
          <div className="flex flex-col leading-tight group-data-[collapsible=icon]:hidden">
            <span className="text-editorial text-lg text-sidebar-foreground">
              Verdant
            </span>
            <span className="text-[10px] uppercase tracking-[0.18em] text-sidebar-foreground/60">
              Intelligence
            </span>
          </div>
        </Link>
      </SidebarHeader>

      <SidebarContent className="px-2">
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {primary.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild isActive={isActive(item.url)}>
                    <Link to={item.url}>
                      <item.icon />
                      <span>{item.title}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel className="text-[10px] uppercase tracking-[0.18em] text-sidebar-foreground/50">
            {t("sidebar.competitorMonitoring")}
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  onClick={() => navigate({ to: COMPETITOR_DASHBOARD_BASE })}
                  className={isOnCompetitorSubRoute ? "bg-sidebar-accent/40" : ""}
                >
                  <LayoutDashboard />
                  <span className="flex-1">{t("nav.competitorDashboard")}</span>
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation();
                      setManuallyExpanded((prev) => !prev);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.stopPropagation();
                        setManuallyExpanded((prev) => !prev);
                      }
                    }}
                    className="rounded p-0.5 -mr-1 hover:bg-sidebar-accent"
                  >
                    {competitorExpanded ? (
                      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-sidebar-foreground/50" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-sidebar-foreground/50" />
                    )}
                  </span>
                </SidebarMenuButton>
                {competitorExpanded && (
                  <SidebarMenuSub>
                    {COMPETITOR_SUB_ROUTES.map((r) => (
                      <SidebarMenuSubItem key={r.url}>
                        <SidebarMenuSubButton asChild isActive={pathname === r.url}>
                          <Link to={r.url}>
                            <span>{t(r.key)}</span>
                          </Link>
                        </SidebarMenuSubButton>
                      </SidebarMenuSubItem>
                    ))}
                  </SidebarMenuSub>
                )}
              </SidebarMenuItem>

              <SidebarMenuItem>
                <SidebarMenuButton asChild isActive={pathname.startsWith(COMPETITOR_COMPANIES_URL)}>
                  <Link to={COMPETITOR_COMPANIES_URL} className="flex items-center gap-2">
                    <Building2 />
                    <span>{t("nav.competitorCompanies")}</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel className="text-[10px] uppercase tracking-[0.18em] text-sidebar-foreground/50">
            {t("sidebar.categories")}
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {categories.map((c) => (
                <SidebarMenuItem key={c.id}>
                  <SidebarMenuButton
                    asChild
                    isActive={pathname === `/category/${c.id}`}
                  >
                    <Link
                      to="/category/$slug"
                      params={{ slug: c.id }}
                      className="flex items-center gap-2"
                    >
                      <span
                        className="h-2 w-2 rounded-full shrink-0"
                        style={{ backgroundColor: c.hue }}
                      />
                      <span>{categoryLabel(c, lang)}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="p-3 group-data-[collapsible=icon]:hidden">
        <div className="rounded-lg border border-sidebar-border/60 bg-sidebar-accent/40 p-3 text-xs text-sidebar-foreground/80">
          <div className="flex items-center gap-1.5 text-sidebar-primary font-medium">
            <Sparkles className="h-3.5 w-3.5" /> {t("sidebar.briefingTitle")}
          </div>
          <p className="mt-1.5 leading-relaxed text-sidebar-foreground/70">
            {t("sidebar.briefingText", { important: importantCount, regulatory: regulatoryCount })}
          </p>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}