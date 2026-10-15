import { createFileRoute } from "@tanstack/react-router";
import { ReviewPage } from "@/components/review-page";

export const Route = createFileRoute("/review/daily")({
  component: () => (
    <ReviewPage
      kind="daily"
      title="競合アラート日次ダイジェスト"
      description="その日確定した変更イベントをまとめた日次ダイジェストです。確信度の高い日は自動配信済みのため参照のみ、それ以外は内容を確認のうえ承認して送信/却下してください。"
      emptyHint="該当する日次ダイジェストがありません。"
    />
  ),
});
