import re


class Score:
    """
    러닝 트래커 점수 체계.

    - 데일리 포인트: 사진 1건(하루 1회). 거리/시간 조건 중 '가장 높은' 1개 적용.
    - 주간 포인트: 한 주 누적 마일리지/시간 달성 보너스. 전체 조건 중 '가장 높은' 1개만.
    - 이벤트 포인트: 사진으로 알 수 없는 동호회/대회 참가 (웹에서 수동 등록).
    - 러닝 상점: 점수로 상품 구매(차감).
    """

    # 데일리: (점수, 종류, 임계값) — 위에서부터 첫 충족 항목 적용(=최고점)
    #   종류: 'dist'=거리(km) 이상, 'time'=시간(분) 이상, 'either'=(거리,분) 중 하나 이상
    DAILY = [
        (50, "dist", 10),
        (49, "time", 60),
        (48, "dist", 7),
        (47, "time", 40),
        (46, "dist", 5),
        (45, "time", 30),
        (44, "either", (3, 20)),
        (43, "dist", 1),
    ]

    # 주간: (점수, 종류, 임계값) — 충족 항목 중 '최고 1개'만 부여
    WEEKLY = [
        (1000, "dist", 30),
        (990, "time", 200),
        (980, "dist", 20),
        (970, "time", 120),
        (960, "dist", 10),
        (950, "time", 60),
        (940, "dist", 3),
        (930, "time", 20),
    ]

    # 이벤트(수동 등록): key -> (표시명, 점수)
    EVENT_TYPES = {
        "club_regular": ("정기 동호회 참석", 300),
        "race_full": ("풀코스 참가", 2000),
        "race_half": ("하프 코스 참가", 1500),
        "race_10k": ("10KM 참가", 1000),
    }

    # 러닝 상점: (상품명, 구매점수) — 표시 순서대로
    SHOP_ITEMS = [
        ("스마트워치 (1회 한정)", 100000),
        ("러닝화 (1회 한정)", 70000),
        ("러닝 선글라스", 45000),
        ("러닝 상의", 35000),
        ("러닝모자", 30000),
        ("러닝 헤어밴드", 25000),
        ("러닝 벨트", 20000),
        ("러닝 겨울 장갑", 18000),
        ("무릎보호대", 15000),
        ("발목보호대", 12000),
        ("손목 밴드", 10000),
        ("러닝양말", 5000),
    ]

    @staticmethod
    def parse_minutes(total_time) -> float:
        """'HH:MM:SS' / 'MM:SS' / 'M' 등을 분(float)으로 변환."""
        if not total_time:
            return 0.0
        parts = re.split(r"[:：]", str(total_time).strip())
        try:
            nums = [float(p) for p in parts if p != ""]
        except ValueError:
            return 0.0
        if len(nums) == 3:
            h, m, s = nums
        elif len(nums) == 2:
            h, m, s = 0, nums[0], nums[1]
        elif len(nums) == 1:
            h, m, s = 0, nums[0], 0
        else:
            return 0.0
        return h * 60 + m + s / 60

    def calculate_daily_point(self, distance, minutes: float = 0.0) -> int:
        """거리(km)와 시간(분)으로 데일리 포인트 산정."""
        distance = distance or 0
        minutes = minutes or 0
        for pts, kind, thr in self.DAILY:
            if kind == "dist" and distance >= thr:
                return pts
            if kind == "time" and minutes >= thr:
                return pts
            if kind == "either" and (distance >= thr[0] or minutes >= thr[1]):
                return pts
        return 0

    @classmethod
    def weekly_next_goals(cls, mileage_km: float, minutes: float):
        """
        이번 주 누적(거리/시간) 기준, 보너스를 더 올릴 수 있는 가장 가까운 목표 반환.
        반환: (현재보너스, 거리목표 or None, 시간목표 or None)
          목표 = {"need": 더 필요한 양, "target": 임계값, "total": 달성 시 보너스, "gain": 증가분}
        """
        current = cls.weekly_bonus(mileage_km, minutes)
        dist_goal = time_goal = None
        for pts, kind, thr in sorted(cls.WEEKLY, key=lambda x: x[0]):  # 점수 오름차순
            if pts <= current:
                continue
            if kind == "dist" and dist_goal is None:
                dist_goal = {"need": round(thr - mileage_km, 2), "target": thr, "total": pts, "gain": pts - current}
            elif kind == "time" and time_goal is None:
                time_goal = {"need": int(thr - minutes), "target": thr, "total": pts, "gain": pts - current}
        return current, dist_goal, time_goal

    @classmethod
    def weekly_bonus(cls, mileage_km: float, minutes: float) -> int:
        """한 주 누적 거리/시간으로 주간 보너스(최고 1개) 산정."""
        best = 0
        for pts, kind, thr in cls.WEEKLY:
            ok = (kind == "dist" and mileage_km >= thr) or (kind == "time" and minutes >= thr)
            if ok and pts > best:
                best = pts
        return best

    # ──────────────── 안내 문구 ────────────────
    @classmethod
    def daily_lines(cls) -> list:
        label = {
            "dist": lambda t: f"{t}km 이상 러닝",
            "time": lambda t: f"{int(t)}분 이상 러닝",
            "either": lambda t: f"{t[0]}km 또는 {int(t[1])}분 이상 러닝",
        }
        return [f"{label[k](t)} → {p}점" for p, k, t in cls.DAILY]

    @classmethod
    def weekly_lines(cls) -> list:
        label = {
            "dist": lambda t: f"주간 마일리지 {t}km 달성",
            "time": lambda t: f"주간 러닝시간 {int(t)}분 달성",
        }
        return [f"{label[k](t)} → +{p}점" for p, k, t in cls.WEEKLY]

    @classmethod
    def event_lines(cls) -> list:
        return [f"{name} → +{pts}점" for name, pts in cls.EVENT_TYPES.values()]

    @classmethod
    def shop_lines(cls) -> list:
        return [f"{name} → {cost:,}점" for name, cost in cls.SHOP_ITEMS]
