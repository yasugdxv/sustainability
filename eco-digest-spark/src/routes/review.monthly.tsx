import { createFileRoute } from "@tanstack/react-router";
import { ReviewPage } from "@/components/review-page";

export const Route = createFileRoute("/review/monthly")({
  component: () => (
    <ReviewPage
      kind="monthly"
      title="競合月次レビュー"
      description="競合サステナビリティ月次レポートのドラフトは自動送信されません。内容を確認のうえ、承認して送信/却下してください。"
      emptyHint="該当する競合月次ドラフトがありません。先に python monthly_competitor_report.py build を実行してください。"
    />
  ),
});
