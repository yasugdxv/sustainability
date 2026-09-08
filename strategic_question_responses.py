# -*- coding: utf-8 -*-
"""Weekly Strategic Question の回答収集ロジック。LLM/Azureクライアント非依存。
api_server.pyの回答用エンドポイントから直接呼ばれる。

回答者識別はopaqueトークン方式（メールアドレスをURL/ログに一切出さない）。
GETリクエストのみでは回答を記録しない（メールセキュリティ製品のリンク自動アクセス対策、
確認ページ表示→POST送信の2段階にする）設計は呼び出し側（api_server.py）が担保する。
"""
import hashlib
from datetime import datetime, timezone

import strategic_question_service as sqs


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def validate_token(client, question_id: str, raw_token: str, *, is_get: bool = False) -> dict:
    """戻り値: 成功時はdelivery行に"question"キーを足したdict。
    失敗時は {"error": "not_found"|"closed"|"invalid_token"}"""
    question = sqs.get_question(client, question_id)
    if question is None:
        return {"error": "not_found"}

    token_hash = _hash_token(raw_token)
    rows = client.select("sustainability_strategic_question_deliveries", {
        "select": "*", "question_id": f"eq.{question_id}", "response_token_hash": f"eq.{token_hash}",
    })
    if not rows:
        return {"error": "invalid_token"}
    delivery = rows[0]

    if question["question_status"] != "open":
        return {"error": "closed"}
    closes_at = question.get("closes_at")
    if closes_at and datetime.now(timezone.utc).isoformat() > closes_at:
        return {"error": "closed"}

    if is_get and not delivery.get("opened_response_at"):
        client.update("sustainability_strategic_question_deliveries",
                      {"delivery_id": f"eq.{delivery['delivery_id']}"},
                      {"opened_response_at": datetime.now(timezone.utc).isoformat()})

    delivery["question"] = question
    return delivery


def get_option_or_none(client, question_id: str, option_code: str) -> dict | None:
    rows = client.select("sustainability_strategic_question_options", {
        "select": "*", "question_id": f"eq.{question_id}", "option_code": f"eq.{option_code}",
    })
    return rows[0] if rows else None


def record_response(client, delivery: dict, option_id: str, comment: str = None) -> dict:
    """delivery["recipient_key"]をrespondent_keyとして使う。
    (question_id, respondent_key)への重複は上書き（回答変更）として扱う"""
    question_id = delivery["question_id"]
    respondent_key = delivery["recipient_key"]
    existing = client.select("sustainability_strategic_question_responses", {
        "select": "response_id", "question_id": f"eq.{question_id}", "respondent_key": f"eq.{respondent_key}",
    })
    if existing:
        client.update("sustainability_strategic_question_responses",
                      {"response_id": f"eq.{existing[0]['response_id']}"},
                      {"option_id": option_id, "comment": comment,
                       "updated_at": datetime.now(timezone.utc).isoformat()})
        response_id = existing[0]["response_id"]
    else:
        rows = client.insert("sustainability_strategic_question_responses", [{
            "question_id": question_id, "option_id": option_id,
            "delivery_id": delivery["delivery_id"], "respondent_key": respondent_key, "comment": comment,
        }], prefer="return=representation")
        response_id = rows[0]["response_id"]

    client.update("sustainability_strategic_question_deliveries",
                  {"delivery_id": f"eq.{delivery['delivery_id']}"},
                  {"responded_at": datetime.now(timezone.utc).isoformat()})
    return {"response_id": response_id}


def aggregate_results(client, question_id: str) -> dict:
    options = client.select("sustainability_strategic_question_options", {
        "select": "*", "question_id": f"eq.{question_id}", "order": "display_order.asc",
    })
    responses = client.select("sustainability_strategic_question_responses", {
        "select": "*", "question_id": f"eq.{question_id}",
    })
    delivery_count = len(client.select("sustainability_strategic_question_deliveries", {
        "select": "delivery_id", "question_id": f"eq.{question_id}", "send_status": "eq.success",
    }))

    total_responses = len(responses)
    counts_by_option = {}
    for r in responses:
        counts_by_option[r["option_id"]] = counts_by_option.get(r["option_id"], 0) + 1

    by_option = []
    for o in options:
        count = counts_by_option.get(o["option_id"], 0)
        pct = round(count / total_responses * 100, 1) if total_responses else 0.0
        by_option.append({"option_code": o["option_code"], "label": o["label"], "count": count, "pct": pct})

    comments = [r["comment"] for r in responses if r.get("comment")]
    response_rate = round(total_responses / delivery_count, 4) if delivery_count else 0.0

    return {"total_responses": total_responses, "delivery_count": delivery_count,
            "response_rate": response_rate, "by_option": by_option, "comments": comments}
