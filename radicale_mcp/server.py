"""Streamable HTTP MCP surface for calendars, tasks, and contacts in Radicale."""

from __future__ import annotations

import hmac
import os
import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import icalendar
import uvicorn
import vobject
from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .dav import Dav, DavError

mcp = MCPServer(
    "radicale-mcp",
    instructions=("IDs are DAV_URL-relative hrefs. Updates and deletes require the ETag returned by a get/list/create call. "
                  "Calendar searches return matching series resources, not expanded recurrence instances. "
                  "Update changes objects change only named fields; null removes an optional field."),
)


def _etag(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'"[\x21\x23-\x7e]*"', value):
        raise ValueError("expected_etag must be one strong quoted ETag")
    return value


def _limit(value: int) -> int:
    if not 1 <= value <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return value


def _text(value: Any, field: str, *, required: bool = False, multiline: bool = False) -> str:
    if multiline and isinstance(value, str):
        value = value.replace("\r\n", "\n")
    forbidden = "\x00\r" if multiline else "\x00\r\n"
    if not isinstance(value, str) or (required and not value.strip()) or any(c in value for c in forbidden):
        raise ValueError(f"Invalid {field}")
    return value


_UNSET = object()


def _dt(value: str, previous_native: Any = _UNSET) -> date | datetime:
    _text(value, "date/time", required=True)
    try:
        if "T" not in value and " " not in value:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            zone = previous_native.tzinfo if isinstance(previous_native, datetime) else ZoneInfo(os.getenv("MCP_TIMEZONE", "America/Sao_Paulo"))
            return parsed.replace(tzinfo=zone)
        # ISO offsets have no IANA TZID; UTC keeps the instant round-trippable in CalDAV.
        return parsed.astimezone(timezone.utc)
    except (ValueError, KeyError) as exc:
        raise ValueError("Use an ISO date or date-time with a valid timezone") from exc


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _range(start: date | datetime, end: date | datetime) -> None:
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ValueError("start and end must have the same date type")
    try:
        valid = end > start
    except TypeError as exc:
        raise ValueError("start and end must have compatible timezones") from exc
    if not valid:
        raise ValueError("end must be after start")


def _utc_range(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = _dt(value)
    if not isinstance(parsed, datetime):
        parsed = datetime.combine(parsed, datetime.min.time(), ZoneInfo(os.getenv("MCP_TIMEZONE", "America/Sao_Paulo")))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _calendar(data: str) -> icalendar.Calendar:
    try:
        return icalendar.Calendar.from_ical(data)
    except Exception as exc:
        raise DavError("Invalid iCalendar data from DAV") from exc


def _component(cal: icalendar.Calendar, kind: str) -> Any:
    parts = cal.walk("VEVENT" if kind == "event" else "VTODO")
    if not parts:
        raise DavError(f"DAV resource contains no {kind}")
    return next((part for part in parts if "RECURRENCE-ID" not in part), parts[0])


def _property(component: Any, name: str) -> str | None:
    value = component.get(name)
    return str(value) if value is not None else None


def _datetime_property(component: Any, name: str) -> str | None:
    value = component.get(name)
    return _iso(value.dt) if value is not None else None


def _event_view(item_id: str, etag: str, data: str, *, raw: bool = False) -> dict:
    cal = _calendar(data)
    part = _component(cal, "event")
    result = {
        "id": item_id, "etag": etag, "uid": _property(part, "UID"),
        "summary": _property(part, "SUMMARY"), "start": _datetime_property(part, "DTSTART"),
        "end": _datetime_property(part, "DTEND"), "description": _property(part, "DESCRIPTION"),
        "location": _property(part, "LOCATION"),
        "rrule": part["RRULE"].to_ical().decode() if "RRULE" in part else None,
        "recurrence_overrides": sum(1 for p in cal.walk("VEVENT") if "RECURRENCE-ID" in p),
    }
    if raw:
        result["raw_ical"] = data
    return result


def _task_view(item_id: str, etag: str, data: str, *, raw: bool = False) -> dict:
    cal = _calendar(data)
    part = _component(cal, "task")
    result = {
        "id": item_id, "etag": etag, "uid": _property(part, "UID"),
        "summary": _property(part, "SUMMARY"), "description": _property(part, "DESCRIPTION"),
        "start": _datetime_property(part, "DTSTART"), "due": _datetime_property(part, "DUE"),
        "status": _property(part, "STATUS"), "percent_complete": int(part["PERCENT-COMPLETE"]) if "PERCENT-COMPLETE" in part else None,
        "priority": int(part["PRIORITY"]) if "PRIORITY" in part else None,
        "completed_at": _datetime_property(part, "COMPLETED"),
        "rrule": part["RRULE"].to_ical().decode() if "RRULE" in part else None,
    }
    if raw:
        result["raw_ical"] = data
    return result


def _card(data: str) -> Any:
    try:
        return vobject.readOne(data)
    except Exception as exc:
        raise DavError("Invalid vCard data from DAV") from exc


def _card_values(card: Any, name: str) -> list[str]:
    return [str(line.value) for line in card.contents.get(name.lower(), [])]


def _company(card: Any) -> str | None:
    values = card.contents.get("org", [])
    if not values:
        return None
    value = values[0].value
    return str(value[0]) if isinstance(value, list) and value else str(value)


def _set_company(card: Any, company: str | None) -> None:
    if company is None:
        card.contents.pop("org", None)
    elif card.contents.get("org"):
        line = card.contents["org"][0]
        old = line.value
        line.value = [company, *old[1:]] if isinstance(old, list) else [company]
    else:
        card.add("org").value = [company]


def _contact_view(item_id: str, etag: str, data: str, *, raw: bool = False) -> dict:
    card = _card(data)
    result = {
        "id": item_id, "etag": etag,
        "uid": (_card_values(card, "uid") or [None])[0],
        "name": (_card_values(card, "fn") or [None])[0],
        "company": _company(card),
        "title": (_card_values(card, "title") or [None])[0],
        "emails": _card_values(card, "email"), "phones": _card_values(card, "tel"),
        "notes": (_card_values(card, "note") or [None])[0],
    }
    if raw:
        result["raw_vcard"] = data
    return result


def _set_cal(component: Any, key: str, value: Any) -> None:
    component.pop(key, None)
    if value is not None:
        component.add(key, value)


def _set_card(card: Any, key: str, value: Any) -> None:
    card.contents.pop(key.lower(), None)
    if value is None:
        return
    values = value if isinstance(value, list) else [value]
    for item in values:
        card.add(key).value = item


def _changes(changes: dict, allowed: set[str]) -> None:
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise ValueError(f"changes must contain only: {', '.join(sorted(allowed))}")


def _uid(value: str | None) -> str:
    return _text(value, "uid", required=True) if value is not None else str(uuid4())


@mcp.tool()
async def list_calendars() -> list[dict]:
    """List accessible calendars that support VEVENT."""
    return await Dav().collections("event")


@mcp.tool()
async def list_events(calendar_id: str, start: str | None = None, end: str | None = None, limit: int = 100) -> list[dict]:
    """List VEVENT series resources. Date filters use CalDAV REPORT; recurring instances are not expanded."""
    if start and end:
        _range(_dt(start), _dt(end))
    rows = await Dav().report(calendar_id, "event", _utc_range(start), _utc_range(end))
    return [_event_view(p["id"], p.get("etag") or "", p["data"]) for p in rows][:_limit(limit)]


@mcp.tool()
async def search_events(calendar_id: str, query: str, start: str | None = None, end: str | None = None, limit: int = 100) -> list[dict]:
    """Search VEVENT summary, description, location and UID within matching series resources."""
    needle = _text(query, "query", required=True).casefold()
    if start and end:
        _range(_dt(start), _dt(end))
    rows = await Dav().report(calendar_id, "event", _utc_range(start), _utc_range(end))
    matches = (_event_view(p["id"], p.get("etag") or "", p["data"]) for p in rows)
    return [row for row in matches if needle in " ".join(str(row.get(k) or "") for k in ("summary", "description", "location", "uid")).casefold()][:_limit(limit)]


@mcp.tool()
async def get_event(event_id: str) -> dict:
    """Get a VEVENT resource, its ETag, and full raw iCalendar including recurrence overrides."""
    data, etag = await Dav().get(event_id, "event")
    return _event_view(event_id, etag, data, raw=True)


@mcp.tool()
async def create_event(calendar_id: str, summary: str, start: str, end: str, description: str | None = None,
                       location: str | None = None, uid: str | None = None, rrule: str | None = None) -> dict:
    """Create an event; date-only end is exclusive. Recurrence rule uses RFC5545 syntax."""
    dav = Dav()
    await dav.check_collection(calendar_id, "event")
    begins, ends = _dt(start), _dt(end)
    _range(begins, ends)
    cal = icalendar.Calendar()
    cal.add("prodid", "-//radicale-mcp//EN")
    cal.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", _uid(uid))
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", _text(summary, "summary", required=True))
    event.add("dtstart", begins)
    event.add("dtend", ends)
    if description is not None:
        event.add("description", _text(description, "description", multiline=True))
    if location is not None:
        event.add("location", _text(location, "location"))
    if rrule is not None:
        event.add("rrule", icalendar.vRecur.from_ical(_text(rrule, "rrule", required=True)))
    cal.add_component(event)
    item_id = calendar_id + str(uuid4()) + ".ics"
    etag = await dav.put(item_id, cal.to_ical().decode(), kind="event", expected_etag=None)
    return await get_event(item_id)


@mcp.tool()
async def update_event(event_id: str, expected_etag: str, changes: dict[str, Any]) -> dict:
    """Patch the master VEVENT only. Allowed changes: summary, start, end, description, location, rrule. Omit to preserve; null clears optional fields. UID, overrides, VTIMEZONE and unknown properties remain."""
    _changes(changes, {"summary", "start", "end", "description", "location", "rrule"})
    dav = Dav()
    data, _ = await dav.get(event_id, "event")
    cal = _calendar(data)
    part = _component(cal, "event")
    if "RECURRENCE-ID" in part:
        raise ValueError("Cannot patch an override-only resource")
    if "summary" in changes:
        _set_cal(part, "SUMMARY", _text(changes["summary"], "summary", required=True))
    for key in ("description", "location"):
        if key in changes:
            value = changes[key]
            _set_cal(part, key.upper(), _text(value, key, multiline=(key == "description")) if value is not None else None)
    if "rrule" in changes:
        value = changes["rrule"]
        _set_cal(part, "RRULE", icalendar.vRecur.from_ical(_text(value, "rrule", required=True)) if value is not None else None)
    if "start" in changes or "end" in changes:
        if "start" in changes:
            previous = part.get("DTSTART")
            _set_cal(part, "DTSTART", _dt(changes["start"], previous.dt if previous is not None else _UNSET))
        if "end" in changes:
            previous = part.get("DTEND") or part.get("DTSTART")
            _set_cal(part, "DTEND", _dt(changes["end"], previous.dt if previous is not None else _UNSET) if changes["end"] is not None else None)
            if changes["end"] is not None:
                part.pop("DURATION", None)
        if "DTSTART" not in part:
            raise ValueError("event must have DTSTART")
        if "DTEND" in part:
            _range(part["DTSTART"].dt, part["DTEND"].dt)
    etag = await dav.put(event_id, cal.to_ical().decode(), kind="event", expected_etag=_etag(expected_etag))
    return await get_event(event_id)


@mcp.tool()
async def delete_event(event_id: str, expected_etag: str) -> dict:
    """Delete one VEVENT resource only if its ETag still matches."""
    await Dav().delete(event_id, "event", _etag(expected_etag))
    return {"deleted": True, "id": event_id}


@mcp.tool()
async def list_task_lists() -> list[dict]:
    """List accessible calendars that support VTODO."""
    return await Dav().collections("task")


@mcp.tool()
async def list_tasks(task_list_id: str, start: str | None = None, end: str | None = None, limit: int = 100) -> list[dict]:
    """List VTODO series resources; date range uses CalDAV REPORT without recurrence expansion."""
    if start and end:
        _range(_dt(start), _dt(end))
    rows = await Dav().report(task_list_id, "task", _utc_range(start), _utc_range(end))
    return [_task_view(p["id"], p.get("etag") or "", p["data"]) for p in rows][:_limit(limit)]


@mcp.tool()
async def search_tasks(task_list_id: str, query: str, start: str | None = None, end: str | None = None, limit: int = 100) -> list[dict]:
    """Search task summary, description, status and UID in matching series resources."""
    needle = _text(query, "query", required=True).casefold()
    if start and end:
        _range(_dt(start), _dt(end))
    rows = await Dav().report(task_list_id, "task", _utc_range(start), _utc_range(end))
    matches = (_task_view(p["id"], p.get("etag") or "", p["data"]) for p in rows)
    return [row for row in matches if needle in " ".join(str(row.get(k) or "") for k in ("summary", "description", "status", "uid")).casefold()][:_limit(limit)]


@mcp.tool()
async def get_task(task_id: str) -> dict:
    """Get a VTODO resource with its ETag and full raw iCalendar."""
    data, etag = await Dav().get(task_id, "task")
    return _task_view(task_id, etag, data, raw=True)


@mcp.tool()
async def create_task(task_list_id: str, summary: str, description: str | None = None, start: str | None = None,
                      due: str | None = None, priority: int | None = None, uid: str | None = None) -> dict:
    """Create a VTODO task. Dates are ISO; naive date-times use MCP_TIMEZONE."""
    dav = Dav()
    await dav.check_collection(task_list_id, "task")
    begins, ends = _dt(start) if start else None, _dt(due) if due else None
    if begins and ends:
        _range(begins, ends)
    if priority is not None and not 0 <= priority <= 9:
        raise ValueError("priority must be 0-9")
    cal = icalendar.Calendar()
    cal.add("prodid", "-//radicale-mcp//EN")
    cal.add("version", "2.0")
    task = icalendar.Todo()
    task.add("uid", _uid(uid))
    task.add("dtstamp", datetime.now(timezone.utc))
    task.add("summary", _text(summary, "summary", required=True))
    task.add("status", "NEEDS-ACTION")
    if description is not None:
        task.add("description", _text(description, "description", multiline=True))
    if begins:
        task.add("dtstart", begins)
    if ends:
        task.add("due", ends)
    if priority is not None:
        task.add("priority", priority)
    cal.add_component(task)
    item_id = task_list_id + str(uuid4()) + ".ics"
    await dav.put(item_id, cal.to_ical().decode(), kind="task", expected_etag=None)
    return await get_task(item_id)


@mcp.tool()
async def update_task(task_id: str, expected_etag: str, changes: dict[str, Any]) -> dict:
    """Patch VTODO master. Allowed changes: summary, description, start, due, status, percent_complete, priority. Omit preserves; null clears optional fields."""
    allowed = {"summary", "description", "start", "due", "status", "percent_complete", "priority"}
    _changes(changes, allowed)
    dav = Dav()
    data, _ = await dav.get(task_id, "task")
    cal = _calendar(data)
    part = _component(cal, "task")
    if "RECURRENCE-ID" in part:
        raise ValueError("Cannot patch an override-only resource")
    if "summary" in changes:
        _set_cal(part, "SUMMARY", _text(changes["summary"], "summary", required=True))
    if "description" in changes:
        value = changes["description"]
        _set_cal(part, "DESCRIPTION", _text(value, "description", multiline=True) if value is not None else None)
    for field in ("start", "due"):
        if field in changes:
            value = changes[field]
            key = "DTSTART" if field == "start" else "DUE"
            previous = part.get(key) or (part.get("DTSTART") if field == "due" else None)
            _set_cal(part, key, _dt(value, previous.dt if previous is not None else _UNSET) if value is not None else None)
            if field == "due" and value is not None:
                part.pop("DURATION", None)
    begins = part.get("DTSTART")
    ends = part.get("DUE")
    if begins and ends:
        _range(begins.dt, ends.dt)
    if "status" in changes:
        status = changes["status"]
        if status is not None and status not in ("NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"):
            raise ValueError("Invalid VTODO status")
        _set_cal(part, "STATUS", status)
    for field, key, maximum in (("percent_complete", "PERCENT-COMPLETE", 100), ("priority", "PRIORITY", 9)):
        if field in changes:
            value = changes[field]
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum):
                raise ValueError(f"Invalid {field}")
            _set_cal(part, key, value)
    await dav.put(task_id, cal.to_ical().decode(), kind="task", expected_etag=_etag(expected_etag))
    return await get_task(task_id)


@mcp.tool()
async def complete_task(task_id: str, expected_etag: str) -> dict:
    """Mark VTODO completed with current UTC completion time, conditional on ETag."""
    dav = Dav()
    data, _ = await dav.get(task_id, "task")
    cal = _calendar(data)
    part = _component(cal, "task")
    if "RECURRENCE-ID" in part:
        raise ValueError("Cannot complete an override-only resource")
    _set_cal(part, "STATUS", "COMPLETED")
    _set_cal(part, "PERCENT-COMPLETE", 100)
    _set_cal(part, "COMPLETED", datetime.now(timezone.utc))
    await dav.put(task_id, cal.to_ical().decode(), kind="task", expected_etag=_etag(expected_etag))
    return await get_task(task_id)


@mcp.tool()
async def delete_task(task_id: str, expected_etag: str) -> dict:
    """Delete one VTODO resource only if its ETag still matches."""
    await Dav().delete(task_id, "task", _etag(expected_etag))
    return {"deleted": True, "id": task_id}


@mcp.tool()
async def list_addressbooks() -> list[dict]:
    """List accessible CardDAV address books."""
    return await Dav().collections("contact")


@mcp.tool()
async def list_contacts(addressbook_id: str, limit: int = 100) -> list[dict]:
    """List vCard resources in an address book via CardDAV REPORT."""
    rows = await Dav().report(addressbook_id, "contact")
    return [_contact_view(p["id"], p.get("etag") or "", p["data"]) for p in rows][:_limit(limit)]


@mcp.tool()
async def search_contacts(addressbook_id: str, query: str, limit: int = 100) -> list[dict]:
    """Search contacts by name, company, title, email, phone, notes and UID."""
    needle = _text(query, "query", required=True).casefold()
    rows = await Dav().report(addressbook_id, "contact")
    matches = (_contact_view(p["id"], p.get("etag") or "", p["data"]) for p in rows)
    return [row for row in matches if needle in " ".join(str(value or "") for value in row.values()).casefold()][:_limit(limit)]


@mcp.tool()
async def get_contact(contact_id: str) -> dict:
    """Get a vCard resource with its ETag and raw vCard content."""
    data, etag = await Dav().get(contact_id, "contact")
    return _contact_view(contact_id, etag, data, raw=True)


@mcp.tool()
async def create_contact(addressbook_id: str, name: str, company: str | None = None, title: str | None = None,
                         emails: list[str] | None = None, phones: list[str] | None = None,
                         notes: str | None = None, uid: str | None = None) -> dict:
    """Create a vCard contact. Email and phone accept multiple values."""
    dav = Dav()
    await dav.check_collection(addressbook_id, "contact")
    card = vobject.vCard()
    _set_card(card, "uid", _uid(uid))
    _set_card(card, "fn", _text(name, "name", required=True))
    if company is not None:
        _set_company(card, _text(company, "company"))
    for key, value in (("title", title), ("note", notes)):
        if value is not None:
            _set_card(card, key, _text(value, key, multiline=(key == "note")))
    for key, values in (("email", emails), ("tel", phones)):
        if values is not None:
            if not isinstance(values, list):
                raise ValueError(f"{key} must be a list")
            _set_card(card, key, [_text(v, key, required=True) for v in values])
    item_id = addressbook_id + str(uuid4()) + ".vcf"
    await dav.put(item_id, card.serialize(), kind="contact", expected_etag=None)
    return await get_contact(item_id)


@mcp.tool()
async def update_contact(contact_id: str, expected_etag: str, changes: dict[str, Any]) -> dict:
    """Patch vCard fields name, company, title, emails, phones, notes. Omit preserves; null clears optional fields. UID and unknown fields remain."""
    _changes(changes, {"name", "company", "title", "emails", "phones", "notes"})
    dav = Dav()
    data, _ = await dav.get(contact_id, "contact")
    card = _card(data)
    for field, key in (("name", "fn"), ("company", "org"), ("title", "title"), ("notes", "note")):
        if field in changes:
            value = changes[field]
            if field == "name" or value is not None:
                value = _text(value, field, required=(field == "name"), multiline=(field == "notes"))
            if field == "company":
                _set_company(card, value)
            else:
                _set_card(card, key, value)
    for field, key in (("emails", "email"), ("phones", "tel")):
        if field in changes:
            values = changes[field]
            if values is not None and not isinstance(values, list):
                raise ValueError(f"{field} must be a list or null")
            _set_card(card, key, [_text(v, field, required=True) for v in values] if values is not None else None)
    await dav.put(contact_id, card.serialize(), kind="contact", expected_etag=_etag(expected_etag))
    return await get_contact(contact_id)


@mcp.tool()
async def delete_contact(contact_id: str, expected_etag: str) -> dict:
    """Delete one vCard resource only if its ETag still matches."""
    await Dav().delete(contact_id, "contact", _etag(expected_etag))
    return {"deleted": True, "id": contact_id}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    """Check DAV reachability and credentials without exposing data."""
    try:
        await Dav().propfind("", depth=0)
    except (DavError, ValueError):
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


class BearerGuard:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.inner(scope, receive, send)
            return
        token = os.getenv("MCP_TOKEN", "")
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"")
        if not token or not hmac.compare_digest(authorization, b"Bearer " + token.encode("utf-8")):
            await JSONResponse({"error": "unauthorized"}, status_code=401,
                               headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await self.inner(scope, receive, send)


if not os.getenv("MCP_TOKEN"):
    raise RuntimeError("MCP_TOKEN is required")


app = BearerGuard(mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                           host="0.0.0.0"))


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
