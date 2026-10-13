"""题目校验与批量导入（JSON / CSV），以及内置原创种子题。

JSON 格式：题目对象数组，或 {"questions": [...]}。字段：
    level        必填  cet4 / cet6
    type         选填  见 db.QUESTION_TYPES，默认 custom
    stem         必填  题干
    answer       必填  选择题填字母（A/B/C/D）或选项原文；主观题填参考答案
    choices      选填  选项数组（不要带 "A." 前缀）
    explanation  选填  解析 / 评分要点
    material     选填  阅读文章、翻译原文等共享材料
    tags         选填  字符串数组或逗号分隔字符串
    source       选填  出处，如 "2023.6 CET4 第一套"

CSV 格式：表头 level,type,stem,choice_a,choice_b,choice_c,choice_d,answer,explanation,material,tags,source
（UTF-8 编码，Excel 另存为 "CSV UTF-8" 即可）
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

from . import db

SEED_FILE = Path(__file__).parent / "seed" / "original_questions.json"
_PREFIX_RE = re.compile(r"^\s*[A-Ha-h][\.\)、．:：]\s*")


class QuestionError(ValueError):
    pass


def normalize(q: dict) -> dict:
    """校验并规范化一道题，返回可直接传给 db.insert_question 的参数。"""
    level = str(q.get("level", "")).strip().lower().replace("-", "")
    if level not in db.LEVELS:
        raise QuestionError(f"level 必须是 {db.LEVELS} 之一，收到 {q.get('level')!r}")
    qtype = str(q.get("type") or "custom").strip().lower()
    if qtype not in db.QUESTION_TYPES:
        raise QuestionError(f"type 必须是 {list(db.QUESTION_TYPES)} 之一，收到 {qtype!r}")
    stem = str(q.get("stem") or "").strip()
    if not stem:
        raise QuestionError("stem（题干）不能为空")
    answer = str(q.get("answer") or "").strip()
    if not answer:
        raise QuestionError("answer（答案）不能为空")

    choices = q.get("choices")
    if isinstance(choices, str):
        choices = [c for c in re.split(r"[\n|]", choices) if c.strip()]
    choices = [_PREFIX_RE.sub("", str(c)).strip() for c in (choices or []) if str(c).strip()] or None
    if choices:
        if len(choices) < 2:
            raise QuestionError("选择题至少需要 2 个选项")
        letter = answer.upper().rstrip(".")
        if len(letter) == 1 and "A" <= letter < chr(ord("A") + len(choices)):
            answer = letter
        elif answer in choices:
            answer = chr(ord("A") + choices.index(answer))
        else:
            raise QuestionError(f"答案 {answer!r} 既不是有效选项字母，也不在选项中")

    tags = q.get("tags")
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,，;；]", tags) if t.strip()]

    return {
        "level": level,
        "qtype": qtype,
        "stem": stem,
        "answer": answer,
        "choices": choices,
        "explanation": (str(q["explanation"]).strip() or None) if q.get("explanation") else None,
        "material": (str(q["material"]).strip() or None) if q.get("material") else None,
        "source": (str(q["source"]).strip() or None) if q.get("source") else None,
        "tags": tags or None,
    }


def add_many(conn: sqlite3.Connection, items: list[dict], default_source: str | None = None) -> dict:
    added, duplicates, errors = [], 0, []
    for i, raw in enumerate(items, 1):
        try:
            q = normalize(raw)
        except QuestionError as e:
            errors.append(f"第 {i} 题：{e}")
            continue
        q["source"] = q["source"] or default_source
        qid = db.insert_question(conn, **q)
        if qid is None:
            duplicates += 1
        else:
            added.append(qid)
    return {"added": len(added), "duplicates": duplicates, "errors": errors, "ids": added}


def read_file(path: str | Path) -> list[dict]:
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    text = path.read_bytes().decode("utf-8-sig")
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("questions", [])
        if not isinstance(data, list):
            raise QuestionError("JSON 顶层应为题目数组或 {\"questions\": [...]}")
        return data
    if suffix == ".csv":
        rows = []
        for r in csv.DictReader(text.splitlines()):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            choices = [r.get(f"choice_{c}", "") for c in "abcdefgh"]
            r["choices"] = [c for c in choices if c] or None
            rows.append(r)
        return rows
    raise QuestionError("只支持 .json 和 .csv 文件")


def import_file(conn: sqlite3.Connection, path: str | Path) -> dict:
    items = read_file(path)
    return add_many(conn, items, default_source=f"导入：{Path(path).name}")


def load_seed(conn: sqlite3.Connection) -> int:
    if not SEED_FILE.exists():
        return 0
    items = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return add_many(conn, items, default_source="原创练习题")["added"]
