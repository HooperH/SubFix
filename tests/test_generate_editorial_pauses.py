import pytest

from test_generate_v4 import load_v4, unit


def make_case(prefix, suffix, gap=0, punctuation=0.0):
    text = prefix + suffix
    units = [unit(char, i * 5 + (gap if i >= len(prefix) else 0),
                  (i + 1) * 5 + (gap if i >= len(prefix) else 0))
             for i, char in enumerate(text)]
    units[len(prefix) - 1]["asr_punctuation_strength"] = punctuation
    return {"units": units, "expected": [prefix, suffix],
            "manual_boundary_frame": len(prefix) * 5 + gap}


FIXTURE = {
    "fps": 30,
    "cases": [
        make_case("我们最近整理了所有设备", "接下来需要逐一检查它们的运行情况", punctuation=0.55),
        make_case("大家已经完成了今天的练习", "明天继续学习", gap=15),
    ],
    "continuous_clause_case": {
        "units": [unit(char, i * 4, (i + 1) * 4)
                  for i, char in enumerate("开始整理这些实验记录之后")],
        "expected": "开始整理这些实验记录之后",
        "false_model_boundary": 4,
    },
}


@pytest.mark.parametrize("mode", ["live", "narration"])
@pytest.mark.parametrize("max_chars", [None, 25])
def test_model_alone_cannot_detach_short_verb_phrase(monkeypatch, mode, max_chars):
    v4 = load_v4()
    case = FIXTURE["continuous_clause_case"]
    profile = {"boundary_model": {"decision_threshold": 0.7}}
    monkeypatch.setattr(
        v4, "boundary_probability",
        lambda _text, position, _model: 0.98 if position == case["false_model_boundary"] else 0.1,
    )

    rows, _ = v4.segment_canonical_units(case["units"], mode, profile, FIXTURE["fps"], max_chars=max_chars)

    assert [row["text"] for row in rows] == [case["expected"]]
    assert rows[0]["start_frame"] == case["units"][0]["start_frame"]
    assert rows[0]["end_frame"] == case["units"][-1]["end_frame"]


@pytest.mark.parametrize("case", FIXTURE["cases"])
@pytest.mark.parametrize("mode", ["live", "narration"])
def test_real_editorial_boundaries_survive_negative_model_scores(case, mode):
    v4 = load_v4()
    profile = {"modes": {mode: {"boundary_model": {
        "weights": [0.0] * v4.MODEL_BUCKETS, "bias": -8.0,
    }}}}
    rows, diagnostic = v4.segment_canonical_units(case["units"], mode, profile, FIXTURE["fps"], max_chars=25)
    rows, _ = v4.protect_word_boundaries(rows, case["units"], FIXTURE["fps"])
    rows, _ = v4.enforce_hard_char_limit(rows, case["units"], 25)

    assert [row["text"] for row in rows] == case["expected"]
    assert abs(rows[1]["start_frame"] - case["manual_boundary_frame"]) <= 3
    assert rows[0]["end_frame"] <= rows[1]["start_frame"]
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize("gap", [0, 1, 3, 4])
def test_uniform_word_spacing_does_not_invent_clause_breaks(gap):
    v4 = load_v4()
    text = "这个完整语义区间长度还没有超过字幕上限"
    units = [unit(char, i * (5 + gap), i * (5 + gap) + 5) for i, char in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, "live", None, 30, max_chars=25)

    assert [row["text"] for row in rows] == [text]


def test_short_comma_clause_is_not_needlessly_fragmented():
    v4 = load_v4()
    text = "看得没有那么清楚或者画面小一点"
    units = [unit(char, i * 4, (i + 1) * 4) for i, char in enumerate(text)]
    units[7]["asr_punctuation_strength"] = 0.55
    profile = {"boundary_model": {"weights": [0.0] * v4.MODEL_BUCKETS, "bias": -8.0}}
    rows, _ = v4.segment_canonical_units(units, "live", profile, 30, max_chars=25)

    assert [row["text"] for row in rows] == [text]
