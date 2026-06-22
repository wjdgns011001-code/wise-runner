import requests
import base64
import json
import re
import os
import yaml
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from utils.score import Score

def build_record_instruction() -> str:
    """현재 연도/날짜를 주입한 추출 프롬프트를 생성한다."""
    today = datetime.now()
    current_year = today.year
    today_str = today.strftime("%Y-%m-%d")
    return f"""
        # Role
        You are a highly intelligent data extraction agent for fitness activity screenshots. You can identify metrics regardless of the UI layout (horizontal, vertical, or grid).

        # Task
        Analyze the provided fitness activity image and extract the following metrics. Return ONLY the raw JSON.

        # Mapping Logic (Keywords)
        Find the value even if the label varies slightly:
        - "date": Look for date formats (e.g., 5월 2일, 05-02, 2026-05-02).
          **IMPORTANT (year rule): If the YEAR is NOT explicitly shown in the image (e.g. only "5월 2일" or "05-02" is visible), you MUST use the current year {current_year}. Never guess a past year.**
          Use "{today_str}" only if no date is found at all.
        - "total_time": Look for "시간", "총 시간", "Duration". Convert to HH:MM:SS.
        - "total_distance": Look for "거리", "킬로미터", "km", "Distance". (Number only)
        - "cadence": Loof for "케이던스", "평균 케이던스", "spm". (Number only)
        - "average_pace": Look for "평균 페이스", "페이스", "Pace". 
        - "average_heart_rate": Look for "평균 심박수", "bpm", "Heart Rate". (Number only)
        - "total_calories": Look for "칼로리", "총 칼로리", "kcal", "Calories". (Number only)

        # Output JSON Structure
        {{
        "date": "yyyy-mm-dd",
        "total_time": "HH:MM:SS",
        "total_distance": 0.00,
        "cadence": 0,
        "average_pace": MM:SS,
        "average_heart_rate": 0,
        "total_calories": 0
        }}

        # Constraints
        - Output ONLY the JSON string. No explanations.
        - If a value is missing, use null.
        """

other_instruction = """
        # Role
        You are a friendly and witty lifestyle commentator. Your goal is to react to the user's photo with a touch of humor and warmth.

        # Task
        Analyze the provided image and provide a brief, engaging comment. 

        # Guidelines
        - If the image is a selfie or a group photo, compliment their energy or appearance.
        - If it's a food photo, react to how delicious it looks.
        - If it's a landscape or object, describe its vibe briefly.
        - Since the user likely intended to upload a running record, end the comment by gently mentioning that you couldn't find any running data.
        - **Tone**: Professional yet adaptive, supportive, and slightly witty.
        - **Constraint**: Keep the response under 2-3 sentences. Do not use JSON.

        # Constraint
        - Respond in under 50 characters. (50자 이내로 짧게 대답하세요.)
        - Use simple sentences.

        # Example Output
        - "정말 멋진 풍경이네요! 사진만 봐도 힐링 되는 기분이에요. 다만, 이번 사진에서는 러닝 기록을 찾지 못했어요. 기록 화면을 다시 보여주시겠어요?"
        - "와! 다들 표정이 너무 밝으셔서 에너지가 여기까지 느껴지네요. 👍 아쉽게도 상세 기록 수치가 없어서 이번 러닝은 저장하지 못했어요!"
"""


class Record(BaseModel):
    # 저장에 반드시 필요한 값: 러닝 거리(total_distance)와 러닝 시간(total_time).
    # 그 외 값은 사진마다 없을 수 있으므로 모두 선택값으로 둔다.
    user_id: Optional[str] = None
    date: Optional[str] = Field(default=None, validate_default=True, description="YYYY-MM-DD 형식의 활동 날짜")
    total_time: str = Field(..., description="HH:MM:SS 형식의 총 시간")
    total_distance: float = Field(..., description="킬로미터(km) 단위 거리")
    cadence: Optional[int] = Field(default=None, description="케이던스")
    average_pace: Optional[str] = Field(default=None, description="초 단위 평균 페이스")
    average_heart_rate: Optional[int] = None
    total_calories: Optional[int] = None
    score: Optional[int] = None

    @field_validator('date', mode='before')
    @classmethod
    def set_default_date(cls, v):
        # 날짜를 추출하지 못한 경우 오늘 날짜로 대체
        if not v:
            return datetime.now().strftime("%Y-%m-%d")
        return v


class Connect:
    def __init__(self, ip: str, port: int, model_name: str, api_token: str):
        self.ip = ip
        self.port = port
        self.model_name = model_name
        self.api_token = api_token  # 토큰 저장
        # f-string을 사용하여 URL 자동 생성
        self.server_url = f"http://{self.ip}:{self.port}/v1/chat/completions"

    @classmethod
    def from_yaml(cls, config_path="config.yaml"):
        """YAML 파일에서 설정을 읽어 Connect 인스턴스를 생성하는 팩토리 메서드"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        connect_data = config_data.get("connect", {})

        # 언팩 연산자(**)를 사용하여 클래스 생성자에 데이터 전달
        return cls(
            ip=connect_data.get("ip"),
            port=connect_data.get("port"),
            model_name=connect_data.get("model_name"),
            api_token=connect_data.get("api_token")
        )

    def get_image_base64_from_url(self, image_url):
        # 1. URL에서 이미지를 먼저 내 메모리로 가져옴
        response = requests.get(image_url)
        if response.status_code == 200:
            # 2. 가져온 바이너리 데이터를 Base64로 인코딩
            return base64.b64encode(response.content).decode("utf-8")
        else:
            raise Exception("이미지를 다운로드할 수 없습니다.")

    def get_vl_description(self, system_instruction: str, image_url: str):
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": system_instruction},
                        {"type": "image_url", "image_url": {"url": f"{image_url}"}},
                    ],
                }
            ],
            "temperature": 0.1,  # 일관된 답변을 위해 낮게 설정
            "max_tokens": 2048,  # 문서 양에 따라 조절
        }

        headers = {"Content-Type": "application/json"}

        headers["Authorization"] = f"Bearer {self.api_token}"

        # 4. API 호출
        response = requests.post(
            f"{self.server_url}", headers=headers, data=json.dumps(payload)
        )

        # 5. 결과 처리
        if response.status_code == 200:
            result = response.json()
            raw_content = result["choices"][0]["message"]["content"]
            return raw_content
        else:
            return f"Error: {response.status_code}, {response.text}"

    def __call__(self, image_url: str, user_id: str):
        # image_url = self.get_image_base64_from_url(image_url)

        try:
            raw_content = self.get_vl_description(
                system_instruction=build_record_instruction(), image_url=image_url
            )
            match = re.search(r"(\{.*\})", raw_content, re.DOTALL)
            json_str = match.group(1)
            data = json.loads(json_str)

            # 저장 최소 조건: 러닝 거리와 러닝 시간이 모두 존재해야 함.
            # 둘 중 하나라도 없으면 일반 사진으로 간주하여 except 분기로 처리.
            if not data.get("total_distance") or not data.get("total_time"):
                raise ValueError("러닝 거리/시간이 없어 저장하지 않습니다.")

            result = Record(**data)
            minutes = Score.parse_minutes(result.total_time)
            result.score = Score().calculate_daily_point(result.total_distance, minutes)
            result.user_id = user_id
            return result
        except Exception as e:
            print(e)
            # 일반적인 사진이라 가정
            raw_content = self.get_vl_description(
                system_instruction=other_instruction, image_url=image_url
            )
            return raw_content
