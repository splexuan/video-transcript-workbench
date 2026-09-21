"""列表接口的游标分页。

任务、批次、文案三个列表都是倒序的流水，用户翻页时后台还在往里写新行。
用 offset 翻页会因为这个插入而重复或漏项（新行把整体往后挤，第 2 页会重复第 1 页
的最后一条），所以游标里带上「上一页最后一条的排序键」（排序键 + id），下一页从它
之后继续取——插入多少新行都不影响已经翻过的位置。

排序键不限于时间：文案列表可以按字数排，所以游标里存的是排序键的字符串形式，
解码时再按列的类型转回去比较。

游标对外是不透明字符串（base64url），调用方只管把它原样回传。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

# 单页条数上限：和前端一轮渲染的量对齐，别让一个请求把整库拖回来。
MAX_PAGE_SIZE = 200
# 各列表的默认条数由调用方指定，这里只兜一个通用默认
DEFAULT_PAGE_SIZE = 20


class InvalidCursor(ValueError):
    """游标不是本服务发出的（被改坏，或来自另一个版本）。调用方应转成 400。"""


@dataclass(frozen=True, slots=True)
class Cursor:
    """上一页最后一条的排序键。"""

    # 排序键的字符串形式，解码时按列的类型转回真实类型（见 _key_value）
    value: str
    row_id: str


def _key_text(value: Any) -> str:
    """排序键 → 游标里的字符串。

    类型标在值前面（d/f/i）：列表能按时间或字数排，解码时得知道该转回哪种类型。
    不靠列去判断类型——时间列是自定义的 TypeDecorator 包着 DateTime，按列判断
    容易踩空（`isinstance(列类型, DateTime)` 与 `列类型.python_type` 都不成立）。
    """

    if isinstance(value, datetime):
        return f"d:{value.isoformat()}"
    if isinstance(value, float):
        return f"f:{value}"
    return f"i:{value}"


def _key_value(raw: str) -> Any:
    """游标里的字符串 → 排序键的真实类型，否则没法拿去和列比较。"""

    kind, separator, text = raw.partition(":")
    if not separator:
        raise InvalidCursor("分页游标无效，请刷新后重试")
    if kind == "d":
        return datetime.fromisoformat(text)
    if kind == "f":
        return float(text)
    return int(text)


def encode_cursor(value: Any, row_id: str) -> str:
    raw = f"{_key_text(value)}|{row_id}".encode()
    # 去掉补位符：查询串里 `=` 会被转义，去掉更干净，解码时再补回来
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> Cursor:
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("分页游标无效，请刷新后重试") from exc

    key, separator, row_id = raw.partition("|")
    if not separator or not row_id or not key:
        raise InvalidCursor("分页游标无效，请刷新后重试")
    return Cursor(key, row_id)


def fetch_page(
    session: Session,
    statement: Any,
    sort_column: Any,
    id_column: Any,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[Any], str | None]:
    """按 (排序键, id) 倒序取一页，并算出下一页游标；已经到底时游标为 None。

    id 也参与排序是为了让「排序键相同的多行」有稳定顺序——只按字数排，
    字数相同的两行谁在前是不确定的，翻页就可能重复或漏掉。
    """

    if cursor:
        mark = decode_cursor(cursor)
        try:
            mark_value = _key_value(mark.value)
        except ValueError as exc:
            raise InvalidCursor("分页游标无效，请刷新后重试") from exc
        statement = statement.where(
            or_(
                sort_column < mark_value,
                and_(sort_column == mark_value, id_column < mark.row_id),
            )
        )
    # 多取一条用来判断「还有没有下一页」，比再查一次 count 便宜
    statement = statement.order_by(
        sort_column.desc(),
        id_column.desc(),
    ).limit(limit + 1)

    rows = list(session.scalars(statement))
    if len(rows) <= limit:
        return rows, None

    page = rows[:limit]
    last = page[-1]
    return page, encode_cursor(
        getattr(last, sort_column.key),
        getattr(last, id_column.key),
    )
