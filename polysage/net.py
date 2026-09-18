"""出网策略：这台机器开着系统代理（Clash 之类，127.0.0.1:xxxxx），httpx 默认会把所有请求都走代理，
国内站点（硅基流动、ZJU、行情站、B2B）走代理反而断连（SSL EOF），Google 走代理的出口 IP 还被封（503 Sorry）。

所以默认“国内直连、只有明确需要翻墙的域名走代理”：
- PROXY_MODE=auto（默认）：PROXY_HOSTS 里的域名走系统代理，其余直连
- PROXY_MODE=system：全部按系统代理（旧行为）
- PROXY_MODE=off：全部直连
"""
from __future__ import annotations

import os
import urllib.request
from urllib.parse import urlparse

import httpx

# 默认需要走代理的域名（国内直连不通或极慢）
DEFAULT_PROXY_HOSTS = ("google.com", "googleapis.com", "duckduckgo.com", "brave.com", "api.tavily.com",
                       "api.semanticscholar.org", "api.unpaywall.org", "lens.org", "arxiv.org")


def mode() -> str:
    m = (os.getenv("PROXY_MODE") or "auto").strip().lower()
    return m if m in ("auto", "system", "off") else "auto"


def proxy_hosts() -> tuple[str, ...]:
    raw = os.getenv("PROXY_HOSTS", "").strip()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip()) or DEFAULT_PROXY_HOSTS


def system_proxy() -> str | None:
    """系统代理地址（环境变量或 Windows 注册表）；没有则 None。"""
    p = urllib.request.getproxies()
    return p.get("https") or p.get("http") or None


def needs_proxy(url: str | None) -> bool:
    if mode() == "off":
        return False
    if mode() == "system":
        return True
    if not url:
        return False
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return any(host == h or host.endswith("." + h) for h in proxy_hosts())


def client(url: str | None = None, **kw) -> httpx.Client:
    """按目标域名决定是否走系统代理的 httpx.Client；其余参数原样传给 httpx。"""
    kw.setdefault("trust_env", False)
    if needs_proxy(url):
        px = system_proxy()
        if px:
            kw.setdefault("proxy", px)
    return httpx.Client(**kw)


def proxy_for(url: str | None) -> str | None:
    """给不用 httpx 的库（ddgs）用：需要代理时返回地址，否则 None。"""
    return system_proxy() if needs_proxy(url) else None
