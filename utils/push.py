"""
웹 푸시(Web Push) 알림.

- VAPID 키: data/vapid.json 에 1회 생성 후 영구 보관 (구독은 공개키에 묶이므로 재생성 금지)
- 구독 정보: data/push_subs.json — { user_id: [subscription, ...] }
- 발송은 백그라운드 스레드로 처리하여 요청을 막지 않음. 만료된 구독(404/410)은 자동 정리.
"""
import json
import os
import base64
import threading

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization as _ser
from pywebpush import webpush, WebPushException
from py_vapid import Vapid01

VAPID_PATH = "data/vapid.json"
SUBS_PATH = "data/push_subs.json"
VAPID_CLAIMS = {"sub": "mailto:admin@wiserunners.app"}


# ──────────────── VAPID 키 ────────────────

def _gen_vapid():
    pk = ec.generate_private_key(ec.SECP256R1())
    priv_pem = pk.private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()
    ).decode()
    pub_b64 = base64.urlsafe_b64encode(
        pk.public_key().public_bytes(_ser.Encoding.X962, _ser.PublicFormat.UncompressedPoint)
    ).rstrip(b"=").decode()
    return {"private_pem": priv_pem, "public_key": pub_b64}


def _load_vapid() -> dict:
    if os.path.exists(VAPID_PATH):
        with open(VAPID_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    os.makedirs(os.path.dirname(VAPID_PATH), exist_ok=True)
    v = _gen_vapid()
    with open(VAPID_PATH, "w", encoding="utf-8") as f:
        json.dump(v, f)
    return v


def public_key() -> str:
    """브라우저 구독에 쓰는 VAPID 공개키(application server key)."""
    return _load_vapid()["public_key"]


# ──────────────── 구독 저장 ────────────────

def _load_subs() -> dict:
    if not os.path.exists(SUBS_PATH):
        return {}
    try:
        with open(SUBS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_subs(data: dict):
    os.makedirs(os.path.dirname(SUBS_PATH), exist_ok=True)
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_subscription(user_id: str, subscription: dict):
    """기기 구독 등록 (endpoint 기준 중복 제거)."""
    data = _load_subs()
    subs = data.get(user_id, [])
    ep = subscription.get("endpoint")
    subs = [s for s in subs if s.get("endpoint") != ep]
    subs.append(subscription)
    data[user_id] = subs
    _save_subs(data)


def _remove_endpoint(endpoint: str):
    data = _load_subs()
    changed = False
    for uid in list(data.keys()):
        new = [s for s in data[uid] if s.get("endpoint") != endpoint]
        if len(new) != len(data[uid]):
            changed = True
            data[uid] = new
    if changed:
        _save_subs(data)


# ──────────────── 발송 ────────────────

def _send_now(target_user_ids, title, body, url):
    vapid = _load_vapid()
    vp = Vapid01.from_pem(vapid["private_pem"].encode())  # pywebpush 는 Vapid 객체를 받아야 함
    data = _load_subs()
    payload = json.dumps({"title": title, "body": body, "url": url})
    for uid in target_user_ids:
        for sub in list(data.get(uid, [])):
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=vp,
                    vapid_claims=dict(VAPID_CLAIMS),
                )
            except WebPushException as e:
                # 만료/무효 구독 정리
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (404, 410):
                    _remove_endpoint(sub.get("endpoint"))
            except Exception:
                pass


def notify(target_user_ids, title, body, url="/manage"):
    """백그라운드로 푸시 발송 (요청을 막지 않음)."""
    target_user_ids = [u for u in set(target_user_ids) if u]
    if not target_user_ids:
        return
    threading.Thread(
        target=_send_now, args=(target_user_ids, title, body, url), daemon=True
    ).start()


def subscribed_user_ids() -> list:
    return list(_load_subs().keys())
