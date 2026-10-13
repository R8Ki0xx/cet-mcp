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


def _w(word, pos="v", syno=(), real=()):
    return {"id": 0, "word": word, "pos": pos, "uk_phone": None, "trans": [{"pos": pos, "cn": word + "义"}],
            "sentences": [], "real_exam": list(real), "syno": set(syno)}


POOL = [_w(x) for x in ("attract", "seize", "engage", "adopt", "affect", "allocate", "assess", "avoid")]


def test_cloze_skips_sentence_where_word_repeats():
    w = _w("task", real=[{"en": "each task starts when the previous task ends"}, {"en": "a hard task indeed"}])
    q = generators.gen_context_cloze("cet4", w, POOL)
    assert q["stem"].endswith("a hard ______ indeed")
    only_repeat = _w("task", real=[{"en": "each task starts when the previous task ends"}])
    assert generators.gen_context_cloze("cet4", only_repeat, POOL) is None


def test_distractors_exclude_synonyms():
    target = _w("absorb", syno={"attract", "seize", "engage"})
    for kind in ("a", "b", "c", "d", "e"):
        rng = generators._rng("cet4", "absorb", kind)
        ds, _ = generators._pick_distractors(rng, POOL, target, 3, lambda c: c["word"], set())
        assert not {d["word"] for d in ds} & target["syno"]


def _wt(word, pos, cn, rel=()):
    w = _w(word, pos)
    w["trans"] = [{"pos": pos, "cn": cn}]
    w["terms"] = generators._terms(w["trans"])
    w["rel"] = set(rel)
    return w


def test_lookalikes_found_and_related_words_excluded():
    words = [
        _wt("adapt", "v", "适应，改编"), _wt("adopt", "v", "采用，收养"), _wt("adept", "adj", "熟练的"),
        _wt("adaptation", "n", "适应，改编"), _wt("adaptable", "adj", "能适应的", rel={"adapt"}),
        _wt("banana", "n", "香蕉"), _wt("fit", "v", "适应，适合"),
    ]
    alike = generators.build_lookalikes(words)
    names = [w["word"] for w in alike[0]]
    assert names[0] == "adopt"  # 同词性、编辑距离最小的排第一
    assert "adept" in names
    # 派生词、同根词、释义重叠的词、毫不相像的词都不算形近词
    assert not {"adaptation", "adaptable", "fit", "banana"} & set(names)


def test_en2cn_prefers_lookalikes_and_explains_them():
    words = [_wt("adapt", "v", "适应"), _wt("adopt", "v", "采用"), _wt("adept", "adj", "熟练的")]
    pool = words + [_wt(x, "v", x + "义") for x in ("run", "eat", "sing", "swim", "read")]
    alike = generators.build_lookalikes(words)[0]
    q = generators.gen_en2cn("cet4", words[0], pool, alike)
    assert "v. 采用" in q["choices"] and "adj. 熟练的" in q["choices"]
    assert "易混形近词" in q["explanation_prefix"] and "adopt" in q["explanation_prefix"]


def test_levenshtein():
    assert generators.levenshtein("kitten", "sitting") == 3
    assert generators.levenshtein("", "abc") == 3


def test_spelling_hides_phonetic_identical_to_word():
    w = _w("tent", pos="n")
    w["uk_phone"] = "tent"
    assert "音标" not in generators.gen_spelling("cet4", w)["stem"]
    w["uk_phone"] = "əb'sɔ:b"
    assert "音标" in generators.gen_spelling("cet4", w)["stem"]


def test_remove_stale_keeps_answered_questions(conn):
    from cet_mcp import service

    ids = [r[0] for r in conn.execute("SELECT id FROM questions WHERE type = 'en2cn' LIMIT 2")]
    service.submit_answer(conn, ids[0], "A")
    removed = generators.remove_stale(conn, "cet4", ("en2cn",), keep=set())
    left = {r[0] for r in conn.execute("SELECT id FROM questions WHERE type = 'en2cn'")}
    assert left == {ids[0]} and removed >= 1


def test_regeneration_updates_in_place_and_keeps_history(conn):
    from cet_mcp import service

    row = conn.execute("SELECT id, word_id FROM questions WHERE type = 'cn2en' LIMIT 1").fetchone()
    service.submit_answer(conn, row["id"], "A")
    # 模拟旧版生成器留下的错误内容
    conn.execute("UPDATE questions SET choices_json = '[\"x\",\"y\",\"z\",\"w\"]', hash = 'old' WHERE id = ?", (row["id"],))
    generators.generate_all(conn, "cet4")
    fixed = conn.execute("SELECT * FROM questions WHERE id = ?", (row["id"],)).fetchone()
    assert "x" not in json.loads(fixed["choices_json"])
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE type = 'cn2en' AND word_id = ?", (row["word_id"],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE question_id = ?", (row["id"],)).fetchone()[0] == 1
