import json
from datetime import date, timedelta

import pytest

from cet_mcp import importer, service, srs

D = date(2026, 10, 13)


def _vocab_real_id(conn):
    return conn.execute("SELECT id FROM questions WHERE type = 'vocab_real'").fetchone()[0]


def test_practice_hides_answers(conn):
    res = service.practice(conn, level="cet4", count=5)
    assert res["count"] == 5
    for q in res["questions"]:
        assert "answer" not in q and "explanation" not in q and "tags" not in q


def test_wrong_then_master_flow(conn):
    qid = _vocab_real_id(conn)

    r = service.submit_answer(conn, qid, "A", today=D)
    assert r["correct"] is False and r["in_wrongbook"] is True
    assert r["correct_answer"] == "B. absorbed"
    assert r["word_card"]["word"] == "absorb"
    assert service.wrongbook(conn)["total"] == 1

    # 新题模式不再出这道题；明天到期
    assert qid not in {q["id"] for q in service.practice(conn, count=50, mode="new")["questions"]}
    assert service.due_reviews(conn, today=D)["total_due"] == 0
    assert service.due_reviews(conn, today=D + timedelta(days=1))["questions"][0]["id"] == qid

    day = D + timedelta(days=1)
    for i in range(srs.MASTERY_STREAK):
        answer = "absorbed" if i == 0 else "b"  # 选项原文、小写字母都能判对
        r = service.submit_answer(conn, qid, answer, today=day)
        assert r["correct"] is True
        day = date.fromisoformat(r["next_review"])
    assert "已掌握" in r["message"]
    assert service.wrongbook(conn)["total"] == 0
    assert service.wrongbook(conn, include_mastered=True)["total"] == 1


def test_spelling_judge_case_insensitive(conn):
    qid = conn.execute("SELECT id FROM questions WHERE type = 'spelling' AND answer = 'abandon'").fetchone()[0]
    assert service.submit_answer(conn, qid, " Abandon ", today=D)["correct"] is True


def test_subjective_flow(conn):
    res = service.add_question(
        conn, level="cet4", type="translation", stem="翻译：我爱学习。", answer="I love studying.",
        explanation="love doing",
    )
    qid = res["id"]
    r = service.submit_answer(conn, qid, "I like study.", today=D)
    assert r["subjective"] and r["reference_answer"] == "I love studying."
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE question_id = ?", (qid,)).fetchone()[0] == 0
    g = service.grade_subjective(conn, qid, "I like study.", 2, "study 应为 studying", today=D)
    assert g["in_wrongbook"] is True
    with pytest.raises(ValueError):
        service.grade_subjective(conn, qid, "x", 9)


def test_add_question_validation_and_dedupe(conn):
    kw = dict(level="cet6", type="grammar", stem="He ___ here.", choices=["A. is", "B. are"], answer="is")
    r = service.add_question(conn, **kw)
    row = service.get_question(conn, r["id"])
    assert row["answer"] == "A" and json.loads(row["choices_json"]) == ["is", "are"]
    assert service.add_question(conn, **kw)["added"] is False
    with pytest.raises(ValueError):
        service.add_question(conn, level="cet6", type="grammar", stem="x", choices=["a", "b"], answer="C")
    with pytest.raises(ValueError):
        service.add_question(conn, level="cet8", type="grammar", stem="x", answer="y")


def test_import_csv_and_json(conn, tmp_path):
    csv_path = tmp_path / "q.csv"
    csv_path.write_text(
        "level,type,stem,choice_a,choice_b,choice_c,choice_d,answer,explanation,material,tags,source\n"
        "cet4,reading,What is the main idea?,Food,Sport,Music,Art,C,,Passage text,阅读,2023.6 CET4\n"
        "cet9,reading,bad,,,,,x,,,,\n",
        encoding="utf-8-sig",
    )
    r = service.import_questions(conn, str(csv_path))
    assert r["added"] == 1 and len(r["errors"]) == 1

    json_path = tmp_path / "q.json"
    json_path.write_text(json.dumps({"questions": [{"level": "cet6", "type": "cloze", "stem": "s", "answer": "a"}]}), encoding="utf-8")
    assert service.import_questions(conn, str(json_path))["added"] == 1


def test_same_question_on_different_passages_not_deduped(conn):
    base = dict(level="cet4", type="reading", stem="What is the main idea?", choices=["a", "b", "c", "d"], answer="C")
    assert service.add_question(conn, material="Passage one.", **base)["added"] is True
    assert service.add_question(conn, material="Passage two.", **base)["added"] is True
    assert service.add_question(conn, material="Passage two.", **base)["added"] is False


def test_seed_file_is_valid(conn):
    items = json.loads(importer.SEED_FILE.read_text(encoding="utf-8"))
    for q in items:
        importer.normalize(q)  # 不抛异常即合法
    assert importer.load_seed(conn) == len(items)


def test_wrongbook_update_and_export(conn, tmp_path):
    qid = _vocab_real_id(conn)
    service.wrongbook_update(conn, qid, "add", today=D)
    service.wrongbook_update(conn, qid, "note", "absorbed in = 专注于")
    out = service.export_wrongbook(conn, str(tmp_path / "wb.md"))
    text = (tmp_path / "wb.md").read_text(encoding="utf-8")
    assert out["count"] == 1 and "absorbed in = 专注于" in text and "B. absorbed" in text
    assert service.wrongbook_update(conn, qid, "master")["mastered"] is True


def test_report_and_overview(conn):
    ids = [q["id"] for q in service.practice(conn, qtype="en2cn", count=6)["questions"]]
    for i, qid in enumerate(ids):
        ans = service.get_question(conn, qid)["answer"] if i % 2 else "Z"
        service.submit_answer(conn, qid, ans, today=D)
    rep = service.report(conn, days=7, today=D)
    assert rep["attempts"] == 6 and rep["accuracy"] == "50%" and rep["streak_days"] == 1
    assert rep["weakest"] and rep["most_missed_words"]
    ov = service.overview(conn, today=D)
    assert ov["progress"]["wrongbook"] == 3
    assert ov["vocabulary"]["CET4"] == 28
