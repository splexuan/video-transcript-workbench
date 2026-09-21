from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Sequence

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


def documents_as_zip(
    documents: Sequence[Document],
    *,
    subtitle_ids: set[str],
    file_format: str,
) -> bytes:
    """把多篇文案打成一个 zip：每篇一个文件，内容与单篇导出逐字节一致。

    单篇导出怎么写（含 txt / srt 的 BOM），这里就怎么写——两条路径必须同源，
    否则同一篇文案「单独导出」和「批量导出」的结果会不一样。
    """

    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for document in documents:
            content, _, extension = export_document(
                document, file_format, line_by_line=document.id in subtitle_ids
            )
            data = (
                content.encode("utf-8-sig")
                if extension in {"txt", "srt"}
                else content.encode("utf-8")
            )
            bundle.writestr(_entry_name(document.title, extension, used), data)
    return buffer.getvalue()


def _entry_name(title: str, extension: str, used: set[str]) -> str:
    """压缩包里的文件名：非法字符换成下划线，重名补序号。

    标题是用户自己改的，可能带 `/` 或 `:`（Windows 上不合法），也可能两篇同名；
    截断到 80 字是为了别撞上路径长度限制。
    """

    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip()[:80] or "未命名文案"
    name = f"{safe}.{extension}"
    index = 2
    while name in used:
        name = f"{safe} ({index}).{extension}"
        index += 1
    used.add(name)
    return name
