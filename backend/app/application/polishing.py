"""智能分句：把文案整理成带标点的自然段。

平台字幕（尤其 AI 字幕）整段没有标点，条目边界也和语义无关，直接读很难用。
这里做三件事：

1. 合并逐字/滚动字幕的重复内容——B站等平台的逐字字幕会把同一句话反复推送；
2. 补标点：**只在正文还没有标点时**才调用本地标点模型。已经整理过的正文再送一遍
   模型，标点会叠在一起（「。。。」「，，」，段首还会冒出孤立的句号）；
3. 按句末标点切成句子，再按句数/字数组织成自然段。

整理结果只用来填进编辑框让用户过目，不直接改数据库里的分段——确认后再保存。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.application.text_formatting import group_paragraphs, join_paragraph, split_sentences

# 相邻条目至少要重叠这么多字，才认定是同一句的滚动残影
MIN_OVERLAP = 3

# 句末标点：判断正文是不是已经整理过
STRONG_PUNCT = "。！？…!?"
WEAK_PUNCT = "，,、；;：:"
ALL_PUNCT = STRONG_PUNCT + WEAK_PUNCT
# 平均每这么多字还没有一个句末标点，就认为这段正文没有标点、需要模型补
CHARS_PER_SENTENCE = 40

# 连续标点（「。。。」「，。」等）：折叠成一个
_PUNCT_RUN_RE = re.compile(f"[{re.escape(ALL_PUNCT)}]{{2,}}")


def looks_punctuated(text: str) -> bool:
    """正文是不是已经有标点了（整理过一次的正文不该再送模型）。"""

    if not text:
        return False
    strong = sum(1 for char in text if char in STRONG_PUNCT)
    return strong >= max(1, len(text) // CHARS_PER_SENTENCE)


def collapse_punctuation(text: str) -> str:
    """折叠重复或混用的标点：「谢谢。。大家」→「谢谢。大家」，「你好？？？」→「你好？」。

    优先保留一段连续标点里的句末标点（越靠后越可能是真正的收尾），
    没有句末标点时就保留第一个（如「，，」→「，」）。
    """

    def pick(run: str) -> str:
        strong = [char for char in run if char in STRONG_PUNCT]
        return strong[-1] if strong else run[0]

    collapsed = _PUNCT_RUN_RE.sub(lambda match: pick(match.group(0)), text)
    # 段落开头的孤立标点：切句残留的「。」「，」，直接丢掉
    return collapsed.strip().lstrip(ALL_PUNCT)


def _has_text(value: str) -> bool:
    """句子里至少要有字，不能只剩标点（折叠后可能留下孤立的标点碎片）。"""

    return bool(value.strip().strip(ALL_PUNCT).strip())


@dataclass(slots=True)
class PolishedTranscript:
    """一次整理的结果：正文 + 供界面说明用的统计。"""

    text: str
    sentences: int
    lines: int
    merged: int
    punctuated: bool


def merge_overlapping_lines(lines: Sequence[str]) -> tuple[list[str], int]:
    """合并逐字/滚动字幕：相邻条目重复的部分只保留一次。

    B站等平台的逐字字幕会把同一句话反复推送（每次多一两个字，或整屏刷新），
    直接拼接会得到「谢谢谢谢谢谢大家」。这里只做能确定判断的几种情况：

    - 与该条完全相同、或只是它的尾缀 → 丢弃；
    - 它以该条开头（整屏刷新）→ 用新的替换旧的；
    - 结尾与下一条开头有较长重叠 → 去掉重复部分再接上。

    判断不了的一律原样保留：宁可留下重复，也不猜着删内容。
    """

    merged: list[str] = []
    dropped = 0

    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        if not merged:
            merged.append(text)
            continue

        previous = merged[-1]
        if text == previous or previous.endswith(text):
            dropped += 1
            continue
        if text.startswith(previous):
            merged[-1] = text
            dropped += 1
            continue

        overlap = _tail_head_overlap(previous, text)
        if overlap >= MIN_OVERLAP:
            merged[-1] = previous + text[overlap:]
            dropped += 1
            continue

        merged.append(text)

    return merged, dropped


def _tail_head_overlap(left: str, right: str) -> int:
    """left 的结尾与 right 的开头最长重叠字数。"""

    limit = min(len(left), len(right))
    for size in range(limit, 0, -1):
        if left.endswith(right[:size]):
            return size
    return 0


def polish_lines(
    lines: Sequence[str],
    *,
    subtitle: bool,
    punctuate: Callable[[str], str] | None = None,
) -> PolishedTranscript:
    """把文案条目整理成自然段。

    subtitle 为真时先合并滚动重复，再用 punctuate 补标点（传 None 表示不补，
    例如标点模型尚未安装）。识别结果自带标点，只需要重新切成句子再成段。
    """

    merged_count = 0
    working = list(lines)
    if subtitle and punctuate is not None:
        # 补标点前先合并重复：把带重复的文本喂给标点模型，标点会加在重复内容上
        working, merged_count = merge_overlapping_lines(working)

    blocks = [text.strip() for text in working if text.strip()]
    joined = join_paragraph(blocks)

    punctuated = False
    if subtitle and punctuate is not None and joined and not looks_punctuated(joined):
        joined = punctuate(joined)
        punctuated = True

    # 正文里已有的标点也过一遍折叠：整理过的文案可能残留「。。。」「，。」
    joined = collapse_punctuation(joined)

    sentences = [sentence for sentence in split_sentences(joined) if _has_text(sentence)]
    if not sentences:
        return PolishedTranscript(
            text="",
            sentences=0,
            lines=len(blocks),
            merged=merged_count,
            punctuated=punctuated,
        )

    paragraphs = group_paragraphs([(None, None, sentence) for sentence in sentences])
    text = "\n\n".join(join_paragraph(block) for block in paragraphs if block)

    return PolishedTranscript(
        text=text,
        sentences=len(sentences),
        lines=len(blocks),
        merged=merged_count,
        punctuated=punctuated,
    )
