from __future__ import annotations

import json

from app.application.text_formatting import (
    group_paragraphs,
    join_paragraph,
    lines_as_text,
    paragraphs_as_text,
)
from app.infrastructure.models import Document


def timestamp(milliseconds: int | None, separator: str = ",") -> str:
    total = max(0, milliseconds or 0)
    hours, remain = divmod(total, 3_600_000)
    minutes, remain = divmod(remain, 60_000)
    seconds, millis = divmod(remain, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _segment_tuples(document: Document) -> list[tuple[int | None, int | None, str]]:
    segments = sorted(document.segments, key=lambda item: item.position)
    return [(segment.start_ms, segment.end_ms, segment.text) for segment in segments]


def export_document(
    document: Document,
    file_format: str,
    *,
    line_by_line: bool = False,
) -> tuple[str, str, str]:
    """导出文档。

    line_by_line 用于平台字幕来源：字幕没有标点、条目边界也和语义无关，
    合并成段落只会更难读，保持「一行一句」更接近字幕原样。
    """

    segments = sorted(document.segments, key=lambda item: item.position)
    safe_format = file_format.lower()

    if safe_format == "txt":
        blocks = _segment_tuples(document)
        if line_by_line:
            content = lines_as_text(blocks)
        else:
            # 识别结果自带标点，组织成可读段落（段内成句，段落之间空行）
            content = paragraphs_as_text(blocks)
        return content, "text/plain; charset=utf-8", "txt"

    if safe_format == "srt":
        blocks = [
            f"{index}\n{timestamp(segment.start_ms)} --> {timestamp(segment.end_ms)}\n{segment.text.strip()}"
            for index, segment in enumerate(segments, start=1)
            if segment.text.strip()
        ]
        return "\n\n".join(blocks) + "\n", "application/x-subrip; charset=utf-8", "srt"

    if safe_format == "vtt":
        blocks = [
            f"{timestamp(segment.start_ms, '.')} --> {timestamp(segment.end_ms, '.')}\n{segment.text.strip()}"
            for segment in segments
            if segment.text.strip()
        ]
        return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n", "text/vtt; charset=utf-8", "vtt"

    if safe_format == "json":
        payload = {
            "id": document.id,
            "title": document.title,
            "platform": document.platform,
            "source": document.source_value,
            "duration_seconds": document.duration_seconds,
            "paragraphs": (
                [text.strip() for _, _, text in _segment_tuples(document) if text.strip()]
                if line_by_line
                else [
                    join_paragraph(block)
                    for block in group_paragraphs(_segment_tuples(document))
                ]
            ),
            "segments": [
                {
                    "position": segment.position,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "text": segment.text,
                    "raw_text": segment.raw_text,
                }
                for segment in segments
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8", "json"

    raise ValueError("不支持的导出格式")
