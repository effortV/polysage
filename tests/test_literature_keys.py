"""文献 API：OpenAlex 多个 key 轮换（失效 / 限流自动换下一个）、Wiley TDM 取全文。全程不联网。"""
from __future__ import annotations

import httpx
import pytest

from polysage.config import settings
from polysage.sources import openalex, wiley


@pytest.fixture
def oa(monkeypatch):
    monkeypatch.setattr(settings, "openalex_api_keys_raw", "KEY1aaaaaaaaaa,\nKEY2bbbbbbbbbb；KEY3cccccccccc")
    monkeypatch.setattr(settings, "openalex_api_key", "KEY1aaaaaaaaaa")
    monkeypatch.setattr(openalex.time, "sleep", lambda s: None)
    openalex._state.clear()
    calls: list[str] = []
    codes: dict[str, int] = {}

    def fake_request(url, params, key):
        calls.append(key)
        code = codes.get(key, 200)
        return httpx.Response(code, json={"results": [], "meta": {}} if code == 200 else {"error": "x"})

    monkeypatch.setattr(openalex, "_request", fake_request)
    yield calls, codes
    openalex._state.clear()


def test_keys_merge_and_dedupe(oa):
    assert settings.openalex_keys == ["KEY1aaaaaaaaaa", "KEY2bbbbbbbbbb", "KEY3cccccccccc"]
    assert openalex.key_report()[0] == ("KEY1…aaaa", "未检查")


def test_rotation_skips_bad_and_limited_keys(oa):
    calls, codes = oa
    codes.update({"KEY1aaaaaaaaaa": 401, "KEY2bbbbbbbbbb": 429})
    assert openalex.search("LLDPE film") == []
    assert calls == ["KEY1aaaaaaaaaa", "KEY2bbbbbbbbbb", "KEY2bbbbbbbbbb", "KEY3cccccccccc"]   # 429 先歇一下重试一次
    assert dict(openalex.key_report()) == {"KEY1…aaaa": "无效", "KEY2…bbbb": "限流", "KEY3…cccc": "可用"}
    calls.clear()
    openalex.search("LLDPE film")
    assert calls == ["KEY3cccccccccc"]                                                            # 坏的冷却期内不再用


def test_all_keys_bad_falls_back_to_no_key(oa):
    calls, codes = oa
    codes.update({k: 403 for k in settings.openalex_keys})
    assert openalex.search("x") == [] and calls[-1] == ""                                         # 最后不带 key 试一次
    codes[""] = 403
    openalex._state.clear()
    with pytest.raises(RuntimeError, match="OpenAlex"):
        openalex.search("x")


def test_check_keys_reports_each(oa):
    _calls, codes = oa
    codes.update({"KEY2bbbbbbbbbb": 403})
    assert openalex.check_keys() == [("KEY1…aaaa", "可用"), ("KEY2…bbbb", "无效"), ("KEY3…cccc", "可用")]


def test_wiley_fulltext_and_chain(monkeypatch):
    from polysage.ingest import parsers, pipeline
    from polysage.sources import unpaywall
    from polysage.sources.base import SearchHit

    monkeypatch.setattr(settings, "wiley_tdm_token", "wiley-token-xyz")
    monkeypatch.setattr(settings, "elsevier_api_key", "")
    monkeypatch.setattr(wiley.time, "sleep", lambda s: None)
    seen = {}

    def fake_download(doi, token):
        seen["doi"], seen["token"] = doi, token
        return httpx.Response(200, content=b"%PDF-1.7 fake")

    monkeypatch.setattr(wiley, "_download", fake_download)
    monkeypatch.setattr(parsers, "parse_pdf", lambda path: ("正文 " * 1000, {}))
    assert wiley.is_wiley("https://doi.org/10.1002/app.51234") and not wiley.is_wiley("10.1016/j.polymer.2020.1")
    assert wiley.get_fulltext("10.1016/j.polymer.2020.1") is None                                # 不是 Wiley 的不试
    text, path = wiley.get_fulltext("10.1002/app.51234")
    assert seen == {"doi": "10.1002/app.51234", "token": "wiley-token-xyz"} and path.endswith(".pdf") and len(text) > 1500

    monkeypatch.setattr(unpaywall, "find_oa_pdf", lambda doi: ("", ""))
    hit = SearchHit(provider="openalex", external_id="W9", title="LLDPE blown film", source_type="paper", doi="10.1002/app.51234")
    body, fpath, note = pipeline._try_fulltext(hit)
    assert note == "Wiley 全文（TDM）" and body and fpath

    monkeypatch.setattr(wiley, "_download", lambda doi, token: httpx.Response(403, text="not entitled"))
    assert wiley.get_fulltext("10.1002/app.51234") is None                                        # 没权限就算了
