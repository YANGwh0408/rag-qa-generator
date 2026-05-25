"""领导可配置的生成维度：数字 → 自然语言说明，注入 system / user 侧提示。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DimensionParams:
    qa_type: int = 1
    difficulty: int = 1
    answer_length: int = 2
    language_style: int = 1
    qa_count_per_chunk: int = 0
    coverage_strategy: int = 1


QA_TYPE_DESC = {
    1: "标准问答（RAG 常用）：如「XXX 是什么？」→「XXX 是……」",
    2: "详细解释型：如「请详细说明 XXX 的流程？」→ 分步骤说明",
    3: "操作指导型：如「如何设置 XXX？」→ 第一步…第二步…",
    4: "FAQ 常见问题型：如「XXX 报错怎么办？」→ 排查与处理",
    5: "考试题型：单选 / 多选 / 判断；在 answer 中写清正确选项与简要解析",
}

DIFFICULTY_DESC = {
    1: "基础级：概念、定义、「是什么」",
    2: "进阶级：原理、原因、对比",
    3: "高级：场景应用、异常分析、组合推理",
}

ANSWER_LENGTH_DESC = {
    1: "答案极简：约 10–30 字",
    2: "答案标准：约 30–100 字",
    3: "答案详细：约 100–300 字",
    4: "答案超详细：300 字以上，可含步骤或小例",
}

LANGUAGE_STYLE_DESC = {
    1: "正式书面（手册 / 制度 / 技术文档语气）",
    2: "通俗易懂（科普、面向普通用户）",
    3: "专业严谨（面向工程师 / 技术读者）",
    4: "简洁干练（只列要点，少铺垫）",
}

COVERAGE_DESC = {
    1: "选题优先覆盖核心知识点",
    2: "全覆盖：尽量覆盖片段内各类信息点",
    3: "重点抓可操作步骤与流程",
    4: "重点抓易错点、注意事项与边界条件",
}


def qa_count_instruction(qa_count_per_chunk: int) -> tuple[int, str]:
    """
    返回 (建议的 pairs 数字用于模板占位, 给模型的补充说明)。
    qa_count_per_chunk: 0=自动, 1–3 对应条数, 4 对应 4–5 条。
    """
    if qa_count_per_chunk == 0:
        return (
            5,
            "问答条数：请根据片段信息密度自行决定，建议 1～8 条，信息少则少生成，勿灌水重复。",
        )
    if qa_count_per_chunk == 1:
        return 1, "问答条数：恰好 1 对。"
    if qa_count_per_chunk == 2:
        return 2, "问答条数：恰好 2 对。"
    if qa_count_per_chunk == 3:
        return 3, "问答条数：恰好 3 对。"
    return 5, "问答条数：恰好 4～5 对，互不重复、覆盖不同信息点。"


def build_dimension_system_addon(d: DimensionParams) -> str:
    """由 DimensionParams 拼出「qa_type / difficulty …」说明块。

    主站流水线已不再注入 User 模板；保留本函数供自定义脚本或将来扩展引用。
    """
    qt = QA_TYPE_DESC.get(d.qa_type, QA_TYPE_DESC[1])
    diff = DIFFICULTY_DESC.get(d.difficulty, DIFFICULTY_DESC[1])
    alen = ANSWER_LENGTH_DESC.get(d.answer_length, ANSWER_LENGTH_DESC[2])
    lang = LANGUAGE_STYLE_DESC.get(d.language_style, LANGUAGE_STYLE_DESC[1])
    cov = COVERAGE_DESC.get(d.coverage_strategy, COVERAGE_DESC[1])
    _, cnt_line = qa_count_instruction(d.qa_count_per_chunk)
    lines = [
        "【本次生成维度（请严格遵守）】",
        f"- 生成类型 qa_type={d.qa_type}：{qt}",
        f"- 问题难度 difficulty={d.difficulty}：{diff}",
        f"- 答案长度 answer_length={d.answer_length}：{alen}",
        f"- 语言风格 language_style={d.language_style}：{lang}",
        f"- 内容覆盖 coverage_strategy={d.coverage_strategy}：{cov}",
        f"- {cnt_line}",
    ]
    if d.qa_type == 5:
        lines.append(
            "- 考试题型时：每条用 question 写题干（选项可写在题干内）；"
            "answer 写正确答案（如「B」或「对/错」）并给一句依据片段的解析。"
        )
    return "\n".join(lines)
