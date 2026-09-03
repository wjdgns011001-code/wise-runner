from urllib3 import response
import uvicorn
import json
import yaml
import os
import requests  # 이미지 다운로드가 필요할 경우 사용
from pathlib import Path
import jinja2
import hashlib
import base64
import uuid
import asyncio
import re
from urllib.parse import quote
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from datetime import datetime

from settings import db_handler
from utils.connect import Connect, Record
from settings.db_handler import Database
from utils import marathon
from utils import filestore
from utils import notice
from utils import rewards
from utils import social
from utils import push
from utils import meeting
from utils import medicine
from utils.score import Score


# ──────────────── User Mapping (user_id ↔ 사용자명) ────────────────
USER_MAPPING_PATH = "data/user_mapping.json"

def _load_user_mapping() -> dict:
    """user_mapping.json 파일 로드"""
    if not os.path.exists(USER_MAPPING_PATH):
        # 파일이 없으면 빈 dict 생성
        os.makedirs(os.path.dirname(USER_MAPPING_PATH), exist_ok=True)
        with open(USER_MAPPING_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)
        return {}
    with open(USER_MAPPING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_user_mapping(mapping: dict):
    """user_mapping.json 파일 저장"""
    with open(USER_MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

def get_user_name(user_id: str) -> str:
    """user_id에 해당하는 사용자명 반환 (없으면 빈 문자열)"""
    mapping = _load_user_mapping()
    if user_id not in mapping:
        return ""  # 매핑에 없는 user_id -> 미등록
    name = mapping.get(user_id, "")
    if name == "__pending__":
        return ""  # 이름 입력 대기 중 -> 미등록
    if name == "":
        # 매핑에는 있지만 이름이 비어있는 경우 (기존 데이터)
        return "이름 미등록 사용자"  # 기본 표시명 (id 노출 안 함)
    return name

def is_pending_user(user_id: str) -> bool:
    """아직 이름을 입력하지 않은 대기 상태인 사용자인지 확인"""
    mapping = _load_user_mapping()
    return mapping.get(user_id) == "__pending__"

def set_pending_user(user_id: str):
    """사용자를 이름 입력 대기 상태로 설정"""
    mapping = _load_user_mapping()
    mapping[user_id] = "__pending__"
    _save_user_mapping(mapping)

def register_user_name(user_id: str, user_name: str):
    """사용자명 저장"""
    mapping = _load_user_mapping()
    mapping[user_id] = user_name
    _save_user_mapping(mapping)


def _kakao_response(msg) -> JSONResponse:
    """카카오톡 simpleText 응답 규격으로 감싸 반환."""
    response_body = {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": msg}}]},
    }
    return JSONResponse(content=response_body, headers={"ngrok-skip-browser-warning": "69420"})


config = Connect.from_yaml("config.yaml")
connect = Connect(ip=config.ip, port=config.port, model_name=config.model_name, api_token=config.api_token)
db_handler = Database()

# 사진 분석(VL 호출)은 느린 동기 작업 → 스레드로 분리하고 동시 처리 개수를 제한한다.
# (이벤트 루프를 막지 않아 텍스트 명령/다른 사용자는 즉시 처리됨)
VL_MAX_CONCURRENCY = 4
_vl_semaphore = asyncio.Semaphore(VL_MAX_CONCURRENCY)
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
template_dir = os.path.join(os.path.dirname(__file__), "templates")


# ──────────────── 관리자 인증 ────────────────
ADMIN_COOKIE = "admin_token"
_ADMIN_SALT = "running-tracker-admin"  # 쿠키 토큰용 고정 솔트


def _load_admin_password() -> str:
    """config.yaml 의 admin.password 로드 (없으면 기본값)."""
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get("admin") or {}).get("password") or "admin"
    except Exception:
        return "admin"


def _admin_token() -> str:
    """현재 관리자 비밀번호로부터 쿠키에 저장할 토큰 생성."""
    pw = _load_admin_password()
    return hashlib.sha256((_ADMIN_SALT + pw).encode("utf-8")).hexdigest()


def is_admin(request: Request) -> bool:
    """요청의 쿠키가 유효한 관리자 토큰인지 확인."""
    return request.cookies.get(ADMIN_COOKIE) == _admin_token()

# Jinja2 직접 사용 (Jinja2Templates 생략 - 3.1.x 캐시 버그 회피)
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(template_dir),
    auto_reload=False,
    cache_size=0,
)

def render_template(name: str, **context) -> str:
    """Jinja2 템플릿 렌더링 헬퍼"""
    template = jinja_env.get_template(name)
    # get_user_name 함수를 템플릿 컨텍스트에 항상 포함
    if "get_user_name" not in context:
        context["get_user_name"] = get_user_name
    return template.render(**context)



async def get_message(user_id: int, user_message: str) -> str:
    if "점수" in user_message or "포인트" in user_message:
        b = db_handler.get_points_breakdown(user_id=user_id)
        score_notice = notice.build_score_notice()
        msg = (
            f"💎 **회원님의 점수 현황**\n\n"
            f"🏃 데일리 포인트: {b['daily']:,}점\n"
            f"📅 주간 보너스: {b['weekly']:,}점\n"
            f"🎽 참가 포인트: {b['events']:,}점\n"
            f"--- \n"
            f"✨ 총 획득: {b['earned']:,}점\n"
            f"🛍️ 사용(구매): -{b['spent']:,}점\n"
            f"💰 **사용 가능 점수: {b['available']:,}점**\n\n"
            f"--- \n"
            f"📊 **점수 적립 기준**\n"
            f"{score_notice['content']}"
        )
        return msg
    if "기록" in user_message:
        user_info = db_handler.get_user_stats(user_id=user_id)
        msg = (
            f"🏆 **전체 러닝 리포트**\n\n"
            f"🏃‍♂️ **총 러닝 횟수:** {user_info.total_runs}회\n"
            f"📍 **총 누적 거리:** {user_info.total_distance:.2f} km\n"
            f"⏱️ **총 누적 시간:** {user_info.total_time}\n"
            f"💎 **총 획득 포인트:** {user_info.total_point:,} P\n"
            f"--- \n"
            f"🔥 **개인 최고 기록**\n"
            f"🛣️ **최장 거리:** {user_info.best_distance}/km\n"
            f"⚡ **최고 케이던스:** {user_info.best_cadence} spm\n"
            f"🍎 **최대 소모 칼로리:** {user_info.best_calories} kcal\n"
        )
        return msg
    if "순위" in user_message:
        msg = db_handler.get_user_rank(user_id=user_id)
        return msg
    if "공지" in user_message:
        return notice.chatbot_text()
    if "help" in user_message.lower() or "사용법" in user_message:
        msg = (
            "📖 **러닝 트래커 이용 가이드**\n\n"
            "1️⃣ **기록 저장 (사진 전송) 📸**\n"
            " - 러닝 결과 사진을 보내면 AI가 분석 후 저장합니다.\n"
            " - **하루에 1회**만 기록되니 유의해 주세요!\n\n"
            '2️⃣ **나의 순위 확인 ("점수") 🎁**\n'
            " - '점수'라고 입력하면 내 랭킹을 보여드려요.\n\n"
            '3️⃣ **상세 통계 확인 ("기록") 📊**\n'
            " - '기록'이라고 입력하면 누적 데이터를 보여드려요.\n\n"
            '4️⃣ **나의 순위 확인 ("순위") 🏆**\n'
            " - **'순위'**라고 입력하면 전체 사용자 중 내 위치를 알려드려요.\n"
            " - 점수를 기준으로 나의 랭킹을 확인할 수 있습니다.\n\n"
            '5️⃣ **공지 확인 ("공지") 📢**\n'
            " - '공지'라고 입력하면 점수 기준 등 공지사항을 보여드려요.\n\n"
            "--- \n"
            "오늘도 활기차게 달려보세요! 🔥"
        )
        return msg
    else:
        msg = (
            "❓ **무엇을 도와드릴까요?**\n\n"
            "러닝 기록 관리 방법이 궁금하시다면\n"
            "아래 키워드 중 하나를 입력해 주세요!\n\n"
            "👉 **'사용법'** 또는 **'help'** 입력\n\n"
            "--- \n"
            "📸 사진을 보내시면 바로 기록 분석을 시작합니다!"
        )
        return msg


async def handle_interaction(user_id: str, utterance: str = "", image_url: str = None) -> str:
    """
    카카오/웹 공용 대화 처리. (user_id, 입력 텍스트, 이미지 URL) -> 응답 메시지.
    이미지 URL 은 http(s) 주소 또는 base64 data URI 모두 가능.
    """
    utterance = utterance or ""

    # ── 새 사용자 이름 등록 처리 ──
    if is_pending_user(user_id) and not image_url:
        user_name = utterance.strip()
        if user_name:
            register_user_name(user_id, user_name)
            return f"✅ 반갑습니다, {user_name}님! 🎉\n\n이제 사진을 보내주시면 러닝 기록을 분석해 드립니다.\n📸 사진을 보내주세요!"
        return "😅 이름을 다시 입력해 주세요!"

    if image_url:
        # ── 사진 업로드 처리 ──
        if not get_user_name(user_id):
            if not is_pending_user(user_id):
                set_pending_user(user_id)
            return (
                "👋 **처음 뵙겠습니다!**\n\n"
                "러닝 기록을 저장하기 위해 먼저\n"
                "**이름**을 입력해 주세요! 😊\n\n"
                "예시: `홍길동`"
            )
        try:
            # VL 호출을 스레드로 분리(이벤트 루프 비점유) + 동시 개수 제한
            async with _vl_semaphore:
                record = await asyncio.to_thread(connect, image_url, user_id)
            try:
                if isinstance(record, Record):
                    msg = (
                        f"✅ 러닝 기록 추출 결과\n\n"
                        f"📅 날짜: {record.date}\n"
                        f"⏱️ 총 시간: {record.total_time}\n"
                        f"🏃 총 거리: {record.total_distance}km\n"
                        f"⚡ 평균 페이스: {record.average_pace if record.average_pace else '-'}/km\n"
                        f"👣 평균 케이던스: {record.cadence if record.cadence else '-'}\n"
                        f"💓 평균 심박수: {record.average_heart_rate if record.average_heart_rate else '-'}\n"
                        f"🔥 총 칼로리: {record.total_calories if record.total_calories else '-'}\n"
                        f"--------------------------\n"
                        f"🎁 획득 점수: {record.score}점\n"
                    )
                    _, days_diff, is_first, is_dup = db_handler.upsert(record)

                    if is_dup:
                        # 완전히 동일한 기록 → 저장하지 않음
                        return msg + "ℹ️ 이미 동일한 기록이 등록되어 있어 추가하지 않았어요."

                    # 새 기록 → 올린 사람 제외 나머지 구독자에게 피드 알림
                    others = [u for u in push.subscribed_user_ids() if u != user_id]
                    push.notify(
                        others,
                        "🏃 새 러닝 기록",
                        f"{get_user_name(user_id) or '누군가'}님이 {record.total_distance}km 기록을 올렸어요!",
                        url="/manage",
                    )

                    if is_first:
                        msg += "🎉 달리기 시작한 첫날 기록을 축하드립니다!"
                    elif days_diff == 0:
                        msg += "➕ 오늘 추가 기록을 등록했어요! (하루에도 여러 번 기록할 수 있어요)"
                    elif days_diff > 0:
                        msg += f"🔥 무려 {days_diff}일 만에 다시 달리셨네요! 환영합니다."
                    else:
                        msg += f"🗓️ 과거({record.date}) 기록을 추가했습니다."
                    return msg
                return f"{record}"
            except Exception:
                return "데이터 분석에 실패하였습니다."
        except Exception:
            return "system error"

    return await get_message(user_id=user_id, user_message=utterance)


@app.post("/")
async def set_point(request: Request):
    # 카카오로부터 전달받은 전체 JSON 데이터
    payload = await request.json()
    user_request = payload.get("userRequest", {})
    utterance = user_request.get("utterance", "")
    media = user_request.get("params", {}).get("media", {})
    print(f"유저 입력 값 : {utterance}", flush=True)

    image_url = None
    if media and media.get("type") == "image":
        image_url = media.get("url")
    elif utterance.startswith("http"):
        image_url = utterance

    user_id = user_request.get("user", {}).get("id")
    msg = await handle_interaction(user_id, utterance, image_url)
    return _kakao_response(msg)


# ──────────────── 관리도구 페이지 ────────────────

@app.get("/manage", response_class=HTMLResponse)
async def manage_feed(request: Request, page: int = 1):
    """메인 화면: 러닝 피드 (전체 기록 + 좋아요, 15개씩 페이징). '나'는 쿠키로 식별."""
    users = _registered_users()
    name_of = {u["user_id"]: u["name"] for u in users}
    me = get_me(request)
    me_user = next((u for u in users if u["user_id"] == me), None)

    all_recs = sorted(db_handler._load_all(), key=lambda x: x.get("date", ""), reverse=True)
    page_recs, page, total_pages = _paginate(all_recs, page)
    feed = []
    for r in page_recs:
        key = f"{r['user_id']}|{r['date']}"
        feed.append({
            "owner_name": name_of.get(r["user_id"], "익명"),
            "is_me": bool(me) and r["user_id"] == me,
            "date": r["date"],
            "distance": r.get("total_distance"),
            "time": r.get("total_time"),
            "score": r.get("score"),
            "key": key,
            "likes": social.like_count(key),
            "liked": social.liked_by(key, me) if me else False,
        })

    # 내 이름 선택 시: 주간 보너스 격려 토스트
    encourage = None
    if me_user:
        wk_km, wk_min = db_handler.get_week_progress(me)
        cur_bonus, dist_goal, time_goal = Score.weekly_next_goals(wk_km, wk_min)
        encourage = _build_encourage(wk_km, wk_min, cur_bonus, dist_goal, time_goal)

    html = render_template(
        "manage/feed.html",
        active="feed", users=users, feed=feed, me=me, me_user=me_user, encourage=encourage,
        page=page, total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.get("/manage/dashboard", response_class=HTMLResponse)
async def manage_dashboard(request: Request, page: int = 1, msg: str = ""):
    """대시보드 페이지 (사용자 랭킹 15명씩 페이징)"""
    all_records = db_handler._load_all()
    
    # 전체 통계
    user_ids = list(set(r["user_id"] for r in all_records))
    total_distance = sum(r["total_distance"] for r in all_records if r.get("total_distance"))
    total_score = sum(r["score"] for r in all_records if r.get("score"))
    total_calories = sum(r["total_calories"] for r in all_records if r.get("total_calories"))
    total_records = len(all_records)
    avg_distance = round(total_distance / total_records, 2) if total_records else 0
    
    stats = {
        "total_users": len(user_ids),
        "total_records": total_records,
        "total_distance": round(total_distance, 2),
        "total_score": total_score,
        "total_calories": total_calories,
        "avg_distance": avg_distance,
    }
    
    # 사용자별 통계 (점수 내림차순, 15명씩 페이징)
    user_stats_list = []
    for uid in user_ids:
        s = db_handler.get_user_stats(uid)
        user_stats_list.append(s)
    user_stats_list.sort(key=lambda x: x.total_point, reverse=True)

    per_page = 15
    total = len(user_stats_list)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    rankings = user_stats_list[start:start + per_page]

    html = render_template(
        "manage/dashboard.html",
        active="dashboard", stats=stats, rankings=rankings,
        page=page, total_pages=total_pages, start_rank=start,
        is_admin=is_admin(request), members=_registered_users(),
        msg=msg,
    )
    return HTMLResponse(content=html)


@app.post("/manage/members/add")
async def manage_members_add(request: Request, name: str = Form(...)):
    """신규 회원 등록 (관리자 전용) — 이름으로 회원 생성."""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?next=/manage/dashboard&error=관리자만+등록할+수+있습니다", status_code=303)
    name = (name or "").strip()
    if not name:
        return RedirectResponse(url="/manage/dashboard?msg=이름을+입력하세요", status_code=303)
    register_user_name("web_" + uuid.uuid4().hex, name)
    return RedirectResponse(url=f"/manage/dashboard?msg={name}+회원이+등록되었습니다", status_code=303)



@app.get("/manage/user/{user_id}", response_class=HTMLResponse)
async def manage_user_detail(request: Request, user_id: str, page: int = 1, wpage: int = 1, msg: str = ""):
    """개별 사용자 상세: 통계 + 점수 획득 내역(데일리/주간/참가/구매)"""
    stats = db_handler.get_user_stats(user_id)
    breakdown = db_handler.get_points_breakdown(user_id)
    recent = db_handler.get_recent_stats(user_id)
    all_records = db_handler.get_user_records(user_id)  # 최신순 Record 리스트
    all_weekly = [w for w in db_handler.get_weekly_breakdown(all_records) if w["bonus"] > 0]
    events = rewards.list_events(user_id)
    purchases = rewards.list_purchases(user_id)
    record_count = len(all_records)
    weekly_count = len(all_weekly)
    records, page, total_pages = _paginate(all_records, page)
    weekly, wpage, w_total_pages = _paginate(all_weekly, wpage)

    html = render_template(
        "manage/user_detail.html",
        active="dashboard",
        user_id=user_id,
        user_name=get_user_name(user_id) or "(이름 미등록)",
        is_owner=(get_me(request) == user_id),
        msg=msg,
        stats=stats,
        breakdown=breakdown,
        recent=recent,
        records=records,
        record_count=record_count,
        weekly=weekly,
        weekly_count=weekly_count,
        events=events,
        purchases=purchases,
        page=page,
        total_pages=total_pages,
        wpage=wpage,
        w_total_pages=w_total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/records/delete/{rid}")
async def manage_records_delete(request: Request, rid: str):
    """개인 기록 삭제 (본인만)."""
    me = get_me(request)
    rec = db_handler.get_record(rid)
    if not rec:
        return RedirectResponse(url="/manage/dashboard?msg=기록을+찾을+수+없습니다", status_code=303)
    if not me or me != rec.get("user_id"):
        return RedirectResponse(url=f"/manage/user/{rec.get('user_id')}?msg=본인만+삭제할+수+있어요", status_code=303)
    db_handler.delete_record(rid, owner_id=me)
    return RedirectResponse(url=f"/manage/user/{me}?msg=기록이+삭제되었습니다", status_code=303)


@app.get("/manage/records/edit/{rid}", response_class=HTMLResponse)
async def manage_records_edit_form(request: Request, rid: str):
    """개인 기록 수정 폼 (본인만)."""
    me = get_me(request)
    rec = db_handler.get_record(rid)
    if not rec:
        return HTMLResponse("<p>기록을 찾을 수 없습니다.</p>", status_code=404)
    if not me or me != rec.get("user_id"):
        return RedirectResponse(url=f"/manage/user/{rec.get('user_id')}?msg=본인만+수정할+수+있어요", status_code=303)
    html = render_template("manage/record_edit.html", active="dashboard", r=rec)
    return HTMLResponse(content=html)


@app.post("/manage/records/edit/{rid}")
async def manage_records_edit(request: Request, rid: str,
                              date: str = Form(...), total_distance: float = Form(...),
                              total_time: str = Form(...), average_pace: str = Form(""),
                              cadence: str = Form(""), average_heart_rate: str = Form(""),
                              total_calories: str = Form("")):
    """개인 기록 수정 저장 (본인만). 점수 자동 재계산."""
    me = get_me(request)
    rec = db_handler.get_record(rid)
    if not rec:
        return RedirectResponse(url="/manage/dashboard?msg=기록을+찾을+수+없습니다", status_code=303)
    if not me or me != rec.get("user_id"):
        return RedirectResponse(url=f"/manage/user/{rec.get('user_id')}?msg=본인만+수정할+수+있어요", status_code=303)

    def _int_or_none(v):
        v = (v or "").strip()
        return int(v) if v.isdigit() else None

    db_handler.update_record(rid, {
        "date": date,
        "total_distance": total_distance,
        "total_time": total_time,
        "average_pace": (average_pace or "").strip() or None,
        "cadence": _int_or_none(cadence),
        "average_heart_rate": _int_or_none(average_heart_rate),
        "total_calories": _int_or_none(total_calories),
    }, owner_id=me)
    return RedirectResponse(url=f"/manage/user/{me}?msg=기록이+수정되었습니다", status_code=303)


@app.get("/manage/purchases", response_class=HTMLResponse)
async def manage_purchases(request: Request, msg: str = "", page: int = 1):
    """물품 구매 내역 (관리자 취소 가능)"""
    name_of = {u["user_id"]: u["name"] for u in _registered_users()}
    purchases = rewards.list_purchases()
    for p in purchases:
        p["user_name"] = name_of.get(p.get("user_id"), "(이름 미등록)")
    total_spent = sum(p.get("cost", 0) for p in purchases if p.get("status", "completed") == "completed")
    pending_cnt = sum(1 for p in purchases if p.get("status") == "pending")
    purchases, page, total_pages = _paginate(purchases, page)
    html = render_template(
        "manage/purchases.html",
        active="purchases",
        purchases=purchases,
        total_spent=total_spent,
        pending_cnt=pending_cnt,
        msg=msg,
        is_admin=is_admin(request),
        page=page,
        total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/purchases/complete/{purchase_id}")
async def manage_purchases_complete(request: Request, purchase_id: str):
    """구매 신청 완료 처리 (관리자 전용) — 이때 포인트 차감."""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?next=/manage/purchases&error=관리자만+처리할+수+있습니다", status_code=303)
    meta = rewards.get_purchase(purchase_id)
    if not meta:
        return RedirectResponse(url="/manage/purchases?msg=내역을+찾을+수+없습니다", status_code=303)
    if meta.get("status") == "completed":
        return RedirectResponse(url="/manage/purchases?msg=이미+완료된+구매입니다", status_code=303)
    # 완료 시점에 보유 점수(획득-기완료) 확인
    b = db_handler.get_points_breakdown(meta["user_id"])
    if b["available"] < meta.get("cost", 0):
        return RedirectResponse(url=f"/manage/purchases?msg=포인트+부족으로+완료할+수+없습니다(보유+{b['available']:,})", status_code=303)
    rewards.complete_purchase(purchase_id)
    return RedirectResponse(url=f"/manage/purchases?msg={meta.get('item','구매')}+완료(-{meta.get('cost',0):,}점)", status_code=303)


@app.post("/manage/purchases/cancel/{purchase_id}")
async def manage_purchases_cancel(request: Request, purchase_id: str):
    """구매 취소 (관리자 전용) — 사용 점수 자동 환원"""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?next=/manage/purchases&error=관리자만+취소할+수+있습니다", status_code=303)
    meta = rewards.get_purchase(purchase_id)
    ok = rewards.delete_purchase(purchase_id)
    if ok and meta:
        msg = f"{meta.get('item','구매')}+취소+완료+(+{meta.get('cost',0):,}점+환원)"
    else:
        msg = "취소할+내역을+찾을+수+없습니다"
    return RedirectResponse(url=f"/manage/purchases?msg={msg}", status_code=303)


@app.get("/manage/marathons", response_class=HTMLResponse)
async def manage_marathons(request: Request, refresh: int = 0, region: str = "", sort: str = "", page: int = 1):
    """전국 마라톤 대회 일정 (하루 1회 크롤링 캐시). region=metro 면 서울/경기만. sort=status/dday."""
    data = marathon.get_marathons(force=bool(refresh))
    today = datetime.now().strftime("%Y-%m-%d")
    events = data.get("events", [])

    total_count = len(events)
    if region == "metro":
        events = [e for e in events if marathon.is_metro(e.get("place", ""))]

    # 접수 상태 및 D-day 계산
    for e in events:
        rs, re_end = e.get("reg_start", ""), e.get("reg_end", "")
        if rs and today < rs:
            e["reg_status"] = "접수예정"
        elif re_end and today > re_end:
            e["reg_status"] = "접수마감"
        elif rs or re_end:
            e["reg_status"] = "접수중"
        else:
            e["reg_status"] = ""

        try:
            d = (datetime.strptime(e["date"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
            e["dday"] = "D-DAY" if d == 0 else f"D-{d}"
        except Exception:
            e["dday"] = ""

    # 정렬 (컬럼 헤더 화살표 기준). 기본은 접수상태 오름차순 = 마감 아래로.
    status_rank = {"접수중": 0, "접수예정": 1, "": 2, "접수마감": 3}
    if sort == "dday_asc":
        events.sort(key=lambda e: e["date"])
    elif sort == "dday_desc":
        events.sort(key=lambda e: e["date"], reverse=True)
    elif sort == "status_desc":
        events.sort(key=lambda e: (-status_rank.get(e["reg_status"], 2), e["date"]))
    else:
        sort = "status_asc"
        events.sort(key=lambda e: (status_rank.get(e["reg_status"], 2), e["date"]))

    shown_count = len(events)
    events, page, total_pages = _paginate(events, page)
    page_extra = f"&sort={sort}" + (f"&region={region}" if region else "")

    html = render_template(
        "manage/marathons.html",
        active="marathons",
        events=events,
        updated=data.get("updated", ""),
        error=data.get("error"),
        region=region,
        sort=sort,
        total_count=total_count,
        shown_count=shown_count,
        page=page,
        total_pages=total_pages,
        page_extra=page_extra,
    )
    return HTMLResponse(content=html)


@app.get("/manage/login", response_class=HTMLResponse)
async def manage_login(request: Request, error: str = "", next: str = "/manage/files"):
    """관리자 로그인 폼"""
    html = render_template("manage/login.html", active="", error=error, next=next)
    return HTMLResponse(content=html)


@app.post("/manage/login")
async def manage_login_submit(password: str = Form(...), next: str = Form("/manage/files")):
    """관리자 로그인 처리"""
    if password == _load_admin_password():
        resp = RedirectResponse(url=next or "/manage/files", status_code=303)
        resp.set_cookie(
            ADMIN_COOKIE, _admin_token(),
            httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7,
        )
        return resp
    return RedirectResponse(url="/manage/login?error=비밀번호가+올바르지+않습니다", status_code=303)


@app.get("/manage/logout")
async def manage_logout():
    """관리자 로그아웃"""
    resp = RedirectResponse(url="/manage/files", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@app.get("/manage/files", response_class=HTMLResponse)
async def manage_files(request: Request, msg: str = "", page: int = 1):
    """자료실: 문서 목록 + 업로드 폼"""
    files = filestore.list_files()
    for f in files:
        f["size_h"] = filestore.human_size(f.get("size", 0))
    files, page, total_pages = _paginate(files, page)
    html = render_template(
        "manage/files.html",
        active="files",
        files=files,
        msg=msg,
        is_admin=is_admin(request),
        page=page,
        total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/files/upload")
async def manage_files_upload(file: UploadFile = File(...)):
    """문서 업로드"""
    try:
        content = await file.read()
        if not content:
            return RedirectResponse(url="/manage/files?msg=빈+파일은+업로드할+수+없습니다", status_code=303)
        filestore.add_file(file.filename, content)
        return RedirectResponse(url="/manage/files?msg=업로드+완료", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/manage/files?msg=업로드+실패", status_code=303)


@app.get("/manage/files/download/{file_id}")
async def manage_files_download(file_id: str):
    """문서 다운로드 (id 로만 조회하여 경로 탈출 방지)"""
    meta = filestore.get_file(file_id)
    if not meta:
        return JSONResponse(content={"error": "파일을 찾을 수 없습니다."}, status_code=404)
    path = filestore.file_path(meta)
    if not os.path.exists(path):
        return JSONResponse(content={"error": "파일이 존재하지 않습니다."}, status_code=404)
    filestore.increment_download(file_id)
    return FileResponse(path, filename=meta["name"], media_type="application/octet-stream")


@app.post("/manage/files/delete/{file_id}")
async def manage_files_delete(request: Request, file_id: str):
    """문서 삭제 (관리자 전용)"""
    if not is_admin(request):
        # 권한 없음 -> 로그인 페이지로
        return RedirectResponse(url="/manage/login?error=관리자만+삭제할+수+있습니다", status_code=303)
    ok = filestore.delete_file(file_id)
    msg = "삭제+완료" if ok else "삭제할+파일을+찾을+수+없습니다"
    return RedirectResponse(url=f"/manage/files?msg={msg}", status_code=303)


def _guide_images(prefix: str) -> list:
    """static/guide/ 에서 prefix(android/ios)로 시작하는 캡처 이미지 경로 목록 (정렬)."""
    folder = "static/guide"
    if not os.path.isdir(folder):
        return []
    imgs = [
        f"/static/guide/{f}"
        for f in os.listdir(folder)
        if f.lower().startswith(prefix) and f.lower().endswith((".png", ".jpg", ".jpeg"))
    ]
    return sorted(imgs)


@app.get("/manage/notices", response_class=HTMLResponse)
async def manage_notices(request: Request, msg: str = "", page: int = 1):
    """공지사항: 점수 기준 + 앱 설치 방법(고정) + 관리자 공지 목록 + 작성 폼"""
    notices, page, total_pages = _paginate(notice.list_notices(), page)
    guide = notice.build_install_guide()
    guide["android_imgs"] = _guide_images("android")
    guide["ios_imgs"] = _guide_images("ios")
    html = render_template(
        "manage/notices.html",
        active="notices",
        score_notice=notice.build_score_notice(),
        install_guide=guide,
        notices=notices,
        msg=msg,
        is_admin=is_admin(request),
        page=page,
        total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/notices/add")
async def manage_notices_add(request: Request, title: str = Form(...), content: str = Form("")):
    """공지 작성 (관리자 전용)"""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?error=관리자만+공지를+작성할+수+있습니다", status_code=303)
    meta = notice.add_notice(title, content)
    # 새 공지 → 전체 구독자에게 알림
    push.notify(
        push.subscribed_user_ids(),
        "📢 새 공지사항",
        meta.get("title", "공지가 등록되었습니다"),
        url="/manage/notices",
    )
    return RedirectResponse(url="/manage/notices?msg=공지가+등록되었습니다", status_code=303)


@app.post("/manage/notices/delete/{notice_id}")
async def manage_notices_delete(request: Request, notice_id: str):
    """공지 삭제 (관리자 전용)"""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?error=관리자만+삭제할+수+있습니다", status_code=303)
    ok = notice.delete_notice(notice_id)
    msg = "공지가+삭제되었습니다" if ok else "삭제할+공지를+찾을+수+없습니다"
    return RedirectResponse(url=f"/manage/notices?msg={msg}", status_code=303)


# ──────────────── 동호회 정기모임 ────────────────

@app.get("/manage/meetings", response_class=HTMLResponse)
async def manage_meetings(request: Request, msg: str = "", page: int = 1):
    """정기모임: 모임글 목록 + 참석 + 단체사진. '나'는 쿠키로 식별."""
    me = get_me(request)
    me_user = next((u for u in _registered_users() if u["user_id"] == me), None)
    today = datetime.now().strftime("%Y-%m-%d")

    raw, page, total_pages = _paginate(meeting.list_meetings(), page, per_page=10)
    meetings = []
    for m in raw:
        att = m.get("attendees", [])
        mtype = m.get("type", "regular")
        meetings.append({
            "id": m["id"],
            "type": mtype,
            "type_label": meeting.TYPES.get(mtype, "정기모임"),
            "is_regular": mtype == "regular",
            "title": m["title"],
            "date": m["date"],
            "place": m.get("place", ""),
            "content": m.get("content", ""),
            "author_name": get_user_name(m.get("author_id")) or "익명",
            "attendee_names": [get_user_name(u) or "익명" for u in att],
            "attend_count": len(att),
            "attending": bool(me) and me in att,
            "photos": ["/meeting/photo/" + fn for fn in m.get("photos", [])],
            "can_photo": bool(me),
            "can_delete": bool(me) and (me == m.get("author_id") or is_admin(request)),
        })
    html = render_template(
        "manage/meetings.html",
        active="meetings", meetings=meetings, me=me, me_user=me_user,
        users=_registered_users(), today=today, msg=msg,
        types=meeting.TYPES,
        page=page, total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/meetings/add")
async def manage_meetings_add(request: Request, title: str = Form(...), date: str = Form(...),
                              place: str = Form(""), content: str = Form(""), mtype: str = Form("regular")):
    me = get_me(request)
    if not me:
        return RedirectResponse(url="/manage/meetings?msg=먼저+이름을+선택하세요", status_code=303)
    if not date:
        return RedirectResponse(url="/manage/meetings?msg=날짜는+필수입니다", status_code=303)
    m = meeting.add_meeting(me, title, date, place, content, mtype)
    # 새 모임 → 전체 알림
    push.notify(
        push.subscribed_user_ids(),
        "📅 새 정기모임",
        f"{m['date']} {m['title']}" + (f" @ {m['place']}" if m['place'] else ""),
        url="/manage/meetings",
    )
    return RedirectResponse(url="/manage/meetings?msg=모임글이+등록되었습니다", status_code=303)


@app.post("/manage/meetings/attend")
async def manage_meetings_attend(request: Request, mid: str = Form(...)):
    me = get_me(request)
    if not me:
        return RedirectResponse(url="/manage/meetings?msg=먼저+이름을+선택하세요", status_code=303)
    m = meeting.get_meeting(mid)
    attending, _ = meeting.toggle_attend(mid, me)
    # 정기모임 참석 → '정기 동호회 참석'(+300) 이벤트를 대회참가기록에 자동 동기화
    if m and m.get("type", "regular") == "regular":
        if attending:
            if not rewards.has_event_ref(me, mid):
                rewards.add_event(me, "club_regular", m["date"], ref=mid)
        else:
            rewards.delete_events_by_ref(mid, me)
    return RedirectResponse(url="/manage/meetings", status_code=303)


@app.post("/manage/meetings/photo/{mid}")
async def manage_meetings_photo(request: Request, mid: str, file: UploadFile = File(...)):
    me = get_me(request)
    if not me:
        return RedirectResponse(url="/manage/meetings?msg=먼저+이름을+선택하세요", status_code=303)
    m = meeting.get_meeting(mid)
    if not m:
        return RedirectResponse(url="/manage/meetings?msg=모임을+찾을+수+없습니다", status_code=303)
    content = await file.read()
    if content:
        meeting.save_photo(mid, file.filename, content)
        return RedirectResponse(url="/manage/meetings?msg=사진이+업로드되었습니다", status_code=303)
    return RedirectResponse(url="/manage/meetings?msg=빈+파일", status_code=303)


@app.post("/manage/meetings/delete/{mid}")
async def manage_meetings_delete(request: Request, mid: str):
    me = get_me(request)
    m = meeting.get_meeting(mid)
    if not m:
        return RedirectResponse(url="/manage/meetings?msg=모임을+찾을+수+없습니다", status_code=303)
    # 작성자 본인 또는 관리자만 삭제
    if not (me and (me == m.get("author_id") or is_admin(request))):
        return RedirectResponse(url="/manage/meetings?msg=작성자+또는+관리자만+삭제할+수+있어요", status_code=303)
    meeting.delete_meeting(mid)
    rewards.delete_events_by_ref(mid)  # 이 모임으로 적립된 정기모임 참석 점수도 회수
    return RedirectResponse(url="/manage/meetings?msg=모임글이+삭제되었습니다", status_code=303)


@app.get("/meeting/photo/{name}")
async def meeting_photo(name: str):
    p = meeting.photo_path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


@app.get("/manage/events", response_class=HTMLResponse)
async def manage_events(request: Request, msg: str = "", page: int = 1):
    """대회 참가 등록 (관리자 전용) — 사용자별 대회 참가 점수 부여/조회"""
    users = _registered_users()
    name_of = {u["user_id"]: u["name"] for u in users}
    events = rewards.list_events()
    for e in events:
        e["user_name"] = name_of.get(e.get("user_id"), "(이름 미등록)")
    events, page, total_pages = _paginate(events, page)
    # 동호회 참석 + 대회 참가 모두 등록 가능
    event_types = [
        {"key": k, "label": v[0], "points": v[1]}
        for k, v in Score.EVENT_TYPES.items()
    ]
    html = render_template(
        "manage/events.html",
        active="events",
        users=users,
        event_types=event_types,
        events=events,
        msg=msg,
        is_admin=is_admin(request),
        today=datetime.now().strftime("%Y-%m-%d"),
        page=page,
        total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/events/add")
async def manage_events_add(request: Request, uid: str = Form(...), event_type: str = Form(...), date: str = Form("")):
    """대회 참가 등록 (관리자 전용)"""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?next=/manage/events&error=관리자만+등록할+수+있습니다", status_code=303)
    try:
        rewards.add_event(uid, event_type, date)
        label = Score.EVENT_TYPES.get(event_type, ("이벤트",))[0]
        return RedirectResponse(url=f"/manage/events?msg={label}+등록+완료", status_code=303)
    except Exception:
        return RedirectResponse(url="/manage/events?msg=등록+실패", status_code=303)


@app.post("/manage/events/delete/{event_id}")
async def manage_events_delete(request: Request, event_id: str):
    """이벤트 삭제 (관리자 전용)"""
    if not is_admin(request):
        return RedirectResponse(url="/manage/login?next=/manage/events&error=관리자만+삭제할+수+있습니다", status_code=303)
    ok = rewards.delete_event(event_id)
    msg = "삭제+완료" if ok else "삭제할+항목을+찾을+수+없습니다"
    return RedirectResponse(url=f"/manage/events?msg={msg}", status_code=303)


def _paginate(items, page: int, per_page: int = 15):
    """리스트 페이징 헬퍼. 반환: (해당 페이지 항목, 보정된 page, 전체 페이지수)."""
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return items[start:start + per_page], page, total_pages


def _registered_users() -> list:
    """이름이 등록된 사용자 목록 [{user_id, name}] (이름순)."""
    mapping = _load_user_mapping()
    users = [
        {"user_id": uid, "name": name}
        for uid, name in mapping.items()
        if name and name != "__pending__"
    ]
    users.sort(key=lambda x: x["name"])
    return users


# ──────────────── 기기별 "나" 식별 + 계정 비밀번호 ────────────────
ME_COOKIE = "me_uid"
_ME_SECRET = "wise-runner-me-cookie-secret"   # 쿠키 서명용
USER_PW_PATH = "data/user_pw.json"


def _load_user_pw() -> dict:
    if not os.path.exists(USER_PW_PATH):
        return {}
    try:
        with open(USER_PW_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_user_pw(data: dict):
    os.makedirs(os.path.dirname(USER_PW_PATH), exist_ok=True)
    with open(USER_PW_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _pw_hash(pw: str) -> str:
    return hashlib.sha256(("uchamp-pw:" + pw).encode("utf-8")).hexdigest()


def has_user_password(uid: str) -> bool:
    return uid in _load_user_pw()


def set_user_password(uid: str, pw: str):
    d = _load_user_pw()
    d[uid] = _pw_hash(pw)
    _save_user_pw(d)


def check_user_password(uid: str, pw: str) -> bool:
    return _load_user_pw().get(uid) == _pw_hash(pw)


def _me_sign(uid: str) -> str:
    """쿠키 서명: 서버 비밀 + uid + 계정 비번해시 기반 (비번 바뀌면 무효화)."""
    pwh = _load_user_pw().get(uid, "")
    return hashlib.sha256((_ME_SECRET + uid + pwh).encode("utf-8")).hexdigest()[:32]


def get_me(request: Request) -> str:
    """서명된 쿠키에서 내 user_id 추출 (위조 불가). 유효하지 않으면 ''."""
    raw = request.cookies.get(ME_COOKIE, "")
    if "." not in raw:
        return ""
    uid, sig = raw.rsplit(".", 1)
    if not any(u["user_id"] == uid for u in _registered_users()):
        return ""
    if sig != _me_sign(uid):
        return ""
    return uid


def _set_me_cookie(resp, uid: str):
    resp.set_cookie(ME_COOKIE, f"{uid}.{_me_sign(uid)}",
                    max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.get("/login-as", response_class=HTMLResponse)
async def login_as_form(request: Request, uid: str, next: str = "/manage", error: str = ""):
    """계정 선택 → 비밀번호 설정(최초) 또는 입력(이후)."""
    name = get_user_name(uid)
    if not name:
        return RedirectResponse(url=next, status_code=303)
    html = render_template(
        "login_as.html", uid=uid, member_name=name, next=next, error=error,
        is_set=has_user_password(uid),
    )
    return HTMLResponse(content=html)


@app.post("/login-as")
async def login_as_submit(uid: str = Form(...), password: str = Form(...),
                          confirm: str = Form(""), next: str = Form("/manage")):
    name = get_user_name(uid)
    if not name:
        return RedirectResponse(url=next, status_code=303)
    if has_user_password(uid):
        # 기존 비번 입력
        if not check_user_password(uid, password):
            return RedirectResponse(
                url=f"/login-as?uid={uid}&next={next}&error=비밀번호가+올바르지+않습니다", status_code=303)
    else:
        # 최초: 비번 설정 (확인 일치 필요)
        if len(password) < 1 or password != confirm:
            return RedirectResponse(
                url=f"/login-as?uid={uid}&next={next}&error=비밀번호와+확인이+일치하지+않습니다", status_code=303)
        set_user_password(uid, password)
    return _set_me_cookie(RedirectResponse(url=next, status_code=303), uid)


@app.get("/whoami/clear")
async def whoami_clear(next: str = "/manage"):
    """다른 사람으로 바꾸기 (쿠키 해제)."""
    resp = RedirectResponse(url=next, status_code=303)
    resp.delete_cookie(ME_COOKIE)
    return resp


@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, msg: str = ""):
    """사용자 포털: 점수 확인 + 러닝 상점(구매) + 구매 내역. '나'는 쿠키로 식별."""
    users = _registered_users()
    uid = get_me(request)
    selected = next((u for u in users if u["user_id"] == uid), None)

    context = {
        "active": "",
        "users": users,
        "selected": selected,
        "msg": msg,
    }
    if selected:
        b = db_handler.get_points_breakdown(uid)
        purchases = rewards.list_purchases(uid)
        pending = sum(p.get("cost", 0) for p in purchases if p.get("status") == "pending")
        spendable = b["available"] - pending  # 신청 대기 예약분 제외
        shop = [
            {"name": n, "cost": c, "affordable": spendable >= c, "link": _coupang_link(n)}
            for n, c in Score.SHOP_ITEMS
        ]
        context.update({
            "breakdown": b,
            "shop": shop,
            "purchases": purchases,
            "pending": pending,
            "spendable": spendable,
        })
    html = render_template("portal.html", **context)
    return HTMLResponse(content=html)


def _coupang_link(item_name: str) -> str:
    """상품명으로 쿠팡 '판매량순(구매 많은 순)' 검색 링크 생성. 괄호 부가설명은 제거."""
    q = re.sub(r"\s*\(.*?\)\s*", " ", item_name).strip()
    return "https://www.coupang.com/np/search?q=" + quote(q) + "&sorter=saleCountDesc"


def _build_encourage(km, mins, cur_bonus, dist_goal, time_goal) -> str:
    """주간 보너스 격려 토스트 문구 생성."""
    head = f"이번 주 {km}km · {mins}분 달렸어요! 🏃"
    if not dist_goal and not time_goal:
        return f"{head}\n🎉 이번 주 최고 보너스(+{cur_bonus:,}점) 달성! 멋져요!"
    lines = [head]
    if dist_goal:
        lines.append(f"📍 {dist_goal['need']}km만 더 뛰면 +{dist_goal['gain']:,}점 (주간 {dist_goal['target']}km 달성)")
    if time_goal:
        lines.append(f"⏱️ {time_goal['need']}분만 더 뛰면 +{time_goal['gain']:,}점 (주간 {time_goal['target']}분 달성)")
    return "\n".join(lines)


@app.post("/api/like")
async def api_like(uid: str = Form(...), key: str = Form(...)):
    """러닝 피드 좋아요 토글. 새로 좋아요면 기록 주인에게 알림."""
    liked, count = social.toggle_like(key, uid)
    if liked:
        owner = (key.split("|", 1)[0]) if key else ""
        if owner and owner != uid:
            push.notify(
                [owner],
                "❤️ 좋아요",
                f"{get_user_name(uid) or '누군가'}님이 회원님 기록에 좋아요를 눌렀어요!",
                url="/manage",
            )
    return JSONResponse({"liked": liked, "count": count})


# ──────────────── 웹 푸시 알림 ────────────────

@app.get("/api/push/vapid")
async def push_vapid():
    """브라우저 구독에 필요한 VAPID 공개키."""
    return JSONResponse({"publicKey": push.public_key()})


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    """기기 푸시 구독 등록. body: {uid, subscription}"""
    body = await request.json()
    uid = body.get("uid")
    sub = body.get("subscription")
    if not uid or not sub:
        return JSONResponse({"ok": False, "error": "uid/subscription 필요"}, status_code=400)
    push.add_subscription(uid, sub)
    return JSONResponse({"ok": True})


@app.post("/portal/buy")
async def portal_buy(uid: str = Form(...), item: str = Form(...)):
    """러닝 상점 구매 '신청' — 즉시 차감하지 않고 관리자 완료 시 차감."""
    b = db_handler.get_points_breakdown(uid)
    cost = next((c for n, c in Score.SHOP_ITEMS if n == item), None)
    if cost is None:
        return RedirectResponse(url="/portal?msg=존재하지+않는+상품", status_code=303)
    # 신청 대기(pending) 금액도 미리 예약 처리하여 보유 점수 초과 신청 방지
    pending = sum(p.get("cost", 0) for p in rewards.list_purchases(uid) if p.get("status") == "pending")
    if b["available"] - pending < cost:
        return RedirectResponse(url="/portal?msg=점수가+부족합니다(신청+대기+포함)", status_code=303)
    rewards.add_purchase(uid, item)
    return RedirectResponse(url=f"/portal?msg={item}+구매+신청+완료(관리자+승인+대기)", status_code=303)


# ──────────────── 복약 기록 ────────────────

@app.get("/manage/medicines", response_class=HTMLResponse)
async def manage_medicines(request: Request, msg: str = "", page: int = 1):
    """복약 기록: 내 기록 목록 + 추가 폼. '나'는 쿠키로 식별하며 본인 기록만 보인다."""
    me = get_me(request)
    me_user = next((u for u in _registered_users() if u["user_id"] == me), None)
    now = datetime.now()

    meds, page, total_pages = _paginate(medicine.list_medicines(me) if me else [], page)
    html = render_template(
        "manage/medicines.html",
        active="medicines", meds=meds, me=me, me_user=me_user,
        users=_registered_users(), msg=msg,
        today=now.strftime("%Y-%m-%d"), now_time=now.strftime("%H:%M"),
        stat=medicine.summary(me) if me else None,
        page=page, total_pages=total_pages,
    )
    return HTMLResponse(content=html)


@app.post("/manage/medicines/add")
async def manage_medicines_add(request: Request, name: str = Form(...), dose: str = Form(""),
                               date: str = Form(""), time: str = Form(""), memo: str = Form(""),
                               taken: str = Form("")):
    """복약 기록 추가 (본인 것으로 저장)."""
    me = get_me(request)
    if not me:
        return RedirectResponse(url="/manage/medicines?msg=먼저+이름을+선택하세요", status_code=303)
    if not (name or "").strip():
        return RedirectResponse(url="/manage/medicines?msg=약+이름은+필수입니다", status_code=303)
    medicine.add_medicine(me, name, dose, date, time, memo, taken=bool(taken))
    return RedirectResponse(url="/manage/medicines?msg=복약+기록이+추가되었습니다", status_code=303)


@app.get("/manage/medicines/edit/{mid}", response_class=HTMLResponse)
async def manage_medicines_edit_form(request: Request, mid: str):
    """복약 기록 수정 폼 (본인만)."""
    me = get_me(request)
    m = medicine.get_medicine(mid)
    if not m:
        return RedirectResponse(url="/manage/medicines?msg=기록을+찾을+수+없습니다", status_code=303)
    if not me or me != m.get("user_id"):
        return RedirectResponse(url="/manage/medicines?msg=본인만+수정할+수+있어요", status_code=303)
    html = render_template("manage/medicine_edit.html", active="medicines", m=m)
    return HTMLResponse(content=html)


@app.post("/manage/medicines/edit/{mid}")
async def manage_medicines_edit(request: Request, mid: str, name: str = Form(...),
                                dose: str = Form(""), date: str = Form(""), time: str = Form(""),
                                memo: str = Form(""), taken: str = Form("")):
    """복약 기록 수정 저장 (본인만)."""
    me = get_me(request)
    m = medicine.get_medicine(mid)
    if not m:
        return RedirectResponse(url="/manage/medicines?msg=기록을+찾을+수+없습니다", status_code=303)
    if not me or me != m.get("user_id"):
        return RedirectResponse(url="/manage/medicines?msg=본인만+수정할+수+있어요", status_code=303)
    if not (name or "").strip():
        return RedirectResponse(url=f"/manage/medicines/edit/{mid}", status_code=303)
    medicine.update_medicine(mid, {
        "name": name, "dose": dose, "date": date, "time": time,
        "memo": memo, "taken": bool(taken),
    }, owner_id=me)
    return RedirectResponse(url="/manage/medicines?msg=복약+기록이+수정되었습니다", status_code=303)


@app.post("/manage/medicines/toggle/{mid}")
async def manage_medicines_toggle(request: Request, mid: str):
    """복용 완료 ↔ 아직 안 먹음 토글 (본인만)."""
    me = get_me(request)
    if not me:
        return RedirectResponse(url="/manage/medicines?msg=먼저+이름을+선택하세요", status_code=303)
    state = medicine.toggle_taken(mid, owner_id=me)
    if state is None:
        return RedirectResponse(url="/manage/medicines?msg=본인+기록만+변경할+수+있어요", status_code=303)
    msg = "복용+완료로+표시했습니다" if state else "아직+안+먹음으로+표시했습니다"
    return RedirectResponse(url=f"/manage/medicines?msg={msg}", status_code=303)


@app.post("/manage/medicines/delete/{mid}")
async def manage_medicines_delete(request: Request, mid: str):
    """복약 기록 삭제 (본인만)."""
    me = get_me(request)
    if not me:
        return RedirectResponse(url="/manage/medicines?msg=먼저+이름을+선택하세요", status_code=303)
    ok = medicine.delete_medicine(mid, owner_id=me)
    msg = "복약+기록이+삭제되었습니다" if ok else "본인+기록만+삭제할+수+있어요"
    return RedirectResponse(url=f"/manage/medicines?msg={msg}", status_code=303)


# ──────────────── PWA (회원용: 포털 + 채팅) ────────────────

# 홈 화면(앱) 이름. TEST_APP_NAME 은 기능 테스트용 별도 설치본의 이름이며,
# 이름만 바꾸고 싶으면 아래 문자열만 수정하면 된다.
APP_NAME = "와이즈러너스"
TEST_APP_NAME = "와이즈러너스 플러스"


def _manifest(name: str, start_url: str, app_id: str = None) -> dict:
    """PWA manifest 생성. id 가 다르면 홈 화면에 서로 다른 앱으로 설치된다.
    (id 를 주지 않으면 브라우저가 start_url 을 id 로 쓰므로 기존 설치본이 그대로 유지된다.)"""
    m = {}
    if app_id:
        m["id"] = app_id
    m.update({
        "name": name,
        "short_name": name,
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a1a2e",
        "theme_color": "#1a1a2e",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })
    return m


@app.get("/manifest.json")
async def pwa_manifest():
    """기존 회원용 앱 — 이름·설치 상태를 그대로 유지한다(id 미지정)."""
    return JSONResponse(_manifest(APP_NAME, "/manage"))


@app.get("/manifest-test.json")
async def pwa_manifest_test():
    """기능 테스트용 별도 설치본 — 이름이 다르고 복약 기록 화면에서 시작한다."""
    return JSONResponse(_manifest(TEST_APP_NAME, "/manage/medicines", app_id="/wise-runner-test"))


@app.get("/install-test", response_class=HTMLResponse)
async def pwa_install_test():
    """테스트용 앱 설치 안내 페이지 (다른 이름으로 홈 화면에 별도 설치)."""
    html = render_template("install_test.html", app_name=TEST_APP_NAME, main_app_name=APP_NAME)
    return HTMLResponse(content=html)


@app.get("/sw.js")
async def pwa_service_worker():
    sw = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {});

self.addEventListener('push', function (e) {
    let d = {};
    try { d = e.data.json(); } catch (_) {}
    const title = d.title || '와이즈러너스';
    e.waitUntil(self.registration.showNotification(title, {
        body: d.body || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        data: { url: d.url || '/manage' }
    }));
});

self.addEventListener('notificationclick', function (e) {
    e.notification.close();
    const url = (e.notification.data && e.notification.data.url) || '/manage';
    e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (wins) {
        for (const w of wins) { if ('focus' in w) { w.navigate && w.navigate(url); return w.focus(); } }
        if (clients.openWindow) return clients.openWindow(url);
    }));
});
"""
    return Response(content=sw, media_type="application/javascript")


# ──────────────── 웹 채팅 (카카오와 동일 기능) ────────────────

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """웹 채팅 페이지: 사진 등록 + 대화. '나'는 쿠키로 식별(있으면 바로 채팅)."""
    me = get_me(request)
    html = render_template(
        "chat.html",
        users=_registered_users(),
        me=me,
        me_name=get_user_name(me) if me else "",
    )
    return HTMLResponse(content=html)


@app.post("/api/web_user")
async def api_web_user(name: str = Form(...)):
    """웹 신규 사용자 생성(이름 등록) 후 user_id 반환 + 쿠키에 '나'로 저장."""
    name = (name or "").strip()
    if not name:
        return JSONResponse({"error": "이름을 입력해 주세요."}, status_code=400)
    user_id = "web_" + uuid.uuid4().hex
    register_user_name(user_id, name)
    return _set_me_cookie(JSONResponse({"user_id": user_id, "name": name}), user_id)


@app.post("/api/chat")
async def api_chat(
    user_id: str = Form(...),
    message: str = Form(""),
    image: UploadFile = File(None),
):
    """웹 채팅 처리: 텍스트 메시지 또는 사진 업로드를 받아 응답 반환."""
    image_url = None
    if image is not None:
        content = await image.read()
        if content:
            mime = image.content_type or "image/jpeg"
            b64 = base64.b64encode(content).decode("ascii")
            image_url = f"data:{mime};base64,{b64}"
    reply = await handle_interaction(user_id, message, image_url)
    return JSONResponse({"reply": reply})


if __name__ == "__main__":
    # 운영 안정성을 위해 자동 reload 비활성화 (코드 수정 후에는 start.sh 로 수동 재시작)
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=False)
