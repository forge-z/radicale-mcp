import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import tempfile
from datetime import datetime
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import httpx2 as httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "list_calendars", "list_events", "search_events", "get_event", "create_event", "update_event", "delete_event",
    "list_task_lists", "list_tasks", "search_tasks", "get_task", "create_task", "update_task", "complete_task", "delete_task",
    "list_addressbooks", "list_contacts", "search_contacts", "get_contact", "create_contact", "update_contact", "delete_contact",
}


def free_ports():
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class ContainerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suffix = secrets.token_hex(5)
        cls.upstream_image = "ghcr.io/kozea/radicale:stable"
        cls.image = os.environ.get("RADICALE_MCP_TEST_IMAGE", "").strip() or None
        cls.image_built = False
        cls.radicale_container = f"radicale-sidecar-test-{cls.suffix}"
        cls.container = f"radicale-mcp-test-{cls.suffix}"
        cls.network = f"radicale-mcp-net-{cls.suffix}"
        cls.volume = f"radicale-data-test-{cls.suffix}"
        cls.username = f"mcp-test-{cls.suffix}"
        cls.password = secrets.token_urlsafe(24)
        cls.token = secrets.token_urlsafe(32)
        cls.radicale_port, cls.mcp_port = free_ports()
        cls.tmp = tempfile.TemporaryDirectory(prefix="radicale-mcp-test-")
        cls.config_dir = Path(cls.tmp.name)
        cls.config_file = cls.config_dir / "config"
        cls.users_file = cls.config_dir / "users"
        try:
            cls.command(["docker", "pull", cls.upstream_image])
            if cls.image is None:
                cls.image = f"radicale-mcp-test:{cls.suffix}"
                cls.command(["docker", "build", "--tag", cls.image, "."], cwd=ROOT)
                cls.image_built = True
            else:
                cls.command(["docker", "image", "inspect", cls.image])

            hash_process = subprocess.run(
                ["docker", "run", "--rm", "-i", "--entrypoint", "htpasswd",
                 cls.upstream_image, "-niB", cls.username],
                input=cls.password + "\n", text=True, capture_output=True,
            )
            if hash_process.returncode != 0 or not hash_process.stdout.strip():
                raise AssertionError("Could not generate the temporary Radicale htpasswd fixture")
            cls.users_file.write_text(hash_process.stdout, encoding="utf-8")
            cls.config_file.write_text(
                "[server]\nhosts = 0.0.0.0:5232\n\n"
                "[auth]\ntype = htpasswd\nhtpasswd_filename = /etc/radicale/users\n"
                "htpasswd_encryption = bcrypt\n\n"
                "[rights]\ntype = owner_only\n\n"
                "[storage]\nfilesystem_folder = /var/lib/radicale/collections\n"
                "strict_preconditions = True\n\n[web]\ntype = internal\n",
                encoding="utf-8",
            )
            os.chmod(cls.config_dir, 0o755)
            os.chmod(cls.config_file, 0o644)
            os.chmod(cls.users_file, 0o644)
            cls.command(["docker", "network", "create", cls.network])
            cls.command(["docker", "volume", "create", cls.volume])
            cls.command([
                "docker", "run", "-d", "--name", cls.radicale_container,
                "--network", cls.network, "--network-alias", "radicale",
                "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "-p", f"127.0.0.1:{cls.radicale_port}:5232",
                "-v", f"{cls.config_file}:/etc/radicale/config:ro",
                "-v", f"{cls.users_file}:/etc/radicale/users:ro",
                "-v", f"{cls.volume}:/var/lib/radicale",
                cls.upstream_image,
            ])
            cls.wait_radicale_healthy()
        except BaseException:
            cls.cleanup()
            raise

    @staticmethod
    def command(args, *, input=None, env=None, cwd=None):
        result = subprocess.run(
            args, input=input, text=True, capture_output=True, env=env, cwd=cwd,
        )
        if result.returncode != 0:
            raise AssertionError(f"Docker test command failed (exit {result.returncode})")
        return result.stdout

    @classmethod
    def cleanup(cls):
        for name in (getattr(cls, "container", None), getattr(cls, "radicale_container", None)):
            if name:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        if getattr(cls, "network", None):
            subprocess.run(["docker", "network", "rm", cls.network], capture_output=True, text=True)
        if getattr(cls, "volume", None):
            subprocess.run(["docker", "volume", "rm", "-f", cls.volume], capture_output=True, text=True)
        if getattr(cls, "image_built", False) and getattr(cls, "image", None):
            subprocess.run(["docker", "image", "rm", "-f", cls.image], capture_output=True, text=True)
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()

    @staticmethod
    def http_request(method, url, body=None, headers=None):
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read().decode(errors="replace")

    @classmethod
    def wait_radicale_healthy(cls):
        auth = base64.b64encode(f"{cls.username}:{cls.password}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Depth": "0", "Content-Type": "application/xml"}
        deadline = time.monotonic() + 60
        last_status = None
        while time.monotonic() < deadline:
            try:
                status, _, _ = cls.http_request(
                    "PROPFIND", f"http://127.0.0.1:{cls.radicale_port}/", headers=headers,
                )
                if status == 207:
                    return
                last_status = status
            except Exception as exc:
                last_status = type(exc).__name__
            time.sleep(0.5)
        raise AssertionError(f"Radicale fixture did not become ready (last={last_status})")

    @classmethod
    def wait_healthy(cls):
        url = f"http://127.0.0.1:{cls.mcp_port}/health"
        deadline = time.monotonic() + 60
        last_status = None
        while time.monotonic() < deadline:
            try:
                status, _, _ = cls.http_request("GET", url)
                if status == 200:
                    return
                last_status = status
            except Exception as exc:
                last_status = type(exc).__name__
            time.sleep(0.5)
        raise AssertionError(f"MCP health did not become ready (last={last_status})")

    def wait_http_status(self, url, expected, timeout=20):
        deadline = time.monotonic() + timeout
        last_status = None
        while time.monotonic() < deadline:
            try:
                last_status, _, _ = self.http_request("GET", url)
                if last_status == expected:
                    return
            except Exception as exc:
                last_status = type(exc).__name__
            time.sleep(0.5)
        self.fail(f"HTTP endpoint did not return {expected} (last={last_status})")

    def start_mcp(self):
        child_env = os.environ.copy()
        child_env.update({
            "DAV_USERNAME": self.username,
            "DAV_PASSWORD": self.password,
            "MCP_TOKEN": self.token,
        })
        self.command([
            "docker", "run", "-d", "--name", self.container,
            "--network", self.network, "--network-alias", "radicale-mcp",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "-p", f"127.0.0.1:{self.mcp_port}:8080",
            "-e", "DAV_URL=http://radicale:5232/",
            "-e", "DAV_USERNAME", "-e", "DAV_PASSWORD", "-e", "MCP_TOKEN",
            "-e", "MCP_TIMEZONE=America/Sao_Paulo", "-e", "PORT=8080",
            "--pull=never", self.image,
        ], env=child_env)
        self.wait_healthy()
        self.assert_mcp_is_sidecar()

    def assert_mcp_is_sidecar(self):
        mounts = json.loads(self.command([
            "docker", "inspect", "--format", "{{json .Mounts}}", self.container,
        ]))
        self.assertTrue(
            all(mount.get("Type") == "tmpfs" and mount.get("Destination") == "/tmp" for mount in mounts),
            "MCP may have only its own /tmp tmpfs mount",
        )
        radicale_mounts = json.loads(self.command([
            "docker", "inspect", "--format", "{{json .Mounts}}", self.radicale_container,
        ]))
        radicale_destinations = {mount.get("Destination"): mount for mount in radicale_mounts}
        required_radicale_mounts = {"/etc/radicale/config", "/etc/radicale/users", "/var/lib/radicale"}
        self.assertTrue(required_radicale_mounts.issubset(radicale_destinations))
        self.assertLessEqual(set(radicale_destinations), required_radicale_mounts | {"/tmp"})
        if "/tmp" in radicale_destinations:
            self.assertEqual(radicale_destinations["/tmp"].get("Type"), "tmpfs")
        self.assertEqual(radicale_destinations["/etc/radicale/config"].get("Type"), "bind")
        self.assertEqual(radicale_destinations["/etc/radicale/users"].get("Type"), "bind")
        self.assertEqual(radicale_destinations["/var/lib/radicale"].get("Type"), "volume")
        self.assertEqual(radicale_destinations["/var/lib/radicale"].get("Name"), self.volume)
        exposed = json.loads(self.command([
            "docker", "inspect", "--format", "{{json .Config.ExposedPorts}}", self.container,
        ]) or "null") or {}
        self.assertNotIn("5232/tcp", exposed)
        probe = (
            "import importlib.util,os,shutil,sys;"
            "bad=(importlib.util.find_spec('radicale') is not None or "
            "shutil.which('radicale') is not None or "
            "os.path.exists('/etc/radicale/config') or "
            "os.path.exists('/var/lib/radicale'));"
            "sys.exit(1 if bad else 0)"
        )
        self.command(["docker", "exec", self.container, "python", "-c", probe])

    @classmethod
    def assert_radicale_running(cls):
        state = cls.command([
            "docker", "inspect", "--format", "{{.State.Running}}", cls.radicale_container,
        ]).strip()
        if state != "true":
            raise AssertionError("The independent Radicale container is not running")

    def dav(self, method, path, body=None, headers=None):
        auth = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        request_headers = {"Authorization": f"Basic {auth}"}
        request_headers.update(headers or {})
        return self.http_request(
            method, f"http://127.0.0.1:{self.radicale_port}/{path}", body, request_headers,
        )

    def create_calendar(self, name, component):
        calendar_id = f"{self.username}/{name}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:set><d:prop><d:displayname>Integration</d:displayname>'
            f'<c:supported-calendar-component-set><c:comp name="{component}"/></c:supported-calendar-component-set>'
            '</d:prop></d:set></c:mkcalendar>'
        ).encode()
        status, _, _ = self.dav(
            "MKCALENDAR", calendar_id, body,
            {"Content-Type": "application/xml; charset=utf-8"},
        )
        self.assertIn(status, (200, 201, 204), f"Could not create {component} collection (HTTP {status})")
        return calendar_id

    def create_addressbook(self, name):
        addressbook_id = f"{self.username}/{name}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:mkcol xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            '<d:set><d:prop><d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>'
            '<d:displayname>Integration</d:displayname></d:prop></d:set></d:mkcol>'
        ).encode()
        status, _, _ = self.dav(
            "MKCOL", addressbook_id, body,
            {"Content-Type": "application/xml; charset=utf-8"},
        )
        self.assertIn(status, (200, 201, 204), f"Could not create addressbook (HTTP {status})")
        return addressbook_id

    @staticmethod
    def value(result):
        if result.is_error:
            return {"__error__": "\n".join(getattr(block, "text", "") for block in result.content)}
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        text = "\n".join(getattr(block, "text", "") for block in result.content)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def request_unauthorized(self, headers):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "auth-check", "version": "1"},
            },
        }).encode()
        status, _, _ = self.http_request(
            "POST", f"http://127.0.0.1:{self.mcp_port}/mcp", payload,
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers},
        )
        return status

    def test_container_contract(self):

        calendar_id = self.create_calendar(f"events-{self.suffix}", "VEVENT")
        task_list_id = self.create_calendar(f"tasks-{self.suffix}", "VTODO")
        addressbook_id = self.create_addressbook(f"contacts-{self.suffix}")

        self.seed_event_uid = f"seed-event-{self.suffix}"
        self.seed_task_uid = f"seed-task-{self.suffix}"
        self.seed_contact_uid = f"seed-contact-{self.suffix}"
        self.seed_event_id = calendar_id + "dav-seed-event.ics"
        self.seed_task_id = task_list_id + "dav-seed-task.ics"
        self.seed_contact_id = addressbook_id + "dav-seed-contact.vcf"
        seed_event = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//sidecar-test//EN
BEGIN:VEVENT
UID:{self.seed_event_uid}
DTSTAMP:20260926T000000Z
DTSTART:20261002T120000Z
DTEND:20261002T130000Z
SUMMARY:Seeded directly through DAV
END:VEVENT
END:VCALENDAR
"""
        seed_task = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//sidecar-test//EN
BEGIN:VTODO
UID:{self.seed_task_uid}
DTSTAMP:20260926T000000Z
DTSTART;VALUE=DATE:20261003
DUE;VALUE=DATE:20261010
SUMMARY:Seeded task directly through DAV
END:VTODO
END:VCALENDAR
"""
        seed_contact = f"""BEGIN:VCARD
VERSION:3.0
UID:{self.seed_contact_uid}
FN:Seeded contact
EMAIL:seed@example.test
END:VCARD
"""
        for item_id, value, content_type in (
            (self.seed_event_id, seed_event, "text/calendar; charset=utf-8"),
            (self.seed_task_id, seed_task, "text/calendar; charset=utf-8"),
            (self.seed_contact_id, seed_contact, "text/vcard; charset=utf-8"),
        ):
            status, _, _ = self.dav(
                "PUT", item_id, value.encode(),
                {"Content-Type": content_type, "If-None-Match": "*"},
            )
            self.assertIn(status, (200, 201, 204), f"Could not seed DAV item (HTTP {status})")

        self.start_mcp()
        self.assertEqual(self.request_unauthorized({}), 401)
        self.assertEqual(self.request_unauthorized({"Authorization": "Bearer invalid"}), 401)
        self.assertEqual(self.request_unauthorized({"Authorization": f"Bearer {self.token}é"}), 401)
        status, _, propfind = self.dav("PROPFIND", "", headers={"Depth": "0", "Content-Type": "application/xml"})
        self.assertEqual(status, 207, propfind)
        status, _, page = self.dav("GET", "")
        self.assertEqual(status, 200, page)
        self.assertTrue("radicale" in page.casefold() or "<html" in page.casefold(), page[:300])
        token = self.token

        async def exercise():
            ids = {}
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=30,
            ) as http_client:
                transport = streamable_http_client(
                    f"http://127.0.0.1:{self.mcp_port}/mcp", http_client=http_client,
                )
                async with Client(transport) as client:
                        listed = await client.list_tools()
                        self.assertEqual({tool.name for tool in listed.tools}, EXPECTED_TOOLS)

                        async def call(name, arguments=None):
                            result = await client.call_tool(name, arguments or {})
                            return self.value(result)

                        calendars = await call("list_calendars")
                        task_lists = await call("list_task_lists")
                        books = await call("list_addressbooks")
                        self.assertTrue(any(row["id"] == calendar_id for row in calendars), repr(calendars))
                        self.assertTrue(any(row["id"] == task_list_id for row in task_lists))
                        self.assertTrue(any(row["id"] == addressbook_id for row in books))
                        seed_event = await call("get_event", {"event_id": self.seed_event_id})
                        seed_task = await call("get_task", {"task_id": self.seed_task_id})
                        seed_contact = await call("get_contact", {"contact_id": self.seed_contact_id})
                        self.assertEqual(seed_event["uid"], self.seed_event_uid)
                        self.assertEqual(seed_task["uid"], self.seed_task_uid)
                        self.assertEqual(seed_contact["uid"], self.seed_contact_uid)
                        ids["seed_event"] = self.seed_event_id
                        ids["seed_task"] = self.seed_task_id
                        ids["seed_contact"] = self.seed_contact_id

                        event = await call("create_event", {
                            "calendar_id": calendar_id, "summary": "Reunião de integração",
                            "start": "2026-10-03T09:00:00-03:00", "end": "2026-10-03T10:00:00-03:00",
                            "description": "linha 1\nlinha 2", "location": "São Paulo",
                            "rrule": "FREQ=DAILY;COUNT=2",
                        })
                        self.assertNotIn("__error__", event, event)
                        ids["created_event"] = event["id"]
                        self.assertTrue(event["etag"])
                        all_day = await call("create_event", {
                            "calendar_id": calendar_id, "summary": "All-day persistence",
                            "start": "2026-10-20", "end": "2026-10-21",
                        })
                        self.assertEqual(all_day["start"], "2026-10-20")
                        self.assertEqual(all_day["end"], "2026-10-21")
                        ids["persistent_event"] = all_day["id"]
                        self.assertEqual(event["summary"], "Reunião de integração")
                        self.assertEqual(datetime.fromisoformat(event["start"]), datetime.fromisoformat("2026-10-03T09:00:00-03:00"))
                        self.assertEqual(datetime.fromisoformat(event["end"]), datetime.fromisoformat("2026-10-03T10:00:00-03:00"))
                        events = await call("list_events", {"calendar_id": calendar_id})
                        self.assertTrue(any(row["id"] == event["id"] for row in events))
                        matches = await call("search_events", {"calendar_id": calendar_id, "query": "integração"})
                        self.assertTrue(any(row["id"] == event["id"] for row in matches))
                        old_event_etag = event["etag"]
                        event = await call("update_event", {
                            "event_id": event["id"], "expected_etag": event["etag"],
                            "changes": {"summary": "Reunião alterada"},
                        })
                        self.assertEqual(event["summary"], "Reunião alterada")
                        stale = await call("update_event", {
                            "event_id": event["id"], "expected_etag": old_event_etag,
                            "changes": {"summary": "não deve gravar"},
                        })
                        self.assertIn("__error__", stale, stale)
                        deleted = await call("delete_event", {"event_id": event["id"], "expected_etag": event["etag"]})
                        self.assertTrue(deleted["deleted"])
                        self.assertEqual(self.dav("GET", event["id"])[0], 404)

                        direct_event_id = calendar_id + "external-resource"
                        ical = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//integration//EN
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
UID:external-series
DTSTAMP:20260925T000000Z
DTSTART;TZID=America/Sao_Paulo:20261001T090000
DTEND;TZID=America/Sao_Paulo:20261001T100000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Importado
DESCRIPTION:primeira linha\\nsegunda linha
X-PRIVATE-PROPERTY:preserve-this
END:VEVENT
BEGIN:VEVENT
UID:external-series
RECURRENCE-ID;TZID=America/Sao_Paulo:20261008T090000
DTSTAMP:20260925T000000Z
DTSTART;TZID=America/Sao_Paulo:20261008T110000
DTEND;TZID=America/Sao_Paulo:20261008T120000
SUMMARY:Exceção
END:VEVENT
END:VCALENDAR
"""
                        status, _, response = self.dav(
                            "PUT", direct_event_id, ical.encode(),
                            {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
                        )
                        self.assertIn(status, (200, 201, 204), response)
                        external = await call("get_event", {"event_id": direct_event_id})
                        self.assertEqual(external["uid"], "external-series")
                        raw = external["raw_ical"]
                        self.assertIn("TZID=America/Sao_Paulo", raw)
                        self.assertIn("RRULE", raw)
                        self.assertIn("RECURRENCE-ID", raw)
                        self.assertIn("X-PRIVATE-PROPERTY", raw)
                        self.assertIn("preserve-this", raw)
                        exception_range = await call("list_events", {
                            "calendar_id": calendar_id,
                            "start": "2026-10-08T11:30:00-03:00",
                            "end": "2026-10-08T11:45:00-03:00",
                        })
                        self.assertTrue(any(row["id"] == direct_event_id for row in exception_range), exception_range)
                        old_external_etag = external["etag"]
                        external = await call("update_event", {
                            "event_id": direct_event_id, "expected_etag": external["etag"],
                            "changes": {"summary": "Importado atualizado"},
                        })
                        self.assertEqual(external["uid"], "external-series")
                        raw = external["raw_ical"]
                        self.assertIn("TZID=America/Sao_Paulo", raw)
                        self.assertIn("RECURRENCE-ID", raw)
                        self.assertIn("X-PRIVATE-PROPERTY", raw)
                        self.assertIn("preserve-this", raw)
                        raw_dav = self.dav("GET", direct_event_id)[2]
                        self.assertIn("X-PRIVATE-PROPERTY", raw_dav)
                        traversal = await call("get_event", {"event_id": "../outside.ics"})
                        self.assertIn("__error__", traversal, traversal)
                        ids["external_event"] = direct_event_id

                        task = await call("create_task", {
                            "task_list_id": task_list_id, "summary": "Tarefa de integração",
                            "description": "validar DUE", "start": "2026-10-01", "due": "2026-10-10",
                            "priority": 3,
                        })
                        self.assertNotIn("__error__", task, task)
                        ids["task"] = task["id"]
                        self.assertEqual(task["start"], "2026-10-01")
                        self.assertEqual(task["due"], "2026-10-10")
                        listed_tasks = await call("list_tasks", {"task_list_id": task_list_id})
                        self.assertTrue(any(row["id"] == task["id"] for row in listed_tasks))
                        task_wire = self.dav("GET", task["id"])[2]
                        self.assertIn("DUE", task_wire)
                        self.assertIn("20261010", task_wire)
                        task_matches = await call("search_tasks", {"task_list_id": task_list_id, "query": "DUE"})
                        self.assertTrue(any(row["id"] == task["id"] for row in task_matches))
                        old_task_etag = task["etag"]
                        task = await call("update_task", {
                            "task_id": task["id"], "expected_etag": task["etag"],
                            "changes": {"due": "2026-10-11", "description": "linha um\nlinha dois"},
                        })
                        self.assertEqual(task["due"], "2026-10-11")
                        task_wire = self.dav("GET", task["id"])[2]
                        self.assertIn("20261011", task_wire)
                        stale = await call("update_task", {
                            "task_id": task["id"], "expected_etag": old_task_etag,
                            "changes": {"summary": "não deve gravar"},
                        })
                        self.assertIn("__error__", stale, stale)
                        task = await call("complete_task", {"task_id": task["id"], "expected_etag": task["etag"]})
                        self.assertEqual(task["status"], "COMPLETED")
                        self.assertEqual(task["percent_complete"], 100)
                        self.assertTrue(task["completed_at"])
                        stale = await call("delete_task", {"task_id": task["id"], "expected_etag": old_task_etag})
                        self.assertIn("__error__", stale, stale)

                        contact = await call("create_contact", {
                            "addressbook_id": addressbook_id, "name": "Zoë Integração",
                            "company": "ForgeZ Equipamentos", "title": "Engenheira",
                            "emails": ["zoe@example.test", "zoe.alt@example.test"],
                            "phones": ["+55 11 5555-0101", "+55 11 5555-0102"],
                            "notes": "primeira linha\nsegunda linha",
                        })
                        self.assertNotIn("__error__", contact, contact)
                        ids["contact"] = contact["id"]
                        self.assertEqual(contact["company"], "ForgeZ Equipamentos")
                        ids["created_contact_uid"] = contact["uid"]
                        self.assertEqual(len(contact["emails"]), 2)
                        self.assertEqual(len(contact["phones"]), 2)
                        self.assertIn("primeira linha", contact["notes"])
                        contacts = await call("list_contacts", {"addressbook_id": addressbook_id})
                        self.assertTrue(any(row["id"] == contact["id"] for row in contacts))
                        matches = await call("search_contacts", {"addressbook_id": addressbook_id, "query": "zoë"})
                        self.assertTrue(any(row["id"] == contact["id"] for row in matches))
                        old_contact_etag = contact["etag"]
                        contact = await call("update_contact", {
                            "contact_id": contact["id"], "expected_etag": contact["etag"],
                            "changes": {"notes": "nota nova\nsegunda linha", "emails": ["zoe.new@example.test"]},
                        })
                        self.assertEqual(contact["uid"], ids["created_contact_uid"])
                        self.assertEqual(contact["emails"], ["zoe.new@example.test"])
                        self.assertIn("nota nova", contact["notes"])
                        stale = await call("delete_contact", {
                            "contact_id": contact["id"], "expected_etag": old_contact_etag,
                        })
                        self.assertIn("__error__", stale, stale)
                        deleted = await call("delete_contact", {
                            "contact_id": contact["id"], "expected_etag": contact["etag"],
                        })
                        self.assertTrue(deleted["deleted"])
                        self.assertEqual(self.dav("GET", contact["id"])[0], 404)

                        direct_contact_id = addressbook_id + "external-card"
                        vcard = """BEGIN:VCARD
VERSION:3.0
UID:external-card-uid
FN:External Card
EMAIL;TYPE=home:external@example.test
X-PRIVATE-PROPERTY:keep-this
END:VCARD
"""
                        status, _, response = self.dav(
                            "PUT", direct_contact_id, vcard.encode(),
                            {"Content-Type": "text/vcard; charset=utf-8", "If-None-Match": "*"},
                        )
                        self.assertIn(status, (200, 201, 204), response)
                        external = await call("get_contact", {"contact_id": direct_contact_id})
                        self.assertEqual(external["uid"], "external-card-uid")
                        external = await call("update_contact", {
                            "contact_id": direct_contact_id, "expected_etag": external["etag"],
                            "changes": {"name": "External Card atualizada", "notes": "uma linha\ne outra"},
                        })
                        self.assertEqual(external["uid"], "external-card-uid")
                        self.assertIn("X-PRIVATE-PROPERTY", external["raw_vcard"])
                        self.assertIn("keep-this", external["raw_vcard"])
                        ids["external_contact"] = direct_contact_id
                        ids["calendar"] = calendar_id
                        ids["task_list"] = task_list_id
                        ids["addressbook"] = addressbook_id
                        return ids

        ids = asyncio.run(exercise())
        subprocess.run(["docker", "stop", self.radicale_container], check=True, capture_output=True, text=True)
        self.wait_http_status(f"http://127.0.0.1:{self.mcp_port}/health", 503)
        self.assertEqual(
            self.command(["docker", "inspect", "--format", "{{.State.Running}}", self.container]).strip(),
            "true",
        )
        subprocess.run(["docker", "start", self.radicale_container], check=True, capture_output=True, text=True)
        self.wait_radicale_healthy()
        self.wait_healthy()
        subprocess.run(["docker", "restart", self.container], check=True, capture_output=True, text=True)
        self.wait_healthy()
        self.assert_radicale_running()
        subprocess.run(["docker", "restart", self.radicale_container], check=True, capture_output=True, text=True)
        self.wait_radicale_healthy()
        self.wait_healthy()

        async def persistence():
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}"}, timeout=30,
            ) as http_client:
                transport = streamable_http_client(
                    f"http://127.0.0.1:{self.mcp_port}/mcp", http_client=http_client,
                )
                async with Client(transport) as client:
                        for key, name, args in (
                            ("external_event", "get_event", {"event_id": ids["external_event"]}),
                            ("persistent_event", "get_event", {"event_id": ids["persistent_event"]}),
                            ("task", "get_task", {"task_id": ids["task"]}),
                            ("external_contact", "get_contact", {"contact_id": ids["external_contact"]}),
                            ("seed_event", "get_event", {"event_id": ids["seed_event"]}),
                            ("seed_task", "get_task", {"task_id": ids["seed_task"]}),
                            ("seed_contact", "get_contact", {"contact_id": ids["seed_contact"]}),
                        ):
                            result = await client.call_tool(name, args)
                            value = self.value(result)
                            self.assertNotIn("__error__", value, value)
                            self.assertTrue(value["etag"])
                            ids[key] = value
                        self.assertEqual(ids["seed_event"]["uid"], self.seed_event_uid)
                        self.assertEqual(ids["seed_task"]["uid"], self.seed_task_uid)
                        self.assertEqual(ids["seed_contact"]["uid"], self.seed_contact_uid)
                        all_day = ids["persistent_event"]
                        self.assertEqual(all_day["start"], "2026-10-20")
                        self.assertEqual(all_day["end"], "2026-10-21")
                        for tool, id_key, id_arg in (
                            ("delete_event", "external_event", "event_id"),
                            ("delete_event", "persistent_event", "event_id"),
                            ("delete_task", "task", "task_id"),
                            ("delete_contact", "external_contact", "contact_id"),
                            ("delete_event", "seed_event", "event_id"),
                            ("delete_task", "seed_task", "task_id"),
                            ("delete_contact", "seed_contact", "contact_id"),
                        ):
                            value = ids[id_key]
                            deleted = await client.call_tool(tool, {
                                id_arg: value["id"], "expected_etag": value["etag"],
                            })
                            self.assertTrue(self.value(deleted)["deleted"])
                            self.assertEqual(self.dav("GET", value["id"])[0], 404)

        asyncio.run(persistence())


if __name__ == "__main__":
    unittest.main()
