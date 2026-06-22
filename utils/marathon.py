"""
전국 마라톤 대회 일정 크롤러.

출처: roadrun.co.kr 마라톤 일정 (마라톤온라인 등에서도 임베드해 쓰는 사실상 표준 소스)
 - 목록(list.php)에서 대회 번호를 수집하고
 - 상세(view.php?no=)에서 대회명/일시/장소/접수기간을 파싱한다.

하루 1회만 크롤링하도록 data/marathons.json 에 캐시하며,
페이지 접속 시 캐시 날짜가 오늘이 아니면 새로 크롤링한다.
"""
import json
import os
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

LIST_URL = "http://www.roadrun.co.kr/schedule/list.php"
DETAIL_URL = "http://www.roadrun.co.kr/schedule/view.php?no={no}"
CACHE_PATH = "data/marathons.json"
UA = {"User-Agent": "Mozilla/5.0"}


# 서울/경기(수도권) 판별 키워드. 장소명에 도시·구·랜드마크가 들어가는 경우가 많아 키워드 매칭.
# 주의: '남산'은 경주 남산('서남산주차장')과 충돌하므로 제외, '한강'은 남양주한강공원 등과 충돌해 제외.
SEOUL_KEYWORDS = [
    "서울", "여의도", "뚝섬", "상암", "월드컵공원", "잠실", "석촌", "올림픽공원", "올림픽 공원",
    "반포", "잠원", "난지", "망원", "연세대", "고려대", "한양대", "북서울", "청계산",
    "강북", "강남", "강동", "강서", "관악", "광진", "구로", "금천", "노원", "도봉", "동대문",
    "동작", "마포", "서대문", "서초", "성동", "성북", "송파", "양천", "영등포", "용산", "은평",
    "종로", "중랑",
]
GYEONGGI_KEYWORDS = [
    "경기도", "수원", "성남", "의정부", "안양", "부천", "광명", "평택", "동두천", "안산", "고양",
    "과천", "구리", "남양주", "오산", "시흥", "군포", "의왕", "하남", "용인", "파주", "이천",
    "안성", "김포", "화성", "양주", "포천", "여주", "연천", "가평", "양평", "일산", "킨텍스",
    "미사", "분당", "판교", "동탄",
]
METRO_KEYWORDS = SEOUL_KEYWORDS + GYEONGGI_KEYWORDS


def is_metro(place: str) -> bool:
    """장소가 서울/경기(수도권)로 추정되면 True."""
    p = place or ""
    return any(k in p for k in METRO_KEYWORDS)


def _fetch(url: str) -> str:
    """EUC-KR 페이지를 받아 유니코드 문자열로 디코딩."""
    r = requests.get(url, timeout=10, headers=UA)
    r.raise_for_status()
    return r.content.decode("euc-kr", errors="replace")


def _norm_date(s: str) -> str:
    """'2026년6월5일', '2026-6-5' 등에서 'YYYY-MM-DD' 추출."""
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def _parse_detail(no: str):
    """상세 페이지에서 한 대회 정보를 파싱. 실패 시 None."""
    try:
        html = _fetch(DETAIL_URL.format(no=no))
    except Exception:
        return None

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    def grab(label: str, nxt: str) -> str:
        m = re.search(label + r"\s*(.*?)\s*" + nxt, text)
        return m.group(1).strip() if m else ""

    name = grab("대회명", "대표자명")
    when = grab("대회일시", "전화번호")   # 예: '2026년6월5일 출발시간:21시'
    place = grab("대회장소", "주최단체")
    reg = grab("접수기간", "홈페이지")     # 예: '2026년2월3일~2026년4월30일'
    home = grab("홈페이지", "대회장").replace(" ", "")

    reg_parts = reg.split("~") if reg else []
    return {
        "no": no,
        "name": name,
        "date": _norm_date(when),
        "when": when,
        "place": place,
        "reg_start": _norm_date(reg_parts[0]) if len(reg_parts) > 0 else "",
        "reg_end": _norm_date(reg_parts[1]) if len(reg_parts) > 1 else "",
        "homepage": home if home.startswith("http") else "",
    }


def _crawl() -> list:
    """목록 → 상세 동시 크롤링 후, 다가오는 대회만 날짜 오름차순으로 반환."""
    list_html = _fetch(LIST_URL)
    nos = list(dict.fromkeys(re.findall(r"view\.php\?no=(\d+)", list_html)))

    with ThreadPoolExecutor(max_workers=10) as ex:
        parsed = list(ex.map(_parse_detail, nos))

    events = [e for e in parsed if e and e["name"] and e["date"]]

    today = datetime.now().strftime("%Y-%m-%d")
    upcoming = [e for e in events if e["date"] >= today]
    upcoming.sort(key=lambda x: x["date"])
    return upcoming


def get_marathons(force: bool = False) -> dict:
    """
    캐시된 마라톤 일정을 반환한다.
    - 캐시가 오늘 날짜면 그대로 반환(빠름)
    - 아니면 새로 크롤링하여 저장 후 반환(하루 1회)
    - force=True 면 캐시 무시하고 강제 갱신
    반환: {"updated": "YYYY-MM-DD", "events": [...], (옵션) "error": str}
    """
    today = datetime.now().strftime("%Y-%m-%d")

    if not force and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("updated") == today:
                return cache
        except Exception:
            pass

    try:
        events = _crawl()
        cache = {"updated": today, "events": events}
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        return cache
    except Exception as e:
        # 크롤 실패 시 이전 캐시라도 보여준다
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    old = json.load(f)
                old["error"] = f"갱신 실패(이전 데이터 표시): {e}"
                return old
            except Exception:
                pass
        return {"updated": today, "events": [], "error": str(e)}
