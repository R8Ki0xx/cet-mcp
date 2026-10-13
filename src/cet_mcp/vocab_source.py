"""下载并解析 KyleBing/english-vocabulary 四六级词库，构建题库。

数据来源：https://github.com/KyleBing/english-vocabulary （BSD-3-Clause）
文件：full_line_jsonl/full/正序/{四级,六级}.jsonl —— 每行一个词条，含释义、音标、短语、
例句、同近义词、同根词、记忆法、真题词汇题（exam）和真题原句（realExamSentence）。

命令行用法：
    uv run cet-mcp-setup            # 建 CET-4 + CET-6
    uv run cet-mcp-setup cet4       # 只建 CET-4
    uv run cet-mcp-setup --force    # 重新下载词库并补充题目（已有题目与做题记录不受影响）
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

from . import db, generators

BASE_URL = "https://raw.githubusercontent.com/KyleBing/english-vocabulary/master/full_line_jsonl/full/"
SOURCE_FILES = {"cet4": "正序/四级.jsonl", "cet6": "正序/六级.jsonl"}
SOURCE_NAME = "KyleBing/english-vocabulary"

POS_NORMALIZE = {"vt": "v", "vi": "v", "a": "adj", "ad": "adv", "pron": "pron", "art": "art"}


def log(msg: str) -> None:
    # MCP stdio 传输占用 stdout，日志一律写 stderr
    print(msg, file=sys.stderr, flush=True)


def raw_path(level: str) -> Path:
    return db.DATA_DIR / "raw" / f"{level}.jsonl"


def download(level: str, force: bool = False) -> Path:
    dest = raw_path(level)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = BASE_URL + urllib.parse.quote(SOURCE_FILES[level])
    log(f"下载 {level} 词库：{url}")
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as f:
        total = 0
        while chunk := resp.read(1 << 16):
            f.write(chunk)
            total += len(chunk)
    tmp.replace(dest)
    log(f"  完成，{total / 1e6:.1f} MB")
    return dest


def iter_entries(path: Path) -> Iterator[dict]:
    # 按字节读取再解码，避免 Windows 默认编码导致的乱码
    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue


def norm_pos(pos: str | None) -> str:
    pos = (pos or "").strip().rstrip(".").lower()
    return POS_NORMALIZE.get(pos, pos)


def parse_entry(entry: dict) -> dict | None:
    try:
        c = entry["content"]["word"]["content"]
        word = (entry.get("headWord") or entry["content"]["word"]["wordHead"]).strip()
    except (KeyError, TypeError, AttributeError):
        return None
    if not word:
        return None

    trans = [
        {"pos": norm_pos(t.get("pos")), "cn": (t.get("tranCn") or "").strip(), "en": (t.get("tranOther") or "").strip()}
        for t in c.get("trans") or []
        if (t.get("tranCn") or "").strip()
    ]
    phrases = [
        {"en": p.get("pContent", ""), "cn": p.get("pCn", "")} for p in (c.get("phrase") or {}).get("phrases") or []
    ]
    sentences = [
        {"en": s.get("sContent", ""), "cn": s.get("sCn", "")} for s in (c.get("sentence") or {}).get("sentences") or []
    ]
    syno = [
        {"pos": norm_pos(s.get("pos")), "cn": s.get("tran", ""), "words": [w.get("w") for w in s.get("hwds") or []]}
        for s in (c.get("syno") or {}).get("synos") or []
    ]
    rel = [
        {"pos": norm_pos(r.get("pos")), "words": [{"w": w.get("hwd"), "cn": (w.get("tran") or "").strip()} for w in r.get("words") or []]}
        for r in (c.get("relWord") or {}).get("rels") or []
    ]
    real_exam = [
        {"en": s.get("sContent", ""), **(s.get("sourceInfo") or {})}
        for s in (c.get("realExamSentence") or {}).get("sentences") or []
    ]
    exams = []
    for e in c.get("exam") or []:
        choices = sorted(e.get("choices") or [], key=lambda x: x.get("choiceIndex", 0))
        ans = (e.get("answer") or {}).get("rightIndex")
        if not e.get("question") or not choices or not ans:
            continue
        exams.append(
            {
                "question": e["question"].strip(),
                "choices": [ch.get("choice", "") for ch in choices],
                "answer_index": int(ans) - 1,
                "explain": ((e.get("answer") or {}).get("explain") or "").strip(),
            }
        )

    return {
        "word": word,
        "uk_phone": c.get("ukphone") or c.get("phone"),
        "us_phone": c.get("usphone"),
        "pos": trans[0]["pos"] if trans else "",
        "trans": trans,
        "phrases": phrases,
        "sentences": sentences,
        "syno": syno,
        "rel": rel,
        "rem_method": ((c.get("remMethod") or {}).get("val") or "").strip() or None,
        "real_exam": real_exam,
        "exams": exams,
    }


def _dump(x) -> str | None:
    return json.dumps(x, ensure_ascii=False) if x else None


def load_words(conn: sqlite3.Connection, level: str, path: Path) -> tuple[int, int]:
    """把词条写入 words 表（源数据有重复词条，按单词合并），真题词汇题写入 questions 表。
    返回 (该级别去重后的词数, 新增真题数)。"""
    n_exam = 0
    for entry in iter_entries(path):
        w = parse_entry(entry)
        if not w:
            continue
        conn.execute(
            """INSERT INTO words (word, level, uk_phone, us_phone, pos, trans_json, phrases_json,
                                  sentences_json, syno_json, rel_json, rem_method, real_exam_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(word, level) DO UPDATE SET
                 uk_phone=COALESCE(excluded.uk_phone, uk_phone), us_phone=COALESCE(excluded.us_phone, us_phone),
                 pos=COALESCE(NULLIF(excluded.pos, ''), pos), trans_json=COALESCE(excluded.trans_json, trans_json),
                 phrases_json=COALESCE(excluded.phrases_json, phrases_json),
                 sentences_json=COALESCE(excluded.sentences_json, sentences_json),
                 syno_json=COALESCE(excluded.syno_json, syno_json), rel_json=COALESCE(excluded.rel_json, rel_json),
                 rem_method=COALESCE(excluded.rem_method, rem_method),
                 real_exam_json=COALESCE(excluded.real_exam_json, real_exam_json)""",
            (
                w["word"], level, w["uk_phone"], w["us_phone"], w["pos"], _dump(w["trans"]),
                _dump(w["phrases"]), _dump(w["sentences"]), _dump(w["syno"]), _dump(w["rel"]),
                w["rem_method"], _dump(w["real_exam"]),
            ),
        )
        word_id = conn.execute("SELECT id FROM words WHERE word = ? AND level = ?", (w["word"], level)).fetchone()[0]
        for e in w["exams"]:
            if not 0 <= e["answer_index"] < len(e["choices"]):
                continue
            qid = db.insert_question(
                conn,
                level=level,
                qtype="vocab_real",
                stem=e["question"],
                choices=e["choices"],
                answer=chr(ord("A") + e["answer_index"]),
                explanation=e["explain"] or None,
                source=f"{level.upper()} 真题词汇（{SOURCE_NAME}）",
                word_id=word_id,
                tags=[w["word"]],
            )
            n_exam += qid is not None
    n_words = conn.execute("SELECT COUNT(*) FROM words WHERE level = ?", (level,)).fetchone()[0]
    return n_words, n_exam


def build(levels: list[str] | None = None, force: bool = False, conn: sqlite3.Connection | None = None) -> dict:
    """下载词库并构建题库，返回各级别统计。"""
    levels = [lv.lower() for lv in (levels or list(db.LEVELS))]
    bad = [lv for lv in levels if lv not in db.LEVELS]
    if bad:
        raise ValueError(f"未知级别：{bad}，可选 {db.LEVELS}")
    own = conn is None
    conn = conn or db.connect()
    stats = {}
    try:
        for level in levels:
            path = download(level, force=force)
            with conn:
                n_words, n_exam = load_words(conn, level, path)
                generated = generators.generate_all(conn, level)
                db.set_meta(conn, f"built_{level}", "1")
            stats[level] = {"words": n_words, "vocab_real_new": n_exam, **{f"{k}_new": v for k, v in generated.items()}}
            log(f"{level}: {stats[level]}")
        with conn:
            from . import importer

            stats["seed_new"] = importer.load_seed(conn)
    finally:
        if own:
            conn.close()
    return stats


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    force = "--force" in args
    levels = [a for a in args if not a.startswith("--")] or None
    stats = build(levels, force=force)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
