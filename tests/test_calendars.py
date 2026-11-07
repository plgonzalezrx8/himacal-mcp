"""Calendar behavior against a disposable local CalDAV provider."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import caldav
import pytest
from icalendar import Calendar, Event, Timezone

from himacal.calendars import CalendarService, EventInput, EventPatch
from himacal.config import AccountConfig, CalDAVConfig, Settings
from himacal.errors import ProviderError


@pytest.fixture(scope="module")
def provider(tmp_path_factory):
    root = tmp_path_factory.mktemp("radicale")
    passwords = root / "users"
    passwords.write_text("alice:test\nbob:test\n", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "radicale",
            "--hosts",
            f"127.0.0.1:{port}",
            "--auth-type",
            "htpasswd",
            "--auth-htpasswd-filename",
            str(passwords),
            "--auth-htpasswd-encryption",
            "plain",
            "--storage-filesystem-folder",
            str(root / "storage"),
            "--logging-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Radicale did not start")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Radicale did not become ready")
        url = f"http://127.0.0.1:{port}/"
        clients = {}
        calendars = {}
        accounts = {}
        for name in ("alice", "bob"):
            password = root / f"{name}.secret"
            password.write_text("test", encoding="utf-8")
            clients[name] = caldav.DAVClient(
                url=url,
                username=name,
                password="test",
                require_tls=False,
                enable_rfc6764=False,
                rate_limit_handle=False,
            )
            calendars[name] = clients[name].principal().make_calendar(name="Work", cal_id="work")
            accounts[name] = AccountConfig(
                caldav=CalDAVConfig(
                    url=url,
                    username=name,
                    password_file=password,
                    timezone="America/New_York",
                )
            )
        yield Settings(accounts=accounts, mcp_password="example"), calendars
        for client in clients.values():
            client.close()
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def service(provider):
    settings, _ = provider
    instance = CalendarService(settings)
    yield instance
    instance.close()


def error_code(call, expected):
    with pytest.raises(ProviderError) as exc:
        call()
    assert exc.value.code == expected


def calendar_id(service, account="alice"):
    calendars = service.list_calendars(account)["calendars"]
    assert len(calendars) == 1
    return calendars[0]["id"]


def event(
    summary="Focus", start="2026-02-01T10:00:00-05:00", end="2026-02-01T11:00:00-05:00", **kwargs
):
    return EventInput(summary=summary, start=start, end=end, **kwargs)


def test_timezone_update_requires_explicit_start_and_end(service):
    selected = calendar_id(service)
    original = service.create_event("alice", selected, event())
    error_code(
        lambda: service.update_event(
            "alice",
            selected,
            original["uid"],
            original["etag"],
            EventPatch(timezone="America/Los_Angeles"),
            "series",
        ),
        "invalid_input",
    )
    assert service.get_event("alice", selected, original["uid"])["etag"] == original["etag"]


@pytest.mark.parametrize("exception_kind", ["override", "RDATE", "EXDATE"])
def test_series_date_type_change_does_not_orphan_recurrence_metadata(
    service, provider, exception_kind
):
    _, calendars = provider
    selected = calendar_id(service)
    original = service.create_event("alice", selected, event(recurrence_rule="FREQ=DAILY;COUNT=3"))
    if exception_kind == "override":
        original = service.update_event(
            "alice",
            selected,
            original["uid"],
            original["etag"],
            EventPatch(start="2026-02-02T12:00:00-05:00", end="2026-02-02T13:00:00-05:00"),
            "occurrence",
            "2026-02-02T10:00:00-05:00",
        )
    else:
        resource = calendars["alice"].get_event_by_uid(original["uid"])
        with resource.edit_icalendar_instance() as parsed:
            parsed.walk("VEVENT")[0].add(exception_kind, datetime(2026, 2, 2, 15, tzinfo=UTC))
        resource.save(precondition="etag")
        original = service.get_event("alice", selected, original["uid"])
    error_code(
        lambda: service.update_event(
            "alice",
            selected,
            original["uid"],
            original["etag"],
            EventPatch(start="2026-02-01", end="2026-02-02"),
            "series",
        ),
        "invalid_input",
    )
    assert service.get_event("alice", selected, original["uid"])["etag"] == original["etag"]


def test_account_calendar_routing_and_etag_conflicts(service):
    alice = calendar_id(service)
    bob = calendar_id(service, "bob")
    assert alice != bob
    error_code(
        lambda: service.list_events("bob", alice, "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"),
        "calendar_not_found",
    )
    created = service.create_event("alice", alice, event())
    uid, etag = created["uid"], created["etag"]
    assert etag and not etag.startswith("W/")
    read = service.get_event("alice", alice, uid)
    assert read["event"]["summary"] == "Focus"
    listed = service.list_events("alice", alice, "2026-02-01T14:00:00Z", "2026-02-01T16:00:00Z")
    assert next(item for item in listed["events"] if item["uid"] == uid)["recurrence_id"] is None
    after_end = service.list_events("alice", alice, "2026-02-01T16:00:00Z", "2026-02-01T17:00:00Z")
    assert all(item["uid"] != uid for item in after_end["events"])
    updated = service.update_event(
        "alice", alice, uid, etag, EventPatch(summary="Changed"), scope="series"
    )
    error_code(lambda: service.delete_event("alice", alice, uid, etag, scope="series"), "conflict")
    assert service.get_event("alice", alice, uid)["event"]["summary"] == "Changed"
    service.delete_event("alice", alice, uid, updated["etag"], scope="series")
    error_code(lambda: service.get_event("alice", alice, uid), "event_not_found")


def test_date_validation_all_day_and_bounded_search(service):
    cal = calendar_id(service)
    for start, end, zone in [
        ("2026-03-08T02:30:00", "2026-03-08T03:30:00", "America/New_York"),
        ("2026-11-01T01:30:00", "2026-11-01T02:30:00", "America/New_York"),
        ("2026-02-01", "2026-02-01", None),
        ("2026-02-01", "2026-02-01T11:00:00Z", None),
    ]:
        error_code(
            lambda start=start, end=end, zone=zone: service.create_event(
                "alice", cal, event(start=start, end=end, timezone=zone)
            ),
            "invalid_input",
        )
    created = service.create_event("alice", cal, event(start="2026-05-01", end="2026-05-03"))
    read = service.get_event("alice", cal, created["uid"])["event"]
    assert (read["start"], read["end"], read["all_day"]) == ("2026-05-01", "2026-05-03", True)
    error_code(
        lambda: service.list_events("alice", cal, "2026-01-01T00:00:00Z", "2028-01-01T00:00:00Z"),
        "invalid_input",
    )
    service.delete_event("alice", cal, created["uid"], created["etag"], scope="series")


def test_occurrence_move_cancel_restore_preserves_parent(service, provider):
    _, calendars = provider
    parent = Calendar()
    parent.add("prodid", "-//Calendar test//EN")
    parent.add("version", "2.0")
    parent.add_component(Timezone.from_tzinfo(ZoneInfo("America/New_York")))
    master = Event()
    master.add("uid", "recurring-preservation@example.test")
    master.add("dtstamp", datetime.fromisoformat("2026-01-01T00:00:00+00:00"))
    master.add(
        "dtstart",
        datetime.fromisoformat("2026-02-01T10:00:00").replace(tzinfo=ZoneInfo("America/New_York")),
    )
    master.add(
        "dtend",
        datetime.fromisoformat("2026-02-01T11:00:00").replace(tzinfo=ZoneInfo("America/New_York")),
    )
    master.add("summary", "Daily")
    master.add("rrule", {"freq": "daily", "count": 3})
    master.add("x-preserve", "keep me")
    parent.add_component(master)
    calendars["alice"].add_event(parent.to_ical().decode(), no_overwrite=True)
    cal = calendar_id(service)
    uid = "recurring-preservation@example.test"
    result = service.list_events("alice", cal, "2026-02-01T00:00:00Z", "2026-02-05T00:00:00Z")
    occurrences = [item for item in result["events"] if item["uid"] == uid]
    assert len(occurrences) == 3
    original = occurrences[1]["recurrence_id"]
    assert original.startswith("2026-02-02T10:00:00")
    moved = service.update_event(
        "alice",
        cal,
        uid,
        occurrences[1]["etag"],
        EventPatch(
            start="2026-02-02T15:00:00-05:00",
            end="2026-02-02T16:00:00-05:00",
        ),
        scope="occurrence",
        recurrence_id=original,
    )
    again = service.list_events("alice", cal, "2026-02-01T00:00:00Z", "2026-02-05T00:00:00Z")
    changed = next(
        item for item in again["events"] if item["uid"] == uid and item["recurrence_id"] == original
    )
    assert datetime.fromisoformat(changed["start"]) == datetime.fromisoformat(
        "2026-02-02T15:00:00-05:00"
    )
    cancelled = service.delete_event(
        "alice", cal, uid, moved["etag"], scope="occurrence", recurrence_id=original
    )
    again = service.list_events("alice", cal, "2026-02-01T00:00:00Z", "2026-02-05T00:00:00Z")
    assert not any(
        item["uid"] == uid and item["recurrence_id"] == original for item in again["events"]
    )
    restored = service.update_event(
        "alice",
        cal,
        uid,
        cancelled["etag"],
        EventPatch(summary="Restored"),
        scope="occurrence",
        recurrence_id=original,
    )
    read = service.get_event("alice", cal, uid)
    ical = Calendar.from_ical(read["ical"])
    assert any(item.name == "VTIMEZONE" for item in ical.subcomponents)
    saved_master = next(item for item in ical.walk("VEVENT") if not item.get("RECURRENCE-ID"))
    assert str(saved_master["X-PRESERVE"]) == "keep me"
    assert "RRULE" in saved_master
    again = service.list_events("alice", cal, "2026-02-01T00:00:00Z", "2026-02-05T00:00:00Z")
    assert any(
        item["uid"] == uid and item["recurrence_id"] == original and item["summary"] == "Restored"
        for item in again["events"]
    )
    service.delete_event("alice", cal, uid, restored["etag"], scope="series")


def test_scheduling_events_are_readable_but_never_written(service, provider):
    _, calendars = provider
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Calendar test//EN\r\nBEGIN:VEVENT\r\n"
        "UID:scheduling@example.test\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:20260201T100000Z\r\n"
        "DTEND:20260201T110000Z\r\nSUMMARY:Meeting\r\nORGANIZER:mailto:owner@example.test\r\n"
        "ATTENDEE:mailto:guest@example.test\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendars["alice"].add_event(data, no_overwrite=True)
    cal = calendar_id(service)
    read = service.get_event("alice", cal, "scheduling@example.test")
    assert read["event"]["summary"] == "Meeting"
    error_code(
        lambda: service.update_event(
            "alice", cal, read["uid"], read["etag"], EventPatch(summary="No"), scope="series"
        ),
        "unsupported",
    )
    error_code(
        lambda: service.delete_event("alice", cal, read["uid"], read["etag"], scope="series"),
        "unsupported",
    )
    assert service.get_event("alice", cal, read["uid"])["event"]["summary"] == "Meeting"


def test_recurrence_tracks_dst_and_rejects_unbounded_results(service):
    cal = calendar_id(service)
    for rule in (
        "FREQ=NOT_A_FREQUENCY",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;COUNT=0",
        "FREQ=DAILY;BYMONTH=13",
        "FREQ=DAILY;COUNT=2;COUNT=3",
    ):
        error_code(
            lambda rule=rule: service.create_event("alice", cal, event(recurrence_rule=rule)),
            "invalid_input",
        )
    created = service.create_event(
        "alice",
        cal,
        event(
            start="2026-03-07T09:00:00",
            end="2026-03-07T10:00:00",
            timezone="America/New_York",
            recurrence_rule="FREQ=DAILY;COUNT=3",
        ),
    )
    found = service.list_events("alice", cal, "2026-03-07T00:00:00Z", "2026-03-11T00:00:00Z")
    starts = [item["start"] for item in found["events"] if item["uid"] == created["uid"]]
    assert starts == [
        "2026-03-07T09:00:00-05:00",
        "2026-03-08T09:00:00-04:00",
        "2026-03-09T09:00:00-04:00",
    ]
    error_code(
        lambda: service.list_events(
            "alice", cal, "2026-03-07T00:00:00Z", "2026-03-11T00:00:00Z", limit=2
        ),
        "limit_exceeded",
    )
    service.delete_event("alice", cal, created["uid"], created["etag"], scope="series")


def test_large_rdate_series_is_rejected_before_expansion(service, provider):
    _, calendars = provider
    cal = calendar_id(service)
    parsed = Calendar()
    parsed.add("version", "2.0")
    parsed.add("prodid", "-//Calendar test//EN")
    component = Event()
    component.add("uid", "many-dates@example.test")
    component.add("dtstamp", datetime(2026, 1, 1, tzinfo=UTC))
    component.add("dtstart", datetime(2026, 8, 1, 9, tzinfo=UTC))
    component.add("dtend", datetime(2026, 8, 1, 10, tzinfo=UTC))
    component.add(
        "rdate", [datetime(2026, 8, 2, tzinfo=UTC) + timedelta(minutes=n) for n in range(10001)]
    )
    parsed.add_component(component)
    calendars["alice"].add_event(parsed.to_ical().decode(), no_overwrite=True)
    error_code(
        lambda: service.list_events("alice", cal, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
        "limit_exceeded",
    )
    read = service.get_event("alice", cal, "many-dates@example.test")
    service.delete_event("alice", cal, read["uid"], read["etag"], scope="series")


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_provider_conflict_between_read_and_conditional_write(
    service, provider, monkeypatch, operation
):
    _, calendars = provider
    cal = calendar_id(service)
    created = service.create_event("alice", cal, event(summary="Before"))
    external = calendars["alice"].get_event_by_uid(created["uid"])
    external.load()
    modified = Calendar.from_ical(external.data)
    modified.walk("VEVENT")[0]["SUMMARY"] = "Another client"
    original_put = caldav.DAVClient.put
    original_delete = caldav.DAVClient.delete
    attempts = []

    def race(client, url, *args, **kwargs):
        attempts.append(url)
        original_put(
            external.client, str(external.url), modified.to_ical(), {"If-Match": external.etag}
        )
        if operation == "update":
            return original_put(client, url, *args, **kwargs)
        return original_delete(client, url, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(caldav.DAVClient, "put" if operation == "update" else "delete", race)
        if operation == "update":
            call = lambda: service.update_event(
                "alice",
                cal,
                created["uid"],
                created["etag"],
                EventPatch(summary="Ours"),
                scope="series",
            )
        else:
            call = lambda: service.delete_event(
                "alice", cal, created["uid"], created["etag"], scope="series"
            )
        error_code(call, "conflict")
    assert len(attempts) == 1
    current = service.get_event("alice", cal, created["uid"])
    assert current["event"]["summary"] == "Another client"
    service.delete_event("alice", cal, created["uid"], current["etag"], scope="series")


@pytest.mark.parametrize("provider_etag", [None, 'W/"weak"'])
def test_missing_or_weak_provider_etag_cannot_write(service, monkeypatch, provider_etag):
    cal = calendar_id(service)
    created = service.create_event("alice", cal, event())
    from caldav.calendarobjectresource import CalendarObjectResource

    with monkeypatch.context() as patch:
        patch.setattr(CalendarObjectResource, "etag", property(lambda _: provider_etag))
        assert service.get_event("alice", cal, created["uid"])["etag"] is None
        error_code(
            lambda: service.update_event(
                "alice",
                cal,
                created["uid"],
                created["etag"],
                EventPatch(summary="No"),
                scope="series",
            ),
            "etag_unavailable",
        )
        error_code(
            lambda: service.delete_event(
                "alice", cal, created["uid"], created["etag"], scope="series"
            ),
            "etag_unavailable",
        )
    assert service.get_event("alice", cal, created["uid"])["event"]["summary"] == "Focus"
    service.delete_event("alice", cal, created["uid"], created["etag"], scope="series")


def test_response_lost_after_write_reports_unknown_without_retry(service, monkeypatch):
    cal = calendar_id(service)
    created = service.create_event("alice", cal, event(summary="Before"))
    original_put = caldav.DAVClient.put
    attempts = []

    def response_lost(client, url, *args, **kwargs):
        attempts.append(url)
        original_put(client, url, *args, **kwargs)
        raise OSError("private provider details")

    with monkeypatch.context() as patch:
        patch.setattr(caldav.DAVClient, "put", response_lost)
        error_code(
            lambda: service.update_event(
                "alice",
                cal,
                created["uid"],
                created["etag"],
                EventPatch(summary="Accepted"),
                scope="series",
            ),
            "outcome_unknown",
        )
    assert len(attempts) == 1
    saved = service.get_event("alice", cal, created["uid"])
    assert saved["event"]["summary"] == "Accepted"
    service.delete_event("alice", cal, created["uid"], saved["etag"], scope="series")


def test_missing_fresh_etag_after_multiget_does_not_reuse_search_etag(
    service, provider, monkeypatch
):
    _, calendars = provider
    cal = calendar_id(service)
    created = service.create_event("alice", cal, event())
    resource = calendars["alice"].get_event_by_uid(created["uid"])
    target_url = str(resource.url)
    original_request = caldav.DAVClient.request
    from caldav.calendarobjectresource import CalendarObjectResource

    original_multiget = CalendarObjectResource._post_load_by_multiget

    def get_fails(client, url, method="GET", *args, **kwargs):
        if str(url) == target_url and method == "GET":
            raise OSError("GET not supported")
        return original_request(client, url, method, *args, **kwargs)

    def without_version(instance, items):
        return original_multiget(instance, [(url, data, {}) for url, data, tags in items])

    with monkeypatch.context() as patch:
        patch.setattr(caldav.DAVClient, "request", get_fails)
        patch.setattr(CalendarObjectResource, "_post_load_by_multiget", without_version)
        assert service.get_event("alice", cal, created["uid"])["etag"] is None
        error_code(
            lambda: service.update_event(
                "alice",
                cal,
                created["uid"],
                created["etag"],
                EventPatch(summary="No"),
                scope="series",
            ),
            "etag_unavailable",
        )
    assert service.get_event("alice", cal, created["uid"])["event"]["summary"] == "Focus"
    service.delete_event("alice", cal, created["uid"], created["etag"], scope="series")
