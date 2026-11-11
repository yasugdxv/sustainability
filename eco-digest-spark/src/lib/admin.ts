import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

// 競合クロール候補管理・戦略質問/組織判断ナレッジ（旧Streamlit画面の移行先）
import { API_BASE } from "@/lib/apiBase";
import { authFetch } from "@/lib/auth";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, init); // SSOが有効なら、アクセストークンを付ける
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API error ${res.status}`);
  }
  return res.json();
}

// ─── 競合クロール候補 ───
export interface SourceCandidate {
  id: string;
  url: string;
  pageTitle: string | null;
  urlType: string;
  isActive: boolean;
  isCrawlable: boolean;
  discoveredFromSourceId: string | null;
  lastCheckedAt: string | null;
}

export type CandidateStatus = "unconfirmed" | "active" | "all";

export function useCandidates(status: CandidateStatus) {
  return useQuery({
    queryKey: ["competitorCandidates", status] as const,
    queryFn: () =>
      request<{ urlTypes: string[]; candidates: SourceCandidate[] }>(`/api/competitor-candidates?status=${status}`),
  });
}

export function useCandidateAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string; action: "activate" | "deactivate"; urlType?: string }) =>
      request<{ ok: boolean }>(`/api/competitor-candidates/${input.id}/${input.action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: input.action === "activate" ? JSON.stringify({ urlType: input.urlType }) : undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["competitorCandidates"] }),
  });
}

// ─── 戦略質問・組織判断ナレッジ（閲覧のみ） ───
export interface StrategicQuestion {
  id: string;
  status: string;
  periodStart: string | null;
  theme: string | null;
  title: string | null;
  questionText: string | null;
  dimensionLabel: string | null;
  dimensionKey: string | null;
  insightStatus: string | null;
  insightError: string | null;
  options: { code: string; label: string; description: string | null; isStatusQuo: boolean }[];
  analysis: unknown;
  candidates: unknown;
  aggregate: { totalResponses: number; deliveryCount: number; responseRate: number } | null;
}

export interface DecisionInsight {
  id: string;
  theme: string | null;
  dimensionLabel: string | null;
  observedAt: string | null;
  responseCount: number | null;
  deliveryCount: number | null;
  confidence: string | number | null;
  guardrailLabel: string | null;
  observedTendency: string | null;
  distribution: unknown;
  reasoningSummary: string | null;
  representativeReasoning: string[];
  publishable: boolean;
}

export function useStrategicQuestions() {
  return useQuery({
    queryKey: ["strategicQuestions"] as const,
    queryFn: () => request<{ questions: StrategicQuestion[] }>("/api/strategic-questions"),
  });
}

export function useDecisionInsights() {
  return useQuery({
    queryKey: ["decisionInsights"] as const,
    queryFn: () => request<{ insights: DecisionInsight[] }>("/api/decision-insights"),
  });
}
