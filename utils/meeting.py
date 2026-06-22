"""
동호회 정기모임 저장소.

data/meetings.json: [
  { id, title, date, place, content, author_id, created_at,
    attendees: [user_id...], photos: [filename...] }
]
모임 사진은 data/meetings/ 에 저장하고 /meeting/photo/<filename> 로 서빙한다.
"""
import json
import os
import re
import uuid
from datetime import datetime

MEETINGS_PATH = "data/meetings.json"
PHOTO_DIR = "data/meetings"


def _load() -> list:
    if not os.path.exists(MEETINGS_PATH):
        return []
    try:
        with open(MEETINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list):
    os.makedirs(os.path.dirname(MEETINGS_PATH), exist_ok=True)
    with open(MEETINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def list_meetings() -> list:
    """모임 날짜 내림차순(최신/예정 우선)."""
    items = _load()
    items.sort(key=lambda m: m.get("date", ""), reverse=True)
    return items


def get_meeting(mid: str):
    for m in _load():
        if m.get("id") == mid:
            return m
    return None


def add_meeting(author_id: str, title: str, date: str, place: str = "", content: str = "") -> dict:
    meta = {
        "id": uuid.uuid4().hex,
        "title": (title or "").strip() or "(제목 없음)",
        "date": date,
        "place": (place or "").strip(),
        "content": (content or "").strip(),
        "author_id": author_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "attendees": [],
        "photos": [],
    }
    items = _load()
    items.append(meta)
    _save(items)
    return meta


def toggle_attend(mid: str, user_id: str):
    """참석 토글. 반환: (참석중 여부, 참석 인원수)."""
    items = _load()
    for m in items:
        if m.get("id") == mid:
            att = m.setdefault("attendees", [])
            if user_id in att:
                att.remove(user_id)
                attending = False
            else:
                att.append(user_id)
                attending = True
            _save(items)
            return attending, len(att)
    return False, 0


def save_photo(mid: str, original_name: str, content: bytes):
    """모임 단체 사진 저장 후 모임에 연결."""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    ext = os.path.splitext(os.path.basename(original_name or ""))[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        ext = ".jpg"
    fname = uuid.uuid4().hex + ext
    with open(os.path.join(PHOTO_DIR, fname), "wb") as f:
        f.write(content)
    items = _load()
    for m in items:
        if m.get("id") == mid:
            m.setdefault("photos", []).append(fname)
            _save(items)
            break
    return fname


def photo_path(filename: str):
    """경로 탈출 방지하여 실제 파일 경로 반환 (없으면 None)."""
    name = os.path.basename(filename or "")
    p = os.path.join(PHOTO_DIR, name)
    return p if os.path.exists(p) else None


def delete_meeting(mid: str) -> bool:
    items = _load()
    target = next((m for m in items if m.get("id") == mid), None)
    if not target:
        return False
    for fn in target.get("photos", []):
        try:
            p = os.path.join(PHOTO_DIR, os.path.basename(fn))
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    _save([m for m in items if m.get("id") != mid])
    return True
