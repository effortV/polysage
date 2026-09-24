"""路径、环境变量与全局设置。所有模块只从这里取配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _load_streamlit_secrets() -> None:
    """部署在 Streamlit Community Cloud 时没有 .env：把 st.secrets 里的大写键并入环境变量（本地 .env 优先）。"""
    import sys

    if "streamlit" not in sys.modules:
        return
    try:
        import streamlit as st

        for k, v in st.secrets.items():
            if isinstance(v, (str, int, float)) and str(k).isupper() and not os.getenv(k):
                os.environ[k] = str(v)
    except Exception:  # noqa: BLE001  无 secrets 文件或非 Streamlit 运行环境
        pass


_load_streamlit_secrets()

# POLYSAGE_HOME：数据与知识库根目录（测试或多项目并行时可指向别处），默认为项目目录
HOME = Path(os.getenv("POLYSAGE_HOME") or ROOT)
DATA_DIR = HOME / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
UPLOAD_DIR = DATA_DIR / "uploads"
INDEX_DIR = DATA_DIR / "index"
DB_PATH = DATA_DIR / "polysage.db"

KB_DIR = HOME / "knowledge"
KB = {
    "project": KB_DIR / "00_项目",
    "material": KB_DIR / "01_材料卡",
    "literature": KB_DIR / "02_文献卡",
    "patent": KB_DIR / "03_专利卡",
    "price": KB_DIR / "04_价格卡",
    "formulation": KB_DIR / "05_配方库",
    "experiment": KB_DIR / "06_实验数据",
    "model": KB_DIR / "07_模型",
    "review": KB_DIR / "08_复盘",
}

for _p in [DATA_DIR, DOWNLOAD_DIR, UPLOAD_DIR, INDEX_DIR, *KB.values()]:
    _p.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Settings:
    openalex_api_key: str = field(default_factory=lambda: _env("OPENALEX_API_KEY"))
    openalex_mailto: str = field(default_factory=lambda: _env("OPENALEX_MAILTO"))
    elsevier_api_key: str = field(default_factory=lambda: _env("ELSEVIER_API_KEY"))
    elsevier_insttoken: str = field(default_factory=lambda: _env("ELSEVIER_INSTTOKEN"))
    unpaywall_email: str = field(default_factory=lambda: _env("UNPAYWALL_EMAIL"))
    semantic_scholar_api_key: str = field(default_factory=lambda: _env("SEMANTIC_SCHOLAR_API_KEY"))
    tavily_api_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY"))
    # 搜索 API（服务器 IP 被必应降级 / 搜狗 403 / 360 验证码时的正路，任配一个即可）
    bocha_api_key: str = field(default_factory=lambda: _env("BOCHA_API_KEY"))
    zhipu_api_key: str = field(default_factory=lambda: _env("ZHIPU_API_KEY"))
    # 访问口令：设置后打开界面要先输入；对外网开放（隧道）前必须设置
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD"))

    sf_api_key: str = field(default_factory=lambda: _env("SILICONFLOW_API_KEY"))
    sf_base_url: str = field(default_factory=lambda: _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"))
    sf_chat_model: str = field(default_factory=lambda: _env("SILICONFLOW_CHAT_MODEL", "deepseek-ai/DeepSeek-V4-Pro"))
    sf_embedding_model: str = field(default_factory=lambda: _env("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"))
    sf_rerank_model: str = field(default_factory=lambda: _env("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"))

    # 第二套对话模型（OpenAI 兼容），如浙大膜中心 zju-qwen；LLM_PROFILE 决定当前用哪套
    llm_profile: str = field(default_factory=lambda: _env("LLM_PROFILE", "siliconflow"))
    zju_api_key: str = field(default_factory=lambda: _env("ZJU_API_KEY"))
    zju_base_url: str = field(default_factory=lambda: _env("ZJU_BASE_URL", "https://api.zjumembrane.cn/v1"))
    zju_chat_model: str = field(default_factory=lambda: _env("ZJU_CHAT_MODEL", "zju-qwen"))
    zju_supports_thinking: bool = field(default_factory=lambda: _env("ZJU_SUPPORTS_THINKING", "0") in ("1", "true", "True"))

    http_timeout: float = 40.0
    user_agent: str = "PolySage/0.1 (packaging-film R&D; contact via OPENALEX_MAILTO)"

    # ---- 当前对话模型（按 llm_profile 解析）----
    @property
    def chat_api_key(self) -> str:
        return self.zju_api_key if self.llm_profile == "zju" else self.sf_api_key

    @property
    def chat_base_url(self) -> str:
        return self.zju_base_url if self.llm_profile == "zju" else self.sf_base_url

    @property
    def chat_model(self) -> str:
        return self.zju_chat_model if self.llm_profile == "zju" else self.sf_chat_model

    @property
    def chat_supports_thinking(self) -> bool:
        """是否向对话接口发送 enable_thinking / thinking_budget（硅基流动 DeepSeek 支持；其他服务未知时不发）。"""
        return self.zju_supports_thinking if self.llm_profile == "zju" else True

    @property
    def llm_ready(self) -> bool:
        return bool(self.chat_api_key)

    @property
    def embed_ready(self) -> bool:
        """向量/重排固定走硅基流动 bge-m3；没有 SiliconFlow key 时退化为 BM25。"""
        return bool(self.sf_api_key)

    def status(self) -> dict[str, bool]:
        """给设置页展示：哪些接口已配置。"""
        return {
            f"当前对话模型：{self.chat_model}（{self.llm_profile}）": bool(self.chat_api_key),
            "SiliconFlow (DeepSeek / bge-m3 向量)": bool(self.sf_api_key),
            "ZJU (zju-qwen)": bool(self.zju_api_key),
            "OpenAlex": True,  # 无 key 也可用
            "Elsevier Scopus/ScienceDirect": bool(self.elsevier_api_key),
            "Unpaywall": bool(self.unpaywall_email),
            "Semantic Scholar": True,
            "Crossref": True,
            "Google Patents": True,
            "网页搜索 (Tavily / DuckDuckGo)": True,
        }


settings = Settings()


def reload_settings() -> Settings:
    """重新读取 .env（设置页保存后调用）。

    就地更新同一个 settings 对象：其他模块是 `from .config import settings` 引用的同一实例，
    若这里重新赋值，llm/sources 等模块仍会拿着旧对象（旧密钥）。
    """
    load_dotenv(ROOT / ".env", override=True)
    fresh = Settings()
    for f in fields(Settings):
        setattr(settings, f.name, getattr(fresh, f.name))
    return settings
