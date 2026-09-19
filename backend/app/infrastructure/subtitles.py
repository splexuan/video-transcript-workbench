from __future__ import annotations

import html
import re
from pathlib import Path

from app.domain import TranscriptChunk

TIMESTAMP_RE = re.compile(
    r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?"
)
TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]+\}")


def parse_timestamp(value: str) -> int:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        hours, minutes, seconds = parts
    seconds_value = float(seconds)
    return int((int(hours) * 3600 + int(minutes) * 60 + seconds_value) * 1000)


def clean_caption(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    text = html.unescape(TAG_RE.sub("", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_subtitle(path: Path) -> list[TranscriptChunk]:
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = content.replace("\r\n", "\n").split("\n")
    chunks: list[TranscriptChunk] = []
    index = 0

    while index < len(lines):
        match = TIMESTAMP_RE.search(lines[index])
        if not match:
            index += 1
            continue

        caption_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            caption_lines.append(lines[index])
            index += 1

        text = clean_caption(caption_lines)
        if text and (not chunks or chunks[-1].text != text):
            chunks.append(
                TranscriptChunk(
                    start_ms=parse_timestamp(match.group("start")),
                    end_ms=parse_timestamp(match.group("end")),
                    text=text,
                )
            )
        index += 1

    return chunks

