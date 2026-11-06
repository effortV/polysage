"""访问密码与核心密钥：都只存加盐哈希（.env 里的 APP_PASSWORD_HASH / ADMIN_KEY_HASH），不存明文。

改访问密码必须先输对核心密钥；核心密钥连续输错 5 次，锁 10 分钟。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .config import settings

ITERATIONS = 240_000
MAX_FAILS = 5
LOCK_SECONDS = 600
_fails: list[float] = []           # 核心密钥输错的时间（进程内，所有会话共用）


def hash_secret(secret: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${dk.hex()}"


def verify(secret: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (secret or "").encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


# ---------------- 访问密码（打开网页时要输的） ----------------

def password_required() -> bool:
    return bool(settings.app_password_hash or settings.app_password)


def check_password(pw: str) -> bool:
    if settings.app_password_hash:
        return verify(pw, settings.app_password_hash)
    if settings.app_password:
        return hmac.compare_digest((pw or "").encode("utf-8"), settings.app_password.encode("utf-8"))
    return True


# ---------------- 核心密钥 ----------------

def admin_key_set() -> bool:
    return bool(settings.admin_key_hash)


def locked_seconds() -> int:
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < LOCK_SECONDS]
    return int(LOCK_SECONDS - (now - _fails[0])) if len(_fails) >= MAX_FAILS else 0


def check_admin_key(key: str) -> tuple[bool, str]:
    if not admin_key_set():
        return False, "服务器上还没有设置核心密钥，请联系管理员。"
    wait = locked_seconds()
    if wait:
        return False, f"核心密钥输错次数太多，请 {wait // 60 + 1} 分钟后再试。"
    if verify(key, settings.admin_key_hash):
        _fails.clear()
        return True, ""
    _fails.append(time.time())
    left = MAX_FAILS - len(_fails)
    return False, "核心密钥不对。" + (f"还能再试 {left} 次。" if left > 0 else "已锁定 10 分钟。")


def change_password(admin_key: str, new_pw: str, confirm: str) -> tuple[bool, str]:
    ok, msg = check_admin_key(admin_key)
    if not ok:
        return False, msg
    if len(new_pw or "") < 6:
        return False, "新密码至少 6 位。"
    if new_pw != confirm:
        return False, "两次输入的新密码不一致。"
    from .envfile import set_env

    set_env({"APP_PASSWORD_HASH": hash_secret(new_pw)}, remove=["APP_PASSWORD"])
    return True, "访问密码已修改：其他人下次打开网页要用新密码。"
