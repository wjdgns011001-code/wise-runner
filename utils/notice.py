"""
공지사항 저장소.

- 관리자가 작성하는 일반 공지를 data/notices.json 에 보관한다.
- 점수 기준 공지는 Score.criteria_lines() 로부터 항상 최신값을 생성하므로
  이 저장소에 넣지 않고, 표시 시점에 동적으로 합쳐 보여준다(아래 build_score_notice).
"""
import json
import os
import uuid
from datetime import datetime

from utils.score import Score

NOTICE_PATH = "data/notices.json"


def _ensure():
    os.makedirs(os.path.dirname(NOTICE_PATH), exist_ok=True)
    if not os.path.exists(NOTICE_PATH):
        with open(NOTICE_PATH, "w", encoding="utf-8") as f:
            json.dump([], f)


def _load() -> list:
    _ensure()
    with open(NOTICE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(items: list):
    with open(NOTICE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def list_notices() -> list:
    """작성일 내림차순(최신순) 공지 목록."""
    items = _load()
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


def add_notice(title: str, content: str) -> dict:
    meta = {
        "id": uuid.uuid4().hex,
        "title": (title or "").strip() or "(제목 없음)",
        "content": (content or "").strip(),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = _load()
    items.append(meta)
    _save(items)
    return meta


def delete_notice(notice_id: str) -> bool:
    items = _load()
    if not any(it.get("id") == notice_id for it in items):
        return False
    _save([it for it in items if it.get("id") != notice_id])
    return True


def build_score_notice() -> dict:
    """현재 점수 기준을 실제 로직(Score)에서 생성한 고정 공지."""
    body = (
        "🏃 **데일리 포인트** (사진 인증, 하루 1회 · 거리·시간 중 최고 1개)\n"
        + "\n".join(f"· {line}" for line in Score.daily_lines())
        + "\n\n📅 **주간 포인트** (한 주 누적 달성 · 최고 1개)\n"
        + "\n".join(f"· {line}" for line in Score.weekly_lines())
        + "\n\n🎽 **참가 포인트** (웹에서 직접 등록)\n"
        + "\n".join(f"· {line}" for line in Score.event_lines())
        + "\n\n🛍️ **러닝 상점** (점수로 구매)\n"
        + "\n".join(f"· {line}" for line in Score.shop_lines())
    )
    return {
        "id": "score-criteria",
        "title": "📊 점수 적립 기준 안내",
        "content": body,
        "pinned": True,
    }


APP_URL = "https://harmonica-cattishly-unpledged.ngrok-free.dev/manage"


def build_install_guide() -> dict:
    """앱(PWA) 설치 방법 안내 (안드로이드/아이폰)."""
    return {
        "title": "📱 앱 설치 방법",
        "app_url": APP_URL,
        "android": [
            "크롬(Chrome) 브라우저로 앱 주소에 접속합니다.",
            "우측 상단 ⋮ (점 3개) 메뉴를 누릅니다.",
            "‘앱 설치’ 또는 ‘홈 화면에 추가’를 누릅니다.",
            "‘설치’를 누르면 홈 화면에 와이즈러너스 아이콘이 생깁니다.",
        ],
        "ios": [
            "사파리(Safari) 브라우저로 앱 주소에 접속합니다. (크롬 말고 ‘사파리’여야 합니다)",
            "화면 하단 가운데 공유 버튼(□에 ↑ 화살표)을 누릅니다.",
            "메뉴를 내려 ‘홈 화면에 추가’를 누릅니다.",
            "오른쪽 위 ‘추가’를 누르면 홈 화면에 아이콘이 생깁니다.",
        ],
    }


def chatbot_text() -> str:
    """카카오 챗봇 '공지' 응답용 텍스트 (점수 기준 + 관리자 공지)."""
    parts = ["📢 **공지사항**\n"]

    score = build_score_notice()
    parts.append(f"📌 **{score['title']}**\n{score['content']}")

    for n in list_notices():
        parts.append(f"--- \n📌 **{n['title']}** ({n['created_at'][:10]})\n{n['content']}")

    return "\n\n".join(parts)
