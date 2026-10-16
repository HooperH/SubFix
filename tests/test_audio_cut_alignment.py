"""Execute actual alignment planners in LuaJIT with deterministic audio evidence."""
from pathlib import Path

import pytest

from test_normalize_length_options import run_lua

SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text()


def section(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))]


CONFIG = section("SUBFIX_AUDIO_ALIGN.min_ctc_confidence =", "SUBFIX_AUDIO_ALIGN.local_onset_pullback_frames =")
PLAN = section("function SUBFIX_AUDIO_ALIGN.qwen_global_candidate_is_accepted(",
               "function SUBFIX_AUDIO_ALIGN.reject_neighbor_gap_outlier(")
SETUP = '''
    SUBFIX_AUDIO_ALIGN = {}
''' + CONFIG + '''
    row = {start_frame = 100, end_frame = 160, text = "test"}
    result = {row = row, matched = true, alignment_mode = "qwen3_forced_aligner",
        row_remap_decision = "accepted_local_match", row_remap_score = 0.99,
        ctc_start_frame = 106, ctc_end_frame = 151,
        audio_source = {start_frame = 0, end_frame = 500}}
'''


def review_setup():
    return SETUP + PLAN + section(
        "function SUBFIX_AUDIO_ALIGN.ctc_candidate_start_frame(",
        "function SUBFIX_AUDIO_ALIGN.format_frame_range(",
    ) + '''
        result.ctc_confidence = 1
        result.ctc_start_frame, result.stable_ts_start_frame = 110, 110
        result.old_start_frame, result.old_end_frame = 100, 160
        result.local_onset_frames = {116}
        result.batch_index = 1
        review = {}
        for k, v in pairs(result) do review[k] = v end
        review.ctc_start_frame, review.stable_ts_start_frame = 116, 116
        review.local_onset_frames = {116}
        rows, positions = {row}, {[row] = 1}
        function apply_review()
            return SUBFIX_AUDIO_ALIGN.apply_ctc_review_results(
                {result}, {review}, {[1] = {target_row = row, fast_result = result}},
                rows, positions, 30)
        end
    '''


def test_qwen_review_accepts_better_onset_even_with_full_text_score_and_larger_move(run_lua):
    run_lua(review_setup() + '''
        local stats = apply_review()
        assert(stats.adopted_count == 1, "full text score must not block audio evidence")
        assert(result.ctc_start_frame == 116 and result.review_adopted)
        assert(result.review_selection_reason == "closer_to_shared_audio_onset")
    ''')


@pytest.mark.parametrize("change", [
    "review.local_onset_frames = {}",
    "result.local_onset_frames = {}",
    "review.local_onset_frames = {121}",
    "review.ctc_start_frame = 109; review.stable_ts_start_frame = 109",
    "review.ctc_start_frame = 111; review.stable_ts_start_frame = 111",
    "review.row_remap_score = 0.5",
    "review.ctc_start_frame = 0/0",
    "review.ctc_start_frame = 'bad'",
    "review.ctc_end_frame = math.huge",
    "review.ctc_end_frame = 115",
    "review.ctc_end_frame = 501",
    "review.audio_source = {start_frame = 115, end_frame = 500}",
    "review.ctc_start_frame = 210; review.stable_ts_start_frame = 210; review.local_onset_frames = {210}",
])
def test_qwen_review_rejects_unsupported_or_unsafe_candidate(run_lua, change):
    run_lua(review_setup() + change + '''
        local stats = apply_review()
        assert(stats.adopted_count == 0 and stats.rejected_count == 1)
        assert(result.ctc_start_frame == 110)
    ''')


def test_qwen_review_defers_filled_tail_to_joint_audio_plan(run_lua):
    run_lua(review_setup() + '''
        local previous = {start_frame = 50, end_frame = 118}
        rows, positions = {previous, row}, {[previous] = 1, [row] = 2}
        assert(apply_review().adopted_count == 1, "display tail is not a speech boundary")
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, rows, positions, 30, 0, {})
        assert(not plan[row].can_move, "unresolved actual overlap must still be rejected")
    ''')


def test_qwen_review_trigger_uses_raw_candidate_and_audio_mismatch(run_lua):
    run_lua(review_setup() + '''
        result.stable_ts_start_frame = 116
        local requested, reason = SUBFIX_AUDIO_ALIGN.is_ctc_review_candidate(result, rows, positions, 30)
        assert(requested and reason:find("audio_onset_mismatch", 1, true))
    ''')


def test_ctc_review_keeps_real_confidence_gate(run_lua):
    run_lua(review_setup() + '''
        result.alignment_mode, review.alignment_mode = "ctc_forced_alignment", "ctc_forced_alignment"
        assert(apply_review().adopted_count == 0)
        result.ctc_confidence, review.ctc_confidence = 0.5, 0.9
        review.stable_ts_start_frame = 105
        assert(apply_review().adopted_count == 1)
    ''')


@pytest.mark.parametrize("fps", [24, 29.97, 60])
def test_qwen_review_is_stable_and_flows_through_joint_plan(run_lua, fps):
    run_lua(review_setup() + f'fps = {fps}\n' + '''
        result.ctc_start_frame = 116 - math.floor(fps * 0.2)
        local meta = {[1] = {target_row = row, fast_result = result}}
        assert(SUBFIX_AUDIO_ALIGN.apply_ctc_review_results(
            {result}, {review}, meta, rows, positions, fps).adopted_count == 1)
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, rows, positions, fps, 0, {})[row]
        assert(plan.can_move and plan.final_start == 116 and plan.final_end == 151)
        row.start_frame, row.end_frame = plan.final_start, plan.final_end
        assert(SUBFIX_AUDIO_ALIGN.apply_ctc_review_results(
            {result}, {review}, meta, rows, positions, fps).adopted_count == 0)
        local again = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, rows, positions, fps, 0, {})[row]
        assert(again.final_start == row.start_frame and again.final_end == row.end_frame)
    ''')


def test_default_audio_bias_is_zero(run_lua):
    run_lua(SETUP + 'assert(SUBFIX_AUDIO_ALIGN.normalize_length_start_bias_frames == 0)')


@pytest.mark.parametrize("bias", [0, -2, 2])
def test_qwen_timing_has_one_bias_and_is_repeatable(run_lua, bias):
    run_lua(SETUP + PLAN + f'bias = {bias}\n' + '''
        for iteration = 1, 3 do
            local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
                {result}, {row}, {[row] = 1}, 30, bias, {})[row]
            assert(plan.can_move)
            assert(plan.final_start == 106 + bias, "bias stacked or drifted")
            assert(plan.final_end == 151, "speech end was ignored or shifted")
            row.start_frame, row.end_frame = plan.final_start, plan.final_end
        end
    ''')


@pytest.mark.parametrize("fps", [24, 29.97, 60])
def test_nearby_cuts_never_trim_speech(run_lua, fps):
    run_lua(SETUP + PLAN + f'fps = {fps}\n' + '''
        local tolerance = math.max(1, math.floor(fps * 0.1 + 0.5))
        local before, after = 106 - tolerance, 151 + tolerance
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, {row}, {[row] = 1}, fps, 0,
            {before, 107, 150, after, 300})[row]
        assert(plan.final_start == before and plan.final_end == after)
        assert(plan.start_cut_snapped and plan.end_cut_snapped)
        local unsafe = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, {row}, {[row] = 1}, fps, 0, {107, 150, 300})[row]
        assert(unsafe.final_start == 106 and unsafe.final_end == 151)
    ''')


def test_cut_snap_respects_audio_and_neighbor_bounds(run_lua):
    run_lua(SETUP + PLAN + '''
        result.audio_source = {start_frame = 105, end_frame = 152}
        local prior = {start_frame = 50, end_frame = 106}
        local next_row = {start_frame = 152, end_frame = 190}
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, {prior, row, next_row}, {[row] = 2}, 30, 0, {104, 105, 154})[row]
        assert(plan.can_move and plan.final_start == 106 and plan.final_end == 151)
    ''')


def test_late_subtitle_can_move_beyond_old_end_when_audio_supports_it(run_lua):
    run_lua(SETUP + PLAN + '''
        row.end_frame = 120
        result.ctc_start_frame, result.ctc_end_frame = 130, 180
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, {row}, {[row] = 1}, 30, 0, {})[row]
        assert(plan.can_move and plan.final_start == 130 and plan.final_end == 180)
    ''')


def test_joint_candidates_do_not_overlap(run_lua):
    run_lua(SETUP + PLAN + '''
        local second = {start_frame = 160, end_frame = 220}
        local other = {row = second, matched = true, alignment_mode = "qwen3_forced_aligner",
            row_remap_decision = "accepted_local_match", row_remap_score = 0.99,
            ctc_start_frame = 153, ctc_end_frame = 210, audio_source = result.audio_source}
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result, other}, {row, second}, {[row] = 1, [second] = 2}, 30, 0, {152})
        assert(plan[row].can_move and plan[second].can_move)
        assert(plan[row].final_end <= plan[second].final_start)
    ''')


def test_joint_late_moves_do_not_require_multiple_runs(run_lua):
    run_lua(SETUP + PLAN + '''
        local second = {start_frame=160,end_frame=220}
        result.ctc_start_frame,result.ctc_end_frame=130,190
        local other={row=second,matched=true,alignment_mode="qwen3_forced_aligner",
            row_remap_decision="accepted_local_match",row_remap_score=0.99,
            ctc_start_frame=195,ctc_end_frame=245,audio_source=result.audio_source}
        for iteration=1,3 do
            local plan=SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
                {result,other},{row,second},{[row]=1,[second]=2},30,0,{})
            assert(plan[row].can_move and plan[second].can_move)
            assert(plan[row].final_start==130 and plan[row].final_end==190)
            assert(plan[second].final_start==195 and plan[second].final_end==245)
            row.start_frame,row.end_frame=plan[row].final_start,plan[row].final_end
            second.start_frame,second.end_frame=plan[second].final_start,plan[second].final_end
        end
    ''')


@pytest.mark.parametrize("mutation", [
    "result.row_remap_score = 0.1",
    "result.ctc_start_frame = 400; result.ctc_end_frame = 450",
    "result.ctc_start_frame = 0/0",
    "result.row_remap_score = 0/0",
])
def test_unsafe_candidates_are_not_written(run_lua, mutation):
    run_lua(SETUP + PLAN + mutation + '''
        local plan = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result}, {row}, {[row] = 1}, 30, 0, {100})[row]
        assert(not plan or not plan.can_move)
    ''')


def test_fallback_audio_covers_missing_rows_once_and_excludes_disabled_music(run_lua):
    body = section("function SUBFIX_AUDIO_ALIGN.copy_row_for_audio_source(",
                   "function SUBFIX_AUDIO_ALIGN.split_alignment_batch_plan_for_accuracy(")
    run_lua('''
        SUBFIX_AUDIO_ALIGN = {
            get_rows_frame_range = function() return 0, 500 end,
            frame_overlap = function(a,b,c,d) return math.max(0, math.min(b,d)-math.max(a,c)) end,
            audio_source_from_item = function(item, track, index)
                return {start_frame = item.a, end_frame = item.b,
                    track_index = track, item_index = index, file_name = item.name}
            end
        }
        function clip(a,b,name)
            return {a=a,b=b,name=name or "dialogue.wav",
                GetStart=function(self) return self.a end, GetEnd=function(self) return self.b end}
        end
        local tracks = {{clip(0,300)}, {clip(300,400)}, {clip(0,500)}, {clip(400,500,"bgm.wav")}}
        timeline = {GetTrackCount=function() return 4 end,
            GetIsTrackEnabled=function(_,_,index) return index ~= 3 end,
            GetItemListInTrack=function(_,_,index) return tracks[index] end}
        rows = {{start_frame=20,end_frame=50}, {start_frame=320,end_frame=360},
                {start_frame=420,end_frame=460}}
    ''' + body + '''
        local plan = assert(SUBFIX_AUDIO_ALIGN.find_primary_audio_track_batches(timeline, rows, 30))
        assert(plan.track_index == 1 and #plan.batches == 2)
        assert(#plan.unassigned_rows == 1 and plan.unassigned_rows[1] == rows[3])
        assert(plan.fallback_assigned_count == 1)
        local seen = {}
        for _, batch in ipairs(plan.batches) do
            for _, row in ipairs(batch.rows) do
                assert(not seen[row.source_row_ref]); seen[row.source_row_ref] = true
                assert(batch.audio_source.track_index < 3)
            end
        end
        assert(seen[rows[1]] and seen[rows[2]])
    ''')


@pytest.mark.parametrize("fps", [24, 29.97, 60])
def test_explicit_gap_fill_is_independent_of_audio_verification(run_lua, fps):
    run_lua(SETUP + PLAN + f'fps = {fps}\n' + """
        local left = {start_frame=100,end_frame=151,audio_alignment_end_at_cut=true}
        local right = {start_frame=153,end_frame=300}
        local limit = math.floor(fps * 2 + 0.5)
        for _, gap in ipairs({1, math.floor(fps * 0.5), limit}) do
            right.start_frame = left.end_frame + gap
            assert(SUBFIX_AUDIO_ALIGN.can_fill_aligned_gap(left,right,fps), "ordinary gap not filled")
        end
        right.start_frame = left.end_frame + limit + 1
        assert(not SUBFIX_AUDIO_ALIGN.can_fill_aligned_gap(left,right,fps), "long pause must remain")
        right.start_frame = left.end_frame
        assert(not SUBFIX_AUDIO_ALIGN.can_fill_aligned_gap(left,right,fps))
        right.start_frame = left.end_frame - 1
        assert(not SUBFIX_AUDIO_ALIGN.can_fill_aligned_gap(left,right,fps))
    """)


def test_protected_runner_applies_cuts_and_reports_unmatched_rows(run_lua):
    runner = section("function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill(",
                     "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill(")
    run_lua(SETUP + PLAN + '''
        local missing = {start_frame=200,end_frame=220,text="unmatched"}
        rows = {row,missing}
        timeline = {}
        get_resolve = function() return {GetProjectManager=function() return {
            GetCurrentProject=function() return {GetCurrentTimeline=function() return timeline end} end
        } end} end
        collect_visible_timeline_cut_frames = function() return {104,154} end
        is_normalize_progress_cancelled = function() return false end
        sort_rows_by_timing = function() end
        LogMsg = function() end
        SUBFIX_AUDIO_ALIGN.clamp_normalize_length_bias_frames = function(n) return tonumber(n) or 0 end
        SUBFIX_AUDIO_ALIGN.find_primary_audio_track_batches = function() return {
            batches={},unassigned_rows={missing},fallback_assigned_count=1} end
        SUBFIX_AUDIO_ALIGN.split_alignment_batch_plan_for_accuracy = function(plan) return plan end
        SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches = function() return {
            result,{row=missing,matched=false,reason="no audio"}},{} end
        SUBFIX_AUDIO_ALIGN.build_ctc_review_batch_plan = function() return nil end
        SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record = function(r) return r end
        SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics = function(records,info,fps,stats)
            assert(records[1].start_cut_snapped and records[1].end_cut_snapped)
            assert(stats.review_required_count == 1 and stats.cut_snapped_count == 1)
            return {}
        end
    ''' + runner + '''
        local changed,effective,err,stats = SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill(rows,30,{})
        assert(changed == 1 and effective and not err)
        assert(row.start_frame == 104 and row.end_frame == 154)
        assert(row.audio_alignment_verified and row.audio_alignment_end_at_cut)
        assert(not row.audio_alignment_warning and missing.audio_alignment_warning)
        assert(missing.start_frame == 200 and missing.end_frame == 220)
        assert(stats.fallback_assigned_count == 1)
        local changed_again, effective_again = SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill(rows,30,{})
        assert(changed_again == 0 and effective_again, "already aligned is success")
    ''')


def test_unmatched_audio_is_visible_in_review(run_lua):
    from test_suspicious_subtitle_review import COLLECTOR, SETUP as REVIEW_SETUP

    run_lua(REVIEW_SETUP + COLLECTOR + '''
        local issues = collect_pre_delivery_final_check_issues({
            {text="hello",start_frame=100,end_frame=150,audio_alignment_warning="no audio"}
        },nil,30)
        local issue = assert(find_issue(issues,"音频对齐待复查"))
        assert(issue.reason == "no audio" and issue.start_frame == 100)
    ''')


def test_rejected_future_candidate_cannot_leave_an_overlap(run_lua):
    run_lua(SETUP + PLAN + '''
        local second={start_frame=160,end_frame=220}
        result.ctc_start_frame,result.ctc_end_frame=110,180
        local other={row=second,matched=true,alignment_mode="qwen3_forced_aligner",
            row_remap_decision="accepted_local_match",row_remap_score=0.99,
            ctc_start_frame=190,ctc_end_frame=230,
            audio_source={start_frame=0,end_frame=185}}
        local plan=SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(
            {result,other},{row,second},{[row]=1,[second]=2},30,0,{})
        assert(not plan[row].can_move and not plan[second].can_move)
        assert(plan[row].final_end <= plan[second].final_start)
    ''')
