from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.excel_importer import (
    NormalizedSpottingEvent,
    canonical_aircraft_type,
    canonical_airline_name,
    iter_spotting_events,
    normalize_registration,
    repair_future_dates,
    split_route,
)


SOURCE_WORKBOOK = Path(__file__).resolve().parents[3] / "work" / "source" / "transport.xlsx"


def test_registration_normalization():
    assert normalize_registration(" g-xleg ") == "G-XLEG"
    assert normalize_registration(30572) == "30572"


def test_route_split():
    assert split_route("伦敦希思罗 —— 旧金山") == ("伦敦希思罗", "旧金山")
    assert split_route("—— 上海虹桥") == (None, "上海虹桥")


def test_airline_alias_is_normalized_to_english():
    assert canonical_airline_name("维珍大西洋航空") == "Virgin Atlantic"


def test_generic_a380_is_expanded_to_workbook_display_variant():
    assert canonical_aircraft_type("A380") == "Airbus A380-841"
    assert canonical_aircraft_type("Airbus A380") == "Airbus A380-841"
    assert canonical_aircraft_type("A380-841") == "Airbus A380-841"


def test_specific_non_a380_workbook_type_is_preserved():
    assert canonical_aircraft_type("Airbus A350-1041") == "Airbus A350-1041"
    assert canonical_aircraft_type("Boeing 787-10") == "Boeing 787-10"


def test_opt_in_future_year_repair_preserves_month_and_day_scope():
    event = NormalizedSpottingEvent(
        source_row=99,
        source_event_group=1,
        airline_raw="Virgin Atlantic",
        registration="G-VEYR",
        aircraft_type_raw="Airbus A330-941",
        aircraft_type="Airbus A330-941",
        aircraft_notes=None,
        spotting_location_raw="London Heathrow",
        spotting_date=date(2027, 11, 5),
        flight_number="VS117",
        route_text_original="London Heathrow -- Miami",
        route_departure_raw="London Heathrow",
        route_arrival_raw="Miami",
        quality_status="review",
        quality_reasons=("future_date",),
    )
    repaired, count = repair_future_dates(
        [event], date(2025, 11, 5), current_date=date(2026, 8, 25)
    )
    assert count == 1
    assert repaired[0].spotting_date == date(2025, 11, 5)
    assert repaired[0].quality_status == "ready"


def test_compact_corrected_workbook_layout_is_detected(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "航空"
    sheet.append(
        [
            "Virgin Atlantic",
            "G-VJAM",
            "Airbus A350-1041",
            "London Heathrow",
            None,
            date(2025, 11, 5),
            "VS302",
            "London Heathrow —— Delhi",
            None,
            date(2026, 5, 16),
            "VS10",
            "John Fitzgerald Kennedy —— London Heathrow",
        ]
    )
    sheet.append(
        [
            "British Airways",
            "G-XLEG",
            "Airbus A380-841",
            "London Heathrow",
            None,
            date(2025, 11, 5),
            "BA285",
            "London Heathrow —— San Francisco",
        ]
    )
    workbook_path = tmp_path / "compact.xlsx"
    workbook.save(workbook_path)

    events = list(
        iter_spotting_events(workbook_path, current_date=date(2026, 8, 26))
    )

    assert len(events) == 3
    assert [(item.source_row, item.source_event_group) for item in events] == [
        (1, 1),
        (1, 2),
        (2, 1),
    ]
    assert events[0].spotting_location_raw == "London Heathrow"
    assert events[1].flight_number == "VS10"
    assert all(item.quality_status == "ready" for item in events)


@pytest.mark.skipif(
    not SOURCE_WORKBOOK.is_file(),
    reason="Private source workbook is intentionally excluded from the public artefact",
)
def test_workbook_unpivots_to_expected_event_count():
    events = list(
        iter_spotting_events(SOURCE_WORKBOOK, current_date=date(2026, 8, 24))
    )
    assert len(events) == 1377
    assert sum(event.quality_status == "review" for event in events) >= 13


@pytest.mark.skipif(
    not SOURCE_WORKBOOK.is_file(),
    reason="Private source workbook is intentionally excluded from the public artefact",
)
def test_ba_a380_case_study_rows():
    events = list(
        iter_spotting_events(SOURCE_WORKBOOK, current_date=date(2026, 8, 24))
    )
    case_study = [
        event
        for event in events
        if event.registration in {"G-XLEG", "G-XLEK", "G-XLEL"}
    ]
    assert {event.flight_number for event in case_study} == {"BA285", "BA217", "BA107"}
    assert all(event.spotting_date == date(2025, 11, 5) for event in case_study)

