"""Bounded CalDAV event operations with conditional writes."""

from __future__ import annotations

import copy
import hashlib
import posixpath
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
import recurring_ical_events
from caldav.elements import dav
from caldav.lib import error as dav_error
from dateutil.rrule import rrulestr
from dateutil.tz import datetime_ambiguous, datetime_exists
from icalendar import Calendar, Event, Timezone, vRecur
from pydantic import BaseModel, ConfigDict

from himacal.errors import ProviderError
from himacal.tools import register_tool

MAX_WINDOW = timedelta(days=366)
MAX_RESULTS = 1000
MAX_RECURRENCE_CANDIDATES = 10000
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"\Z')


class EventInput(BaseModel):
    """A timed or all-day event; an all-day end is exclusive."""

    model_config = ConfigDict(extra="forbid")
    summary: str
    start: str
    end: str
    timezone: str | None = None
    description: str | None = None
    location: str | None = None
    recurrence_rule: str | None = None


class EventPatch(BaseModel):
    """Fields to change; timezone requires start and end. Null text/rule clears it."""

    model_config = ConfigDict(extra="forbid")
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    timezone: str | None = None
    description: str | None = None
    location: str | None = None
    recurrence_rule: str | None = None


class _DAVClient(caldav.DAVClient):
    def _redirect_target(self, method, url_obj, status_code, headers):
        if method.upper() in {"PUT", "DELETE"}:
            return None
        return super()._redirect_target(method, url_obj, status_code, headers)


def _invalid(message: str) -> ProviderError:
    return ProviderError("invalid_input", message)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise _invalid("Unknown IANA timezone.") from None


def _when(value: str, zone_name: str | None, default_zone: str) -> date | datetime:
    if not isinstance(value, str):
        raise _invalid("Dates and times must be ISO 8601 strings.")
    try:
        if _DATE.fullmatch(value):
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid("Dates and times must be ISO 8601 strings.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(zone_name or default_zone))
        if not datetime_exists(parsed) or datetime_ambiguous(parsed):
            raise _invalid("Local time is ambiguous or does not exist; provide an offset.")
    elif zone_name:
        localized = parsed.astimezone(_zone(zone_name))
        if localized.replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise _invalid("The offset does not match the specified timezone.")
        parsed = localized
    else:
        # Numeric offsets are encoded in UTC; they are not valid TZID definitions.
        parsed = parsed.astimezone(UTC)
    return parsed


def _times(start: str, end: str, zone_name: str | None, default_zone: str):
    first = _when(start, zone_name, default_zone)
    last = _when(end, zone_name, default_zone)
    _ordered(first, last)
    return first, last


def _ordered(first: date | datetime, last: date | datetime):
    if isinstance(first, datetime) != isinstance(last, datetime):
        raise _invalid("Start and end must both be dates or both be datetimes.")
    if isinstance(first, datetime):
        ordered = last.astimezone(UTC) > first.astimezone(UTC)
    else:
        ordered = last > first
    if not ordered:
        raise _invalid("End must be after start; all-day ends are exclusive.")


def _validate_rule(value: str, start: date | datetime) -> str:
    rule = value.removeprefix("RRULE:")
    if not rule or len(rule) > 2048 or "\n" in rule or "\r" in rule:
        raise _invalid("Invalid recurrence rule.")
    try:
        parts = vRecur.from_ical(rule)
        allowed = {
            "FREQ",
            "UNTIL",
            "COUNT",
            "INTERVAL",
            "BYSECOND",
            "BYMINUTE",
            "BYHOUR",
            "BYDAY",
            "BYMONTHDAY",
            "BYYEARDAY",
            "BYWEEKNO",
            "BYMONTH",
            "BYSETPOS",
            "WKST",
        }
        keys = [part.split("=", 1)[0].upper() for part in rule.split(";")]
        if (
            len(set(keys)) != len(keys)
            or set(parts) - allowed
            or "FREQ" not in parts
            or ("COUNT" in parts and "UNTIL" in parts)
        ):
            raise ValueError
        for key in ("COUNT", "INTERVAL"):
            if key in parts and (len(parts[key]) != 1 or int(parts[key][0]) <= 0):
                raise ValueError
        ranges = {
            "BYSECOND": (0, 59),
            "BYMINUTE": (0, 59),
            "BYHOUR": (0, 23),
            "BYMONTH": (1, 12),
            "BYMONTHDAY": (-31, 31),
            "BYYEARDAY": (-366, 366),
            "BYWEEKNO": (-53, 53),
            "BYSETPOS": (-366, 366),
        }
        for key, (minimum, maximum) in ranges.items():
            for number in parts.get(key, []):
                value = int(str(number))
                if not minimum <= value <= maximum or (minimum < 0 and value == 0):
                    raise ValueError
        initial = start if isinstance(start, datetime) else datetime.combine(start, time.min)
        rrulestr(rule, dtstart=initial)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise _invalid("Invalid recurrence rule.") from None
    return rule


def _strong_etag(etag) -> bool:
    return isinstance(etag, str) and bool(_STRONG_ETAG.fullmatch(etag))


def _id(account_id: str, calendar) -> str:
    return hashlib.sha256(f"{account_id}\0{calendar.url}".encode()).hexdigest()


def _iso(component, key):
    value = component.decoded(key, None)
    return value.isoformat() if isinstance(value, (date, datetime)) else None


def _info(component) -> dict:
    value = component.decoded("DTSTART", None)
    end = component.decoded("DTEND", None)
    if end is None and value is not None and component.get("DURATION") is not None:
        end = value + component.decoded("DURATION")
    return {
        "uid": str(component.get("UID", "")),
        "summary": str(component.get("SUMMARY", "")),
        "start": value.isoformat() if isinstance(value, (date, datetime)) else None,
        "end": end.isoformat() if isinstance(end, (date, datetime)) else None,
        "all_day": isinstance(value, date) and not isinstance(value, datetime),
        "description": str(component["DESCRIPTION"]) if "DESCRIPTION" in component else None,
        "location": str(component["LOCATION"]) if "LOCATION" in component else None,
        "recurrence_id": _iso(component, "RECURRENCE-ID"),
        "recurrence_rule": component["RRULE"].to_ical().decode() if "RRULE" in component else None,
        "status": str(component["STATUS"]) if "STATUS" in component else None,
    }


def _identity(value: date | datetime, default_zone: str):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_zone(default_zone))
        return value.astimezone(UTC)
    return value


def _same_recurrence(left, right, default_zone: str) -> bool:
    return type(left) is type(right) and _identity(left, default_zone) == _identity(
        right, default_zone
    )


def _recurring(master) -> bool:
    return any(key in master for key in ("RRULE", "RDATE", "EXDATE"))


def _check_expansion(calendar, end: datetime, default_zone: str):
    components = list(calendar.walk("VEVENT"))
    count = sum("RECURRENCE-ID" in component for component in components)
    for component in components:
        for key in ("RDATE", "EXDATE"):
            values = component.get(key, [])
            if not isinstance(values, list):
                values = [values]
            count += sum(len(value.dts) for value in values)
        if count > MAX_RECURRENCE_CANDIDATES:
            raise ProviderError(
                "limit_exceeded", "Recurrence expansion exceeds the supported limit."
            )
        if "RRULE" not in component:
            continue
        initial = component.decoded("DTSTART")
        if isinstance(initial, date) and not isinstance(initial, datetime):
            initial = datetime.combine(initial, time.min)
        try:
            text = _validate_rule(component["RRULE"].to_ical().decode(), initial)
        except ProviderError:
            raise ProviderError(
                "provider_error", "The provider returned an invalid recurrence rule."
            ) from None
        rule = rrulestr(text, dtstart=initial)
        boundary = (
            end.astimezone(_zone(default_zone)).replace(tzinfo=None)
            if initial.tzinfo is None
            else end.astimezone(initial.tzinfo)
        )
        for occurrence in rule:
            if occurrence > boundary:
                break
            count += 1
            if count > MAX_RECURRENCE_CANDIDATES:
                raise ProviderError(
                    "limit_exceeded", "Recurrence expansion exceeds the supported limit."
                )


def _expand(calendar, start: datetime, end: datetime, default_zone: str):
    _check_expansion(calendar, end, default_zone)
    copy_for_search = copy.deepcopy(calendar)
    if "X-WR-TIMEZONE" not in copy_for_search:
        copy_for_search.add("X-WR-TIMEZONE", default_zone)
    return recurring_ical_events.of(copy_for_search).between(start, end)


def _check_no_scheduling(calendar):
    if "METHOD" in calendar or any(
        "ORGANIZER" in part or "ATTENDEE" in part for part in calendar.walk()
    ):
        raise ProviderError(
            "unsupported",
            "Scheduling events cannot be modified because writes can send invitations or replies.",
        )


def _ensure_timezones(calendar, component):
    existing = {str(part.get("TZID", "")) for part in calendar.walk("VTIMEZONE")}
    for field in ("DTSTART", "DTEND"):
        value = component.decoded(field, None)
        zone = value.tzinfo if isinstance(value, datetime) else None
        if isinstance(zone, ZoneInfo) and zone.key not in existing and zone.key != "UTC":
            # shortcut: transitions cover 100 years beyond the event; extend if longer horizons are needed.
            first_year = max(1, min(1970, value.year - 1))
            last_year = min(9999, max(2100, value.year + 101))
            calendar.add_component(
                Timezone.from_tzinfo(
                    zone, first_date=date(first_year, 1, 1), last_date=date(last_year, 1, 1)
                )
            )
            existing.add(zone.key)


def _check_resource_url(calendar, resource):
    parent, child = urlsplit(str(calendar.url)), urlsplit(str(resource.url))
    parent_path = posixpath.normpath(unquote(parent.path))
    child_path = posixpath.normpath(unquote(child.path))
    if (
        (parent.scheme, parent.netloc) != (child.scheme, child.netloc)
        or posixpath.dirname(child_path) != parent_path
        or child.query
        or child.fragment
    ):
        raise ProviderError("event_not_found", "Event was not found in this calendar.")


def _master(calendar, uid: str):
    events = list(calendar.walk("VEVENT"))
    if not events or any(str(part.get("UID", "")) != uid for part in events):
        raise ProviderError("event_not_found", "Event was not found in this calendar.")
    masters = [part for part in events if "RECURRENCE-ID" not in part]
    if len(masters) != 1:
        raise ProviderError("unsupported", "This event resource has no unique series parent.")
    return masters[0]


def _apply(component, patch: EventPatch, default_zone: str):
    fields = patch.model_fields_set
    if "timezone" in fields and not {"start", "end"}.issubset(fields):
        raise _invalid("Changing timezone requires explicit start and end values.")
    if "summary" in fields:
        if patch.summary is None:
            raise _invalid("Summary cannot be null.")
        component["SUMMARY"] = patch.summary
    for field in ("description", "location"):
        if field in fields:
            component.pop(field.upper(), None)
            if getattr(patch, field) is not None:
                component.add(field, getattr(patch, field))
    if "start" in fields or "end" in fields:
        if ("start" in fields and patch.start is None) or ("end" in fields and patch.end is None):
            raise _invalid("Start and end cannot be null.")
        current_start = component.decoded("DTSTART", None)
        current_end = component.decoded("DTEND", None)
        duration = component.decoded("DURATION", None)
        start = (
            _when(patch.start, patch.timezone, default_zone) if "start" in fields else current_start
        )
        end = _when(patch.end, patch.timezone, default_zone) if "end" in fields else current_end
        if end is None and duration is not None:
            end = start + duration
        if start is None or end is None:
            raise _invalid("Specify both start and end for this event.")
        _ordered(start, end)
        if "start" in fields:
            component.pop("DTSTART", None)
            component.add("DTSTART", start)
        if "end" in fields:
            component.pop("DTEND", None)
            component.pop("DURATION", None)
            component.add("DTEND", end)
    if "recurrence_rule" in fields:
        if patch.recurrence_rule is not None:
            rule = _validate_rule(patch.recurrence_rule, component.decoded("DTSTART"))
            component.pop("RRULE", None)
            component.add("RRULE", rule)
        else:
            component.pop("RRULE", None)


class CalendarService:
    def __init__(self, settings):
        self.settings = settings

    def close(self):
        """Connections are closed at the end of each operation."""

    @contextmanager
    def _account(self, account_id: str):
        account = self.settings.require_account(account_id, "caldav").caldav
        try:
            password = account.password_file.read_text(encoding="utf-8").strip()
            if not password:
                raise OSError
            client = _DAVClient(
                url=account.url,
                username=account.username,
                password=password,
                timeout=self.settings.command_timeout,
                require_tls=False,
                rate_limit_handle=False,
            )
        except Exception:  # noqa: BLE001 - provider errors must not disclose credentials.
            raise ProviderError(
                "provider_error", "Could not initialize the calendar provider."
            ) from None
        try:
            yield account, client
        except ProviderError:
            raise
        except dav_error.NotFoundError:
            raise ProviderError(
                "event_not_found", "Event was not found in this calendar."
            ) from None
        except Exception:  # noqa: BLE001 - provider diagnostics are not safe output.
            raise ProviderError("provider_error", "The calendar provider request failed.") from None
        finally:
            client.close()

    def _calendar(self, client, account_id, calendar_id):
        for calendar in client.principal().calendars():
            if _id(account_id, calendar) == calendar_id:
                return calendar
        raise ProviderError("calendar_not_found", "Calendar was not found on this account.")

    def _resource(self, calendar, uid):
        if not isinstance(uid, str) or not uid or len(uid) > 1024 or any(ord(c) < 32 for c in uid):
            raise _invalid("Invalid event UID.")
        resource = calendar.get_event_by_uid(uid)
        _check_resource_url(calendar, resource)
        resource.props.pop(dav.GetEtag.tag, None)
        resource.load()
        _check_resource_url(calendar, resource)
        parsed = Calendar.from_ical(resource.data)
        master = _master(parsed, uid)
        return resource, parsed, master

    def _version(self, resource, expected_etag):
        if not _strong_etag(expected_etag):
            raise _invalid("A strong expected ETag is required.")
        if not _strong_etag(resource.etag):
            raise ProviderError(
                "etag_unavailable", "The provider did not return a strong event ETag."
            )
        if expected_etag != resource.etag:
            raise ProviderError("conflict", "The event changed since it was read.")

    def _write(self, action):
        try:
            action()
        except (
            dav_error.ETagMismatchError,
            dav_error.ScheduleTagMismatchError,
            dav_error.NotFoundError,
        ):
            raise ProviderError("conflict", "The event changed since it was read.") from None
        except dav_error.DAVError as exc:
            if exc.status in (409, 412):
                raise ProviderError("conflict", "The event changed since it was read.") from None
            if exc.status is not None and 400 <= exc.status < 500:
                raise ProviderError(
                    "provider_error", "The calendar provider rejected the write."
                ) from None
            raise ProviderError(
                "outcome_unknown",
                "The provider may have accepted the write; check the calendar before retrying.",
            ) from None
        except Exception:  # noqa: BLE001 - an interrupted write may have been accepted.
            raise ProviderError(
                "outcome_unknown",
                "The provider may have accepted the write; check the calendar before retrying.",
            ) from None

    def _read_result(self, calendar, account_id, calendar_id, uid):
        resource, parsed, master = self._resource(calendar, uid)
        return {
            "account_id": account_id,
            "calendar_id": calendar_id,
            "uid": uid,
            "etag": resource.etag if _strong_etag(resource.etag) else None,
            "event": _info(master),
            "exceptions": [
                _info(part) for part in parsed.walk("VEVENT") if "RECURRENCE-ID" in part
            ],
            "ical": resource.data,
        }

    def list_calendars(self, account_id: str) -> dict:
        """List discovered calendars available to one configured account."""
        with self._account(account_id) as (_, client):
            calendars = client.principal().calendars()
            return {
                "account_id": account_id,
                "calendars": [
                    {
                        "id": _id(account_id, calendar),
                        "name": calendar.get_display_name() or "Unnamed calendar",
                    }
                    for calendar in calendars
                ],
            }

    def list_events(
        self,
        account_id: str,
        calendar_id: str,
        start: str,
        end: str,
        limit: int = MAX_RESULTS,
        query: str | None = None,
    ) -> dict:
        """Search a calendar in a bounded window, expanding recurring events."""
        with self._account(account_id) as (account, client):
            first, last = _times(start, end, None, account.timezone)
            if not isinstance(first, datetime):
                first = datetime.combine(first, time.min, _zone(account.timezone))
                last = datetime.combine(last, time.min, _zone(account.timezone))
            first, last = first.astimezone(UTC), last.astimezone(UTC)
            if last - first > MAX_WINDOW or not 1 <= limit <= MAX_RESULTS:
                raise _invalid("Search windows are limited to 366 days and 1,000 results.")
            calendar = self._calendar(client, account_id, calendar_id)
            resources = calendar.search(
                start=first, end=last, event=True, expand=False, post_filter=False
            )
            events = []
            for candidate in resources:
                _check_resource_url(calendar, candidate)
                candidate.props.pop(dav.GetEtag.tag, None)
                candidate.load()
                _check_resource_url(calendar, candidate)
                parsed = Calendar.from_ical(candidate.data)
                parts = list(parsed.walk("VEVENT"))
                if not parts:
                    continue
                uid = str(parts[0].get("UID", ""))
                master = _master(parsed, uid)
                for component in _expand(parsed, first, last, account.timezone):
                    if str(component.get("STATUS", "")).upper() == "CANCELLED":
                        continue
                    info = _info(component)
                    value = component.decoded("DTSTART", None)
                    if value is None:
                        continue
                    converted = _identity(value, account.timezone)
                    if isinstance(converted, date) and not isinstance(converted, datetime):
                        converted = datetime.combine(
                            converted, time.min, _zone(account.timezone)
                        ).astimezone(UTC)
                    if converted >= last:
                        continue
                    end_value = component.decoded("DTEND", None)
                    if end_value is None and component.get("DURATION") is not None:
                        end_value = value + component.decoded("DURATION")
                    if (
                        end_value is None
                        and isinstance(value, date)
                        and not isinstance(value, datetime)
                    ):
                        end_value = value + timedelta(days=1)
                    if end_value is not None:
                        end_value = _identity(end_value, account.timezone)
                        if isinstance(end_value, date) and not isinstance(end_value, datetime):
                            end_value = datetime.combine(
                                end_value, time.min, _zone(account.timezone)
                            ).astimezone(UTC)
                        if end_value <= first:
                            continue
                    elif converted < first:
                        continue
                    if not _recurring(master):
                        info["recurrence_id"] = None
                    if (
                        query
                        and query.casefold()
                        not in " ".join(
                            filter(None, (info["summary"], info["description"], info["location"]))
                        ).casefold()
                    ):
                        continue
                    info["etag"] = candidate.etag if _strong_etag(candidate.etag) else None
                    events.append(info)
                    if len(events) > limit:
                        raise ProviderError(
                            "limit_exceeded",
                            "Search returned more events than the requested limit.",
                        )
            events.sort(
                key=lambda item: (item["start"] or "", item["uid"], item["recurrence_id"] or "")
            )
            return {"account_id": account_id, "calendar_id": calendar_id, "events": events}

    def get_event(self, account_id: str, calendar_id: str, uid: str) -> dict:
        """Get the complete event series, exceptions, and its current ETag."""
        with self._account(account_id) as (_, client):
            return self._read_result(
                self._calendar(client, account_id, calendar_id), account_id, calendar_id, uid
            )

    def create_event(self, account_id: str, calendar_id: str, event: EventInput) -> dict:
        """Create an event. Invitation and attendee fields are not supported."""
        with self._account(account_id) as (account, client):
            first, last = _times(event.start, event.end, event.timezone, account.timezone)
            rule = (
                _validate_rule(event.recurrence_rule, first)
                if event.recurrence_rule is not None
                else None
            )
            parsed, component = Calendar(), Event()
            parsed.add("PRODID", "-//Himacal MCP//EN")
            parsed.add("VERSION", "2.0")
            uid = str(uuid.uuid4())
            for key, value in (
                ("UID", uid),
                ("DTSTAMP", datetime.now(UTC)),
                ("SUMMARY", event.summary),
                ("DTSTART", first),
                ("DTEND", last),
                ("DESCRIPTION", event.description),
                ("LOCATION", event.location),
                ("RRULE", rule),
            ):
                if value is not None:
                    component.add(key, value)
            parsed.add_component(component)
            _ensure_timezones(parsed, component)
            calendar = self._calendar(client, account_id, calendar_id)
            resource = caldav.Event(client=client, parent=calendar, data=parsed.to_ical())
            self._write(lambda: resource.save(raw_data=parsed.to_ical(), no_overwrite=True))
            try:
                return self._read_result(calendar, account_id, calendar_id, uid)
            except Exception:  # noqa: BLE001 - a successful write must not be retried blindly.
                raise ProviderError(
                    "outcome_unknown",
                    "The event was submitted but its saved version could not be read; check the calendar before retrying.",
                ) from None

    def _occurrence(self, parsed, master, recurrence_id, default_zone):
        if not _recurring(master) or not recurrence_id:
            raise _invalid("A recurring series and its original recurrence_id are required.")
        value = _when(recurrence_id, None, default_zone)
        initial = master.decoded("DTSTART", None)
        if type(value) is not type(initial):
            raise _invalid("Recurrence ID must have the same date or datetime type as the series.")
        for part in parsed.walk("VEVENT"):
            if "RECURRENCE-ID" in part and _same_recurrence(
                part.decoded("RECURRENCE-ID"), value, default_zone
            ):
                return part
        point = (
            value
            if isinstance(value, datetime)
            else datetime.combine(value, time.min, _zone(default_zone))
        )
        point = _identity(point, default_zone)
        for part in _expand(
            parsed, point - timedelta(seconds=1), point + timedelta(seconds=1), default_zone
        ):
            candidate = part.decoded("RECURRENCE-ID", None)
            if candidate is not None and _same_recurrence(candidate, value, default_zone):
                override = copy.deepcopy(part)
                for name in ("RRULE", "RDATE", "EXDATE"):
                    override.pop(name, None)
                parsed.add_component(override)
                return override
        raise ProviderError(
            "event_not_found", "That original occurrence was not found in the series."
        )

    def update_event(
        self,
        account_id: str,
        calendar_id: str,
        uid: str,
        expected_etag: str,
        event: EventPatch,
        scope: Literal["series", "occurrence"],
        recurrence_id: str | None = None,
    ) -> dict:
        """Update a complete series or restore and update one original occurrence."""
        with self._account(account_id) as (account, client):
            calendar = self._calendar(client, account_id, calendar_id)
            resource, parsed, master = self._resource(calendar, uid)
            self._version(resource, expected_etag)
            _check_no_scheduling(parsed)
            if scope == "occurrence":
                if "recurrence_rule" in event.model_fields_set:
                    raise _invalid("Recurrence rules can only be edited on a series.")
                component = self._occurrence(parsed, master, recurrence_id, account.timezone)
                component.pop("STATUS", None)
            elif scope == "series" and recurrence_id is None:
                component = master
            else:
                raise _invalid("Choose series or occurrence scope and its original recurrence ID.")
            previous_start = component.decoded("DTSTART", None)
            _apply(component, event, account.timezone)
            current_start = component.decoded("DTSTART", None)
            if (
                scope == "series"
                and isinstance(previous_start, datetime) != isinstance(current_start, datetime)
                and any(
                    any(key in part for key in ("RECURRENCE-ID", "RDATE", "EXDATE"))
                    for part in parsed.walk("VEVENT")
                )
            ):
                raise _invalid(
                    "Cannot convert timed/all-day dates while recurrence exceptions exist."
                )
            if "start" in event.model_fields_set and "RRULE" in component:
                _validate_rule(component["RRULE"].to_ical().decode(), current_start)
            _ensure_timezones(parsed, component)
            self._write(lambda: resource.save(raw_data=parsed.to_ical(), precondition="etag"))
            try:
                return self._read_result(calendar, account_id, calendar_id, uid)
            except Exception:  # noqa: BLE001 - a successful write must not be retried blindly.
                raise ProviderError(
                    "outcome_unknown",
                    "The update was submitted but its saved version could not be read; check the calendar before retrying.",
                ) from None

    def delete_event(
        self,
        account_id: str,
        calendar_id: str,
        uid: str,
        expected_etag: str,
        scope: Literal["series", "occurrence"],
        recurrence_id: str | None = None,
    ) -> dict:
        """Delete an entire event irreversibly, or cancel one restorable occurrence."""
        with self._account(account_id) as (account, client):
            calendar = self._calendar(client, account_id, calendar_id)
            resource, parsed, master = self._resource(calendar, uid)
            self._version(resource, expected_etag)
            _check_no_scheduling(parsed)
            if scope == "occurrence":
                component = self._occurrence(parsed, master, recurrence_id, account.timezone)
                component["STATUS"] = "CANCELLED"
                self._write(lambda: resource.save(raw_data=parsed.to_ical(), precondition="etag"))
                try:
                    return self._read_result(calendar, account_id, calendar_id, uid)
                except Exception:  # noqa: BLE001 - a successful write must not be retried blindly.
                    raise ProviderError(
                        "outcome_unknown",
                        "The cancellation was submitted but its saved version could not be read; check the calendar before retrying.",
                    ) from None
            if scope != "series" or recurrence_id is not None:
                raise _invalid("Choose series or occurrence scope and its original recurrence ID.")
            self._write(lambda: resource.delete(if_match=expected_etag))
            return {
                "account_id": account_id,
                "calendar_id": calendar_id,
                "uid": uid,
                "deleted": True,
                "reversible": False,
            }

    def register_tools(self, mcp):
        register_tool(mcp, self.list_calendars, name="calendar_list", read_only=True)
        register_tool(mcp, self.list_events, name="calendar_list_events", read_only=True)
        register_tool(mcp, self.get_event, name="calendar_get_event", read_only=True)
        register_tool(mcp, self.create_event, name="calendar_create_event")
        register_tool(mcp, self.update_event, name="calendar_update_event")
        register_tool(mcp, self.delete_event, name="calendar_delete_event", destructive=True)
