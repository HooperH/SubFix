import pytest

from test_generate_v4 import load_v4, unit, _run_v4_pipeline_with_max_chars


@pytest.mark.parametrize("mode", ["live", "narration"])
@pytest.mark.parametrize(
    "prefix,clause",
    [
        ("在启动的第二天", "小车就开始认真地尝试识别道路的标记"),
        ("在训练的第三天", "小明已经能够独立完成老师安排的练习"),
        ("在启动的第12天", "孩子开始主动模仿老师说话时的声音"),
        ("在完成训练之后", "孩子已经能够独立完成老师安排的练习"),
        ("在第二天", "孩子已经能够独立完成老师安排的练习"),
    ],
)
def test_temporal_clause_splits_without_pause_or_punctuation(mode, prefix, clause):
    v4 = load_v4()
    text = prefix + clause
    units = [unit(char, 100 + i * 5, 105 + i * 5) for i, char in enumerate(text)]

    rows, diagnostic = v4.segment_canonical_units(units, mode, None, 30, max_chars=25)
    rows, _ = v4.protect_word_boundaries(rows, units, 30)
    rows, _ = v4.enforce_hard_char_limit(rows, units, 25)

    assert [row["text"] for row in rows] == [prefix, clause]
    assert rows[0]["start_frame"] == 100
    assert rows[0]["end_frame"] == rows[1]["start_frame"] == 100 + len(prefix) * 5
    assert rows[1]["end_frame"] == 100 + len(text) * 5
    assert diagnostic["text_conservation_failed_count"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "在启动的第二天的课堂上小车开始尝试模仿老师的发音",
        "在训练的第三天内完成老师安排的全部练习",
        "在启动的第二天到第三天之间反复进行测试",
        "在启动的第二天下午小车开始尝试模仿老师的发音",
        "在启动的第二天我们见面了",
        "我们讨论在启动的第二天进行声音测试的方案",
        "在第二天就开始的课程中孩子尝试模仿老师的发音",
        "在启动的第二天结束后小车就开始尝试模仿老师的发音",
    ],
)
def test_temporal_expression_does_not_split_inside_noun_phrase(text):
    v4 = load_v4()
    units = [unit(char, i * 5, (i + 1) * 5) for i, char in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, "narration", None, 30, max_chars=40)

    assert [row["text"] for row in rows] == [text]


def test_temporal_split_still_respects_short_video_limit():
    v4 = load_v4()
    text = "在启动的第二天小车就开始认真地尝试识别道路的标记"
    units = [unit(char, i * 5, (i + 1) * 5) for i, char in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, "narration", None, 30, max_chars=10)
    rows, _ = v4.protect_word_boundaries(rows, units, 30)
    rows, _ = v4.enforce_hard_char_limit(rows, units, 10)

    assert rows[0]["text"] == "在启动的第二天"
    assert "".join(row["text"] for row in rows) == text
    assert all(len(row["text"]) <= 10 for row in rows)
    assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(rows, rows[1:]))


def test_temporal_split_does_not_invent_time_inside_multi_character_unit():
    v4 = load_v4()
    text = "在启动的第二天小车就开始认真地尝试识别道路的标记"
    rows, _ = v4.segment_canonical_units([unit(text, 100, 250)], "narration", None, 30, max_chars=25)

    assert [row["text"] for row in rows] == [text]
    assert (rows[0]["start_frame"], rows[0]["end_frame"]) == (100, 250)


def test_temporal_boundary_survives_batch_generation(tmp_path, monkeypatch):
    text = "在启动的第二天小车就开始认真地尝试识别道路的标记"
    payload = _run_v4_pipeline_with_max_chars(tmp_path, monkeypatch, text, {"max_chars": 25})

    assert payload["ok"] is True
    assert [row["text"] for row in payload["subtitle_rows"]] == [
        "在启动的第二天", "小车就开始认真地尝试识别道路的标记"
    ]


@pytest.mark.parametrize("mode", ["live", "narration"])
@pytest.mark.parametrize("gap", [0, 3, 6, 12])
@pytest.mark.parametrize("suffix", ["型的外部结构", "的外部结构"])
def test_copular_clause_precedes_misleading_internal_pause(mode, gap, suffix):
    v4 = load_v4()
    prefix, predicate = "前面模型上的这块小挡板", "就是车辆模型的外部结构"
    text = prefix + predicate
    pause = text.index(suffix)
    units = [unit(c, i * 5 + (gap if i >= pause else 0),
                  (i + 1) * 5 + (gap if i >= pause else 0)) for i, c in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, mode, None, 30, max_chars=25)
    rows, _ = v4.protect_word_boundaries(rows, units, 30)
    rows, _ = v4.enforce_hard_char_limit(rows, units, 25)
    assert [row["text"] for row in rows] == [prefix, predicate]
    assert rows[0]["end_frame"] == rows[1]["start_frame"] == len(prefix) * 5
    assert rows[-1]["end_frame"] == units[-1]["end_frame"]


@pytest.mark.parametrize("prefix,predicate", [
    ("屏幕上这条红色的曲线", "就是传感器记录的实时温度"),
    ("我们刚刚看到的那块面板", "正是这台设备的外部保护结构"),
    ("每天坚持训练积累的经验", "才是帮助我们解决问题的关键"),
    ("眼前这片逐渐亮起的区域", "便是系统当前正在处理的范围"),
    ("屏幕上这条红色的曲线", "也就是传感器记录的实时温度"),
])
def test_copular_boundaries_generalize_to_other_subjects(prefix, predicate):
    v4 = load_v4()
    text = prefix + predicate
    units = [unit(c, i * 5, (i + 1) * 5) for i, c in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, "live", None, 30, max_chars=25)
    assert [row["text"] for row in rows] == [prefix, predicate]


@pytest.mark.parametrize("text", [
    "这就是设备的外壳",
    "如果你认真观察就是没有仪器也能够发现这里的问题",
    "只要你坚持练习就是再难的动作也能够慢慢熟悉",
    "我们讨论的就是这台设备在实际运行过程中产生的问题",
    "这个按钮按下去不就是让设备进入正常工作的状态吗",
])
def test_copular_rule_keeps_short_and_dependent_phrases_together(text):
    v4 = load_v4()
    units = [unit(c, i * 5, (i + 1) * 5) for i, c in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, "live", None, 30, max_chars=40)
    assert [row["text"] for row in rows] == [text]


def test_copular_rule_does_not_invent_unit_timestamps():
    v4 = load_v4()
    text = "前面模型上的这块小挡板就是车辆模型的外部结构"
    rows, _ = v4.segment_canonical_units([unit(text, 100, 240)], "live", None, 30, max_chars=25)
    assert [row["text"] for row in rows] == [text]
    assert (rows[0]["start_frame"], rows[0]["end_frame"]) == (100, 240)


@pytest.mark.parametrize("hard_break", ["silence", "speaker", "punctuation"])
def test_compact_predicate_preserves_hard_boundaries(hard_break):
    v4 = load_v4()
    text = "就是设备的外部保护结构"
    cut = 5
    units = [unit(c, i * 5, (i + 1) * 5) for i, c in enumerate(text)]
    if hard_break == "silence":
        for item in units[cut:]:
            item["start_frame"] += 24
            item["end_frame"] += 24
    elif hard_break == "speaker":
        for item in units[cut:]:
            item["track_index"] = 2
    else:
        units[cut - 1]["asr_punctuation_strength"] = 1.0
    rows, _ = v4.segment_canonical_units(units, "live", None, 30, max_chars=25)
    assert [row["text"] for row in rows] == [text[:cut], text[cut:]]
    assert rows[1]["start_frame"] == units[cut]["start_frame"]


def test_copular_boundary_respects_short_video_limit():
    v4 = load_v4()
    text = "前面模型上的这块小挡板就是车辆模型的外部结构"
    units = [unit(c, i * 5, (i + 1) * 5) for i, c in enumerate(text)]
    rows, _ = v4.segment_canonical_units(units, "live", None, 30, max_chars=10)
    rows, _ = v4.protect_word_boundaries(rows, units, 30)
    rows, _ = v4.enforce_hard_char_limit(rows, units, 10)
    assert "".join(row["text"] for row in rows) == text
    assert all(len(row["text"]) <= 10 for row in rows)
    assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(rows, rows[1:]))


def test_copular_boundary_survives_batch_generation(tmp_path, monkeypatch):
    text = "前面模型上的这块小挡板就是车辆模型的外部结构"
    payload = _run_v4_pipeline_with_max_chars(tmp_path, monkeypatch, text, {"max_chars": 25})
    assert payload["ok"] is True
    assert [row["text"] for row in payload["subtitle_rows"]] == [
        "前面模型上的这块小挡板", "就是车辆模型的外部结构",
    ]
