"""
자료실 파일 저장소.

- 업로드된 실제 파일은 data/uploads/ 에 충돌 없는 이름(uuid+확장자)으로 저장
- 메타데이터(원본 파일명, 크기, 업로드 일시, 다운로드 수)는 data/files.json 에 보관
- 다운로드는 파일 id 로만 조회하므로 경로 탈출(path traversal)이 발생하지 않는다.
"""
import json
import os
import re
import uuid
from datetime import datetime

UPLOAD_DIR = "data/uploads"
META_PATH = "data/files.json"


def _ensure():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    if not os.path.exists(META_PATH):
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump([], f)


def _load() -> list:
    _ensure()
    with open(META_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(items: list):
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _safe_name(name: str) -> str:
    """표시/다운로드용 원본 파일명 정리(경로 제거 및 위험문자 치환)."""
    name = os.path.basename(name or "").strip()
    name = name.replace("\x00", "")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "file"


def list_files() -> list:
    """업로드 일시 내림차순으로 메타데이터 반환."""
    items = _load()
    items.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    return items


def add_file(original_name: str, content: bytes) -> dict:
    """파일 저장 후 메타데이터 반환."""
    _ensure()
    safe = _safe_name(original_name)
    ext = os.path.splitext(safe)[1]
    file_id = uuid.uuid4().hex
    stored = file_id + ext

    with open(os.path.join(UPLOAD_DIR, stored), "wb") as f:
        f.write(content)

    meta = {
        "id": file_id,
        "name": safe,
        "stored": stored,
        "size": len(content),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "downloads": 0,
    }
    items = _load()
    items.append(meta)
    _save(items)
    return meta


def get_file(file_id: str):
    """id 로 메타데이터 조회. 없으면 None."""
    for it in _load():
        if it.get("id") == file_id:
            return it
    return None


def file_path(meta: dict) -> str:
    return os.path.join(UPLOAD_DIR, meta["stored"])


def increment_download(file_id: str):
    items = _load()
    for it in items:
        if it.get("id") == file_id:
            it["downloads"] = it.get("downloads", 0) + 1
            break
    _save(items)


def delete_file(file_id: str) -> bool:
    """파일과 메타데이터 삭제. 성공 시 True."""
    items = _load()
    target = next((it for it in items if it.get("id") == file_id), None)
    if not target:
        return False
    try:
        p = os.path.join(UPLOAD_DIR, target["stored"])
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    items = [it for it in items if it.get("id") != file_id]
    _save(items)
    return True


def human_size(n: int) -> str:
    """바이트를 사람이 읽기 쉬운 단위로."""
    size = float(n or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
