import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

// 競合の日次ダイジェスト／月次レポートのレビュー（旧Streamlitレビュー画面の移行先）
import { API_BASE } from "@/lib/apiBase";
import { authFetch } from "@/lib/auth";

export type ReviewKind = "daily" | "monthly";
export type ReviewFilter = "pending" | "approved" | "rejected" | "sent";

export const REVIEW_FILTER_LABELS: Record<ReviewFilter, string> = {
  pending: "レビュー待ち",
  approved: "承認済み（送信待ち/再送可）",
  rejected: "却下",
  sent: "送信済み",
};

export interface ReviewItem {
  id: string;
  subject: string;
  dateLabel: string;
  status: string;
  rawStatus: string;
  auto: boolean; // 日次: 確信度が高く自動配信済み
  reviewable: boolean;
  meta: string;
  reviewerId: string | null;
  sendStatus: string | null;
  htmlBody?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, init); // SSOが有効なら、アクセストークンを付ける
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API error ${res.status}`);
  }
  return res.json();
}

export function useReviewItems(kind: ReviewKind, filter: ReviewFilter | "") {
  return useQuery({
    queryKey: ["reviewItems", kind, filter] as const,
    queryFn: () => request<{ items: ReviewItem[] }>(`/api/review/${kind}?filter=${filter}`),
  });
}

export function useReviewItem(kind: ReviewKind, id: string | null) {
  return useQuery({
    queryKey: ["reviewItem", kind, id] as const,
    queryFn: () => request<ReviewItem>(`/api/review/${kind}/${id}`),
    enabled: !!id,
  });
}

export function useReviewAction(kind: ReviewKind) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string; action: "approve" | "reject"; freeText?: string }) =>
      request<{ ok: boolean }>(`/api/review/${kind}/${input.id}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: input.action, freeText: input.freeText || null }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reviewItems", kind] });
      qc.invalidateQueries({ queryKey: ["reviewItem", kind] });
      qc.invalidateQueries({ queryKey: ["competitorUnreadCount"] });
    },
  });
}
