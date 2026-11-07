"""记住登录 30 天（改密码即失效）与配方助手的历史对话（打开 / 删除 / 新对话不堆空会话）。"""
from __future__ import annotations

import time

from polysage import auth
from polysage.agents import runner
from polysage.config import settings


def test_remember_login_token(monkeypatch):
    monkeypatch.setattr(settings, "app_password", "")
    monkeypatch.setattr(settings, "app_password_hash", auth.hash_secret("first-pass"))
    tok = auth.issue_token()
    assert auth.token_ok(tok)
    exp = int(tok.split(".")[0])
    assert 29.9 * 86400 < exp - time.time() <= 30 * 86400 + 1                     # 30 天
    assert not auth.token_ok(tok, now=time.time() + 31 * 86400)                   # 过期
    assert not auth.token_ok(f"{exp + 3600}.{tok.split('.')[1]}")                  # 改了到期时间，签名对不上
    assert not auth.token_ok("") and not auth.token_ok(None) and not auth.token_ok("abc")
    mark = auth.session_mark()
    # 改密码（哪怕改回同一个密码，盐不同）：之前记住的登录、开着的会话标记都失效
    monkeypatch.setattr(settings, "app_password_hash", auth.hash_secret("first-pass"))
    assert not auth.token_ok(tok) and auth.session_mark() != mark
    assert auth.token_ok(auth.issue_token())                                     # 用新密码重新登录后又能记住
    # 旧版明文密码也一样：改了就失效
    monkeypatch.setattr(settings, "app_password_hash", "")
    monkeypatch.setattr(settings, "app_password", "plain-1")
    t2 = auth.issue_token()
    assert auth.token_ok(t2)
    monkeypatch.setattr(settings, "app_password", "plain-2")
    assert not auth.token_ok(t2)
    assert "first-pass" not in auth._SECRET_FILE.read_text(encoding="utf-8")      # 服务器密钥里没有密码


def test_chat_history_list_open_delete():
    before = {s["id"] for s in runner.list_sessions(include_archived=True)}
    a = runner.create_session("advisor")
    runner._add(a, "user", "性能不变或者提升，最低成本是多少？这句话很长很长很长很长很长很长很长很长很长很长，再加一点")
    runner._add(a, "assistant", "答：8515 元/吨")
    b = runner.create_session("advisor")
    runner._add(b, "user", "现在的价格是多少？")
    runner._add(b, "user", "按期货算呢？")
    empty = runner.create_session("advisor")
    chats = [c for c in runner.list_chats("advisor") if c["id"] not in before]   # 只看本测试建的
    assert [c["id"] for c in chats] == [b, a]                                   # 新的在前，空对话不列
    assert chats[0]["label"] == "现在的价格是多少？" and chats[0]["n_q"] == 2
    assert chats[1]["label"].endswith("…") and len(chats[1]["label"]) == 41     # 第一个问题当标题，太长截断
    # 新对话：已经有一个空的就接着用，不再多建
    for s in runner.list_sessions(include_archived=True):
        assert s["id"] in before or s["id"] in (a, b, empty)
    assert runner.new_chat("advisor", current=b) == empty
    assert runner.new_chat("advisor", current=empty) == empty
    # 删除：会话和问答记录一起删
    runner.delete_session(a)
    assert runner.get_session(a) is None and runner.messages(a) == []
    assert [c["id"] for c in runner.list_chats("advisor") if c["id"] not in before] == [b]
    runner._add(empty, "user", "新问题")
    assert runner.new_chat("advisor", current=empty) not in (a, b, empty)       # 当前这个问过了才新建
    for sid in (b, empty):
        runner.delete_session(sid)
