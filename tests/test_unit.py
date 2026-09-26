"""Focused backend regressions that do not require Docker or a DAV server."""

import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import icalendar
import vobject

os.environ.setdefault("MCP_TOKEN", "unit-test-token")
from radicale_mcp import server  # noqa: E402
from radicale_mcp.dav import Dav, DavError, _path_id  # noqa: E402


SERIES = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTIMEZONE
TZID:America/Sao_Paulo
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:-0300
TZOFFSETTO:-0300
TZNAME:-03
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:uid-1
DTSTART;TZID=America/Sao_Paulo:20260925T090000
DTEND;TZID=America/Sao_Paulo:20260925T100000
SUMMARY:Original
RRULE:FREQ=WEEKLY
EXDATE;TZID=America/Sao_Paulo:20261002T090000
X-CUSTOM:keep
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:alarm
TRIGGER:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:uid-1
RECURRENCE-ID;TZID=America/Sao_Paulo:20261009T090000
DTSTART;TZID=America/Sao_Paulo:20261009T110000
SUMMARY:Override
END:VEVENT
END:VCALENDAR
"""

DURATION = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:duration-1
DTSTART;TZID=America/Sao_Paulo:20260925T090000
DURATION:PT1H
SUMMARY:Duration
END:VEVENT
END:VCALENDAR
"""

CARD = """BEGIN:VCARD
VERSION:3.0
UID:card-1
FN:Ana
ORG:Acme;R&D
EMAIL;TYPE=WORK:a@example.org
X-UNKNOWN:keep
NOTE:first\\nsecond
END:VCARD
"""


class FakeDav:
    def __init__(self, data):
        self.data = data
        self.saved = None
        self.kind = None

    async def get(self, _item_id, _kind):
        return self.data, '"etag1"'

    async def put(self, _item_id, data, *, kind, expected_etag):
        assert expected_etag == '"etag1"'
        self.saved = data
        self.kind = kind
        return '"etag2"'


class BackendUnit(unittest.IsolatedAsyncioTestCase):
    async def event_patch(self, data, changes):
        dav = FakeDav(data)
        with patch.object(server, "Dav", return_value=dav), patch.object(
            server, "get_event", new_callable=AsyncMock, return_value={}
        ):
            await server.update_event("user/calendar/item.ics", '"etag1"', changes)
        self.assertEqual(dav.kind, "event")
        return icalendar.Calendar.from_ical(dav.saved)

    async def contact_patch(self, data, changes):
        dav = FakeDav(data)
        with patch.object(server, "Dav", return_value=dav), patch.object(
            server, "get_contact", new_callable=AsyncMock, return_value={}
        ):
            await server.update_contact("user/contacts/item.vcf", '"etag1"', changes)
        self.assertEqual(dav.kind, "contact")
        return vobject.readOne(dav.saved)

    def test_dav_url_defaults_to_radicale_alias_without_network(self):
        with patch.dict(
            os.environ,
            {"DAV_USERNAME": "fixture-user", "DAV_PASSWORD": "fixture-password"},
            clear=True,
        ):
            dav = Dav()
        self.assertEqual(dav.base, "http://radicale:5232/")
        self.assertEqual(dav.origin, ("http", "radicale", 5232))

    async def test_event_patch_preserves_series_and_unknown_fields(self):
        cal = await self.event_patch(SERIES, {"summary": "Changed"})
        master = next(part for part in cal.walk("VEVENT") if "RECURRENCE-ID" not in part)
        self.assertEqual(str(master["UID"]), "uid-1")
        self.assertEqual(master["DTSTART"].params["TZID"], "America/Sao_Paulo")
        self.assertEqual(master["DTEND"].params["TZID"], "America/Sao_Paulo")
        for field in ("RRULE", "EXDATE", "X-CUSTOM"):
            self.assertIn(field, master)
        self.assertEqual(master.subcomponents[0].name, "VALARM")
        self.assertEqual(len(cal.walk("VTIMEZONE")), 1)
        self.assertEqual(sum("RECURRENCE-ID" in part for part in cal.walk("VEVENT")), 1)

        cal = await self.event_patch(SERIES, {"end": "2026-09-25T11:00:00-03:00"})
        master = server._component(cal, "event")
        self.assertEqual(master["DTSTART"].params["TZID"], "America/Sao_Paulo")
        self.assertEqual(master["DTEND"].dt.isoformat(), "2026-09-25T14:00:00+00:00")

    async def test_duration_and_timezone_patch(self):
        cal = await self.event_patch(DURATION, {"start": "2026-09-25T10:00:00"})
        master = server._component(cal, "event")
        self.assertIn("DURATION", master)
        self.assertNotIn("DTEND", master)

        for tzid in ("America/New_York", None):
            param = f";TZID={tzid}" if tzid else ""
            ics = (
                "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:other\n"
                f"DTSTART{param}:20260925T090000\nDTEND{param}:20260925T100000\n"
                "SUMMARY:Other\nEND:VEVENT\nEND:VCALENDAR\n"
            )
            cal = await self.event_patch(ics, {"end": "2026-09-25T11:00:00"})
            master = server._component(cal, "event")
            self.assertEqual(master["DTSTART"].dt.tzinfo, master["DTEND"].dt.tzinfo)
            self.assertEqual(master["DTEND"].dt.tzinfo is None, tzid is None)
            if tzid:
                self.assertEqual(master["DTEND"].params["TZID"], tzid)

    async def test_contact_patch_preserves_other_fields(self):
        card = await self.contact_patch(CARD, {"company": "New Acme", "notes": "line 1\r\nline 2"})
        self.assertEqual(card.org.value, ["New Acme", "R&D"])
        self.assertEqual(card.uid.value, "card-1")
        self.assertEqual(card.email.value, "a@example.org")
        self.assertEqual(card.x_unknown.value, "keep")
        self.assertEqual(card.note.value, "line 1\nline 2")

    async def test_search_filters_full_report_before_limit(self):
        rows = [{"id": str(i), "etag": '"e"', "data": ""} for i in range(1001)]
        class ReportDav:
            async def report(self, *_args):
                return rows
        def view(item_id, _etag, _data):
            return {"id": item_id, "summary": "needle" if item_id == "1000" else "other"}
        with patch.object(server, "Dav", return_value=ReportDav()), patch.object(server, "_event_view", side_effect=view), patch.object(server, "_task_view", side_effect=view), patch.object(server, "_contact_view", side_effect=view):
            event = await server.search_events("user/calendar/", "needle", limit=1)
            task = await server.search_tasks("user/tasks/", "needle", limit=1)
            contact = await server.search_contacts("user/contacts/", "needle", limit=1)
        self.assertEqual([event[0]["id"], task[0]["id"], contact[0]["id"]], ["1000"] * 3)

    async def test_etag_paths_offset_and_bearer(self):
        for bad in ("*", 'W/"weak"', '"old", "current"', "bare"):
            with self.assertRaises(ValueError):
                server._etag(bad)
        self.assertEqual(server._etag('"strong"'), '"strong"')
        for bad in ("u/../x", "u/%2e%2e/x", "u/%252e%252e/x", "u/%2fadmin", "http://evil/x"):
            with self.assertRaises(ValueError):
                _path_id(bad)
        with patch.dict(os.environ, {"DAV_URL": "http://radicale:5232/base/", "DAV_USERNAME": "user", "DAV_PASSWORD": "password"}):
            dav = Dav()
            for bad in ("http://evil/base/item", "/other/item", "http://attacker@127.0.0.1:5232/base/item"):
                with self.assertRaises(DavError):
                    dav.id_from_href(bad)
        self.assertEqual(server._dt("2026-09-25T09:00:00-03:00").isoformat(), "2026-09-25T12:00:00+00:00")
        self.assertEqual(server._dt("2026-09-25T09:00:00").utcoffset().total_seconds(), -10800)
        self.assertIsNone(server._dt("2026-09-25T09:00:00", datetime(2026, 9, 25)).tzinfo)
        self.assertEqual(server._dt("2026-09-25T09:00:00", datetime(2026, 9, 25, tzinfo=ZoneInfo("America/New_York"))).tzinfo.key, "America/New_York")

        class Inner:
            called = False
            async def __call__(self, _scope, _receive, _send):
                self.called = True
        inner = Inner()
        sent = []
        async def send(message):
            sent.append(message)
        guard = server.BearerGuard(inner)
        await guard({"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer \xff")]}, None, send)
        self.assertFalse(inner.called)
        self.assertEqual(sent[0]["status"], 401)


if __name__ == "__main__":
    unittest.main()
