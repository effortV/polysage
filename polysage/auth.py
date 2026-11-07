"""访问密码与核心密钥：都只存加盐哈希（.env 里的 APP_PASSWORD_HASH / ADMIN_KEY_HASH），不存明文。

改访问密码必须先输对核心密钥；核心密钥连续输错 5 次，锁 10 分钟。

登录后可在这台设备上记住 30 天：浏览器里存一个签名令牌（到期时间 + 签名），签名里带着当前访问密码的哈希，
所以一改密码，所有设备上记住的登录都自动失效，要用新密码重新登录。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from .config import DATA_DIR, settings

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


# ---------------- 记住登录（30 天） ----------------

REMEMBER_DAYS = 30
COOKIE = "hn_login"
_SECRET_FILE = DATA_DIR / ".login_secret"


def _login_secret() -> bytes:
    """签名用的服务器随机密钥（data/ 下，不进代码库）；第一次用时生成。"""
    try:
        return bytes.fromhex(_SECRET_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_SECRET_FILE, "x", encoding="utf-8") as f:          # 两个会话同时生成时只认先写的那个
            f.write(secrets.token_hex(32))
        os.chmod(_SECRET_FILE, 0o600)
    except FileExistsError:
        pass
    except OSError:
        pass
    return bytes.fromhex(_SECRET_FILE.read_text(encoding="utf-8").strip())


def _password_material() -> str:
    if settings.app_password_hash:
        return settings.app_password_hash                              # 带盐：同一个密码重设一次也会变
    return "plain:" + hashlib.sha256((settings.app_password or "").encode("utf-8")).hexdigest()


def _sign(exp: int) -> str:
    return hmac.new(_login_secret(), f"{exp}|{_password_material()}".encode("utf-8"), hashlib.sha256).hexdigest()


def session_mark() -> str:
    """本会话“已登录”的标记：跟着访问密码变，改了密码，开着的页面下一次操作也要重新登录。"""
    return hashlib.sha256(_password_material().encode("utf-8")).hexdigest()[:16]


def issue_token(now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + REMEMBER_DAYS * 86400)
    return f"{exp}.{_sign(exp)}"


def token_ok(token: str | None, now: float | None = None) -> bool:
    try:
        exp_s, sig = (token or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    now = now if now is not None else time.time()
    if not (now < exp <= now + REMEMBER_DAYS * 86400 + 60):
        return False
    return hmac.compare_digest(sig, _sign(exp))


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
    return True, "访问密码已修改：之前记住登录的设备都已失效，下次打开网页要用新密码登录。"
