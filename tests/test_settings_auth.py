"""设置：服务商随便加、切换与备用；访问密码只存哈希，改密码要核心密钥，输错多次锁定。"""
from __future__ import annotations

import pytest

from polysage import auth, envfile, providers
from polysage.config import settings


def test_providers_add_edit_switch(monkeypatch):
    pid = providers.upsert({"name": "通义千问", "base_url": "https://dashscope.example/compatible-mode/v1/",
                            "model": "qwen-plus", "api_key": "sk-abcdef123456", "thinking": "enable_thinking"})
    p = providers.get(pid)
    assert p["base_url"].endswith("/v1") and providers.masked(p["api_key"]) == "sk-…3456"
    providers.upsert({"id": pid, "name": "通义千问", "base_url": p["base_url"], "model": "qwen-max", "api_key": ""})
    assert providers.get(pid)["api_key"] == "sk-abcdef123456" and providers.get(pid)["model"] == "qwen-max"   # 密钥留空不改
    providers.set_active(pid)
    assert settings.llm_profile == pid and settings.chat_model == "qwen-max"
    assert "LLM_PROFILE=" + pid in envfile.ENV_PATH.read_text(encoding="utf-8")                             # 重启也保持
    with pytest.raises(ValueError):
        providers.delete(pid)                                                                               # 正在用的不能删
    monkeypatch.setattr(settings, "sf_api_key", "")
    assert providers.fallback(pid) is None                                                                  # 硅基流动一把钥匙都没有
    monkeypatch.setattr(settings, "sf_api_key", "fake")
    assert providers.fallback(pid) == "siliconflow"                                                         # 用向量检索那把也算配齐
    providers.upsert({"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
                      "model": "deepseek-ai/DeepSeek-V4-Pro", "api_key": "sk-sf-999999", "thinking": "enable_thinking"})
    assert "SILICONFLOW_API_KEY=sk-sf-999999" in envfile.ENV_PATH.read_text(encoding="utf-8")              # 向量检索同步


def test_password_hash_and_admin_key(monkeypatch):
    stored = auth.hash_secret("core-key-!@#")
    assert stored.startswith("pbkdf2_sha256$") and "core-key" not in stored
    assert auth.verify("core-key-!@#", stored) and not auth.verify("wrong", stored)
    monkeypatch.setattr(settings, "admin_key_hash", stored)
    monkeypatch.setattr(settings, "app_password", "old-plain")
    monkeypatch.setattr(settings, "app_password_hash", "")
    assert auth.password_required() and auth.check_password("old-plain")
    envfile.ENV_PATH.write_text("APP_PASSWORD=old-plain\nOTHER=1\n", encoding="utf-8")
    auth._fails.clear()
    ok, msg = auth.change_password("wrong", "newpass1", "newpass1")
    assert not ok and "核心密钥不对" in msg
    ok, _ = auth.change_password("core-key-!@#", "123", "123")
    assert not ok                                                                                          # 太短
    ok, _ = auth.change_password("core-key-!@#", "newpass1", "newpass2")
    assert not ok                                                                                          # 两次不一致
    ok, msg = auth.change_password("core-key-!@#", "newpass1", "newpass1")
    assert ok
    text = envfile.ENV_PATH.read_text(encoding="utf-8")
    assert "APP_PASSWORD_HASH=pbkdf2_sha256$" in text and "APP_PASSWORD=old-plain" not in text and "OTHER=1" in text
    assert settings.app_password_hash and auth.check_password("newpass1") and not auth.check_password("old-plain")


def test_admin_key_lockout(monkeypatch):
    monkeypatch.setattr(settings, "admin_key_hash", auth.hash_secret("right"))
    auth._fails.clear()
    for _ in range(auth.MAX_FAILS):
        assert not auth.check_admin_key("nope")[0]
    ok, msg = auth.check_admin_key("right")
    assert not ok and "分钟后再试" in msg                                                                  # 锁定期间对的也不放
    auth._fails.clear()
    assert auth.check_admin_key("right")[0]


def test_no_admin_key_means_no_change(monkeypatch):
    monkeypatch.setattr(settings, "admin_key_hash", "")
    ok, msg = auth.change_password("anything", "newpass1", "newpass1")
    assert not ok and "还没有设置核心密钥" in msg


def test_orchestration_tools_registered():
    from polysage.agents import roles
    from polysage.agents import tools as T

    adv = roles.ROLES["advisor"]
    for name in ("start_research", "refresh_market", "background_status"):
        assert name in T._REGISTRY and name in adv.tools
    text, _ = T.run("background_status", {})
    assert "后台" in text


def test_web_price_cannot_be_actual(monkeypatch):
    from polysage.agents import tools as T

    seen = {}

    def fake_record(code, price, price_type, **kw):
        seen["type"] = price_type
        return {"base_cost": None, "n_flagged": 0, "top5": []}

    from polysage import pricing

    monkeypatch.setattr(pricing, "record_price", fake_record)
    text, _ = T.run("add_price", {"code": "RL", "price": 5150, "price_type": "actual", "source": "网查·东莞某经营部，未电话询价核实"})
    assert seen["type"] == "estimate" and "按估计价登记" in text
    T.run("add_price", {"code": "R1", "price": 8250, "price_type": "actual", "source": "甲方口述实际进价"})
    assert seen["type"] == "actual"
