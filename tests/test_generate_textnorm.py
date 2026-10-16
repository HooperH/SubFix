"""问题 5（数字与格式规范化）单测：subfix_generate_textnorm.py。

覆盖点：
- 「数字+白名单量词」主机制正例（十块钱、四十块钱、十一月）。
- 不定冠词式「一」「两」在量词前保留汉字（一个/一条/下一个/两个/一只）。
- 电话/编号式读法（幺/一等单字数字连续 >= 3 个，不依赖后续量词）。
- 黑名单兜底反例：一模一样、十分（感谢）、一起（去）、一下（想了一下）、一直、万一。
- ASCII 固定词大小写（ai/iphone/ok），仅独立 token，汉字不受影响，纯英文句不误伤。
- 空串 / None 防御。
- normalize_subtitle_rows：只改 text 字段，不改帧字段，diagnostic 计数正确。
"""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "subfix_generate_textnorm.py"


def load_textnorm():
    spec = importlib.util.spec_from_file_location("subfix_generate_textnorm", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


textnorm = load_textnorm()


# ---------------------------------------------------------------------------
# 正例：数字 + 白名单量词触发
# ---------------------------------------------------------------------------

def test_single_digit_with_whitelist_quantifier_converts():
    assert textnorm.normalize_subtitle_text("十块钱") == "10块钱"


def test_two_char_numeral_with_whitelist_quantifier_converts():
    assert textnorm.normalize_subtitle_text("四十块钱") == "40块钱"


def test_numeral_before_percent_converts():
    assert textnorm.normalize_subtitle_text("五十%") == "50%"


def test_numeral_before_non_whitelist_word_does_not_convert():
    # 「分」不在白名单量词表内，紧邻的数字不应被转换。
    assert textnorm.normalize_subtitle_text("十分感谢") == "十分感谢"


# ---------------------------------------------------------------------------
# 不定冠词式「一」「两」在量词前保留汉字；「十」及多字数字照常转换
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "一个",
        "一条",
        "下一个",
        "两个",
        "一只",
    ],
)
def test_article_like_yi_and_liang_before_quantifier_are_preserved(text):
    assert textnorm.normalize_subtitle_text(text) == text


def test_standalone_shi_before_quantifier_still_converts():
    assert textnorm.normalize_subtitle_text("十块钱") == "10块钱"


def test_multi_char_numeral_shiyi_before_quantifier_still_converts():
    assert textnorm.normalize_subtitle_text("十一月") == "11月"


# ---------------------------------------------------------------------------
# 正例：电话/编号式读法（连续 >= 3 个单字数字，独立触发条件）
# ---------------------------------------------------------------------------

def test_phone_style_digit_run_converts_without_quantifier():
    assert textnorm.normalize_subtitle_text("幺八幺八黄金眼") == "1818黄金眼"


def test_short_digit_run_below_threshold_does_not_trigger_phone_style():
    # 只有两个单字数字且后面不是白名单量词，不满足任何触发条件。
    assert textnorm.normalize_subtitle_text("八九不离十") == "八九不离十"


# ---------------------------------------------------------------------------
# 反例：成语/习语黑名单兜底
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "一模一样",
        "十分感谢",
        "一起去",
        "想了一下",
        "一直在等",
        "万一失败了",
    ],
)
def test_idiom_blacklist_examples_are_not_converted(text):
    assert textnorm.normalize_subtitle_text(text) == text


# ---------------------------------------------------------------------------
# ASCII 固定词大小写
# ---------------------------------------------------------------------------

def test_ai_lowercase_between_chinese_chars_converts_to_uppercase():
    assert textnorm.normalize_subtitle_text("这是ai产品") == "这是AI产品"


def test_iphone_standalone_converts_to_camel_case():
    assert textnorm.normalize_subtitle_text("iphone") == "iPhone"


def test_ok_standalone_converts_to_uppercase():
    assert textnorm.normalize_subtitle_text("好的ok没问题") == "好的OK没问题"


def test_ai_inside_english_word_is_not_mistakenly_converted():
    # "said" 中含有子串 "ai"，但前后都是 ASCII 字母，不应被当作独立 token 转换。
    assert textnorm.normalize_subtitle_text("he said hello") == "he said hello"


def test_pure_english_sentence_unchanged():
    assert textnorm.normalize_subtitle_text("The book is good") == "The book is good"


# ---------------------------------------------------------------------------
# 防御：空串 / None
# ---------------------------------------------------------------------------

def test_empty_string_returns_empty_string():
    assert textnorm.normalize_subtitle_text("") == ""


def test_none_returns_empty_string():
    assert textnorm.normalize_subtitle_text(None) == ""


# ---------------------------------------------------------------------------
# 组合场景
# ---------------------------------------------------------------------------

def test_mixed_sentence_with_number_and_ascii_word():
    assert textnorm.normalize_subtitle_text("这次我们从十块钱开始用ai测谎") == "这次我们从10块钱开始用AI测谎"


# ---------------------------------------------------------------------------
# normalize_subtitle_rows
# ---------------------------------------------------------------------------

def test_normalize_subtitle_rows_only_changes_text_field_and_counts_changes():
    rows = [
        {"text": "十块钱", "start_frame": 100, "end_frame": 130},
        {"text": "一模一样", "start_frame": 130, "end_frame": 160},
        {"text": "普通句子", "start_frame": 160, "end_frame": 190},
    ]

    normalized_rows, diagnostic = textnorm.normalize_subtitle_rows(rows)

    assert normalized_rows[0]["text"] == "10块钱"
    assert normalized_rows[0]["start_frame"] == 100
    assert normalized_rows[0]["end_frame"] == 130
    assert normalized_rows[1]["text"] == "一模一样"
    assert normalized_rows[2]["text"] == "普通句子"
    assert diagnostic == {"textnorm_changed_row_count": 1}


# ---------------------------------------------------------------------------
# 回归：几万/概数误转（真实全片生成发现的问题）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "几万块钱",
        "一定要几万块钱吧",
        "二三十块钱",
        "十几块钱",
        "二十来个",
        "好几十块钱",
    ],
)
def test_fuzzy_and_approximate_numerals_are_not_converted(text):
    assert textnorm.normalize_subtitle_text(text) == text


def test_regression_sishi_kuai_qian_still_converts():
    assert textnorm.normalize_subtitle_text("四十块钱") == "40块钱"


def test_regression_sanqian_kuai_qian_still_converts():
    assert textnorm.normalize_subtitle_text("三千块钱") == "3000块钱"


def test_regression_shiyi_yue_still_converts():
    assert textnorm.normalize_subtitle_text("十一月") == "11月"


def test_bare_wan_before_quantifier_without_valid_leading_digit_is_not_converted():
    # 「万」打头无有效前导数字，解析结果为 0 但原文非零，判定解析失败保留原文。
    assert textnorm.normalize_subtitle_text("万块钱") == "万块钱"


def test_normalize_subtitle_rows_handles_empty_list_and_missing_text_key():
    empty_rows, empty_diagnostic = textnorm.normalize_subtitle_rows([])
    assert empty_rows == []
    assert empty_diagnostic == {"textnorm_changed_row_count": 0}

    rows_without_text = [{"start_frame": 0, "end_frame": 10}]
    normalized_rows, diagnostic = textnorm.normalize_subtitle_rows(rows_without_text)
    assert normalized_rows == rows_without_text
    assert diagnostic == {"textnorm_changed_row_count": 0}
