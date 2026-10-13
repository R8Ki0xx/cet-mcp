"""题库业务逻辑：抽题、判题、SM-2 复习、错题本、统计。server.py 只做 MCP 包装。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, importer, srs

PRACTICE_MODES = ("new", "any", "wrong", "due")


# ---------- 基础工具 ----------

def _today(today: date | None) -> date:
    return today or date.today()


def _now_str(today: date | None) -> str:
    if today:
        return f"{today.isoformat()} {datetime.now().strftime('%H:%M:%S')}"
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _letter(i: int) -> str:
    return chr(ord("A") + i)


def _choices(row: sqlite3.Row) -> list[str]:
    return json.loads(row["choices_json"]) if row["choices_json"] else []


def _labeled(choices: list[str]) -> list[str]:
    return [f"{_letter(i)}. {c}" for i, c in enumerate(choices)]


def type_name(qtype: str) -> str:
    return db.QUESTION_TYPES.get(qtype, qtype)


def get_question(conn: sqlite3.Connection, qid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if not row:
        raise ValueError(f"题目 {qid} 不存在")
    return row


def public_question(row: sqlite3.Row) -> dict:
    """给学生看的题目（不含答案、解析、可能泄露答案的标签）。"""
    q = {
        "id": row["id"],
        "level": row["level"].upper(),
        "type": type_name(row["type"]),
        "stem": row["stem"],
    }
    if row["material"]:
        q["material"] = row["material"]
    choices = _choices(row)
    if choices:
        q["choices"] = _labeled(choices)
    if row["source"]:
        q["source"] = row["source"]
    if row["type"] in db.SUBJECTIVE_TYPES:
        q["hint"] = "主观题：作答后调用 submit_answer 获取参考答案与评分要点，再由 grade_subjective 记录得分"
    return q


def _norm_text(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .。,，;；!！?？\"'“”‘’")


def resolve_choice(row: sqlite3.Row, answer: str) -> str | None:
    """把用户答案（字母 / "B. xxx" / 选项原文）转换成选项字母。"""
    choices = _choices(row)
    a = answer.strip()
    m = re.match(r"^\(?([A-Za-z])\)?(?:[\.\)、．:：\s]|$)", a)
    if m:
        letter = m.group(1).upper()
        if ord(letter) - ord("A") < len(choices):
            return letter
    for i, c in enumerate(choices):
        if _norm_text(c) == _norm_text(a):
            return _letter(i)
    return None


def judge(row: sqlite3.Row, answer: str) -> bool:
    if row["choices_json"]:
        return resolve_choice(row, answer) == row["answer"].strip().upper()
    return _norm_text(answer) == _norm_text(row["answer"])


def _answer_display(row: sqlite3.Row) -> str:
    choices = _choices(row)
    if choices:
        i = ord(row["answer"].upper()) - ord("A")
        if 0 <= i < len(choices):
            return f"{row['answer']}. {choices[i]}"
    return row["answer"]


# ---------- SM-2 状态读写 ----------

def _load_state(conn: sqlite3.Connection, qid: int) -> srs.ReviewState:
    r = conn.execute("SELECT * FROM reviews WHERE question_id = ?", (qid,)).fetchone()
    if not r:
        return srs.ReviewState()
    return srs.ReviewState(
        ease=r["ease"], interval=r["interval"], reps=r["reps"], lapses=r["lapses"],
        due_date=r["due_date"], in_wrongbook=bool(r["in_wrongbook"]), mastered=bool(r["mastered"]),
    )


def _record(
    conn: sqlite3.Connection, qid: int, user_answer: str, correct: bool, quality: int,
    feedback: str | None, today: date | None,
) -> srs.ReviewState:
    t = _today(today)
    before = _load_state(conn, qid)
    after = srs.review(before, quality, t)
    now = _now_str(today)
    with conn:
        conn.execute(
            "INSERT INTO attempts (question_id, user_answer, correct, quality, feedback, answered_at) VALUES (?, ?, ?, ?, ?, ?)",
            (qid, user_answer, int(correct), quality, feedback, now),
        )
        conn.execute(
            """INSERT INTO reviews (question_id, ease, interval, reps, lapses, due_date, last_reviewed,
                                    last_wrong, in_wrongbook, mastered)
               VALUES (:qid, :ease, :interval, :reps, :lapses, :due, :now, :wrong, :wb, :m)
               ON CONFLICT(question_id) DO UPDATE SET
                 ease=:ease, interval=:interval, reps=:reps, lapses=:lapses, due_date=:due,
                 last_reviewed=:now, last_wrong=COALESCE(:wrong, last_wrong),
                 in_wrongbook=:wb, mastered=:m""",
            {
                "qid": qid, "ease": after.ease, "interval": after.interval, "reps": after.reps,
                "lapses": after.lapses, "due": after.due_date, "now": now,
                "wrong": None if correct else now, "wb": int(after.in_wrongbook), "m": int(after.mastered),
            },
        )
    return after


def _state_summary(before: srs.ReviewState, after: srs.ReviewState) -> dict:
    s = {"next_review": after.due_date, "interval_days": after.interval, "in_wrongbook": after.in_wrongbook}
    if after.mastered and not before.mastered:
        s["message"] = f"已连续答对 {srs.MASTERY_STREAK} 次，移出错题本，标记为已掌握 🎉"
    elif after.in_wrongbook and not before.in_wrongbook:
        s["message"] = "已加入错题本，明天复习"
    return s


# ---------- 练习 ----------

def practice(
    conn: sqlite3.Connection,
    level: str | None = None,
    qtype: str | None = None,
    count: int = 10,
    mode: str = "new",
    tag: str | None = None,
    today: date | None = None,
) -> dict:
    if mode not in PRACTICE_MODES:
        raise ValueError(f"mode 必须是 {PRACTICE_MODES} 之一")
    count = max(1, min(int(count), 50))
    where, params = [], []
    if level:
        where.append("q.level = ?")
        params.append(level.lower())
    if qtype:
        if qtype not in db.QUESTION_TYPES:
            raise ValueError(f"type 必须是 {list(db.QUESTION_TYPES)} 之一")
        where.append("q.type = ?")
        params.append(qtype)
    if tag:
        where.append("(',' || q.tags || ',') LIKE ?")
        params.append(f"%,{tag},%")
    order = "RANDOM()"
    if mode == "new":
        where.append("r.question_id IS NULL")
    elif mode == "wrong":
        where.append("r.in_wrongbook = 1")
    elif mode == "due":
        where.append("r.due_date <= ?")
        params.append(_today(today).isoformat())
        order = "r.in_wrongbook DESC, r.due_date ASC, r.lapses DESC"
    sql = f"""SELECT q.* FROM questions q LEFT JOIN reviews r ON r.question_id = q.id
              {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY {order} LIMIT ?"""
    rows = conn.execute(sql, [*params, count]).fetchall()
    result = {"count": len(rows), "questions": [public_question(r) for r in rows]}
    if not rows:
        total = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        result["hint"] = (
            "题库为空，请先调用 setup_question_bank 构建题库" if total == 0 else "没有符合条件的题目，可放宽级别/题型/模式"
        )
    return result


def due_reviews(conn: sqlite3.Connection, level: str | None = None, limit: int = 20, today: date | None = None) -> dict:
    data = practice(conn, level=level, count=limit, mode="due", today=today)
    where, params = ["r.due_date <= ?"], [_today(today).isoformat()]
    if level:
        where.append("q.level = ?")
        params.append(level.lower())
    total = conn.execute(
        f"SELECT COUNT(*) FROM reviews r JOIN questions q ON q.id = r.question_id WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    data["total_due"] = total
    data.pop("hint", None)
    if not data["questions"]:
        data["message"] = "今天没有到期的复习题"
        nxt = conn.execute(
            f"""SELECT r.due_date, COUNT(*) n FROM reviews r JOIN questions q ON q.id = r.question_id
                WHERE r.due_date > ? {'AND q.level = ?' if level else ''}
                GROUP BY r.due_date ORDER BY r.due_date LIMIT 1""",
            params,
        ).fetchone()
        if nxt:
            data["message"] += f"。下次复习：{nxt['due_date']}，共 {nxt['n']} 题（想现在就重做错题可用 practice 的 wrong 模式）"
    return data


# ---------- 判题 ----------

def word_card(conn: sqlite3.Connection, word: str, level: str | None = None) -> dict | None:
    sql = "SELECT * FROM words WHERE word = ? COLLATE NOCASE"
    params: list = [word.strip()]
    if level:
        sql += " AND level = ?"
        params.append(level.lower())
    rows = conn.execute(sql + " ORDER BY level", params).fetchall()
    if not rows:
        return None
    r = rows[0]
    load = lambda k: json.loads(r[k]) if r[k] else []  # noqa: E731
    # 同一个词可能同时在四级、六级词表里，真题原句合并两个级别并去重
    real_exam, seen = [], set()
    for x in rows:
        for s in json.loads(x["real_exam_json"]) if x["real_exam_json"] else []:
            if s.get("en") not in seen:
                seen.add(s.get("en"))
                real_exam.append(s)
    card = {
        "word": r["word"],
        "levels": [x["level"].upper() for x in rows],
        "phonetic": {"uk": r["uk_phone"], "us": r["us_phone"]},
        "meanings": [f"{t['pos']}. {t['cn']}" + (f"  ({t['en']})" if t.get("en") else "") for t in load("trans_json")],
        "phrases": [f"{p['en']}  {p['cn']}" for p in load("phrases_json")][:8],
        "examples": [f"{s['en']}  {s['cn']}" for s in load("sentences_json")][:3],
        "synonyms": [f"{s['pos']}. {s['cn']}：{', '.join(w for w in s['words'] if w)}" for s in load("syno_json")],
        "related": [
            f"{g['pos']}. " + "；".join(f"{w['w']} {w['cn']}" for w in g["words"][:4]) for g in load("rel_json")
        ],
        "memory_tip": r["rem_method"],
        "real_exam_sentences": [
            f"[{' '.join(x for x in (s.get('year'), s.get('level'), s.get('paper'), s.get('type')) if x)}] {s['en']}"
            for s in real_exam
        ][:5],
    }
    return {k: v for k, v in card.items() if v}


def submit_answer(conn: sqlite3.Connection, qid: int, answer: str, today: date | None = None) -> dict:
    row = get_question(conn, qid)
    if row["type"] in db.SUBJECTIVE_TYPES:
        return {
            "id": qid,
            "type": type_name(row["type"]),
            "subjective": True,
            "your_answer": answer,
            "reference_answer": row["answer"],
            "rubric": row["explanation"],
            "next_step": "请对照参考答案与评分要点，按 0-5 分（5=接近满分，3=及格，<3 需复习）评分，"
            "并调用 grade_subjective(question_id, user_answer, score, feedback) 记录",
        }

    correct = judge(row, answer)
    before = _load_state(conn, qid)
    after = _record(conn, qid, answer, correct, srs.QUALITY_CORRECT if correct else srs.QUALITY_WRONG, None, today)
    result = {
        "id": qid,
        "correct": correct,
        "your_answer": answer,
        "correct_answer": _answer_display(row),
        "explanation": row["explanation"],
        **_state_summary(before, after),
    }
    if row["word_id"]:
        w = conn.execute("SELECT word, level FROM words WHERE id = ?", (row["word_id"],)).fetchone()
        if w and not correct:
            result["word_card"] = word_card(conn, w["word"], w["level"])
    return {k: v for k, v in result.items() if v is not None}


def grade_subjective(
    conn: sqlite3.Connection, qid: int, user_answer: str, score: int, feedback: str | None = None,
    today: date | None = None,
) -> dict:
    row = get_question(conn, qid)
    score = int(score)
    if not 0 <= score <= 5:
        raise ValueError("score 必须是 0-5 的整数")
    before = _load_state(conn, qid)
    after = _record(conn, qid, user_answer, score >= 3, score, feedback, today)
    return {"id": qid, "type": type_name(row["type"]), "score": score, **_state_summary(before, after)}


# ---------- 错题本 ----------

WRONGBOOK_SORTS = {
    "lapses": "r.lapses DESC, r.last_wrong DESC",
    "recent": "r.last_wrong DESC",
    "due": "r.due_date ASC",
}


def wrongbook(
    conn: sqlite3.Connection, level: str | None = None, qtype: str | None = None, sort: str = "lapses",
    limit: int = 30, include_mastered: bool = False, with_answers: bool = True,
) -> dict:
    if sort not in WRONGBOOK_SORTS:
        raise ValueError(f"sort 必须是 {list(WRONGBOOK_SORTS)} 之一")
    where = ["(r.in_wrongbook = 1" + (" OR (r.mastered = 1 AND r.lapses > 0))" if include_mastered else ")")]
    params: list = []
    if level:
        where.append("q.level = ?")
        params.append(level.lower())
    if qtype:
        where.append("q.type = ?")
        params.append(qtype)
    cond = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM reviews r JOIN questions q ON q.id = r.question_id WHERE {cond}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT q.*, r.lapses, r.last_wrong, r.due_date, r.note, r.mastered,
                   (SELECT user_answer FROM attempts a WHERE a.question_id = q.id AND a.correct = 0
                    ORDER BY a.id DESC LIMIT 1) AS last_wrong_answer,
                   (SELECT feedback FROM attempts a WHERE a.question_id = q.id AND a.correct = 0
                    ORDER BY a.id DESC LIMIT 1) AS last_feedback
            FROM reviews r JOIN questions q ON q.id = r.question_id
            WHERE {cond} ORDER BY {WRONGBOOK_SORTS[sort]} LIMIT ?""",
        [*params, max(1, min(int(limit), 200))],
    ).fetchall()
    items = []
    for r in rows:
        item = public_question(r)
        item.update({"wrong_times": r["lapses"], "last_wrong": r["last_wrong"], "next_review": r["due_date"]})
        if r["mastered"]:
            item["mastered"] = True
        if with_answers:
            item["your_last_wrong_answer"] = r["last_wrong_answer"]
            item["correct_answer"] = _answer_display(r)
            if r["explanation"]:
                item["explanation"] = r["explanation"]
            if r["last_feedback"]:
                item["last_feedback"] = r["last_feedback"]
        if r["note"]:
            item["note"] = r["note"]
        items.append(item)
    return {"total": total, "items": items}


def wrongbook_update(conn: sqlite3.Connection, qid: int, action: str, note: str | None = None, today: date | None = None) -> dict:
    get_question(conn, qid)
    t = _today(today).isoformat()
    with conn:
        if action in ("add", "note"):
            conn.execute("INSERT OR IGNORE INTO reviews (question_id, due_date) VALUES (?, ?)", (qid, t))
        if action == "add":
            conn.execute("UPDATE reviews SET in_wrongbook = 1, mastered = 0, due_date = ? WHERE question_id = ?", (t, qid))
        elif action == "remove":
            conn.execute("UPDATE reviews SET in_wrongbook = 0 WHERE question_id = ?", (qid,))
        elif action == "master":
            conn.execute("UPDATE reviews SET in_wrongbook = 0, mastered = 1 WHERE question_id = ?", (qid,))
        elif action == "note":
            if not note:
                raise ValueError("action=note 时需要提供 note")
            conn.execute("UPDATE reviews SET note = ? WHERE question_id = ?", (note, qid))
        else:
            raise ValueError("action 必须是 add / remove / master / note")
    r = conn.execute("SELECT in_wrongbook, mastered, note FROM reviews WHERE question_id = ?", (qid,)).fetchone()
    if not r:
        return {"id": qid, "in_wrongbook": False, "mastered": False, "note": None}
    return {"id": qid, "in_wrongbook": bool(r["in_wrongbook"]), "mastered": bool(r["mastered"]), "note": r["note"]}


def export_wrongbook(conn: sqlite3.Connection, path: str | None = None, level: str | None = None) -> dict:
    data = wrongbook(conn, level=level, sort="lapses", limit=200)
    out = Path(path).expanduser() if path else db.DATA_DIR / f"错题本_{date.today():%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 四六级错题本{'（' + level.upper() + '）' if level else ''}", "",
             f"导出时间：{datetime.now():%Y-%m-%d %H:%M}　共 {data['total']} 题", ""]
    for i, q in enumerate(data["items"], 1):
        lines.append(f"## {i}. [{q['level']} · {q['type']}] 错 {q['wrong_times']} 次（#{q['id']}）")
        if q.get("source"):
            lines.append(f"> 出处：{q['source']}")
        if q.get("material"):
            lines += ["", q["material"]]
        lines += ["", q["stem"].replace("\n", "  \n"), ""]
        lines += [f"- {c}" for c in q.get("choices", [])]
        subjective = bool(q.get("hint"))
        lines += ["", f"**{'参考答案' if subjective else '正确答案'}**：{q['correct_answer']}"]
        if q.get("your_last_wrong_answer"):
            lines += ["", f"**我上次的答案**：{q['your_last_wrong_answer']}"]
        if q.get("last_feedback"):
            lines += ["", f"**批改意见**：{q['last_feedback']}"]
        if q.get("explanation"):
            lines += ["", f"**{'评分要点' if subjective else '解析'}**：", "", q["explanation"].replace("\n", "  \n")]
        if q.get("note"):
            lines += ["", f"**笔记**：{q['note']}"]
        lines += ["", "---", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return {"path": str(out), "count": len(data["items"]), "total": data["total"]}


# ---------- 出题 / 导入 ----------

def add_question(conn: sqlite3.Connection, **fields) -> dict:
    with conn:
        res = importer.add_many(conn, [fields], default_source="AI 出题")
    if res["errors"]:
        raise ValueError(res["errors"][0])
    if res["duplicates"]:
        return {"added": False, "message": "题库中已有相同题目"}
    return {"added": True, "id": res["ids"][0]}


def import_questions(conn: sqlite3.Connection, file_path: str) -> dict:
    with conn:
        res = importer.import_file(conn, file_path)
    res.pop("ids", None)
    res["errors"] = res["errors"][:20]
    return res


# ---------- 统计 ----------

def _accuracy(c: int, n: int) -> str:
    return f"{c / n:.0%}" if n else "-"


def overview(conn: sqlite3.Connection, today: date | None = None) -> dict:
    t = _today(today).isoformat()
    bank: dict = {}
    for r in conn.execute("SELECT level, type, COUNT(*) n FROM questions GROUP BY level, type ORDER BY level, type"):
        bank.setdefault(r["level"].upper(), {})[type_name(r["type"])] = r["n"]
    words = {r["level"].upper(): r["n"] for r in conn.execute("SELECT level, COUNT(*) n FROM words GROUP BY level")}
    a = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(correct), 0) c, COUNT(DISTINCT question_id) d FROM attempts").fetchone()
    rv = conn.execute(
        """SELECT SUM(due_date <= ?) due, SUM(in_wrongbook) wb, SUM(mastered) m FROM reviews""", (t,)
    ).fetchone()
    result = {
        "question_bank": bank,
        "vocabulary": words,
        "progress": {
            "answered_questions": a["d"],
            "total_attempts": a["n"],
            "accuracy": _accuracy(a["c"], a["n"]),
            "due_today": rv["due"] or 0,
            "wrongbook": rv["wb"] or 0,
            "mastered": rv["m"] or 0,
        },
        "question_types": db.QUESTION_TYPES,
    }
    if not bank:
        result["hint"] = "题库为空，请先调用 setup_question_bank 构建题库（首次需下载约 30MB 词库）"
    return result


def _streak(days_with_activity: set[str], today: date) -> int:
    d = today if today.isoformat() in days_with_activity else today - timedelta(days=1)
    n = 0
    while d.isoformat() in days_with_activity:
        n += 1
        d -= timedelta(days=1)
    return n


def report(conn: sqlite3.Connection, days: int = 7, today: date | None = None) -> dict:
    t = _today(today)
    start = (t - timedelta(days=max(1, days) - 1)).isoformat()
    rows = conn.execute(
        """SELECT q.level, q.type, a.correct, substr(a.answered_at, 1, 10) day
           FROM attempts a JOIN questions q ON q.id = a.question_id WHERE a.answered_at >= ?""",
        (start,),
    ).fetchall()
    by_type: dict = {}
    by_day: dict = {}
    for r in rows:
        k = f"{r['level'].upper()} {type_name(r['type'])}"
        s = by_type.setdefault(k, [0, 0])
        s[0] += r["correct"]
        s[1] += 1
        d = by_day.setdefault(r["day"], [0, 0])
        d[0] += r["correct"]
        d[1] += 1
    all_days = {r[0] for r in conn.execute("SELECT DISTINCT substr(answered_at, 1, 10) FROM attempts")}
    weak = sorted(((c / n, k) for k, (c, n) in by_type.items() if n >= 3 and c < n))
    missed = conn.execute(
        """SELECT w.word, w.level, SUM(r.lapses) lapses FROM reviews r
           JOIN questions q ON q.id = r.question_id JOIN words w ON w.id = q.word_id
           WHERE r.lapses > 0 GROUP BY w.id ORDER BY lapses DESC, r.last_wrong DESC LIMIT 15"""
    ).fetchall()
    total_c = sum(c for c, _ in by_type.values())
    total_n = sum(n for _, n in by_type.values())
    return {
        "period": f"{start} ~ {t.isoformat()}",
        "attempts": total_n,
        "accuracy": _accuracy(total_c, total_n),
        "streak_days": _streak(all_days, t),
        "by_type": {k: {"attempts": n, "accuracy": _accuracy(c, n)} for k, (c, n) in sorted(by_type.items())},
        "by_day": {k: {"attempts": n, "accuracy": _accuracy(c, n)} for k, (c, n) in sorted(by_day.items())},
        "weakest": [f"{k}（正确率 {acc:.0%}）" for acc, k in weak[:3]],
        "most_missed_words": [f"{m['word']}（{m['level'].upper()}，错 {m['lapses']} 次）" for m in missed],
        "wrongbook_size": conn.execute("SELECT COUNT(*) FROM reviews WHERE in_wrongbook = 1").fetchone()[0],
    }
