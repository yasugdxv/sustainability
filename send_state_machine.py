"""
中央集権的な「承認→送信」状態遷移・排他制御ヘルパー（Decision 3 / Option B）。

背景:
weekly_email_report.py / competitor_daily_digest.py / monthly_competitor_report.py の
3モジュールが、それぞれ独自に「承認→送信」のガード処理を実装しており、二重送信や
既に確定した行への誤った再遷移を防ぐ一貫した排他制御が存在しなかった
（pmo-003: エラー状態のまま再送されてしまう、pmo-009: 同時クリックで二重送信される、
 pmo-010: 却下ガードが3モジュールでバラバラに再実装されている）。
本モジュールは、その3モジュールが共通で呼び出す「原子的なclaim（申告）」の
仕組みを提供する。review_status/statusの語彙自体（各テーブルでバラバラなことは
Decision 3のスコープ外）はここでは一切変更しない。

重要な設計制約（実DBのCHECK制約を直接確認した上での判断。今回のフェーズでは
DBスキーマ変更をしない、というNON-GOALのため）:
    weekly_email_reports.review_status /
    competitor_daily_alert_digests.review_status /
    monthly_reports.status
    はいずれも、sql/配下の既存マイグレーションで
        check (review_status in ('review_required','approved','rejected','sent'))
        check (status in ('DRAFT','GENERATED','UNDER_REVIEW','REVISION_REQUIRED',
                           'APPROVED','SENT','CANCELLED'))
    のようなCHECK制約が既にかかっており、'sending'のような新しい遷移状態の値を
    これらの列に追加することはできない（追加しようとすると実DBでは
    IntegrityErrorになる。ローカルのFakeSupabaseClientはCHECK制約を再現しない
    ため、この問題はテストでは表面化しない点に注意）。

    そのため、「今まさに送信処理中」という一時的な状態は、これらの列ではなく、
    全モジュールに既に存在し、かつCHECK制約の無い自由入力text列
    send_error_message（もともと直近の送信失敗メッセージを保持するための列）を
    再利用して持たせる。送信中はここに一時マーカー文字列を書き込み、送信完了後は
    （成功時はNone、失敗時は実際のエラーメッセージで）呼び出し側の既存の
    record_send_result()/同等のUPDATEが必ず上書きする。つまりこのモジュールの
    外から見たsend_error_messageの意味（「直近の送信エラー、なければNone」）は
    変わらない。

原子性の実現方法（compare-and-swap、条件付きUPDATE）:
    guarded_transition()は、呼び出し側が直前にDBから読んだ「現在の値」をそのまま
    WHERE条件に使い、`UPDATE ... WHERE <条件> ...`という条件付きUPDATEを実行する。
    article_crawler.SupabaseClient.update()にこのフェーズで追加した
    prefer="return=representation" を使うことで、実際に更新された行の一覧を
    返させ、0件だった＝直前の読み取り後に他の遷移と競合していた、と判別できる
    ようにする。PostgRESTへのUPDATEはPostgres側の行ロックにより直列化されるため、
    同時に2つの承認リクエストが来ても、一方だけがこの条件付きUPDATEに成功する
    （もう一方は0件になり、conflictとして検知できる）。
"""
from datetime import datetime, timedelta, timezone

# クラッシュ・タイムアウト等でsend_error_messageに送信中マーカーが残ったまま
# プロセスが停止した場合に、何分経過後から「放棄されたclaim」とみなして
# 再claimを許可するか（無期限にロックされたままになるのを防ぐ）
STALE_CLAIM_TIMEOUT = timedelta(minutes=15)

_MARKER_PREFIX = "__SENDING__:"


class TransitionConflict(Exception):
    """要求した条件付き状態遷移が、対象行が期待した現在値ではなかったために
    適用できなかったこと（＝他の遷移と競合したこと）を表す"""


def _make_marker(now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{_MARKER_PREFIX}{now.isoformat()}"


def _marker_age(value) -> timedelta:
    """valueが送信中マーカーなら、そのマーカーが書き込まれてからの経過時間を返す。
    マーカーでない（Noneや実際のエラーメッセージ）場合はNone"""
    if not isinstance(value, str) or not value.startswith(_MARKER_PREFIX):
        return None
    try:
        marked_at = datetime.fromisoformat(value[len(_MARKER_PREFIX):])
    except ValueError:
        return None
    if marked_at.tzinfo is None:
        marked_at = marked_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - marked_at


def is_fresh_in_flight_marker(value) -> bool:
    """現在も有効な（STALE_CLAIM_TIMEOUTを超えていない）送信中マーカーかどうか"""
    age = _marker_age(value)
    return age is not None and age < STALE_CLAIM_TIMEOUT


def _value_filter(value) -> str:
    """Python値からPostgRESTのeq./is.nullフィルタ文字列を作る
    （Noneは "eq.None" ではなく "is.null" にしないと実DBで一致しないため）"""
    return "is.null" if value is None else f"eq.{value}"


def guarded_transition(client, table: str, id_field: str, id_value, guards: dict,
                        patch: dict) -> list:
    """id_field=id_value に加え、guards（列名→PostgRESTフィルタ文字列。
    'eq.x' / 'is.null' / 'in.(a,b)' / 'not.in.(a,b)' 等）の条件をすべて満たす
    行だけを対象に、条件付きUPDATEでpatchを適用する。

    戻り値: 実際に更新された行のリスト（更新後の内容）。空リストなら、
    呼び出し側が条件を読んだ後に他の遷移・状態変化と競合していたことを意味し、
    呼び出し側は処理を中止すべき（＝二重送信・不整合な遷移の防止）"""
    params = {id_field: f"eq.{id_value}", **guards}
    updated = client.update(table, params, patch, prefer="return=representation")
    return updated or []


class SendStateMachine:
    """1テーブル分の「承認→送信」の排他制御をまとめるヘルパー。
    review_status/statusという列名や値の語彙自体は各モジュール側が指定する
    （このクラスは特定のテーブルの語彙を知らない、汎用の仕組み）"""

    def __init__(self, client, table: str, id_field: str, guard_field: str = "send_error_message"):
        self.client = client
        self.table = table
        self.id_field = id_field
        self.guard_field = guard_field
        # 監査ログ（成功・失敗いずれの遷移試行も記録する）。呼び出し側が
        # DBの監査テーブルにも残したければ、この内容をそのまま渡せる
        self.audit_log = []

    def _record(self, id_value, action: str, ok: bool, detail=None) -> None:
        self.audit_log.append({
            "id": id_value, "action": action, "ok": ok, "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
        })

    def claim_for_sending(self, row: dict, *, status_field: str, blocked_statuses,
                           extra_patch: dict = None) -> dict:
        """rowは呼び出し側が直前にDBから読んだ行そのもの（send_error_message等の
        「現在値」をこの行から読み取り、CASの条件に使う）。

        以下のいずれかならclaim失敗（send_email()等を一切呼ばずに即座に返す）:
          - 現在send_error_messageに「新鮮な（放棄されていない）送信中マーカー」が
            入っている＝他のリクエストが今まさに処理中
          - status_field（review_status/status）が既にblocked_statuses
            （'rejected'/'sent'やCANCELLED/SENT等）のいずれかである
          - 上記チェック後の条件付きUPDATE自体が0件だった＝チェックした直後に
            他の遷移と競合した（レースコンディション）

        成功時はsend_error_messageに送信中マーカーを書き込んだ後の行を返す。
        呼び出し側はこの後、実際の送信を行い、成功なら send_error_message=None・
        review_statusを終端値へ、失敗なら実際のエラーメッセージへ、既存の
        record_send_result() 等（本モジュールの外）で必ず上書きする"""
        id_value = row[self.id_field]
        current_guard_value = row.get(self.guard_field)

        if is_fresh_in_flight_marker(current_guard_value):
            self._record(id_value, "claim_for_sending", False, "in_flight")
            return {"ok": False, "error": "既に送信処理中です（他のリクエストが処理中の可能性があります）"}

        if row.get(status_field) in blocked_statuses:
            self._record(id_value, "claim_for_sending", False, "blocked_status")
            return {"ok": False, "error": "この状態からは送信できません"}

        guards = {
            self.guard_field: _value_filter(current_guard_value),
            status_field: "not.in.(" + ",".join(blocked_statuses) + ")",
        }
        patch = {self.guard_field: _make_marker()}
        if extra_patch:
            patch.update(extra_patch)

        updated = guarded_transition(self.client, self.table, self.id_field, id_value, guards, patch)
        ok = bool(updated)
        self._record(id_value, "claim_for_sending", ok, None if ok else "conflict")
        if not ok:
            return {"ok": False, "error": "他のリクエストと競合したため送信を中止しました（二重送信防止）"}
        return {"ok": True, "row": updated[0]}

    def guard_reject(self, row: dict, *, status_field: str, blocked_statuses,
                      patch: dict) -> dict:
        """却下操作用の条件付きUPDATE。既にblocked_statuses（通常は「送信済み」）
        になっている行を誤って却下扱いにしてしまわないようにする
        （3モジュールでバラバラだった却下ガードの共通化。pmo-010）"""
        id_value = row[self.id_field]
        if row.get(status_field) in blocked_statuses:
            self._record(id_value, "guard_reject", False, "blocked_status")
            return {"ok": False, "error": "この状態からは却下できません"}

        guards = {status_field: "not.in.(" + ",".join(blocked_statuses) + ")"}
        updated = guarded_transition(self.client, self.table, self.id_field, id_value, guards, patch)
        ok = bool(updated)
        self._record(id_value, "guard_reject", ok, None if ok else "conflict")
        if not ok:
            return {"ok": False, "error": "他のリクエストと競合したため却下を中止しました"}
        return {"ok": True, "row": updated[0]}
