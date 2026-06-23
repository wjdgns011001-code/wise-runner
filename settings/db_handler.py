import json
import os
import fcntl
import uuid
from contextlib import contextmanager
from collections import defaultdict
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from datetime import datetime
from utils.connect import Record
from utils.score import Score
from utils import rewards



class UserStats(BaseModel):
    """사용자 통계"""
    user_id: str
    total_time: str = None
    total_point: int = 0
    total_distance: float = 0.0
    total_runs: int = 0
    
    best_distance: int = 0
    best_cadence: int = 0
    best_calories: int = 0
    



class Database:
    """
    JSON 파일 기반 데이터베이스 핸들러.
    user_id + date 를 복합 PK로 사용하여 하루에 하나의 데이터만 유지.
    """

    def __init__(self, file_path: str = "data/records.json"):
        self.file_path = file_path
        self._ensure_file()

    def _ensure_file(self):
        """파일/디렉토리가 없으면 생성"""
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    @contextmanager
    def _lock(self):
        """records 파일에 대한 프로세스 간 배타적 잠금 (동시 upsert 경쟁 방지)."""
        lock_path = self.file_path + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _load_all(self) -> List[dict]:
        """전체 데이터 로드"""
        with open(self.file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_all(self, records: List[dict]):
        """전체 데이터 저장 (임시파일 → 원자적 교체로 부분쓰기/손상 방지)."""
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.file_path)

    def _composite_key(self, user_id: str, date: str) -> str:
        """복합 키 생성: user_id|YYYY-MM-DD"""
        return f"{user_id}|{date}"

    # ──────────────── CRUD ────────────────

    # 중복(동일 기록) 판단에 쓰는 핵심 필드
    _SIG_FIELDS = ("date", "total_distance", "total_time", "average_pace",
                   "cadence", "average_heart_rate", "total_calories")

    def _same_record(self, a: dict, b: dict) -> bool:
        """두 기록이 '같은 데이터'인지 (핵심 필드 전부 일치)."""
        return all(a.get(f) == b.get(f) for f in self._SIG_FIELDS)

    def upsert(self, record: Record) -> tuple[Record, int, bool, bool]:
        """
        기록 저장. 같은 날짜라도 데이터가 다르면 별개 기록으로 추가하고,
        완전히 동일한 기록은 중복 저장하지 않는다.
        반환값: (record, days_diff, is_first, is_dup)
        - days_diff: 기존 가장 최근 기록 대비 경과일 (0=같은날, 양수=더 최근, 음수=과거)
        - is_first: 이 사용자의 첫 기록 여부
        - is_dup: 동일 기록이 이미 있어 저장하지 않음
        """
        with self._lock():
            records = self._load_all()
            new = record.model_dump()
            if not new.get("id"):
                new["id"] = uuid.uuid4().hex

            # 1. 완전히 동일한 기록이 이미 있으면 중복 → 저장 안 함
            for r in records:
                if r["user_id"] == record.user_id and self._same_record(r, new):
                    return record, 0, False, True

            # 2. 경과일/첫기록 계산 (기존 기록 기준)
            user_records = sorted(
                [Record(**r) for r in records if r["user_id"] == record.user_id],
                key=lambda x: x.date, reverse=True,
            )
            is_first = not user_records
            days_diff = 0
            if user_records:
                last_date = datetime.strptime(user_records[0].date, "%Y-%m-%d")
                current_date = datetime.strptime(record.date, "%Y-%m-%d")
                days_diff = (current_date - last_date).days

            # 3. 새 기록 추가 (같은 날짜여도 데이터가 다르면 별개로 저장)
            records.append(new)
            self._save_all(records)
            return record, days_diff, is_first, False


    def get_record(self, record_id: str):
        """id로 단일 기록 조회 (dict) 또는 None."""
        for r in self._load_all():
            if r.get("id") == record_id:
                return r
        return None

    def delete_record(self, record_id: str, owner_id: str = None) -> bool:
        """기록 삭제. owner_id 지정 시 그 사용자 소유일 때만 삭제."""
        with self._lock():
            records = self._load_all()
            target = next((r for r in records if r.get("id") == record_id), None)
            if not target:
                return False
            if owner_id is not None and target.get("user_id") != owner_id:
                return False
            self._save_all([r for r in records if r.get("id") != record_id])
            return True

    def update_record(self, record_id: str, fields: dict, owner_id: str = None) -> bool:
        """기록 수정 후 데일리 점수 재계산. owner_id 지정 시 소유자만."""
        with self._lock():
            records = self._load_all()
            for r in records:
                if r.get("id") == record_id:
                    if owner_id is not None and r.get("user_id") != owner_id:
                        return False
                    for k, v in fields.items():
                        if v is not None:
                            r[k] = v
                    # 거리/시간 변경 반영하여 데일리 점수 재계산
                    r["score"] = Score().calculate_daily_point(
                        r.get("total_distance") or 0, Score.parse_minutes(r.get("total_time"))
                    )
                    self._save_all(records)
                    return True
            return False

    # ──────────────── 조회 ────────────────
    def get_user_records(self, user_id: str) -> List[Record]:
        """특정 사용자의 모든 기록 반환 (날짜 내림차순)"""
        records = self._load_all()
        user_records = [
            Record(**r) for r in records if r["user_id"] == user_id
        ]
        user_records.sort(key=lambda x: x.date, reverse=True)
        return user_records
        
    def get_total_score(self, user_id: str) -> int:
        """사용 가능 점수(획득 - 사용)를 반환."""
        return self.get_points_breakdown(user_id)["available"]

    def get_weekly_bonus_total(self, records: List[Record]) -> int:
        """사용자 기록을 주(ISO week) 단위로 묶어 주간 보너스 합산."""
        weeks = defaultdict(lambda: [0.0, 0.0])  # (연,주) -> [거리, 분]
        for r in records:
            try:
                y, w, _ = datetime.strptime(r.date, "%Y-%m-%d").isocalendar()
            except Exception:
                continue
            weeks[(y, w)][0] += r.total_distance or 0
            weeks[(y, w)][1] += Score.parse_minutes(r.total_time)
        return sum(Score.weekly_bonus(km, mins) for km, mins in weeks.values())

    def get_recent_stats(self, user_id: str) -> dict:
        """이번 주(ISO week) / 이번 달 누적 거리·시간·횟수."""
        now = datetime.now()
        cy, cw, _ = now.isocalendar()
        ym = (now.year, now.month)
        week = {"distance": 0.0, "minutes": 0.0, "runs": 0}
        month = {"distance": 0.0, "minutes": 0.0, "runs": 0}
        for r in self.get_user_records(user_id):
            try:
                d = datetime.strptime(r.date, "%Y-%m-%d")
            except Exception:
                continue
            dist = r.total_distance or 0
            mins = Score.parse_minutes(r.total_time)
            y, w, _ = d.isocalendar()
            if (y, w) == (cy, cw):
                week["distance"] += dist; week["minutes"] += mins; week["runs"] += 1
            if (d.year, d.month) == ym:
                month["distance"] += dist; month["minutes"] += mins; month["runs"] += 1

        def _fmt(p):
            m = int(p["minutes"])
            p["distance"] = round(p["distance"], 2)
            p["time_str"] = f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"
            return p

        return {"week": _fmt(week), "month": _fmt(month), "month_label": f"{now.month}월"}

    def get_week_progress(self, user_id: str):
        """이번 주(ISO week) 누적 거리(km)와 시간(분) 반환."""
        now = datetime.now()
        cy, cw, _ = now.isocalendar()
        km = 0.0
        mins = 0.0
        for r in self.get_user_records(user_id):
            try:
                y, w, _ = datetime.strptime(r.date, "%Y-%m-%d").isocalendar()
            except Exception:
                continue
            if (y, w) == (cy, cw):
                km += r.total_distance or 0
                mins += Score.parse_minutes(r.total_time)
        return round(km, 2), int(mins)

    def get_weekly_breakdown(self, records: List[Record]) -> list:
        """주(ISO week)별 누적 거리/시간/보너스 상세 (최신 주 우선)."""
        weeks = defaultdict(lambda: [0.0, 0.0])
        for r in records:
            try:
                y, w, _ = datetime.strptime(r.date, "%Y-%m-%d").isocalendar()
            except Exception:
                continue
            weeks[(y, w)][0] += r.total_distance or 0
            weeks[(y, w)][1] += Score.parse_minutes(r.total_time)
        out = []
        for (y, w), (km, mins) in sorted(weeks.items(), reverse=True):
            # 해당 ISO 주의 월요일~일요일 날짜 범위
            try:
                start = datetime.fromisocalendar(y, w, 1)
                end = datetime.fromisocalendar(y, w, 7)
                date_range = f"{start.month}/{start.day}~{end.month}/{end.day}"
            except Exception:
                date_range = ""
            out.append({
                "label": f"{y}년 {w}주차",
                "date_range": date_range,
                "mileage": round(km, 2),
                "minutes": int(mins),
                "bonus": Score.weekly_bonus(km, mins),
            })
        return out

    def get_points_breakdown(self, user_id: str) -> dict:
        """
        점수 분해:
        - daily   : 기록별 데일리 포인트 합
        - weekly  : 주간 보너스 합
        - events  : 동호회/대회 등 이벤트 포인트 합
        - earned  : 총 획득(daily+weekly+events)
        - spent   : 상점 구매로 사용한 점수
        - available: 사용 가능(earned - spent)
        """
        records = self.get_user_records(user_id=user_id)
        daily = sum((r.score or 0) for r in records)
        weekly = self.get_weekly_bonus_total(records)
        events = rewards.events_points(user_id)
        earned = daily + weekly + events
        spent = rewards.purchases_cost(user_id)
        return {
            "daily": daily,
            "weekly": weekly,
            "events": events,
            "earned": earned,
            "spent": spent,
            "available": earned - spent,
        }

    # ──────────────── 통계 ────────────────

    def get_user_stats(self, user_id: str) -> UserStats:
        """
        특정 사용자의 통계를 계산합니다.
        - 총 시간 (초 단위 합산)
        - 총 점수
        - 총 거리
        - 총 러닝 횟수
        - 최고 케이던스
        - 최고 칼로리
        """
        records = self.get_user_records(user_id)
        stats = UserStats(user_id=user_id)
        total_seconds = 0 
        for rec in records:
            # 시간 → 초 변환
            parts = rec.total_time.split(":")
            if len(parts) == 3:
                h, m, s = map(int, parts)
                total_seconds += h * 3600 + m * 60 + s
            elif len(parts) == 2:
                m, s = map(int, parts)
                total_seconds += m * 60 + s

            stats.total_distance += rec.total_distance
            stats.total_runs += 1

            if rec.total_distance and rec.total_distance > stats.best_distance:
                stats.best_distance = rec.total_distance

            if rec.cadence and rec.cadence > stats.best_cadence:
                stats.best_cadence = rec.cadence

            if rec.total_calories and rec.total_calories > stats.best_calories:
                stats.best_calories = rec.total_calories
        
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        stats.total_time = f"{hours}:{minutes}:{seconds}"

        # 총 획득 포인트 = 데일리 + 주간 + 이벤트
        stats.total_point = self.get_points_breakdown(user_id)["earned"]
        return stats

    def get_user_rank(self, user_id: str) -> str:
        """
        공동 순위일 경우 '공동' 문구를 추가하여 순위를 반환합니다.
        """
        records = self._load_all()
        user_ids = list(set(r["user_id"] for r in records))
        
        # 1. 모든 유저의 통계 가져오기
        all_stats = [self.get_user_stats(uid) for uid in user_ids]
        
        # 내 통계 찾기
        user_stat = next((s for s in all_stats if s.user_id == user_id), None)
        
        if not user_stat:
            return "❓ 기록을 찾을 수 없습니다."

        # 2. 순위 계산 로직
        # 나보다 점수가 높은 사람 수 + 1
        rank = sum(1 for s in all_stats if s.total_point > user_stat.total_point) + 1
        
        # 나와 점수가 같은 사람 수 (본인 포함)
        same_score_count = sum(1 for s in all_stats if s.total_point == user_stat.total_point)
        
        # 3. '공동' 접두사 결정 (나와 점수가 같은 사람이 2명 이상일 때)
        prefix = "공동 " if same_score_count > 1 else ""
        
        # 4. 순위별 이모지 설정
        medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🏃"
        
        total_users = len(all_stats)
        
        msg = (
            f"{medal} **전체 {total_users}명 중 {prefix}{rank}위**입니다!\n"
            f"💰 총 점수: {user_stat.total_point:,} pt\n"
            f"🛣️ 총 거리: {user_stat.total_distance:.2f} km\n"
            f"--- \n"
            f"상위 {(rank/total_users)*100:.1f}% 기록 중이에요. 화이팅! 🔥"
        )
        
        return msg