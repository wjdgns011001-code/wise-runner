"""
러닝 피드 좋아요 저장소.

data/likes.json 구조: { "record_key": [liker_user_id, ...] }
 - record_key = "기록소유자_user_id|YYYY-MM-DD" (기록의 복합키)
"""
import json
import os

LIKES_PATH = "data/likes.json"


def _load() -> dict:
    if not os.path.exists(LIKES_PATH):
        return {}
    try:
        with open(LIKES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(LIKES_PATH), exist_ok=True)
    with open(LIKES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def toggle_like(record_key: str, liker_id: str):
    """좋아요 토글. 반환: (liked: bool, count: int)"""
    data = _load()
    likers = data.get(record_key, [])
    if liker_id in likers:
        likers.remove(liker_id)
        liked = False
    else:
        likers.append(liker_id)
        liked = True
    data[record_key] = likers
    _save(data)
    return liked, len(likers)


def like_count(record_key: str) -> int:
    return len(_load().get(record_key, []))


def liked_by(record_key: str, liker_id: str) -> bool:
    return liker_id in _load().get(record_key, [])


def all_likes() -> dict:
    return _load()
