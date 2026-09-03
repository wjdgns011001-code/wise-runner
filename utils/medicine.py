"""
복약(약 먹은 기록) 저장소.

- data/medicines.json — 회원별 복약 기록 (약 이름 · 날짜 · 시간 · 복용량 · 메모 · 복용여부)

러닝 점수 체계와는 무관한 개인 기록이며, 본인만 조회/수정/삭제할 수 있다.
"""
import json
import os
import uuid
from datetime import datetime

MEDICINES_PATH = "data/medicines.json"


def _load() -> list:
    os.makedirs(os.path.dirname(MEDICINES_PATH), exist_ok=True)
    if not os.path.exists(MEDICINES_PATH):
        return []
    try:
        with open(MEDICINES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list):
    os.makedirs(os.path.dirname(MEDICINES_PATH), exist_ok=True)
    with open(MEDICINES_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def add_medicine(user_id: str, name: str, dose: str = "", date: str = "",
                 time: str = "", memo: str = "", taken: bool = True) -> dict:
    """복약 기록 추가. name 은 필수, 나머지는 선택."""
    name = (name or "").strip()
    if not name:
        raise ValueError("약 이름은 필수입니다")
    now = datetime.now()
    meta = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "name": name,
        "dose": (dose or "").strip(),
        "date": (date or "").strip() or now.strftime("%Y-%m-%d"),
        "time": (time or "").strip() or now.strftime("%H:%M"),
        "memo": (memo or "").strip(),
        "taken": bool(taken),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = _load()
    items.append(meta)
    _save(items)
    return meta


def list_medicines(user_id: str = None) -> list:
    """복약 기록 목록 (날짜·시간 최신순). user_id 지정 시 그 사람 것만."""
    items = _load()
    if user_id:
        items = [m for m in items if m.get("user_id") == user_id]
    items.sort(key=lambda m: (m.get("date", ""), m.get("time", "")), reverse=True)
    return items


def get_medicine(mid: str):
    for m in _load():
        if m.get("id") == mid:
            return m
    return None


def update_medicine(mid: str, data: dict, owner_id: str = None):
    """복약 기록 수정. owner_id 를 주면 본인 기록만 수정된다. 반환: 수정된 기록 or None."""
    items = _load()
    target = None
    for m in items:
        if m.get("id") != mid:
            continue
        if owner_id is not None and m.get("user_id") != owner_id:
            return None
        for key in ("name", "dose", "date", "time", "memo"):
            if key in data:
                m[key] = (data.get(key) or "").strip()
        if "taken" in data:
            m["taken"] = bool(data["taken"])
        m["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        target = m
        break
    if target:
        _save(items)
    return target


def delete_medicine(mid: str, owner_id: str = None) -> bool:
    """복약 기록 삭제. owner_id 를 주면 본인 기록만 삭제된다."""
    items = _load()
    keep = [
        m for m in items
        if not (m.get("id") == mid and (owner_id is None or m.get("user_id") == owner_id))
    ]
    if len(keep) == len(items):
        return False
    _save(keep)
    return True


def toggle_taken(mid: str, owner_id: str = None):
    """복용 완료 ↔ 미복용 토글. 반환: 토글 후 상태 or None."""
    m = get_medicine(mid)
    if not m:
        return None
    updated = update_medicine(mid, {"taken": not m.get("taken", True)}, owner_id=owner_id)
    return None if updated is None else updated.get("taken")


def summary(user_id: str) -> dict:
    """오늘 복용/전체 건수 요약."""
    items = list_medicines(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    today_items = [m for m in items if m.get("date") == today]
    return {
        "total": len(items),
        "today": len(today_items),
        "today_taken": sum(1 for m in today_items if m.get("taken")),
        "today_left": sum(1 for m in today_items if not m.get("taken")),
    }
