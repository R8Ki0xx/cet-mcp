"""SQLite 连接、表结构与基础写入。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CET_MCP_DATA", PROJECT_ROOT / "data"))
DEFAULT_DB = DATA_DIR / "cet.db"

LEVELS = ("cet4", "cet6")

# 题型 → 中文名
QUESTION_TYPES = {
    "vocab_real": "真题词汇选择",
    "context_cloze": "真题语境填空",
    "en2cn": "英译中",
    "cn2en": "中译英",
    "spelling": "拼写",
    "grammar": "语法/短语",
    "reading": "阅读理解",
    "cloze": "选词填空",
    "listening": "听力",
    "translation": "段落翻译",
    "writing": "写作",
    "custom": "自定义",
}
# 需要 AI 评分的主观题
SUBJECTIVE_TYPES = {"translation", "writing"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL,
    level TEXT NOT NULL,
    uk_phone TEXT,
    us_phone TEXT,
    pos TEXT,
    trans_json TEXT,
    phrases_json TEXT,
    sentences_json TEXT,
    syno_json TEXT,
    rel_json TEXT,
    rem_method TEXT,
    real_exam_json TEXT,
    UNIQUE(word, level)
);
CREATE INDEX IF NOT EXISTS idx_words_word ON words(word COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    type TEXT NOT NULL,
    stem TEXT NOT NULL,
    material TEXT,
    choices_json TEXT,
    answer TEXT NOT NULL,
    explanation TEXT,
    source TEXT,
    word_id INTEGER REFERENCES words(id),
    tags TEXT,
    hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_q_level_type ON questions(level, type);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    user_answer TEXT,
    correct INTEGER NOT NULL,
    quality INTEGER NOT NULL,
    feedback TEXT,
    answered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_attempts_q ON attempts(question_id);
CREATE INDEX IF NOT EXISTS idx_attempts_time ON attempts(answered_at);

CREATE TABLE IF NOT EXISTS reviews (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id),
    ease REAL NOT NULL DEFAULT 2.5,
    interval INTEGER NOT NULL DEFAULT 0,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,
    last_reviewed TEXT,
    last_wrong TEXT,
    in_wrongbook INTEGER NOT NULL DEFAULT 0,
    mastered INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_due ON reviews(due_date);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_path() -> Path:
    return Path(os.environ.get("CET_MCP_DB", DEFAULT_DB))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path else db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def question_hash(level: str, qtype: str, stem: str, answer: str) -> str:
    raw = "\x1f".join([level, qtype, stem.strip(), answer.strip()])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def insert_question(
    conn: sqlite3.Connection,
    *,
    level: str,
    qtype: str,
    stem: str,
    answer: str,
    choices: list[str] | None = None,
    explanation: str | None = None,
    material: str | None = None,
    source: str | None = None,
    word_id: int | None = None,
    tags: list[str] | str | None = None,
) -> int | None:
    """插入一道题；重复（hash 相同）时返回 None。"""
    if isinstance(tags, list):
        tags = ",".join(t.strip() for t in tags if t.strip())
    cur = conn.execute(
        """INSERT OR IGNORE INTO questions
           (level, type, stem, material, choices_json, answer, explanation, source, word_id, tags, hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            level,
            qtype,
            stem,
            material,
            json.dumps(choices, ensure_ascii=False) if choices else None,
            answer,
            explanation,
            source,
            word_id,
            tags or None,
            question_hash(level, qtype, stem, answer),
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
