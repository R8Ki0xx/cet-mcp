import json

from cet_mcp import generators


def test_words_and_real_exam_loaded(conn):
    assert conn.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 28
    q = conn.execute("SELECT * FROM questions WHERE type = 'vocab_real'").fetchone()
    assert q["answer"] == "B"
    assert json.loads(q["choices_json"])[1] == "absorbed"


def test_generated_choice_questions_are_valid(conn):
    rows = conn.execute("SELECT * FROM questions WHERE type IN ('en2cn', 'cn2en', 'context_cloze')").fetchall()
    assert rows
    for r in rows:
        choices = json.loads(r["choices_json"])
        assert len(choices) == 4
        assert len(set(choices)) == 4, "选项不能重复"
        idx = ord(r["answer"]) - ord("A")
        word = conn.execute("SELECT word, trans_json FROM words WHERE id = ?", (r["word_id"],)).fetchone()
        if r["type"] == "en2cn":
            assert choices[idx] == generators.meaning(json.loads(word["trans_json"]))
        else:
            assert choices[idx] == word["word"]


def test_context_cloze_blanks_word_and_keeps_source(conn):
    r = conn.execute("SELECT * FROM questions WHERE type = 'context_cloze'").fetchone()
    assert generators.BLANK in r["stem"] and "absorb " not in r["stem"]
    assert "2020.12" in r["source"]


def test_spelling(conn):
    r = conn.execute(
        "SELECT q.* FROM questions q JOIN words w ON w.id = q.word_id WHERE q.type = 'spelling' AND w.word = 'abandon'"
    ).fetchone()
    assert r["answer"] == "abandon"
    assert "共 7 个字母" in r["stem"]


def test_generation_is_idempotent(conn):
    before = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    counts = generators.generate_all(conn, "cet4")
    assert sum(counts.values()) == 0
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == before
