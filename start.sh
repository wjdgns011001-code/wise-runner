#!/bin/bash
# 러닝 트래커 (재)기동 스크립트 — 어느 PC(macOS/Linux)에서도 동작하도록 작성.
PORT=5000

# 스크립트가 위치한 디렉토리로 이동 (절대경로 의존 제거)
cd "$(dirname "$0")" || exit 1

# 포트를 LISTEN 중인 기존 서버만 종료 (ngrok 등 '연결'은 건드리지 않음)
if command -v lsof >/dev/null 2>&1; then
  PID=$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null)
  if [ -n "$PID" ]; then
    echo "포트 $PORT (PID: $PID) 기존 서버를 종료합니다."
    kill -9 $PID
    sleep 2
  else
    echo "포트 $PORT 를 사용 중인 서버가 없습니다."
  fi
fi

# 파이썬 인터프리터 선택: 프로젝트 가상환경(.venv/venv) 우선, 없으면 python3
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  PY="python3"
fi

# 백그라운드 실행 (로그: running.log)
nohup "$PY" main.py > running.log 2>&1 &
echo "러닝 트래커가 백그라운드에서 시작되었습니다 (포트 $PORT). 로그: running.log"
