"""知识库文件层：00_项目 的 base / 约束 / 过关规则 / 状态摘要，以及各类卡片的 Markdown 读写。

数据库 cards 表与 knowledge/ 目录保持镜像：写卡片时同时写文件与数据库；
LLM 只允许从这些文件读取 base、约束、价格（内部技术路线 3.3 规则）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import db
from .config import KB

PROJECT_FILES = {
    "base": KB["project"] / "base.md",
    "constraints": KB["project"] / "constraints.yaml",
    "rules": KB["project"] / "过关规则.md",
    "summary": KB["project"] / "状态摘要.md",
    "keywords": KB["project"] / "检索关键词.yaml",
}

CARD_DIRS = {"material": KB["material"], "literature": KB["literature"], "patent": KB["patent"], "price": KB["price"]}

CARD_FIELDS: dict[str, list[tuple[str, str]]] = {
    "material": [
        ("name", "名称"), ("grade_examples", "牌号示例"), ("mfr", "MFR (g/10min)"), ("density", "密度 (g/cm³)"),
        ("comonomer", "共聚单体"), ("role", "作用"), ("effects", "对四项性能的影响（方向+幅度+依据）"),
        ("typical_dosage", "典型用量"), ("price_estimate", "价格（估，元/吨）"), ("risk", "风险"),
        ("evidence", "依据来源与可信度"),
    ],
    "literature": [
        ("citation", "出处"), ("system", "研究体系"), ("conclusions", "结论（只记与本项目有关的）"),
        ("data_points", "可复用数据点"), ("credibility", "可信度等级"), ("relevance", "与本项目的关系"),
    ],
    "patent": [
        ("citation", "出处"), ("applicant", "申请人"), ("formulation_range", "配方范围"), ("claimed_effect", "声称效果"),
        ("borrow", "我们能借鉴的点"), ("avoid", "规避点"), ("credibility", "可信度等级"),
    ],
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(read_text(path) or "") or {}


def write_yaml(path: Path, data: Any) -> None:
    write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def project_text(key: str) -> str:
    return read_text(PROJECT_FILES[key])


def constraints() -> dict[str, Any]:
    return read_yaml(PROJECT_FILES["constraints"])


def keywords() -> dict[str, Any]:
    return read_yaml(PROJECT_FILES["keywords"])


# ---------- 卡片 ----------

def _slug(name: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", name.strip())
    return s[:60] or "card"


def card_to_markdown(card_type: str, name: str, data: dict[str, Any], source: dict[str, Any] | None = None) -> str:
    lines = [f"# {name}", ""]
    lines.append(f"- 类型：{card_type}")
    if source:
        link = (source.get("doi") and f"https://doi.org/{source['doi']}") or source.get("url") or source.get("file_path") or ""
        lines.append(f"- 来源：{source.get('title', '')}（{source.get('provider', '')}，{source.get('year') or ''}）{link}")
    lines.append("")
    fields = CARD_FIELDS.get(card_type, [])
    known = {k for k, _ in fields}
    for key, label in fields:
        v = data.get(key)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            lines.append(f"## {label}")
            for kk, vv in v.items():
                lines.append(f"- {kk}：{vv}")
            lines.append("")
        elif isinstance(v, list):
            lines.append(f"## {label}")
            lines.extend(f"- {x}" for x in v)
            lines.append("")
        else:
            lines.append(f"## {label}")
            lines.append(str(v))
            lines.append("")
    extra = {k: v for k, v in data.items() if k not in known and v not in (None, "", [], {})}
    if extra:
        lines.append("## 其他")
        for k, v in extra.items():
            lines.append(f"- {k}：{v}")
    return "\n".join(lines).strip() + "\n"


def save_card(card_type: str, name: str, data: dict[str, Any], *, source_id: int | None = None,
              credibility: int = 3, card_id: int | None = None) -> int:
    source = db.q1("SELECT * FROM sources WHERE id=?", (source_id,)) if source_id else None
    path = CARD_DIRS[card_type] / f"{_slug(name)}.md"
    write_text(path, card_to_markdown(card_type, name, data, source))
    now = db.now()
    existing = card_id and db.q1("SELECT id FROM cards WHERE id=?", (card_id,))
    if not existing:
        existing = db.q1("SELECT id FROM cards WHERE card_type=? AND name=?", (card_type, name))
    if existing:
        db.update("cards", existing["id"], {"data_json": db.dumps(data), "file_path": str(path), "source_id": source_id,
                                            "credibility": credibility, "updated_at": now})
        return existing["id"]
    return db.insert("cards", {"card_type": card_type, "name": name, "source_id": source_id, "file_path": str(path),
                               "data_json": db.dumps(data), "credibility": credibility, "created_at": now,
                               "updated_at": now})


def list_cards(card_type: str | None = None) -> list[dict[str, Any]]:
    if card_type:
        rows = db.q("SELECT * FROM cards WHERE card_type=? ORDER BY updated_at DESC", (card_type,))
    else:
        rows = db.q("SELECT * FROM cards ORDER BY updated_at DESC")
    for r in rows:
        r["data"] = db.loads(r.get("data_json"), {})
    return rows


def get_card(card_id: int) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM cards WHERE id=?", (card_id,))
    if r:
        r["data"] = db.loads(r.get("data_json"), {})
    return r


def delete_card(card_id: int) -> None:
    r = db.q1("SELECT file_path FROM cards WHERE id=?", (card_id,))
    if r and r.get("file_path"):
        p = Path(r["file_path"])
        if p.exists():
            p.unlink()
    db.delete("cards", card_id)


def cards_digest(card_type: str, limit: int = 60, max_chars: int = 6000) -> str:
    """给 LLM 的紧凑摘要：每张卡一行。"""
    out = []
    for c in list_cards(card_type)[:limit]:
        d = c["data"]
        if card_type == "material":
            eff = d.get("effects")
            eff_s = "; ".join(f"{k}{v}" for k, v in eff.items()) if isinstance(eff, dict) else str(eff or "")
            out.append(f"- [{c['name']}] 作用:{d.get('role', '')} | 四项:{eff_s} | 用量:{d.get('typical_dosage', '')} | "
                       f"估价:{d.get('price_estimate', '')} | 风险:{d.get('risk', '')}")
        else:
            concl = d.get("conclusions") or d.get("claimed_effect") or ""
            if isinstance(concl, list):
                concl = "；".join(map(str, concl))
            out.append(f"- [{card_type[:3].upper()}#{c['id']} {c['name']}] {str(concl)[:200]}")
    text = "\n".join(out)
    return text[:max_chars]
