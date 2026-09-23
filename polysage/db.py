"""SQLite 持久层：资料、切片、卡片、会话、原料、价格、配方、模型。

设计原则：
- 每条资料 (sources) 必须有 provider + url/doi + retrieved_at，保证“出处可追溯”。
- knowledge/ 下的 Markdown/CSV 是人类可读镜像；数据库是检索与程序使用的主副本。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    external_id TEXT,
    provider TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    authors TEXT,
    year INTEGER,
    venue TEXT,
    doi TEXT,
    url TEXT,
    file_path TEXT,
    abstract TEXT,
    credibility INTEGER DEFAULT 3,
    license TEXT,
    content_hash TEXT,
    meta_json TEXT,
    retrieved_at TEXT NOT NULL,
    n_chunks INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sources_hash ON sources(content_hash);
CREATE INDEX IF NOT EXISTS ix_sources_doi ON sources(doi);
CREATE INDEX IF NOT EXISTS ix_sources_ext ON sources(provider, external_id);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB,
    n_chars INTEGER
);
CREATE INDEX IF NOT EXISTS ix_chunks_source ON chunks(source_id);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    card_type TEXT NOT NULL,
    name TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    file_path TEXT,
    data_json TEXT,
    credibility INTEGER DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cards_type ON cards(card_type);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    role_key TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    archived INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls_json TEXT,
    tool_call_id TEXT,
    name TEXT,
    citations_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    category TEXT,
    name TEXT NOT NULL,
    grade TEXT,
    supplier TEXT,
    mfi REAL,
    density REAL,
    comonomer TEXT,
    is_recycled INTEGER DEFAULT 0,
    role TEXT,
    typical_min REAL,
    typical_max REAL,
    effects_json TEXT,
    risk TEXT,
    use_flag TEXT,
    tds_source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    notes TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    material_code TEXT NOT NULL REFERENCES materials(code) ON DELETE CASCADE,
    price_type TEXT NOT NULL,
    price REAL NOT NULL,
    unit TEXT DEFAULT '元/吨',
    price_date TEXT,
    source TEXT,
    url TEXT,
    supplier TEXT,
    moq TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prices_mat ON prices(material_code, price_type);

CREATE TABLE IF NOT EXISTS formulations (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    structure TEXT DEFAULT 'mono',
    transparent TEXT,
    components_json TEXT NOT NULL,
    cost_estimate REAL,
    cost_actual REAL,
    density REAL,
    area_index REAL,
    predicted_json TEXT,
    rationale TEXT,
    risks TEXT,
    priority TEXT,
    status TEXT DEFAULT '候选',
    origin TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY,
    target TEXT NOT NULL,
    model_type TEXT NOT NULL,
    n_samples INTEGER,
    metrics_json TEXT,
    features_json TEXT,
    file_path TEXT,
    trained_at TEXT NOT NULL,
    is_current INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    provider TEXT NOT NULL,
    n_results INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recycled_batches (
    id INTEGER PRIMARY KEY,
    material_code TEXT NOT NULL,
    supplier TEXT,
    grade TEXT,
    source_film TEXT,
    batch_no TEXT,
    mfr REAL,
    density REAL,
    ash REAL,
    moisture REAL,
    dsc_note TEXT,
    gel_count REAL,
    odor TEXT,
    price REAL,
    received_at TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    trigger TEXT,
    summary_json TEXT,
    report_path TEXT
);

CREATE TABLE IF NOT EXISTS recommend_runs (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    input_json TEXT,
    output_json TEXT,
    note TEXT
);

-- 供应商寻源：厂商档案与网查/询价得到的报价
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT,
    region TEXT,
    contact TEXT,
    url TEXT,
    materials TEXT,
    notes TEXT,
    credibility INTEGER DEFAULT 3,
    status TEXT DEFAULT '网查',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS supplier_quotes (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    material_code TEXT NOT NULL,
    grade TEXT,
    price REAL NOT NULL,
    unit TEXT DEFAULT '元/吨',
    basis TEXT,
    moq TEXT,
    quote_date TEXT,
    source_url TEXT,
    evidence TEXT,
    credibility INTEGER DEFAULT 3,
    status TEXT DEFAULT '网查',
    in_rfq INTEGER DEFAULT 0,
    actual_price REAL,
    note TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_mat ON supplier_quotes(material_code, status);

CREATE TABLE IF NOT EXISTS sourcing_runs (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    codes_json TEXT,
    summary_json TEXT,
    note TEXT
);

-- 价格预判：每类树脂 1 周 / 1 月的预期（期货 + 现货历史 + 行情评述）
CREATE TABLE IF NOT EXISTS price_forecasts (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    family TEXT NOT NULL,
    current REAL,
    price_1w REAL,
    price_1m REAL,
    pct_1w REAL,
    pct_1m REAL,
    direction TEXT,
    confidence REAL,
    drivers_json TEXT,
    sources_json TEXT,
    method_json TEXT
);
"""

# 旧库升级：materials 表新增列（幂等）
MATERIAL_EXTRA_COLUMNS: dict[str, str] = {
    "producer": "TEXT",
    "melting_point": "REAL",
    "vicat": "REAL",
    "mwd": "TEXT",
    "tds_dart": "REAL",
    "tds_tensile_md": "REAL",
    "tds_tensile_td": "REAL",
    "tds_tear_md": "REAL",
    "tds_tear_td": "REAL",
    "tds_haze": "REAL",
    "tds_gloss": "REAL",
    "tds_seal_init": "REAL",
    "proc_temp": "TEXT",
    "needs_ppa": "INTEGER",
    "availability": "TEXT",
    "price_note": "TEXT",
}


def migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(materials)").fetchall()}
    for name, typ in MATERIAL_EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE materials ADD COLUMN {name} {typ}")
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(formulations)").fetchall()}
    if "cost_used" not in fcols:
        # 混合口径成本：有实价用实价、否则估计价（排名与联动都用它）
        conn.execute("ALTER TABLE formulations ADD COLUMN cost_used REAL")
    # 供应商报价：贸易商日报字段（树脂类别、厂家、仓库地、货物状态、到厂价、渠道）
    qcols = {r[1] for r in conn.execute("PRAGMA table_info(supplier_quotes)").fetchall()}
    for name, typ in (("family", "TEXT"), ("producer", "TEXT"), ("warehouse", "TEXT"), ("delivery", "TEXT"),
                      ("landed_price", "REAL"), ("channel", "TEXT DEFAULT '网查'")):
        if qcols and name not in qcols:
            conn.execute(f"ALTER TABLE supplier_quotes ADD COLUMN {name} {typ}")
    # 配方库：原料采购（每种料怎么来的）与可采购标记
    for name, typ in (("sourcing_note", "TEXT"), ("buyable", "INTEGER")):
        if name not in fcols:
            conn.execute(f"ALTER TABLE formulations ADD COLUMN {name} {typ}")
    conn.commit()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    """进程内单例连接；测试可传入临时路径切换数据库。"""
    global _conn, _conn_path
    target = path or _conn_path or DB_PATH
    with _lock:
        if _conn is None or _conn_path != target:
            if _conn is not None:
                _conn.close()
            _conn = connect(target)
            _conn.executescript(SCHEMA)
            migrate(_conn)
            _conn_path = target
        return _conn


def use_db(path: Path) -> None:
    """切换数据库文件（测试用）。"""
    get_conn(path)


@contextmanager
def tx():
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def q(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with _lock:
        return rows_to_dicts(get_conn().execute(sql, params).fetchall())


def q1(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    r = q(sql, params)
    return r[0] if r else None


def insert(table: str, data: dict[str, Any]) -> int:
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    with tx() as conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
        return int(cur.lastrowid)


def update(table: str, row_id: int, data: dict[str, Any]) -> None:
    sets = ", ".join(f"{k}=?" for k in data)
    with tx() as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*data.values(), row_id))


def delete(table: str, row_id: int) -> None:
    with tx() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))


def execute(sql: str, params: tuple | dict = ()) -> None:
    with tx() as conn:
        conn.execute(sql, params)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


# ---------- 常用查询 ----------

def source_by_hash(content_hash: str) -> dict | None:
    return q1("SELECT * FROM sources WHERE content_hash=?", (content_hash,))


def source_by_external(provider: str, external_id: str) -> dict | None:
    return q1("SELECT * FROM sources WHERE provider=? AND external_id=?", (provider, external_id))


def source_by_doi(doi: str) -> dict | None:
    return q1("SELECT * FROM sources WHERE lower(doi)=lower(?)", (doi,))


def list_sources(limit: int = 500, source_type: str | None = None) -> list[dict]:
    if source_type:
        return q("SELECT * FROM sources WHERE source_type=? ORDER BY id DESC LIMIT ?", (source_type, limit))
    return q("SELECT * FROM sources ORDER BY id DESC LIMIT ?", (limit,))


def kb_stats() -> dict[str, int]:
    out: dict[str, int] = {}
    out["sources"] = q1("SELECT COUNT(*) c FROM sources")["c"]
    out["chunks"] = q1("SELECT COUNT(*) c FROM chunks")["c"]
    out["chunks_embedded"] = q1("SELECT COUNT(*) c FROM chunks WHERE embedding IS NOT NULL")["c"]
    for t in ("material", "literature", "patent", "price"):
        out[f"cards_{t}"] = q1("SELECT COUNT(*) c FROM cards WHERE card_type=?", (t,))["c"]
    out["materials"] = q1("SELECT COUNT(*) c FROM materials WHERE active=1")["c"]
    out["formulations"] = q1("SELECT COUNT(*) c FROM formulations")["c"]
    out["sessions"] = q1("SELECT COUNT(*) c FROM sessions WHERE archived=0")["c"]
    return out
