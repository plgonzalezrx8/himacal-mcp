import os
from datetime import datetime

import pytest
from icalendar import Calendar

if os.environ.get("HIMACAL_INTEGRATION") != "1":
    pytest.skip("Run isolated provider acceptance", allow_module_level=True)

from himacal.calendars import CalendarService, EventInput, EventPatch
from himacal.errors import ProviderError

pytestmark = pytest.mark.integration


def calendar_id(service, account_id, calendar):
    name = calendar.get_display_name()
    return next(
        item["id"]
        for item in service.list_calendars(account_id)["calendars"]
        if item["name"] == name
    )


def test_full_event_crud_preserves_unrelated_data_and_refuses_stale_versions(
    provider_settings, live_calendar
):
    service = CalendarService(provider_settings)
    selected = calendar_id(service, "alpha", live_calendar["alpha"])
    created = service.create_event(
        "alpha", selected, EventInput(summary="Original", start="2030-03-01", end="2030-03-02")
    )
    uid = created["uid"]
    assert created["event"]["all_day"] is True
    assert created["etag"].startswith('"')
    resource = live_calendar["alpha"].get_event_by_uid(uid)
    with resource.edit_icalendar_instance() as document:
        document.walk("VEVENT")[0].add("X-ACCEPTANCE-PRESERVE", "keep me")
    resource.save(precondition="etag")
    with pytest.raises(ProviderError) as stale:
        service.update_event(
            "alpha", selected, uid, created["etag"], EventPatch(summary="stale"), "series"
        )
    assert stale.value.code == "conflict"
    current = service.get_event("alpha", selected, uid)
    changed = service.update_event(
        "alpha", selected, uid, current["etag"], EventPatch(summary="Changed"), "series"
    )
    assert changed["event"]["summary"] == "Changed"
    assert "X-ACCEPTANCE-PRESERVE:keep me" in changed["ical"]
    with pytest.raises(ProviderError):
        service.delete_event("alpha", selected, uid, current["etag"], "series")
    deleted = service.delete_event("alpha", selected, uid, changed["etag"], "series")
    assert deleted["deleted"] is True and deleted["reversible"] is False
    with pytest.raises(ProviderError) as missing:
        service.get_event("alpha", selected, uid)
    assert missing.value.code == "event_not_found"


def test_occurrence_edit_cancel_restore_keeps_series_and_daylight_saving_time(
    provider_settings, live_calendar
):
    service = CalendarService(provider_settings)
    selected = calendar_id(service, "alpha", live_calendar["alpha"])
    series = service.create_event(
        "alpha",
        selected,
        EventInput(
            summary="Weekly",
            start="2030-03-03T09:00:00",
            end="2030-03-03T10:00:00",
            timezone="America/New_York",
            recurrence_rule="FREQ=WEEKLY;COUNT=4",
        ),
    )
    uid = series["uid"]
    window = ("2030-03-01T00:00:00Z", "2030-04-01T00:00:00Z")
    events = service.list_events("alpha", selected, *window)["events"]
    assert len(events) == 4
    assert [datetime.fromisoformat(item["start"]).hour for item in events] == [9, 9, 9, 9]
    assert (
        datetime.fromisoformat(events[0]["start"]).utcoffset()
        != datetime.fromisoformat(events[-1]["start"]).utcoffset()
    )
    original_id = events[1]["recurrence_id"]
    moved = service.update_event(
        "alpha",
        selected,
        uid,
        series["etag"],
        EventPatch(
            start="2030-03-10T11:00:00", end="2030-03-10T12:00:00", timezone="America/New_York"
        ),
        "occurrence",
        original_id,
    )
    events = service.list_events("alpha", selected, *window)["events"]
    assert len(events) == 4
    assert next(item for item in events if item["recurrence_id"] == original_id)[
        "start"
    ].startswith("2030-03-10T11:00:00")
    assert events[0]["start"].startswith("2030-03-03T09:00:00")
    cancelled = service.delete_event(
        "alpha", selected, uid, moved["etag"], "occurrence", original_id
    )
    events = service.list_events("alpha", selected, *window)["events"]
    assert all(
        item["recurrence_id"] != original_id or item["status"] == "CANCELLED" for item in events
    )
    restored = service.update_event(
        "alpha",
        selected,
        uid,
        cancelled["etag"],
        EventPatch(summary="Restored instance"),
        "occurrence",
        original_id,
    )
    events = service.list_events("alpha", selected, *window)["events"]
    assert len(events) == 4
    assert (
        next(item for item in events if item["recurrence_id"] == original_id)["summary"]
        == "Restored instance"
    )
    assert len(Calendar.from_ical(restored["ical"]).walk("VEVENT")) == 2


def test_foreign_calendar_and_scheduling_writes_are_rejected(provider_settings, live_calendar):
    service = CalendarService(provider_settings)
    alpha = calendar_id(service, "alpha", live_calendar["alpha"])
    beta = calendar_id(service, "beta", live_calendar["beta"])
    with pytest.raises(ProviderError):
        service.create_event(
            "alpha", beta, EventInput(summary="Wrong account", start="2030-01-01", end="2030-01-02")
        )
    created = service.create_event(
        "alpha", alpha, EventInput(summary="Invite fixture", start="2030-01-01", end="2030-01-02")
    )
    resource = live_calendar["alpha"].get_event_by_uid(created["uid"])
    with resource.edit_icalendar_instance() as document:
        event = document.walk("VEVENT")[0]
        event.add("ORGANIZER", "mailto:alpha@example.test")
        event.add("ATTENDEE", "mailto:beta@example.test")
    resource.save(precondition="etag")
    invited = service.get_event("alpha", alpha, created["uid"])
    with pytest.raises(ProviderError) as rejected:
        service.update_event(
            "alpha",
            alpha,
            created["uid"],
            invited["etag"],
            EventPatch(summary="No notification"),
            "series",
        )
    assert rejected.value.code == "unsupported"
    assert service.get_event("alpha", alpha, created["uid"])["event"]["summary"] == "Invite fixture"
