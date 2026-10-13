"""根据词库自动生成练习题：真题语境填空、英译中、中译英、拼写。

所有随机选择都以"级别+单词+题型"为种子，重复构建得到相同题目（hash 去重，不会重复入库）。
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import defaultdict

from . import db

BLANK = "______"
N_CHOICES = 4
SPELL_RE = re.compile(r"^[A-Za-z][A-Za-z\-']{2,}$")


def meaning(trans: list[dict], limit: int = 2, max_len: int = 40) -> str:
    parts = []
    for t in trans[:limit]:
        pos = f"{t['pos']}. " if t.get("pos") else ""
        parts.append(pos + t["cn"])
    text = "；".join(parts)
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def word_brief(w: dict) -> str:
    phone = f" /{w['uk_phone']}/" if w.get("uk_phone") else ""
    lines = [f"{w['word']}{phone}  {meaning(w['trans'], limit=4, max_len=120)}"]
    if w["sentences"]:
        s = w["sentences"][0]
        lines.append(f"例：{s['en']}  {s['cn']}")
    return "\n".join(lines)


def _rng(level: str, word: str, kind: str) -> random.Random:
    return random.Random(f"{level}|{word}|{kind}")


def _pick_distractors(
    rng: random.Random, pool: list[dict], target: dict, k: int, key, exclude_values: set[str]
) -> list[dict]:
    """从 pool 中挑 k 个与 target 不同、且 key 值互不相同的干扰项。"""
    chosen: list[dict] = []
    seen = set(exclude_values)
    candidates = rng.sample(pool, min(len(pool), k * 8))
    for c in candidates:
        if c["word"].lower() == target["word"].lower():
            continue
        v = key(c)
        if not v or v in seen:
            continue
        seen.add(v)
        chosen.append(c)
        if len(chosen) == k:
            break
    return chosen


def _assemble(rng: random.Random, correct: str, wrong: list[str]) -> tuple[list[str], str]:
    choices = [correct, *wrong]
    rng.shuffle(choices)
    return choices, chr(ord("A") + choices.index(correct))


def load_level_words(conn: sqlite3.Connection, level: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM words WHERE level = ? ORDER BY id", (level,)).fetchall()
    words = []
    for r in rows:
        trans = json.loads(r["trans_json"] or "[]")
        if not trans:
            continue
        words.append(
            {
                "id": r["id"],
                "word": r["word"],
                "pos": r["pos"] or "",
                "uk_phone": r["uk_phone"],
                "trans": trans,
                "sentences": json.loads(r["sentences_json"] or "[]"),
                "real_exam": json.loads(r["real_exam_json"] or "[]"),
            }
        )
    return words


def _pools(words: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for w in words:
        by_pos[w["pos"]].append(w)
    return by_pos, words


def _pool_for(by_pos, all_words, w) -> list[dict]:
    pool = by_pos.get(w["pos"], [])
    return pool if len(pool) >= 20 else all_words


def gen_en2cn(level: str, w: dict, pool: list[dict]) -> dict | None:
    rng = _rng(level, w["word"], "en2cn")
    correct = meaning(w["trans"])
    ds = _pick_distractors(rng, pool, w, N_CHOICES - 1, lambda c: meaning(c["trans"]), {correct})
    if len(ds) < N_CHOICES - 1:
        return None
    choices, ans = _assemble(rng, correct, [meaning(d["trans"]) for d in ds])
    phone = f" /{w['uk_phone']}/" if w.get("uk_phone") else ""
    return {"stem": f"单词 {w['word']}{phone} 的意思是？", "choices": choices, "answer": ans}


def gen_cn2en(level: str, w: dict, pool: list[dict]) -> dict | None:
    rng = _rng(level, w["word"], "cn2en")
    target_meaning = meaning(w["trans"], limit=1)
    ds = _pick_distractors(
        rng, pool, w, N_CHOICES + 2, lambda c: c["word"].lower(), {w["word"].lower()}
    )
    # 去掉与目标释义完全相同的干扰词（同义词会导致多解）
    ds = [d for d in ds if meaning(d["trans"], limit=1) != target_meaning][: N_CHOICES - 1]
    if len(ds) < N_CHOICES - 1:
        return None
    choices, ans = _assemble(rng, w["word"], [d["word"] for d in ds])
    return {"stem": f"「{target_meaning}」对应的英文单词是？", "choices": choices, "answer": ans}


def gen_spelling(level: str, w: dict) -> dict | None:
    word = w["word"]
    if not SPELL_RE.match(word):
        return None
    phone = f"\n音标：/{w['uk_phone']}/" if w.get("uk_phone") else ""
    hint = word[0] + " _" * (len(word) - 1)
    stem = f"根据释义拼写单词：\n释义：{meaning(w['trans'], limit=3, max_len=80)}{phone}\n提示：{hint}（共 {len(word)} 个字母）"
    return {"stem": stem, "choices": None, "answer": word}


def _source_label(s: dict) -> str:
    bits = [s.get("year"), s.get("level"), s.get("paper"), s.get("type")]
    return " ".join(b for b in bits if b)


def gen_context_cloze(level: str, w: dict, pool: list[dict]) -> dict | None:
    pattern = re.compile(rf"(?<![A-Za-z]){re.escape(w['word'])}(?![A-Za-z])", re.IGNORECASE)
    for s in w["real_exam"]:
        text = (s.get("en") or "").strip()
        if not pattern.search(text):
            continue
        rng = _rng(level, w["word"], "context_cloze")
        ds = _pick_distractors(rng, pool, w, N_CHOICES - 1, lambda c: c["word"].lower(), {w["word"].lower()})
        if len(ds) < N_CHOICES - 1:
            return None
        blanked = pattern.sub(BLANK, text, count=1)
        choices, ans = _assemble(rng, w["word"], [d["word"] for d in ds])
        label = _source_label(s)
        return {
            "stem": f"选择最恰当的词填空（真题原句）：\n{blanked}",
            "choices": choices,
            "answer": ans,
            "source": f"{label} 真题原句" if label else "真题原句",
            "explanation_prefix": f"原句出处：{label}\n原句：{text}\n" if label else f"原句：{text}\n",
        }
    return None


def generate_all(conn: sqlite3.Connection, level: str) -> dict[str, int]:
    words = load_level_words(conn, level)
    by_pos, all_words = _pools(words)
    counts = {"context_cloze": 0, "en2cn": 0, "cn2en": 0, "spelling": 0}
    for w in words:
        pool = _pool_for(by_pos, all_words, w)
        brief = word_brief(w)
        made = {
            "context_cloze": gen_context_cloze(level, w, pool),
            "en2cn": gen_en2cn(level, w, pool),
            "cn2en": gen_cn2en(level, w, pool),
            "spelling": gen_spelling(level, w),
        }
        for qtype, q in made.items():
            if not q:
                continue
            qid = db.insert_question(
                conn,
                level=level,
                qtype=qtype,
                stem=q["stem"],
                choices=q["choices"],
                answer=q["answer"],
                explanation=q.get("explanation_prefix", "") + brief,
                source=q.get("source", f"{level.upper()} 大纲词汇（自动生成）"),
                word_id=w["id"],
                tags=[w["word"]],
            )
            counts[qtype] += qid is not None
    return counts
