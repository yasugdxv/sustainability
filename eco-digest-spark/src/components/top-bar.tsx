import { Link } from "@tanstack/react-router";
import { Bell, ClipboardCheck, Settings } from "lucide-react";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { useLanguage } from "@/lib/i18n";
import { useCompetitorUnreadCount, type Lang } from "@/lib/api";

// 週次メール承認画面(sustainability_expert_dashboard.py、Streamlit)へのリンク。
// 別アプリ(別ポート)のため通常の<a>タグで新規タブに開く
const WEEKLY_REVIEW_URL = "http://localhost:8501";

function LanguageSwitcher() {
  const { lang, setLang, t } = useLanguage();
  const option = (value: Lang) => (
    <button
      onClick={() => setLang(value)}
      className={`px-2 py-1 rounded-full transition-colors ${
        lang === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {t(`lang.${value}`)}
    </button>
  );
  return (
    <div className="flex items-center gap-0.5 text-xs border border-border rounded-full p-0.5 bg-muted/30">
      {option("ja")}
      {option("en")}
    </div>
  );
}

export function TopBar({
  breadcrumb,
}: {
  breadcrumb?: { label: string; href?: string }[];
}) {
  const { t } = useLanguage();
  const { data: unreadData } = useCompetitorUnreadCount();
  const unreadCount = unreadData?.count ?? 0;
  return (
    <header className="sticky top-0 z-30 h-14 border-b border-border bg-background/85 backdrop-blur-md flex items-center gap-3 px-5">
      <SidebarTrigger className="-ml-1" />
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground min-w-0 flex-1">
        {breadcrumb?.map((b, i) => (
          <span key={i} className="flex items-center gap-1.5">
            {i > 0 && <span className="text-muted-foreground/40">/</span>}
            {b.href ? (
              <Link to={b.href} className="hover:text-foreground truncate">
                {b.label}
              </Link>
            ) : (
              <span className="text-foreground truncate">{b.label}</span>
            )}
          </span>
        ))}
      </div>

      <a
        href={WEEKLY_REVIEW_URL}
        target="_blank"
        rel="noreferrer"
        title={t("nav.weeklyApproval")}
        className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors"
      >
        <ClipboardCheck className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">{t("nav.weeklyApproval")}</span>
      </a>

      <LanguageSwitcher />

      <div className="flex items-center gap-1">
        <button className="h-9 w-9 rounded-full hover:bg-muted flex items-center justify-center relative">
          <Bell className="h-4 w-4" />
          {unreadCount > 0 && (
            <span className="absolute top-2 right-2 h-1.5 w-1.5 rounded-full bg-destructive" />
          )}
        </button>
        <button className="h-9 w-9 rounded-full hover:bg-muted flex items-center justify-center">
          <Settings className="h-4 w-4" />
        </button>
        <div className="ml-2 h-9 w-9 rounded-full bg-gradient-to-br from-primary to-accent-leaf flex items-center justify-center text-primary-foreground text-xs font-semibold">
          MK
        </div>
      </div>
    </header>
  );
}