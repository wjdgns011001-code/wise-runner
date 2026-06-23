"""
이벤트(동호회/대회 참가)와 러닝 상점 구매 저장소.

- 이벤트:  data/events.json    — 사진으로 알 수 없는 참가 점수(수동 등록)
- 구매내역: data/purchases.json — 점수로 상품 구매(차감)
"""
import json
import os
import uuid
from datetime import datetime

from utils.score import Score

EVENTS_PATH = "data/events.json"
PURCHASES_PATH = "data/purchases.json"


def _load(path: str) -> list:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: str, items: list):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ──────────────── 이벤트 ────────────────

def add_event(user_id: str, event_type: str, date: str = "", ref: str = None) -> dict:
    """event_type 은 Score.EVENT_TYPES 의 key. ref: 출처 식별(예: 모임 id)."""
    if event_type not in Score.EVENT_TYPES:
        raise ValueError(f"알 수 없는 이벤트 종류: {event_type}")
    label, points = Score.EVENT_TYPES[event_type]
    meta = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "type": event_type,
        "label": label,
        "points": points,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ref": ref,
    }
    items = _load(EVENTS_PATH)
    items.append(meta)
    _save(EVENTS_PATH, items)
    return meta


def list_events(user_id: str = None) -> list:
    items = _load(EVENTS_PATH)
    if user_id:
        items = [e for e in items if e.get("user_id") == user_id]
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


def has_event_ref(user_id: str, ref: str) -> bool:
    """해당 사용자에게 ref(출처)로 등록된 이벤트가 있는지."""
    return any(e.get("user_id") == user_id and e.get("ref") == ref for e in _load(EVENTS_PATH))


def delete_events_by_ref(ref: str, user_id: str = None) -> int:
    """ref(출처)로 등록된 이벤트 삭제. user_id 지정 시 그 사용자만. 삭제 수 반환."""
    items = _load(EVENTS_PATH)
    keep = [
        e for e in items
        if not (e.get("ref") == ref and (user_id is None or e.get("user_id") == user_id))
    ]
    removed = len(items) - len(keep)
    if removed:
        _save(EVENTS_PATH, keep)
    return removed


def delete_event(event_id: str) -> bool:
    items = _load(EVENTS_PATH)
    if not any(e.get("id") == event_id for e in items):
        return False
    _save(EVENTS_PATH, [e for e in items if e.get("id") != event_id])
    return True


def events_points(user_id: str) -> int:
    return sum(e.get("points", 0) for e in _load(EVENTS_PATH) if e.get("user_id") == user_id)


# ──────────────── 상점 구매 ────────────────

def _shop_cost(item_name: str):
    for name, cost in Score.SHOP_ITEMS:
        if name == item_name:
            return cost
    return None


def add_purchase(user_id: str, item_name: str) -> dict:
    """구매 '신청' 생성 (status=pending). 포인트는 관리자가 완료할 때 차감됨."""
    cost = _shop_cost(item_name)
    if cost is None:
        raise ValueError(f"알 수 없는 상품: {item_name}")
    meta = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "item": item_name,
        "cost": cost,
        "status": "pending",          # pending(신청) → completed(구매완료)
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": None,
    }
    items = _load(PURCHASES_PATH)
    items.append(meta)
    _save(PURCHASES_PATH, items)
    return meta


def complete_purchase(purchase_id: str):
    """구매 신청을 완료 처리 (이때부터 포인트 차감 대상). 반환: 메타 or None."""
    items = _load(PURCHASES_PATH)
    target = None
    for p in items:
        if p.get("id") == purchase_id:
            p["status"] = "completed"
            p["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            target = p
            break
    if target:
        _save(PURCHASES_PATH, items)
    return target


def list_purchases(user_id: str = None) -> list:
    items = _load(PURCHASES_PATH)
    if user_id:
        items = [p for p in items if p.get("user_id") == user_id]
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


def purchases_cost(user_id: str) -> int:
    """완료(completed)된 구매만 차감 대상. (status 없는 기존 데이터는 완료로 간주)"""
    return sum(
        p.get("cost", 0)
        for p in _load(PURCHASES_PATH)
        if p.get("user_id") == user_id and p.get("status", "completed") == "completed"
    )


def get_purchase(purchase_id: str):
    for p in _load(PURCHASES_PATH):
        if p.get("id") == purchase_id:
            return p
    return None


def delete_purchase(purchase_id: str) -> bool:
    """구매 취소: 구매 내역을 삭제하면 사용 점수에서 자동 환원된다."""
    items = _load(PURCHASES_PATH)
    if not any(p.get("id") == purchase_id for p in items):
        return False
    _save(PURCHASES_PATH, [p for p in items if p.get("id") != purchase_id])
    return True
