"""文案自动整理：切句、时间轴细分与段落分组。

规则式处理（不用任何网络服务）：
- 切句：按中英标点把长文本切成一句一条，适合逐句校对；
- 时间轴：句内时间按字数比例分配，仅用于快速定位，不代表真实停顿；
- 分段：按「停顿间隙 + 句数/字数上限」把句子组织成可读段落，用于全文导出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 段落分组参数：超过任一阈值就另起一段
PARAGRAPH_GAP_MS = 1_600
PARAGRAPH_MAX_SENTENCES = 5
PARAGRAPH_MAX_CHARS = 180

# 说明：曾经尝试过「按停顿给字幕补标点」，实测后放弃了。
# B站 AI 字幕整段没有标点，而且时间戳首尾相连（上一条的结束就是下一条的开始），
# 停顿间隔恒为 0，条目边界也和语义无关（可能切在词中间）。规则补标点必然出现
# 「逗号出现在随机位置」的结果，得不偿失。现在字幕按原样一行一句展示，
# 需要标点就用识别（识别结果自带标点）。

# 无强标点的超长文本，按逗号兜底切分的目标长度
CLAUSE_FALLBACK_LENGTH = 120

# 强终止标点：出现在句尾即成句
_STRONG_END = "。！？!?…"
# 英文句号仅在「后面是空白或文本结束」时视作句尾，避免切碎小数、缩写
_ENGLISH_DOT_RE = re.compile(r"\.(?=\s|$)")
# 引号/括号闭合字符跟随终止标点时一并保留在句尾（含直引号）
_CLOSERS = "」』”）)]】>\"'"


def split_sentences(text: str) -> list[str]:
    """把一段文本切成句子列表；无标点的短文本原样返回。"""

    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []

    # 统一把英文句号按上下文标记为可切点
    normalized = _ENGLISH_DOT_RE.sub("\u0001", cleaned)
    sentences: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            piece = "".join(buffer).strip()
            if piece:
                sentences.append(piece)
            buffer.clear()

    index = 0
    characters = list(normalized)
    while index < len(characters):
        character = characters[index]
        buffer.append(character)
        if character in _STRONG_END or character == "\u0001":
            # 终止标点后紧跟的引号/括号闭合并入本句
            while index + 1 < len(characters) and characters[index + 1] in _CLOSERS:
                index += 1
                buffer.append(characters[index])
            flush()
        index += 1
    flush()

    if not sentences:
        return [cleaned]

    # 超长无标点文本（识别引擎偶发漏标点）按逗号兜底
    expanded: list[str] = []
    for sentence in sentences:
        expanded.extend(_split_long_clause(sentence))
    return expanded


def _split_long_clause(sentence: str) -> list[str]:
    if len(sentence) <= CLAUSE_FALLBACK_LENGTH:
        return [sentence]
    # 先按逗号/顿号等次级标点切；整段连标点都没有时按固定长度硬切，
    # 保证再长的无标点文本也会被拆成可校对的句子。
    pieces: list[str] = []
    buffer: list[str] = ""
    for character in sentence:
        buffer += character
        if character in "，,、；;" and len(buffer) >= CLAUSE_FALLBACK_LENGTH // 2:
            pieces.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        pieces.append(buffer.strip())

    chunks: list[str] = []
    for piece in pieces:
        if len(piece) <= CLAUSE_FALLBACK_LENGTH:
            chunks.append(piece)
            continue
        step = CLAUSE_FALLBACK_LENGTH // 2
        chunks.extend(piece[index : index + step] for index in range(0, len(piece), step))
    return chunks or [sentence]


@dataclass
class SentenceChunk:
    """切句后的句子及其按比例细分的时间轴。"""

    start_ms: int | None
    end_ms: int | None
    text: str


def reflow_segments(
    segments: list[tuple[int | None, int | None, str]],
) -> list[SentenceChunk]:
    """把分段文本重排为句级列表。

    每个输入元素为 (start_ms, end_ms, text)；句子时间按字数比例在原
    分段的时间范围内分配。没有时间信息的分段只做切句，时间为空。
    """

    chunks: list[SentenceChunk] = []
    for start_ms, end_ms, text in segments:
        sentences = split_sentences(text)
        if not sentences:
            continue
        has_time = start_ms is not None and end_ms is not None and end_ms >= start_ms
        if not has_time or len(sentences) == 1:
            for sentence in sentences:
                chunks.append(SentenceChunk(start_ms=start_ms, end_ms=end_ms, text=sentence))
            continue

        span = (end_ms or 0) - (start_ms or 0)
        weights = [max(len(sentence), 1) for sentence in sentences]
        total_weight = sum(weights)
        cursor = start_ms or 0
        for order, (sentence, weight) in enumerate(zip(sentences, weights, strict=True)):
            if order == len(sentences) - 1:
                sentence_end = end_ms
            else:
                sentence_end = cursor + int(span * weight / total_weight)
            chunks.append(SentenceChunk(start_ms=cursor, end_ms=sentence_end, text=sentence))
            cursor = sentence_end or cursor
    return chunks


def group_paragraphs(
    segments: list[tuple[int | None, int | None, str]],
) -> list[list[str]]:
    """把句级/段级文本组织成段落，返回每段的句子文本列表。

    另起一段的条件（任一满足）：
    - 与上一句之间的时间间隙超过 PARAGRAPH_GAP_MS；
    - 当前段落句数达到上限；
    - 当前段落字数达到上限。
    """

    paragraphs: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    previous_end: int | None = None

    for start_ms, end_ms, text in segments:
        sentence = text.strip()
        if not sentence:
            continue
        new_paragraph = bool(current) and (
            (
                previous_end is not None
                and start_ms is not None
                and start_ms - previous_end >= PARAGRAPH_GAP_MS
            )
            or len(current) >= PARAGRAPH_MAX_SENTENCES
            or current_chars >= PARAGRAPH_MAX_CHARS
        )
        if new_paragraph:
            paragraphs.append(current)
            current = []
            current_chars = 0
        current.append(sentence)
        current_chars += len(sentence)
        if end_ms is not None:
            previous_end = end_ms
    if current:
        paragraphs.append(current)
    return paragraphs


def lines_as_text(segments: list[tuple[int | None, int | None, str]]) -> str:
    """每条一行，段间不留空行。

    平台字幕没有标点，条目边界也和语义无关，硬合并成段落只会更难读；
    保持「一行一句」反而接近字幕原本的形态。
    """

    return "\n".join(text.strip() for _, _, text in segments if text.strip())


def _needs_space(left: str, right: str) -> bool:
    """拼接处是否需要补空格：中文之间不补，英文/数字相邻时补，避免单词粘连。"""

    if not left or not right:
        return False
    return left.isascii() and left.isalnum() and right.isascii() and right.isalnum()


def join_paragraph(sentences: list[str]) -> str:
    """把一个段落的句子连成文本。

    中文片段之间**直接相接**：字幕（尤其 AI 字幕）整段往往没有标点，若按
    「无标点就补空格」处理，连续的语音会被切成「一行一行」的碎片。只有拼接处
    两侧都是英文或数字时才补空格。
    """

    joined = ""
    for sentence in sentences:
        if joined and _needs_space(joined[-1], sentence[:1]):
            joined += " "
        joined += sentence
    return joined


def paragraphs_as_text(segments: list[tuple[int | None, int | None, str]]) -> str:
    """直接产出可读的段落化全文：段内成句，段落之间空一行。"""

    blocks: list[str] = []
    for paragraph in group_paragraphs(segments):
        joined = join_paragraph(paragraph).strip()
        if not joined:
            continue
        # 字幕常整段没有标点，段末补一个句号，读起来才像一个完整的段落
        if joined[-1] not in _STRONG_END:
            joined += "。"
        blocks.append(joined)
    return "\n\n".join(blocks)
