"""根据词库自动生成练习题：真题语境填空、英译中、中译英、拼写。

干扰项优先选同级别、同词性的形近词（如 compel 配 compile / expel / propel），不够再随机补齐；
同近义词、同根词和中文释义重叠的词一律排除，保证答案唯一。
所有随机选择都以"级别+单词+题型"为种子，重复构建得到相同题目；已有的题按 (级别, 题型, 单词) 原地更新。
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import Counter, defaultdict

from . import db

BLANK = "______"
N_CHOICES = 4
SPELL_RE = re.compile(r"^[A-Za-z][A-Za-z\-']{2,}$")
_TERM_SPLIT = re.compile(r"[，,；;、\s（）()\[\]【】…\.<>]+")

ALIKE_MAX_DIST = 0.5  # 形近词：编辑距离 / 较长词长 不超过该值
ALIKE_TOP = 6  # 从最相似的前 N 个形近词里随机挑，避免每次都是同一组
ALIKE_KEEP = 12  # 每个词预先保留的形近词候选数


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


def _terms(trans: list[dict], limit: int = 2) -> set[str]:
    """把前几条中文释义拆成词语，用来判断两个词意思是否重叠。"""
    out = set()
    for t in trans[:limit]:
        for x in _TERM_SPLIT.split(t.get("cn", "")):
            if len(x) >= 2:
                out.add(x)
    return out


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def related(a: dict, b: dict) -> bool:
    """两个词是否不能互为干扰项：同一个词、同近义词、同根词、派生词或中文释义重叠。"""
    aw, bw = a["word"].lower(), b["word"].lower()
    if aw == bw:
        return True
    if aw in b.get("syno", ()) or bw in a.get("syno", ()):
        return True
    if aw in b.get("rel", ()) or bw in a.get("rel", ()):
        return True
    short, long_ = sorted((aw, bw), key=len)
    if len(short) >= 3 and (
        long_.startswith(short)
        or (short.endswith("e") and long_.startswith(short[:-1]) and len(long_) - len(short) <= 4)
    ):
        return True
    return bool(a.get("terms", set()) & b.get("terms", set()))


def _bigrams(word: str) -> set[str]:
    s = f"^{word}$"
    return {s[i : i + 2] for i in range(len(s) - 1)}


def build_lookalikes(words: list[dict]) -> dict[int, list[dict]]:
    """为每个词找形近词，按 (相似度, 是否同词性, 单词) 排序。键为 words 中的下标。"""
    grams = [_bigrams(w["word"].lower()) for w in words]
    index: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(grams):
        for x in g:
            index[x].append(i)

    result: dict[int, list[dict]] = {}
    for i, w in enumerate(words):
        wl = w["word"].lower()
        counts: Counter[int] = Counter()
        for x in grams[i]:
            counts.update(index[x])
        # 编辑距离不超过一半的词，共享的双字母组合至少要占一定比例，先用它粗筛
        need = max(2, len(grams[i]) // 2)
        scored = []
        for j, c in counts.items():
            if j == i or c < need:
                continue
            other = words[j]
            ol = other["word"].lower()
            if abs(len(ol) - len(wl)) > 3:
                continue
            dist = levenshtein(wl, ol) / max(len(wl), len(ol))
            if dist <= ALIKE_MAX_DIST and not related(w, other):
                scored.append((dist, other["pos"] != w["pos"], ol, j))
        scored.sort()
        result[i] = [words[j] for *_, j in scored[:ALIKE_KEEP]]
    return result


def _pick_distractors(
    rng: random.Random,
    pool: list[dict],
    target: dict,
    k: int,
    key,
    exclude_values: set[str],
    alike: list[dict] = (),
    n_alike: int = 0,
) -> tuple[list[dict], list[dict]]:
    """挑 k 个干扰项：先从形近词里取至多 n_alike 个，再从 pool 随机补齐。

    key(c) 返回该干扰项在选项中的展示值，展示值重复或为空的会被跳过。
    返回 (全部干扰项, 其中的形近词)。
    """
    chosen: list[dict] = []
    seen = set(exclude_values)

    def take(c: dict) -> bool:
        if related(target, c):
            return False
        v = key(c)
        if not v or v in seen:
            return False
        seen.add(v)
        chosen.append(c)
        return True

    top = list(alike[:ALIKE_TOP])
    rng.shuffle(top)
    for c in top:
        if len(chosen) >= min(k, n_alike):
            break
        take(c)
    confusables = list(chosen)
    for c in rng.sample(pool, min(len(pool), k * 8)):
        if len(chosen) == k:
            break
        take(c)
    return chosen, confusables


def _assemble(rng: random.Random, correct: str, wrong: list[str]) -> tuple[list[str], str]:
    choices = [correct, *wrong]
    rng.shuffle(choices)
    return choices, chr(ord("A") + choices.index(correct))


def _confusable_note(confusables: list[dict]) -> str:
    if not confusables:
        return ""
    items = "；".join(f"{c['word']} {meaning(c['trans'], limit=1, max_len=30)}" for c in confusables)
    return f"易混形近词：{items}\n"


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
                "terms": _terms(trans),
                "sentences": json.loads(r["sentences_json"] or "[]"),
                "real_exam": json.loads(r["real_exam_json"] or "[]"),
                "syno": {
                    x.lower() for g in json.loads(r["syno_json"] or "[]") for x in g.get("words", []) if x
                },
                "rel": {
                    x["w"].lower() for g in json.loads(r["rel_json"] or "[]") for x in g.get("words", []) if x.get("w")
                },
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


def gen_en2cn(level: str, w: dict, pool: list[dict], alike: list[dict] = ()) -> dict | None:
    rng = _rng(level, w["word"], "en2cn")
    correct = meaning(w["trans"])
    ds, conf = _pick_distractors(
        rng, pool, w, N_CHOICES - 1, lambda c: meaning(c["trans"]), {correct}, alike, n_alike=3
    )
    if len(ds) < N_CHOICES - 1:
        return None
    choices, ans = _assemble(rng, correct, [meaning(d["trans"]) for d in ds])
    phone = f" /{w['uk_phone']}/" if w.get("uk_phone") else ""
    return {
        "stem": f"单词 {w['word']}{phone} 的意思是？",
        "choices": choices,
        "answer": ans,
        "explanation_prefix": _confusable_note(conf),
    }


def gen_cn2en(level: str, w: dict, pool: list[dict], alike: list[dict] = ()) -> dict | None:
    rng = _rng(level, w["word"], "cn2en")
    target_meaning = meaning(w["trans"], limit=1)
    # 与目标释义完全相同的词会导致多解，展示值返回 None 让它被跳过
    key = lambda c: None if meaning(c["trans"], limit=1) == target_meaning else c["word"].lower()  # noqa: E731
    ds, conf = _pick_distractors(rng, pool, w, N_CHOICES - 1, key, {w["word"].lower()}, alike, n_alike=3)
    if len(ds) < N_CHOICES - 1:
        return None
    choices, ans = _assemble(rng, w["word"], [d["word"] for d in ds])
    return {
        "stem": f"「{target_meaning}」对应的英文单词是？",
        "choices": choices,
        "answer": ans,
        "explanation_prefix": _confusable_note(conf),
    }


def gen_spelling(level: str, w: dict) -> dict | None:
    word = w["word"]
    if not SPELL_RE.match(word):
        return None
    phone = w.get("uk_phone") or ""
    # 音标与拼写字母完全相同（如 tent /tent/），或音标里夹带了单词本身（如 read 的
    # "(read, read) red"）时不显示音标，否则等于直接给出答案
    leaks = re.sub(r"[^a-z]", "", phone.lower()) == word.lower() or re.search(
        rf"(?<![a-z]){re.escape(word.lower())}(?![a-z])", phone.lower()
    )
    if phone and not leaks:
        phone = f"\n音标：/{phone}/"
    else:
        phone = ""
    hint = word[0] + " _" * (len(word) - 1)
    stem = f"根据释义拼写单词：\n释义：{meaning(w['trans'], limit=3, max_len=80)}{phone}\n提示：{hint}（共 {len(word)} 个字母）"
    return {"stem": stem, "choices": None, "answer": word}


def _source_label(s: dict) -> str:
    bits = [s.get("year"), s.get("level"), s.get("paper"), s.get("type")]
    return " ".join(b for b in bits if b)


def gen_context_cloze(level: str, w: dict, pool: list[dict], alike: list[dict] = ()) -> dict | None:
    pattern = re.compile(rf"(?<![A-Za-z]){re.escape(w['word'])}(?![A-Za-z])", re.IGNORECASE)
    for s in w["real_exam"]:
        text = (s.get("en") or "").strip()
        # 目标词只出现一次的句子才能出题，出现多次时挖掉一处会泄露答案
        if len(pattern.findall(text)) != 1:
            continue
        rng = _rng(level, w["word"], "context_cloze")
        # 语境题保留一部分随机同词性干扰项，避免四个选项全是形近词
        ds, conf = _pick_distractors(
            rng, pool, w, N_CHOICES - 1, lambda c: c["word"].lower(), {w["word"].lower()}, alike, n_alike=2
        )
        if len(ds) < N_CHOICES - 1:
            return None
        blanked = pattern.sub(BLANK, text, count=1)
        choices, ans = _assemble(rng, w["word"], [d["word"] for d in ds])
        label = _source_label(s)
        origin = f"原句出处：{label}\n原句：{text}\n" if label else f"原句：{text}\n"
        return {
            "stem": f"选择最恰当的词填空（真题原句）：\n{blanked}",
            "choices": choices,
            "answer": ans,
            "source": f"{label} 真题原句" if label else "真题原句",
            "explanation_prefix": origin + _confusable_note(conf),
        }
    return None


GENERATED_TYPES = ("context_cloze", "en2cn", "cn2en", "spelling")


def generate_all(conn: sqlite3.Connection, level: str) -> dict[str, int]:
    """生成并入库：已有的题原地更新，新词插入；最后清理现在已不再生成且从未作答过的题。"""
    words = load_level_words(conn, level)
    lookalikes = build_lookalikes(words)
    keep: set[int] = set()
    by_pos, all_words = _pools(words)
    counts = {"context_cloze": 0, "en2cn": 0, "cn2en": 0, "spelling": 0}
    for i, w in enumerate(words):
        pool = _pool_for(by_pos, all_words, w)
        alike = lookalikes.get(i, [])
        brief = word_brief(w)
        # 语境填空要求选项填进句子后语法上都说得通，只用同词性的形近词
        same_pos_alike = [a for a in alike if a["pos"] == w["pos"]]
        made = {
            "context_cloze": gen_context_cloze(level, w, pool, same_pos_alike),
            "en2cn": gen_en2cn(level, w, pool, alike),
            "cn2en": gen_cn2en(level, w, pool, alike),
            "spelling": gen_spelling(level, w),
        }
        for qtype, q in made.items():
            if not q:
                continue
            qid, inserted = db.upsert_generated(
                conn,
                level=level,
                qtype=qtype,
                word_id=w["id"],
                stem=q["stem"],
                choices=q["choices"],
                answer=q["answer"],
                explanation=q.get("explanation_prefix", "") + brief,
                source=q.get("source", f"{level.upper()} 大纲词汇（自动生成）"),
                tags=[w["word"]],
            )
            if qid:
                keep.add(qid)
            counts[qtype] += inserted
    counts["removed_stale"] = remove_stale(conn, level, GENERATED_TYPES, keep)
    return counts


def remove_stale(conn: sqlite3.Connection, level: str, types: tuple[str, ...], keep: set[int]) -> int:
    """删除题号不在 keep 中、且没有作答记录和复习记录的题（用户做过的题一律保留）。"""
    marks = ",".join("?" * len(types))
    rows = conn.execute(
        f"""SELECT q.id FROM questions q
            WHERE q.level = ? AND q.type IN ({marks})
              AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.question_id = q.id)
              AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.question_id = q.id)""",
        (level, *types),
    ).fetchall()
    stale = [(r["id"],) for r in rows if r["id"] not in keep]
    conn.executemany("DELETE FROM questions WHERE id = ?", stale)
    return len(stale)
