"""数字与格式规范化（问题 5，纯函数，仅标准库）。

对应 CODEX_字幕生成修改计划_20260713.md 「问题 5（P2）：数字与格式规范化」。

设计要点（务必保持，勿改为「先转换后用黑名单挑错」的反向机制）：

1. 中文数字 -> 阿拉伯数字，**主机制是「数字+白名单量词/单位」触发**：
   只有当一段中文数字字符串紧邻（无字符间隔）以下白名单量词/单位之一时，才把该数字
   字符串转换为阿拉伯数字；例如「十块钱」中的「十」因为紧跟白名单词「块」而被转换，
   「十分」「一起」「一下」「一直」「万一」因为后面不是白名单词，天然不会被触发。
   例外：数字串恰好为单独的「一」或「两」时不触发转换（一个/一条/下一个/两个/一只
   属于不定冠词/口语用法，人工字幕保留汉字）；「十」及多字数字（四十、三千、十一）
   照常转换。
2. 电话/编号式读法（「幺」「一」等单字数字连续出现 >= 3 个）是**独立的第二触发条件**，
   不依赖后面是否跟量词——「幺八幺八黄金眼」中「黄金眼」并非量词，但四个单字数字连续
   出现 4 次仍应转换为「1818」。该规则只处理纯个位数字符（不含十/百/千/万等位值字），
   避免与量词触发机制冲突。
3. 成语/习语黑名单（一模一样、十分、一起、一下、一直、万一）只作为**补充保险**：在两条
   触发规则跑之前，把命中的黑名单短语整体做不可逆占位符屏蔽，跑完规则后再还原成原文——
   即便未来触发规则出现意外的边界条件，这些短语也绝对不会被动到。黑名单不是数字转换的
   主判断逻辑，只是兜底。
4. ASCII 固定词大小写规范化（ai -> AI、iphone -> iPhone、ok -> OK）只处理**独立的 ASCII
   token**：要求 token 前后一个字符（如果存在）不是 ASCII 字母或数字。汉字、其他非 ASCII
   字符不会挡住/触发这个判断之外的副作用，因为判断只看「是否是 ASCII 字母数字」。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# 常量表
# ---------------------------------------------------------------------------

# 「数字+量词」主机制的白名单量词/单位。仅在中文数字串紧邻这些词之一时才转换。
QUANTIFIER_WHITELIST: Tuple[str, ...] = (
    "公斤",  # 放在「斤」之前，供排序时优先尝试更长的量词
    "块",
    "元",
    "钱",
    "年",
    "月",
    "日",
    "号",
    "个",
    "档",
    "次",
    "条",
    "只",
    "斤",
    "米",
    "%",
)

# 成语/习语黑名单：仅作补充保险，不是数字转换的主机制。
IDIOM_BLACKLIST: Tuple[str, ...] = (
    "一模一样",
    "十分之一",
    "十分",
    "一起",
    "一下",
    "一直",
    "万一",
)

# ASCII 固定词大小写表（小写 key -> 目标写法）。
FIXED_ASCII_WORDS: Dict[str, str] = {
    "ai": "AI",
    "iphone": "iPhone",
    "ok": "OK",
}

# 中文数字全字符集（含位值字十/百/千/万，供「数字+量词」主机制使用）。
_NUMERAL_CHARS = "零〇一二两三四五六七八九十百千万幺"

# 电话/编号式读法：只用个位数字符（不含位值字），且要求连续 >= 3 个才触发。
_PHONE_DIGIT_CHARS = "幺零〇一二三四五六七八九"
_PHONE_DIGIT_VALUE: Dict[str, int] = {
    "幺": 1,
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# 中文数字 -> 阿拉伯数字的位值换算表（供「数字+量词」主机制解析数字串）。
_DIGIT_VALUE: Dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "幺": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
# 量词前不转换的「不定冠词式」单字数字：中文里「一个/一条/下一个/两个/一只」中的
# 「一」「两」是不定冠词/口语用法，人工字幕全部保留汉字，量词触发机制必须放行。
_ARTICLE_LIKE_NUMERALS: Tuple[str, ...] = ("一", "两")

_UNIT_VALUE: Dict[str, int] = {
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10000,
}


def _build_quantifier_pattern() -> re.Pattern:
    alternatives = "|".join(
        re.escape(word) for word in sorted(QUANTIFIER_WHITELIST, key=len, reverse=True)
    )
    return re.compile(f"[{_NUMERAL_CHARS}]+(?=(?:{alternatives}))")


def _build_ascii_pattern() -> re.Pattern:
    alternatives = "|".join(
        re.escape(word) for word in sorted(FIXED_ASCII_WORDS, key=len, reverse=True)
    )
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


_PHONE_RUN_PATTERN = re.compile(f"[{_PHONE_DIGIT_CHARS}]{{3,}}")
_QUANTIFIER_TRIGGER_PATTERN = _build_quantifier_pattern()
_ASCII_TOKEN_PATTERN = _build_ascii_pattern()


# ---------------------------------------------------------------------------
# 中文数字串解析
# ---------------------------------------------------------------------------

def _parse_chinese_number(numeral_text: str) -> int | None:
    """把「十」「四十」「二十三」「一百二十」这类中文数字串解析为 int。

    解析失败（含未知字符/空串）时返回 None，调用方应保持原文不变。
    """

    if not numeral_text:
        return None

    total = 0
    section = 0
    current_digit = 0
    consumed_any = False

    for char in numeral_text:
        if char in _DIGIT_VALUE:
            current_digit = _DIGIT_VALUE[char]
            consumed_any = True
        elif char in _UNIT_VALUE:
            unit = _UNIT_VALUE[char]
            consumed_any = True
            if unit == 10000:
                section = (section + current_digit) * unit
                total += section
                section = 0
                current_digit = 0
            else:
                if current_digit == 0:
                    current_digit = 1
                section += current_digit * unit
                current_digit = 0
        else:
            return None

    if not consumed_any:
        return None

    total += section + current_digit
    return total


# ---------------------------------------------------------------------------
# 黑名单占位符屏蔽（补充保险，非主机制）
# ---------------------------------------------------------------------------

def _mask_blacklisted_idioms(text: str) -> Tuple[str, Dict[str, str]]:
    placeholder_map: Dict[str, str] = {}
    masked = text
    for idiom in sorted(set(IDIOM_BLACKLIST), key=len, reverse=True):
        if idiom and idiom in masked:
            token = chr(0xE000 + len(placeholder_map))
            masked = masked.replace(idiom, token)
            placeholder_map[token] = idiom
    return masked, placeholder_map


def _unmask_blacklisted_idioms(text: str, placeholder_map: Dict[str, str]) -> str:
    restored = text
    for token, idiom in placeholder_map.items():
        restored = restored.replace(token, idiom)
    return restored


# ---------------------------------------------------------------------------
# 替换回调
# ---------------------------------------------------------------------------

def _phone_run_replacement(match: "re.Match[str]") -> str:
    run = match.group(0)
    return "".join(str(_PHONE_DIGIT_VALUE[char]) for char in run)


def _quantifier_trigger_replacement(match: "re.Match[str]") -> str:
    run = match.group(0)
    # 数字串恰好为「一」或「两」时不触发量词转换（一个/一条/下一个/两个 原样保留）；
    # 「十」及多字数字（四十、三千、十一）照常转换。
    if run in _ARTICLE_LIKE_NUMERALS:
        return run
    value = _parse_chinese_number(run)
    if value is None:
        return run
    return str(value)


def _ascii_fixed_word_replacement(match: "re.Match[str]") -> str:
    return FIXED_ASCII_WORDS[match.group(0).lower()]


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def normalize_subtitle_text(text: Any) -> str:
    """规范化单行字幕文本：中文数字量词触发转换 + ASCII 固定词大小写。

    防御：``None`` 返回空串；非字符串输入会先转换为字符串再处理；空串原样返回。
    """

    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if text == "":
        return text

    working, placeholder_map = _mask_blacklisted_idioms(text)

    # 触发条件 1：电话/编号式读法，个位数字符连续 >= 3 个，不依赖后续量词。
    working = _PHONE_RUN_PATTERN.sub(_phone_run_replacement, working)

    # 触发条件 2（主机制）：数字串紧邻白名单量词/单位。
    working = _QUANTIFIER_TRIGGER_PATTERN.sub(_quantifier_trigger_replacement, working)

    # ASCII 固定词大小写规范化（独立 token，汉字不受影响）。
    working = _ASCII_TOKEN_PATTERN.sub(_ascii_fixed_word_replacement, working)

    working = _unmask_blacklisted_idioms(working, placeholder_map)
    return working


def normalize_subtitle_rows(
    rows: List[Dict[str, Any]] | None,
) -> Tuple[List[Dict[str, Any]] | None, Dict[str, int]]:
    """对逐行字幕的 ``text`` 字段应用 :func:`normalize_subtitle_text`。

    不修改帧字段（``start_frame``/``end_frame`` 等）及除 ``text`` 外的其他字段。
    没有 ``text`` 键的行（或非 dict 行）原样透传，不计入变更计数。
    """

    if not rows:
        return rows, {"textnorm_changed_row_count": 0}

    changed_count = 0
    normalized_rows: List[Dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict) or "text" not in row:
            normalized_rows.append(row)
            continue

        original_text = row.get("text")
        normalized_text = normalize_subtitle_text(original_text)

        new_row = dict(row)
        new_row["text"] = normalized_text
        normalized_rows.append(new_row)

        if isinstance(original_text, str) and normalized_text != original_text:
            changed_count += 1

    return normalized_rows, {"textnorm_changed_row_count": changed_count}
