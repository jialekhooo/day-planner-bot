from datetime import date, datetime, time

import pytest

from planner.agenda import clashing, free_gaps, local_today, span
from planner.bot import _term
from planner.parsing import ParseError, parse_date, parse_entries, parse_entry, parse_time
from planner.storage import Plan, Storage, Term
from planner.timetable import (
    classes_as_text,
    classes_from_cells,
    classes_from_text,
    week_dates,
)

TODAY = date(2026, 8, 11)  # a Tuesday


def _plan(ref: int, start: time | None, end: time | None, day: date = TODAY) -> Plan:
    return Plan(id=ref, day=day, title=f"Plan {ref}", start=start, end=end, done=False)


def test_parses_a_timed_block():
    entry = parse_entry("9am-11am Gym", today=TODAY)
    assert (entry.day, entry.start, entry.end, entry.title) == (
        TODAY,
        time(9, 0),
        time(11, 0),
        "Gym",
    )


def test_parses_military_times_and_a_date():
    entry = parse_entry("14/8 1400-1600 project review", today=TODAY)
    assert (entry.day, entry.start, entry.end) == (date(2026, 8, 14), time(14, 0), time(16, 0))
    assert entry.title == "project review"


def test_parses_a_single_time_as_a_start():
    entry = parse_entry("tomorrow 12.30pm lunch with Ada", today=TODAY)
    assert entry.day == date(2026, 8, 12)
    assert (entry.start, entry.end) == (time(12, 30), None)
    assert entry.title == "lunch with Ada"


def test_a_line_without_a_time_is_a_task_for_today():
    entry = parse_entry("buy milk", today=TODAY)
    assert (entry.day, entry.start, entry.title) == (TODAY, None, "buy milk")


def test_weekday_names_look_forward():
    assert parse_date("friday", TODAY) == date(2026, 8, 14)
    assert parse_date("tue", TODAY) == TODAY
    assert parse_date("next tue", TODAY) == date(2026, 8, 18)


def test_parse_time_rejects_nonsense():
    with pytest.raises(ParseError):
        parse_time("banana")


def test_parses_several_lines_keeping_errors_per_line():
    results = parse_entries("9am-10am Gym\n\n   \n12pm lunch", today=TODAY)
    assert [line for line, _ in results] == ["9am-10am Gym", "12pm lunch"]
    assert all(not isinstance(outcome, ParseError) for _, outcome in results)


def test_untimed_plans_never_clash():
    assert clashing([_plan(1, None, None), _plan(2, None, None)]) == set()


def test_overlapping_blocks_are_flagged():
    plans = [_plan(1, time(9, 0), time(11, 0)), _plan(2, time(10, 0), time(12, 0))]
    assert clashing(plans) == {1, 2}


def test_a_single_time_takes_an_hour():
    start, end = span(_plan(1, time(9, 0), None))
    assert (start, end) == (datetime(2026, 8, 11, 9, 0), datetime(2026, 8, 11, 10, 0))


def test_free_gaps_are_what_is_left_of_the_waking_day():
    plans = [_plan(1, time(9, 0), time(11, 0)), _plan(2, time(14, 0), time(15, 0))]
    assert free_gaps(plans, TODAY) == [
        (time(8, 0), time(9, 0)),
        (time(11, 0), time(14, 0)),
        (time(15, 0), time(22, 0)),
    ]


def test_free_gaps_start_from_now_for_today():
    plans = [_plan(1, time(9, 0), time(11, 0))]
    assert free_gaps(plans, TODAY, after=time(12, 0)) == [(time(12, 0), time(22, 0))]


def test_local_today_follows_the_offset():
    late = datetime(2026, 8, 11, 17, 30)  # 01:30 the next day in GMT+8
    assert local_today(480, late) == date(2026, 8, 12)
    assert local_today(0, late) == date(2026, 8, 11)


def test_plans_are_numbered_from_one_per_user(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    first = storage.add_plan(1, TODAY, "Gym", time(9, 0), time(10, 0))
    second = storage.add_plan(1, TODAY, "Lunch", time(12, 0), None)
    other_user = storage.add_plan(2, TODAY, "Standup", time(9, 0), None)
    assert (first, second, other_user) == (1, 2, 1)
    storage.close()


def test_a_day_lists_timed_plans_before_tasks(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    storage.add_plan(1, TODAY, "buy milk", None, None)
    storage.add_plan(1, TODAY, "Gym", time(9, 0), time(10, 0))
    assert [plan.title for plan in storage.plans_on(1, TODAY)] == ["Gym", "buy milk"]
    storage.close()


def test_done_and_move_change_one_plan(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    ref = storage.add_plan(1, TODAY, "Gym", time(9, 0), time(10, 0))
    assert storage.set_done(1, ref, True)
    assert storage.open_plans(1) == []
    storage.move_plan(1, ref, date(2026, 8, 12), time(18, 0), None)
    moved = storage.get_plan(1, ref)
    assert moved is not None
    assert (moved.day, moved.start, moved.end) == (date(2026, 8, 12), time(18, 0), None)
    storage.close()


def test_nudges_are_sent_once(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    ref = storage.add_plan(1, TODAY, "Gym", time(9, 0), None)
    assert [plan.id for plan in storage.pending_nudges(1, TODAY)] == [ref]
    storage.mark_nudged(1, ref)
    assert storage.pending_nudges(1, TODAY) == []
    storage.close()


TIMETABLE = """
MON 0930-1120 IE4727 LEC/STU S2-B3A_06 Wk1-11
MON 1430-1720 ES5003 LEC/STU LT19
FRI 1030-1220 HW0288 TUT LHN-TR+18 Wk2-13
"""


def test_reads_a_typed_timetable():
    classes = classes_from_text(TIMETABLE)
    assert [(lesson.weekday, lesson.start, lesson.end, lesson.title) for lesson in classes] == [
        (0, time(9, 30), time(11, 20), "IE4727 LEC @ S2-B3A_06"),
        (0, time(14, 30), time(17, 20), "ES5003 LEC @ LT19"),
        (4, time(10, 30), time(12, 20), "HW0288 TUT @ LHN-TR+18"),
    ]


def test_reads_the_weeks_a_class_runs_in():
    weeks = [lesson.weeks for lesson in classes_from_text(TIMETABLE)]
    assert weeks == [tuple(range(1, 12)), (), tuple(range(2, 14))]
    assert classes_from_text("MON 0930to1120 IE4727 LEC Wk12,13")[0].weeks == (12, 13)
    assert classes_from_text("MON 0930to1120 IE4727 LEC W12,13")[0].weeks == (12, 13)  # OCR


def test_ocr_slips_are_read_as_the_course_code():
    cells = [(0, ["1E4727", "LEC/STU", "S2-B3A_06", "0930tol120"])]
    assert classes_from_cells(cells)[0].code == "IE4727"


def test_a_class_spanning_rows_is_only_listed_once():
    cell = (0, ["IE4727", "LEC/STU", "S2-B3A_06", "0930to1120", "Wk1-11"])
    assert len(classes_from_cells([cell, cell])) == 1


def test_lines_without_a_class_are_ignored():
    assert classes_from_text("Academic Year 2026, Semester 1\nLegend: LEC = lecture") == []


def test_classes_become_a_date_each_week_they_run():
    classes = classes_from_text(TIMETABLE)
    dated = week_dates(classes, date(2026, 8, 10), 13)  # a Monday
    assert dated[0] == (date(2026, 8, 10), classes[0])
    assert [day for day, lesson in dated if lesson is classes[2]][0] == date(2026, 8, 21)
    assert len(dated) == 11 + 13 + 12


def test_a_break_week_is_skipped_and_does_not_count():
    classes = classes_from_text("MON 0930-1120 IE4727 LEC LT19")
    dated = week_dates(classes, date(2026, 8, 10), 3, breaks=(date(2026, 9, 28),))
    assert [day for day, _ in dated][:3] == [
        date(2026, 8, 10),
        date(2026, 8, 17),
        date(2026, 8, 24),
    ]
    dated = week_dates(classes, date(2026, 8, 10), 9, breaks=(date(2026, 9, 30),))
    assert date(2026, 9, 28) not in [day for day, _ in dated]
    assert [day for day, _ in dated][-1] == date(2026, 10, 12)  # week 9, a week later


def test_term_reads_week_one_and_the_recess_week():
    term = _term(["10", "Aug", "recess", "28", "Sep"], TODAY, None)
    assert term == Term(date(2026, 8, 10), (date(2026, 9, 28),))


def test_term_falls_back_to_the_last_import_then_to_this_week():
    saved = Term(date(2026, 8, 10), (date(2026, 9, 28),))
    assert _term([], TODAY, saved) == saved
    assert _term([], TODAY, None) == Term(date(2026, 8, 10), ())


def test_a_timetable_survives_being_written_down_and_read_back():
    classes = classes_from_text(TIMETABLE)
    assert classes_from_text(classes_as_text(classes)) == classes


def test_the_timetable_is_remembered(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    storage.save_term(1, Term(date(2026, 8, 10)))
    storage.save_classes(1, "MON 0930-1120 IE4727 LEC LT19")
    assert storage.get_classes(1) == "MON 0930-1120 IE4727 LEC LT19"
    assert storage.get_classes(2) == ""
    storage.close()


def test_the_term_is_remembered(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    term = Term(date(2026, 8, 10), (date(2026, 9, 28),))
    storage.save_term(1, term)
    assert storage.get_term(1) == term
    assert storage.get_term(2) is None
    storage.close()


def test_a_timetable_import_replaces_the_last_one(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    storage.add_plan(1, TODAY, "Gym", time(9, 0), None)
    storage.add_plan(1, TODAY, "IE4727 LEC", time(9, 30), time(11, 20), source="timetable")
    assert storage.delete_from_source(1, "timetable", since=TODAY) == 1
    assert [plan.title for plan in storage.open_plans(1)] == ["Gym"]
    storage.close()


def test_clearing_a_day_leaves_other_days(tmp_path):
    storage = Storage(tmp_path / "planner.sqlite3")
    storage.add_plan(1, TODAY, "Gym", time(9, 0), None)
    storage.add_plan(1, date(2026, 8, 12), "Dentist", time(9, 0), None)
    assert storage.delete_plans(1, TODAY) == 1
    assert [plan.day for plan in storage.open_plans(1)] == [date(2026, 8, 12)]
    storage.close()
