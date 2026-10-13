"""四六级备考题库 / 错题本 MCP 服务器（FastMCP，stdio）。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import db, service, vocab_source

Level = Literal["cet4", "cet6"]
QType = Literal[
    "vocab_real", "context_cloze", "en2cn", "cn2en", "spelling", "grammar",
    "reading", "cloze", "listening", "translation", "writing", "custom",
]

mcp = FastMCP(
    "cet-bank",
    instructions=(
        "四六级（CET-4/CET-6）备考题库与错题本，内置 SM-2 间隔重复。\n"
        "使用约定：\n"
        "1. 首次使用若 bank_overview 显示题库为空，先调用 setup_question_bank。\n"
        "2. 出题时只展示 practice / get_due_reviews 返回的题干和选项，不要提前透露答案；"
        "等用户作答后再调用 submit_answer 判题并讲解。\n"
        "3. 翻译、写作是主观题：submit_answer 会返回参考答案和评分要点，你按要点给 0-5 分并"
        "调用 grade_subjective 记录，同时给出具体修改建议。\n"
        "4. 每次学习建议先做 get_due_reviews 的到期复习，再练新题。\n"
        "5. 你可以用 add_question 按用户薄弱点出新题入库（请保证答案唯一、解析清楚）。"
    ),
)


@contextmanager
def _conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@mcp.tool()
def setup_question_bank(levels: list[Level] | None = None, force: bool = False) -> dict:
    """下载四六级词库（KyleBing/english-vocabulary，约 30MB）并构建题库。

    包含：真题词汇选择题（带解析）、真题原句语境填空、英译中、中译英、拼写，以及原创语法/翻译/写作题。
    可重复调用，已有题目和做题记录不会丢失。levels 默认 ["cet4", "cet6"]；force=True 重新下载词库。
    """
    with _conn() as conn:
        return vocab_source.build(list(levels) if levels else None, force=force, conn=conn)


@mcp.tool()
def bank_overview() -> dict:
    """查看题库概况：各级别各题型题量、词汇量、做题进度、正确率、今日待复习数、错题本数量。"""
    with _conn() as conn:
        return service.overview(conn)


@mcp.tool()
def practice(
    level: Level | None = None,
    type: QType | None = None,
    count: int = 10,
    mode: Literal["new", "any", "wrong", "due"] = "new",
    tag: str | None = None,
) -> dict:
    """抽题练习（不含答案）。

    type 题型：vocab_real 真题词汇选择 / context_cloze 真题原句语境填空 / en2cn 英译中 / cn2en 中译英 /
    spelling 拼写 / grammar 语法 / reading 阅读 / cloze 选词填空 / translation 翻译 / writing 写作 / custom。
    mode：new 只出没做过的题（默认）；any 随机；wrong 只出错题本里的题；due 今天到期该复习的题。
    tag：按标签筛选（自动生成的词汇题以单词为标签，如 "absorb"）。count 最多 50。
    """
    with _conn() as conn:
        return service.practice(conn, level=level, qtype=type, count=count, mode=mode, tag=tag)


@mcp.tool()
def submit_answer(question_id: int, answer: str) -> dict:
    """提交答案并判题。选择题可传字母（"B"）或选项原文；拼写题传单词。

    返回对错、正确答案、解析，答错时附完整单词卡；同时按 SM-2 更新下次复习日期，答错自动进错题本，
    错题连续答对 3 次自动移出错题本。主观题（翻译/写作）返回参考答案与评分要点，需再调用 grade_subjective。
    """
    with _conn() as conn:
        return service.submit_answer(conn, question_id, answer)


@mcp.tool()
def grade_subjective(question_id: int, user_answer: str, score: int, feedback: str | None = None) -> dict:
    """记录主观题（翻译/写作）得分。score 0-5：5 优秀，4 良好，3 及格，2 及以下需重练（进错题本）。

    feedback 写给用户的具体问题，如"漏译'传统节日'；时态错误"，会保存在作答记录里。
    """
    with _conn() as conn:
        return service.grade_subjective(conn, question_id, user_answer, score, feedback)


@mcp.tool()
def get_due_reviews(level: Level | None = None, limit: int = 20) -> dict:
    """获取今天到期需要复习的题（SM-2 排期，错题本中的题优先），不含答案。"""
    with _conn() as conn:
        return service.due_reviews(conn, level=level, limit=limit)


@mcp.tool()
def wrongbook(
    level: Level | None = None,
    type: QType | None = None,
    sort: Literal["lapses", "recent", "due"] = "lapses",
    limit: int = 30,
    include_mastered: bool = False,
    with_answers: bool = True,
) -> dict:
    """查看错题本。sort：lapses 按错误次数 / recent 按最近出错 / due 按复习日期。

    with_answers=False 时不返回答案（适合拿错题直接重做）；include_mastered=True 同时列出曾错但已掌握的题。
    """
    with _conn() as conn:
        return service.wrongbook(
            conn, level=level, qtype=type, sort=sort, limit=limit,
            include_mastered=include_mastered, with_answers=with_answers,
        )


@mcp.tool()
def wrongbook_update(
    question_id: int, action: Literal["add", "remove", "master", "note"], note: str | None = None
) -> dict:
    """手动管理错题本：add 加入 / remove 移出 / master 标记已掌握 / note 写笔记（需 note 参数）。"""
    with _conn() as conn:
        return service.wrongbook_update(conn, question_id, action, note)


@mcp.tool()
def lookup_word(word: str, level: Level | None = None) -> dict:
    """查四六级词汇卡：音标、中英释义、短语、例句、同近义词、同根词、记忆法、真题原句（含年份/题型出处）。"""
    with _conn() as conn:
        card = service.word_card(conn, word, level)
    return card or {"found": False, "message": f"四六级词库中没有 {word!r}（可能不在大纲内，或题库尚未构建）"}


@mcp.tool()
def add_question(
    level: Level,
    type: QType,
    stem: str,
    answer: str,
    choices: list[str] | None = None,
    explanation: str | None = None,
    material: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
) -> dict:
    """向题库添加一道题（AI 出题或用户录入）。

    选择题：choices 传选项原文数组（不带 "A." 前缀），answer 传字母或选项原文。
    翻译/写作：answer 填参考答案，explanation 填评分要点。阅读题的文章放 material。
    """
    with _conn() as conn:
        return service.add_question(
            conn, level=level, type=type, stem=stem, answer=answer, choices=choices,
            explanation=explanation, material=material, tags=tags, source=source,
        )


@mcp.tool()
def import_questions(file_path: str) -> dict:
    """从本地 JSON / CSV 文件批量导入题目（格式见 README），重复题自动跳过，返回导入数与错误行。"""
    with _conn() as conn:
        return service.import_questions(conn, file_path)


@mcp.tool()
def study_report(days: int = 7) -> dict:
    """学习报告：近 N 天做题量与正确率（按题型、按天）、连续打卡天数、最薄弱题型、最常错的单词。"""
    with _conn() as conn:
        return service.report(conn, days=days)


@mcp.tool()
def export_wrongbook(path: str | None = None, level: Level | None = None) -> dict:
    """把错题本导出为 Markdown 文件（含题目、正确答案、我的错误答案、解析、笔记），默认存到 data/ 目录。"""
    with _conn() as conn:
        return service.export_wrongbook(conn, path=path, level=level)


@mcp.prompt()
def daily_session(level: Level = "cet4", new_count: int = 10) -> str:
    """每日练习流程：复习到期题 → 练新题 → 学习小结。"""
    return (
        f"开始今天的 {level.upper()} 练习：\n"
        "1. 先调用 get_due_reviews 取到期复习题，逐题出给我，我答完一题你再调用 submit_answer 判题并讲解。\n"
        f"2. 复习完后调用 practice(level='{level}', count={new_count}) 练新题，题型尽量混合；同样一题一判。\n"
        "3. 答错的题用 lookup_word 补充词汇卡，并点出易混点。\n"
        "4. 最后调用 study_report(days=1) 做今日小结，指出薄弱题型，并按薄弱点用 add_question 出 3 道巩固题。"
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
