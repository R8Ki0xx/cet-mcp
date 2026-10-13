import json

import pytest

from cet_mcp import db, generators, vocab_source


def _entry(word, pos, cn, exam=None, real=None):
    content = {
        "trans": [{"pos": pos, "tranCn": cn, "tranOther": f"meaning of {word}"}],
        "ukphone": word,
        "sentence": {"sentences": [{"sContent": f"I {word} it.", "sCn": "例句"}]},
    }
    if exam:
        content["exam"] = [exam]
    if real:
        content["realExamSentence"] = {"sentences": [{"sContent": real, "sourceInfo": {"year": "2020.12", "level": "CET4", "paper": "第一套", "type": "阅读理解"}}]}
    return {"headWord": word, "content": {"word": {"wordHead": word, "content": content}}}


SAMPLE_WORDS = [
    ("absorb", "vt", "吸收"), ("abandon", "vt", "放弃"), ("achieve", "vt", "实现"), ("adapt", "vi", "适应"),
    ("admire", "vt", "钦佩"), ("adopt", "vt", "采纳"), ("affect", "vt", "影响"), ("allocate", "vt", "分配"),
    ("analyze", "vt", "分析"), ("announce", "vt", "宣布"), ("apply", "vi", "申请"), ("approve", "vt", "批准"),
    ("arrange", "vt", "安排"), ("assess", "vt", "评估"), ("assume", "vt", "假定"), ("attach", "vt", "附上"),
    ("attract", "vt", "吸引"), ("avoid", "vt", "避免"), ("balance", "vt", "平衡"), ("behave", "vi", "表现"),
    ("benefit", "vt", "有益于"), ("borrow", "vt", "借"), ("calculate", "vt", "计算"), ("capture", "vt", "捕获"),
    ("ability", "n", "能力"), ("access", "n", "通道"), ("account", "n", "账户"), ("advantage", "n", "优势"),
]


@pytest.fixture
def sample_jsonl(tmp_path):
    lines = []
    for word, pos, cn in SAMPLE_WORDS:
        exam = real = None
        if word == "absorb":
            exam = {
                "question": "She was so _______ in her job that she didn't hear the knock.",
                "answer": {"explain": "be absorbed in 专注于", "rightIndex": 2},
                "choices": [{"choiceIndex": 1, "choice": "attracted"}, {"choiceIndex": 2, "choice": "absorbed"},
                            {"choiceIndex": 3, "choice": "drawn"}, {"choiceIndex": 4, "choice": "concentrated"}],
            }
            real = "...plants absorb water through their roots..."
        lines.append(json.dumps(_entry(word, pos, cn, exam, real), ensure_ascii=False))
    lines.append("not json")  # 坏行应被跳过
    p = tmp_path / "cet4.jsonl"
    p.write_bytes("\n".join(lines).encode("utf-8"))
    return p


@pytest.fixture
def conn(tmp_path, sample_jsonl):
    c = db.connect(tmp_path / "test.db")
    with c:
        vocab_source.load_words(c, "cet4", sample_jsonl)
        generators.generate_all(c, "cet4")
    yield c
    c.close()
