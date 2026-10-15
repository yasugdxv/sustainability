import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

// 週次メールレビュー（旧Streamlitレビュー画面の移行先）用のAPIフック
const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8010";

export type ReviewStatus = "review_required" | "approved" | "rejected" | "sent";

export interface WeeklyReportSummary {
  id: string;
  subject: string;
  periodStart: string | null;
  periodEnd: string | null;
  reviewStatus: ReviewStatus;
  status: string | null;
  errorMessage: string | null;
  articleCount: number;
  reviewerId: string | null;
  sendStatus: string | null;
}

export interface WeeklyReportDetail extends WeeklyReportSummary {
  htmlBody: string;
  draftContent: Record<string, unknown>;
}

export interface ReviewPrincipal {
  ok: boolean;
  canApprove: boolean;
  userId?: string;
  displayName?: string;
  authSource?: string;
  error: string | null;
}

export const REVIEW_STATUS_LABELS: Record<ReviewStatus, string> = {
  review_required: "レビュー待ち",
  approved: "承認済み（送信待ち/再送可）",
  rejected: "却下",
  sent: "送信済み",
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API error ${res.status}`);
  }
  return res.json();
}

export function useReviewPrincipal() {
  return useQuery({
    queryKey: ["reviewPrincipal"] as const,
    queryFn: () => request<ReviewPrincipal>("/api/review/principal"),
    staleTime: 60_000,
  });
}

export function useWeeklyReports(status: ReviewStatus | "") {
  return useQuery({
    queryKey: ["weeklyReports", status] as const,
    queryFn: () =>
      request<{ reports: WeeklyReportSummary[] }>(`/api/weekly-reports?status=${encodeURIComponent(status)}`),
  });
}

export function useWeeklyReport(id: string | null) {
  return useQuery({
    queryKey: ["weeklyReport", id] as const,
    queryFn: () => request<WeeklyReportDetail>(`/api/weekly-reports/${id}`),
    enabled: !!id,
  });
}

export interface ReviewInput {
  id: string;
  status: "approved" | "rejected" | "review_required";
  freeText?: string;
  draftContent?: Record<string, unknown>;
}

export function useReviewWeeklyReport() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: ReviewInput) =>
      request<{ ok: boolean }>(`/api/weekly-reports/${input.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status: input.status,
          reviewerFeedback: { free_text: input.freeText || null },
          draftContent: input.draftContent,
        }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["weeklyReports"] });
      qc.invalidateQueries({ queryKey: ["weeklyReport"] });
    },
  });
}

// ─── ドラフト生成（旧CLI `weekly_email_report.py build` のボタン化） ───
export interface BuildJob {
  jobId: string;
  state: "running" | "done" | "error" | "lost";
  startedAt: number;
  startedBy?: string;
  error: string | null;
  reportId: string | null;
  log: string;
}

// 画面を開き直しても進行中/直近のジョブを追えるよう、最新のジョブを取得する。
// 実行中の間だけ3秒ごとに再取得し、完了したら一覧を更新する。
export function useLatestBuildJob() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["weeklyBuildJob"] as const,
    queryFn: () => request<{ job: BuildJob | null }>("/api/weekly-reports/build/latest"),
    refetchInterval: (q) => (q.state.data?.job?.state === "running" ? 3000 : false),
  });
  const state = query.data?.job?.state;
  const prev = useRef(state);
  useEffect(() => {
    if (prev.current === "running" && state === "done") qc.invalidateQueries({ queryKey: ["weeklyReports"] });
    prev.current = state;
  }, [state, qc]);
  return query;
}

export function useStartBuild() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { sinceDays?: number; forceGeoRerun?: boolean }) =>
      request<{ jobId: string }>("/api/weekly-reports/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["weeklyBuildJob"] }),
  });
}
