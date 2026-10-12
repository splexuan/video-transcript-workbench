"""中文数字规范化：把中文语境里的阿拉伯数字改写成汉字。

ZipVoice 用 espeak-ng 做文本转音素，而 espeak-ng 对阿拉伯数字一律走英文读法
（2026 会被读成 two thousand twenty-six）。模型包里没有中文数字规则文件
（不像 Kokoro 那样带 number-zh.fst），lexicon.txt 里也没有数字条目，
所以只能在文本层面先改写成汉字，交给 espeak-ng 按中文读。

要不要改写，看这个数字紧邻的是中文还是英文：
    3 个技巧  -> 三个技巧     数字挨着中文，用中文读法
    iPhone 15 -> 不动         产品名里的数字本来就该用英文读
    升级到 v2.0 -> 不动        版本号
    5G 网络  -> 不动          字母紧邻数字，读 five G 才对
这样纯英文句子与中英混排里该保留的部分都不会被误伤。
"""
from __future__ import annotations

import re

_DIGITS = "零一二三四五六七八九"
_SMALL_UNITS = ("", "十", "百", "千")
_BIG_UNITS = ("", "万", "亿", "万亿")
_GROUPED = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 中文语境判定：CJK 汉字、中文标点、全角字符
_CJK = re.compile("[\\u3400-\\u4dbf\\u4e00-\\u9fff\\u3000-\\u303f\\uff00-\\uffef]")


def _four_digits_to_cn(value: int) -> str:
    """把不足一万的整数按「千 百 十」读出来，中间补零。"""

    parts = []
    pending_zero = False
    for index in range(3, -1, -1):
        digit = (value // (10 ** index)) % 10
        if digit == 0:
            # 只有非零位之后出现的零才补：二千零二十六，而不是二十零零六
            if parts:
                pending_zero = True
            continue
        if pending_zero:
            parts.append(_DIGITS[0])
            pending_zero = False
        parts.append(_DIGITS[digit] + _SMALL_UNITS[index])
    return "".join(parts)


def _int_to_cn(value: int) -> str:
    """整数转汉字读法：1200 -> 一千二百，10007 -> 一万零七。"""

    if value == 0:
        return _DIGITS[0]
    groups = []
    while value:
        groups.append(value % 10000)
        value //= 10000

    parts = []
    pending_zero = False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            if parts:
                pending_zero = True
            continue
        if pending_zero:
            parts.append(_DIGITS[0])
            pending_zero = False
        parts.append(_four_digits_to_cn(group))
        parts.append(_BIG_UNITS[index])
    return "".join(parts)


def _number_to_cn(token: str) -> str:
    """一个数字串转汉字。小数按位读：3.5 -> 三点五。"""

    if _GROUPED.match(token):
        token = token.replace(",", "")
    if "." in token:
        whole, frac = token.split(".", 1)
        whole_text = _int_to_cn(int(whole)) if whole else _DIGITS[0]
        frac_text = "".join(_DIGITS[int(ch)] for ch in frac if ch.isdigit())
        return whole_text + "点" + frac_text
    return _int_to_cn(int(token))


def _year_to_cn(token: str) -> str:
    """年份逐位读：2026 -> 二零二六（口语习惯，比「二千零二十六」自然）。"""

    return "".join(_DIGITS[int(ch)] for ch in token.replace(",", ""))


def _is_latin(char: str) -> bool:
    """数字是否紧贴着一个英文字母——那多半是型号 / 产品名 / 单位，不该改写。"""

    return bool(char) and char.isascii() and char.isalpha()


def _touches_latin(text: str, start: int, end: int) -> bool:
    """数字左右（跨过空格）是否连着英文字母。

    必须跨空格看：产品名和数字之间通常有空格（「iPhone 15」），只看紧邻字符的话
    空格会把两边隔开，数字就被当成中文语境里的「十五」了。
    """

    index = start - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    if index >= 0 and _is_latin(text[index]):
        return True
    index = end
    while index < len(text) and text[index] in " \t":
        index += 1
    return index < len(text) and _is_latin(text[index])


def normalize_chinese_numbers(text: str) -> str:
    """把中文语境里的阿拉伯数字改写成汉字；英文语境保持原样。"""

    # 整段里一个汉字都没有，就是纯外文内容，数字按英文读才是对的，直接放行
    if not _CJK.search(text):
        return text

    out = []
    cursor = 0
    for match in _NUMBER.finditer(text):
        start, end = match.span()
        token = match.group()
        out.append(text[cursor:start])
        cursor = end

        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""

        # 连着英文单词/型号的保留：iPhone 15 / v2.0 / 5G
        if _touches_latin(text, start, end):
            out.append(token)
            continue

        if after == "%":
            # 百分号由这里吃掉，否则会变成「百分之三十%」
            out.append("百分之" + _number_to_cn(token))
            cursor = end + 1
            continue

        # 范围表达式（2020-2025）：两种读法会混在一起（一边逐位一边按值），很难听。
        # 这里整体放行交给 espeak-ng，宁可读英文数字也不要念成拼接的怪音。
        # 前半段看右边、后半段看左边，两段都要放行，否则只会保住一半。
        if after == "-" and end + 1 < len(text) and text[end + 1].isdigit():
            out.append(token)
            continue
        if start >= 2 and text[start - 1] == "-" and text[start - 2].isdigit():
            out.append(token)
            continue

        # 带「万 / 亿」的小数：1.2万 是一万二千，不是「一点二万」。单位一并吃掉，
        # 否则原文里的「万」会跟上来变成「一万二千万」。
        if after in ("万", "亿") and "." in token:
            scale = 10_000 if after == "万" else 100_000_000
            value = int(round(float(token.replace(",", "")) * scale))
            out.append(_int_to_cn(value))
            cursor = end + 1
            continue

        # 年份逐位读：后面紧跟「年」才这么处理，否则「2026 个」会读成两千零二十六个。
        # 这里只换数字本身，原文里的「年」留着（多写一个会出现「二零二六年年」）
        if after == "年" and len(token.replace(",", "")) == 4:
            out.append(_year_to_cn(token))
            continue

        out.append(_number_to_cn(token))
    out.append(text[cursor:])
    return "".join(out)