import { createFileRoute } from "@tanstack/react-router";
import { TopBar } from "@/components/top-bar";
import { useCompetitorCompany } from "@/lib/api";
import {
  changeTypeLabel,
  companyCategoryLabel,
  companyDisplayName,
  directionLabel,
  recordTypeLabel,
  themeLabel,
  themeListLabel,
  useLanguage,
} from "@/lib/i18n";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";

export const Route = createFileRoute("/competitors/companies/$companyId")({
  component: CompetitorCompanyDetailPage,
});

function CompetitorCompanyDetailPage() {
  const { companyId } = Route.useParams();
  const { lang, t } = useLanguage();
  const { data, isLoading } = useCompetitorCompany(companyId);

  return (
    <>
      <TopBar
        breadcrumb={[
          { label: t("nav.competitorCompanies"), href: "/competitors/companies" },
          { label: data ? companyDisplayName(data.name, data.nameEn, lang) : "" },
        ]}
      />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-8 py-8 space-y-6">
        {isLoading ? (
          <p className="text-muted-foreground">...</p>
        ) : !data ? (
          <p className="text-sm text-muted-foreground">{t("competitor.companies.notFound")}</p>
        ) : (
          <>
            <section className="border-b border-border pb-5">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                {companyCategoryLabel(data.category, lang)}
              </div>
              <h1 className="text-editorial text-4xl mt-1">{companyDisplayName(data.name, data.nameEn, lang)}</h1>
              {lang !== "en" && data.nameEn && <p className="text-sm text-muted-foreground mt-1">{data.nameEn}</p>}
            </section>

            <Tabs defaultValue="targets">
              <TabsList>
                <TabsTrigger value="targets">{t("competitor.companies.detailTargets")}</TabsTrigger>
                <TabsTrigger value="initiatives">{t("competitor.companies.detailInitiatives")}</TabsTrigger>
                <TabsTrigger value="history">{t("competitor.companies.detailHistory")}</TabsTrigger>
                <TabsTrigger value="sources">{t("competitor.companies.detailSources")}</TabsTrigger>
              </TabsList>

              <TabsContent value="targets">
                {data.targets.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4">{t("competitor.targets.empty")}</p>
                ) : (
                  <div className="card-paper rounded-lg mt-3">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>{t("competitor.targets.theme")}</TableHead>
                          <TableHead>{t("competitor.targets.target")}</TableHead>
                          <TableHead>{t("competitor.targets.baseYear")}</TableHead>
                          <TableHead>{t("competitor.targets.targetYear")}</TableHead>
                          <TableHead>{t("competitor.targets.scope")}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.targets.map((r) => (
                          <TableRow key={r.id}>
                            <TableCell>{themeListLabel(r.themes, lang) || "—"}</TableCell>
                            <TableCell className="max-w-sm truncate" title={r.title ?? ""}>
                              {r.title ?? String(r.targetValue ?? "—")}
                            </TableCell>
                            <TableCell>{r.baseYear ?? "—"}</TableCell>
                            <TableCell>{r.targetYear ?? "—"}</TableCell>
                            <TableCell className="max-w-xs truncate" title={String(r.scope ?? "")}>
                              {r.scope ?? "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </TabsContent>

              <TabsContent value="initiatives">
                {data.initiatives.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4">{t("competitor.initiatives.empty")}</p>
                ) : (
                  <div className="grid grid-cols-2 gap-4 mt-3">
                    {data.initiatives.map((ini) => (
                      <div key={ini.id} className="card-paper rounded-lg p-4">
                        <div className="flex items-center justify-between">
                          <span className="text-editorial text-lg">{ini.title}</span>
                          <Badge variant={ini.isNew ? "default" : "secondary"}>
                            {ini.isNew ? t("competitor.initiatives.new") : t("competitor.initiatives.updated")}
                          </Badge>
                        </div>
                        <p className="text-sm text-muted-foreground mt-2 leading-relaxed">{ini.summary}</p>
                        <div className="flex flex-wrap gap-1 mt-3">
                          {ini.themes.map((th) => (
                            <span key={th} className="text-[10px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
                              {themeLabel(th, lang)}
                            </span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </TabsContent>

              <TabsContent value="history">
                {data.changeHistory.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4">{t("competitor.changes.empty")}</p>
                ) : (
                  <div className="space-y-3 mt-3">
                    {data.changeHistory.map((c) => (
                      <div key={c.id} className="card-paper rounded-lg p-4">
                        <div className="flex items-center gap-2 flex-wrap">
                          <Badge variant="outline">{recordTypeLabel(c.recordType, lang)}</Badge>
                          {c.changeType && <Badge variant="secondary">{changeTypeLabel(c.changeType, lang)}</Badge>}
                          {c.direction && <Badge variant="outline">{directionLabel(c.direction, lang)}</Badge>}
                        </div>
                        <p className="text-sm mt-2 leading-relaxed">{c.summary}</p>
                      </div>
                    ))}
                  </div>
                )}
              </TabsContent>

              <TabsContent value="sources">
                {data.sources.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4">{t("competitor.companies.noSources")}</p>
                ) : (
                  <div className="space-y-2 mt-3">
                    {data.sources.map((s) => (
                      <a
                        key={s.id}
                        href={s.url}
                        target="_blank"
                        rel="noreferrer"
                        className="card-paper rounded-lg p-3 flex items-center justify-between text-sm hover:bg-sidebar-accent/20 transition-colors"
                      >
                        <span className="truncate">{s.url}</span>
                        <span className="text-[10px] uppercase tracking-wider text-muted-foreground shrink-0 ml-3">
                          {s.type}
                        </span>
                      </a>
                    ))}
                  </div>
                )}
              </TabsContent>
            </Tabs>
          </>
        )}
      </main>
    </>
  );
}
