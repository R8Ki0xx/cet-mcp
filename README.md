# cet-mcp · 四六级备考题库 / 错题本 MCP

让 AI 助手在对话里给你出四六级题、判题讲解、自动记录错题，并用 **SM-2 间隔重复**（Anki 同款算法）安排每天该复习的题。数据全部存在本地 SQLite。

## 题库内容（构建后约 3.1 万题）

| 题型 | CET-4 | CET-6 | 说明 |
|---|---:|---:|---|
| 真题词汇选择 `vocab_real` | 267 | 317 | 历年真题词汇题，4 选 1，附中文解析 |
| 真题语境填空 `context_cloze` | 2,698 | 1,882 | 取自真题阅读/听力原句挖空，标注年份、卷次、题型 |
| 英译中 `en2cn` | 4,544 | 3,992 | 干扰项取自同级别同词性单词 |
| 中译英 `cn2en` | 4,536 | 3,987 | 同上，并排除同义词以免出现多个正确答案 |
| 拼写 `spelling` | 4,521 | 3,992 | 给出释义、音标、首字母和字母数 |
| 语法/短语 `grammar` | 10 | 10 | 原创，附解析 |
| 段落翻译 `translation` | 4 | 4 | 原创，附参考译文和评分要点 |
| 写作 `writing` | 3 | 3 | 原创，附范文提纲和评分维度 |

另有大纲词汇 **CET-4 4,544 词 / CET-6 3,992 词**的完整单词卡：英美音标、中英释义、短语、例句、同近义词、同根词、记忆法、真题原句出处。

**数据来源**：[KyleBing/english-vocabulary](https://github.com/KyleBing/english-vocabulary)（BSD-3-Clause），构建时下载到本地 `data/raw/`，不随项目分发。完整的阅读和听力真题篇章受版权保护，没有内置。你可以用 `import_questions` 导入自己整理的真题，也可以让 AI 用 `add_question` 出题入库。

## 安装

需要 [uv](https://docs.astral.sh/uv/)。

```bash
cd cet-mcp
uv sync
uv run cet-mcp-setup
```

`cet-mcp-setup` 会下载约 30MB 词库并构建题库，大约需要 30 秒。也可以在对话里让 AI 调用 `setup_question_bank` 完成同样的事。

### 接入 AI 客户端

本项目是标准的 MCP（stdio）服务器，Claude、Cursor、VS Code Copilot、Cherry Studio、Cline 等支持 MCP 的客户端都能使用。大多数客户端的配置格式如下（把路径换成你本机的 `cet-mcp` 目录）：

```json
{
  "mcpServers": {
    "cet": {
      "command": "uv",
      "args": ["--directory", "/path/to/cet-mcp", "run", "cet-mcp"]
    }
  }
}
```

常见客户端的配置位置：

| 客户端 | 配置方式 |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json`（macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`） |
| Claude Code | `claude mcp add cet --scope user -- uv --directory /path/to/cet-mcp run cet-mcp` |
| Cursor | `~/.cursor/mcp.json` |
| VS Code | 项目内 `.vscode/mcp.json`，顶层键名是 `servers` 而不是 `mcpServers` |
| Cherry Studio 等图形界面客户端 | 在 MCP 设置里新建 stdio 服务器，命令填 `uv`，参数填 `--directory /path/to/cet-mcp run cet-mcp` |

Windows 路径在 JSON 里要写成双反斜杠，例如 `"C:\\Users\\you\\cet-mcp"`。

## 怎么用（直接对 AI 说）

- “开始今天的四级练习”，或者使用提示词 `daily_session`：先复习到期的题，再做新题，最后给出小结
- “来 10 道六级真题词汇题”
- “出 5 道拼写题，只要我没做过的”
- “我的错题本里错得最多的是哪些？拿出来不给答案让我重做一遍”
- “查一下 abandon”
- “给我一道四级翻译”：作答后 AI 会按评分要点打 0–5 分并记录
- “我老错虚拟语气，按真题风格出 5 道题存进题库”
- “看看这周的学习报告”
- “把错题本导出成 Markdown”

## 工具一览

| 工具 | 作用 |
|---|---|
| `setup_question_bank` | 下载词库并构建题库，可以重复执行 |
| `bank_overview` | 查看题量、进度、正确率、今日待复习数和错题数 |
| `practice` | 按级别、题型、模式（new / any / wrong / due）和标签抽题，返回结果不含答案 |
| `submit_answer` | 判题，返回解析和单词卡，同时更新 SM-2 排期和错题本 |
| `grade_subjective` | 记录翻译或写作的 0–5 分及评语 |
| `get_due_reviews` | 今天到期的复习题，错题优先 |
| `wrongbook` / `wrongbook_update` | 查看错题本；手动加入、移出、标记掌握或写笔记 |
| `lookup_word` | 查看完整单词卡 |
| `add_question` / `import_questions` | 出题入库；从 JSON 或 CSV 批量导入 |
| `study_report` | 近 N 天的正确率、薄弱题型、最常错的单词和连续打卡天数 |
| `export_wrongbook` | 把错题本导出为 Markdown |

## 复习规则（SM-2）

- 客观题答对记 4 分，答错记 1 分；主观题由 AI 打 0–5 分
- 低于 3 分：进入错题本，第二天复习，难度系数下调
- 3 分及以上：间隔依次为 1 天 → 6 天 → 上次间隔 × 难度系数（难度系数最低 1.3）
- 错题自上次答错后连续答对 3 次，自动移出错题本并标记为“已掌握”，之后仍按排期偶尔复习

## 导入自己的题目

**JSON**：可以是题目数组，也可以写成 `{"questions": [...]}`

```json
[
  {
    "level": "cet4",
    "type": "reading",
    "material": "（文章全文）",
    "stem": "What is the main idea of the passage?",
    "choices": ["...", "...", "...", "..."],
    "answer": "C",
    "explanation": "（解析）",
    "tags": ["2023.6", "仔细阅读"],
    "source": "2023.6 CET4 第一套"
  }
]
```

**CSV**（UTF-8 编码，Excel 里选“CSV UTF-8”另存）：

```
level,type,stem,choice_a,choice_b,choice_c,choice_d,answer,explanation,material,tags,source
```

字段说明：

- `level`：`cet4` 或 `cet6`
- `type`：`vocab_real` `context_cloze` `en2cn` `cn2en` `spelling` `grammar` `reading` `cloze` `listening` `translation` `writing` `custom` 之一
- `answer`：选择题填字母或选项原文；主观题填参考答案
- 重复的题目会自动跳过

## 开发

```bash
uv run pytest
```

- 数据库默认位于 `data/cet.db`，可用环境变量 `CET_MCP_DB` 指定其他路径
- 代码结构：`srs.py` 实现 SM-2；`db.py` 定义表结构；`vocab_source.py` 负责下载和解析词库；`generators.py` 自动出题；`importer.py` 负责导入；`service.py` 是业务逻辑；`server.py` 包装成 MCP 工具
- 依赖锁定在 `mcp<2`：MCP Python SDK 2.x 把 `FastMCP` 改名为 `MCPServer`，接口不兼容

## 许可证

本项目代码采用 [Apache License 2.0](LICENSE)。词库数据来自 [KyleBing/english-vocabulary](https://github.com/KyleBing/english-vocabulary)（BSD-3-Clause），构建题库时下载到本地，本仓库不包含这些数据。
