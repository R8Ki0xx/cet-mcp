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
    _migrate(conn)
    return conn


# v1：级别+题型+题干+答案；v2 起加入选项和材料（不同文章下的同一问法不再被误判为重复题）
HASH_VERSION = "2"


def question_hash(
    level: str, qtype: str, stem: str, answer: str, choices: list[str] | None = None, material: str | None = None
) -> str:
    raw = "\x1f".join(
        [level, qtype, stem.strip(), answer.strip(), "\x1e".join(choices or []), (material or "").strip()]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def row_hash(row: sqlite3.Row) -> str:
    choices = json.loads(row["choices_json"]) if row["choices_json"] else None
    return question_hash(row["level"], row["type"], row["stem"], row["answer"], choices, row["material"])


def _migrate(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "hash_version") == HASH_VERSION:
        return
    with conn:
        rows = conn.execute("SELECT * FROM questions").fetchall()
        # 新算法比旧算法区分得更细，旧库中互不重复的题在新算法下仍互不重复
        conn.executemany("UPDATE questions SET hash = ? WHERE id = ?", [(row_hash(r), r["id"]) for r in rows])
        set_meta(conn, "hash_version", HASH_VERSION)


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
            question_hash(level, qtype, stem, answer, choices, material),
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def upsert_generated(
    conn: sqlite3.Connection,
    *,
    level: str,
    qtype: str,
    word_id: int,
    stem: str,
    answer: str,
    choices: list[str] | None,
    explanation: str | None,
    source: str | None,
    tags: list[str],
) -> tuple[int | None, bool]:
    """自动生成的题以 (级别, 题型, 单词) 为身份：已存在就原地更新内容（保留题号和做题记录），
    否则插入。返回 (题号, 是否新插入)。"""
    existing = conn.execute(
        """SELECT q.id FROM questions q WHERE q.level = ? AND q.type = ? AND q.word_id = ?
           ORDER BY EXISTS (SELECT 1 FROM attempts a WHERE a.question_id = q.id) DESC, q.id DESC LIMIT 1""",
        (level, qtype, word_id),
    ).fetchone()
    if not existing:
        qid = insert_question(
            conn, level=level, qtype=qtype, stem=stem, answer=answer, choices=choices,
            explanation=explanation, source=source, word_id=word_id, tags=tags,
        )
        return qid, qid is not None
    conn.execute(
        """UPDATE OR IGNORE questions SET stem = ?, choices_json = ?, answer = ?, explanation = ?, source = ?,
           tags = ?, hash = ? WHERE id = ?""",
        (
            stem,
            json.dumps(choices, ensure_ascii=False) if choices else None,
            answer,
            explanation,
            source,
            ",".join(tags),
            question_hash(level, qtype, stem, answer, choices),
            existing["id"],
        ),
    )
    return existing["id"], False


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
