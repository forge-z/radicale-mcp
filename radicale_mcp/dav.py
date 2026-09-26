"""Small, uncached CalDAV/CardDAV client. IDs are paths relative to DAV_URL."""

from __future__ import annotations

import os
import re
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx
import icalendar
import vobject

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
A = "urn:ietf:params:xml:ns:carddav"


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


class DavError(ValueError):
    """Safe upstream error without URL, credentials, or response body."""


def _path_id(path: str, *, collection: bool | None = None) -> str:
    if not path or path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("Invalid DAV ID")
    parts = path.split("/")
    if collection is True and parts[-1] != "":
        raise ValueError("Collection ID must end with /")
    if collection is False and parts[-1] == "":
        raise ValueError("Item ID must not end with /")
    for i, raw in enumerate(parts):
        if not raw:
            if i == len(parts) - 1 and collection is not False:
                continue
            raise ValueError("Invalid DAV ID")
        value = unquote(raw)
        if value in (".", "..") or any(c in value for c in "/\\\x00\r\n") or re.search(r"%[0-9A-Fa-f]{2}", value):
            raise ValueError("Invalid DAV ID")
    return path


class Dav:
    def __init__(self) -> None:
        base = os.getenv("DAV_URL", "http://radicale:5232/")
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("DAV_URL must be an HTTP(S) base URL without credentials or query")
        self.base = base.rstrip("/") + "/"
        self.origin = (parsed.scheme, parsed.hostname, parsed.port)
        self.path = urlsplit(self.base).path
        username = os.getenv("DAV_USERNAME")
        password = os.getenv("DAV_PASSWORD")
        if not username or password is None:
            raise ValueError("DAV_USERNAME and DAV_PASSWORD are required")
        self.auth = httpx.BasicAuth(username, password)

    def url(self, item_id: str = "", *, collection: bool | None = None) -> str:
        if item_id:
            _path_id(item_id, collection=collection)
        return self.base + item_id

    def id_from_href(self, href: str) -> str:
        parsed = urlsplit(urljoin(self.base, href))
        if (parsed.scheme, parsed.hostname, parsed.port) != self.origin or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DavError("DAV returned an href outside its origin")
        if not parsed.path.startswith(self.path):
            raise DavError("DAV returned an href outside DAV_URL")
        relative = parsed.path[len(self.path):]
        if relative:
            _path_id(relative)
        return relative

    async def request(self, method: str, item_id: str, *, collection: bool | None = None,
                      headers: dict[str, str] | None = None, content: bytes | None = None) -> httpx.Response:
        async with httpx.AsyncClient(auth=self.auth, timeout=30, follow_redirects=False, trust_env=False) as client:
            try:
                response = await client.request(method, self.url(item_id, collection=collection), headers=headers, content=content)
            except httpx.HTTPError as exc:
                raise DavError("DAV request failed") from exc
        if response.is_redirect:
            raise DavError("DAV redirect refused")
        if response.status_code >= 400:
            raise DavError(f"DAV {method} failed (HTTP {response.status_code})")
        return response

    def _xml(self, content: bytes) -> list[dict]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise DavError("Invalid DAV XML response") from exc
        out = []
        for node in root.findall(q(D, "response")):
            href = node.findtext(q(D, "href"))
            if not href:
                continue
            props: dict = {"id": self.id_from_href(href)}
            for propstat in node.findall(q(D, "propstat")):
                if " 200 " not in (propstat.findtext(q(D, "status")) or ""):
                    continue
                prop = propstat.find(q(D, "prop"))
                if prop is None:
                    continue
                props["etag"] = prop.findtext(q(D, "getetag")) or props.get("etag")
                props["name"] = prop.findtext(q(D, "displayname")) or props.get("name")
                resource = prop.find(q(D, "resourcetype"))
                if resource is not None:
                    props["types"] = {element.tag for element in resource}
                supported = prop.find(q(C, "supported-calendar-component-set"))
                if supported is not None:
                    props["components"] = {element.get("name", "").upper() for element in supported}
                for tag in (q(C, "calendar-data"), q(A, "address-data")):
                    data = prop.findtext(tag)
                    if data is not None:
                        props["data"] = data
            out.append(props)
        return out

    async def propfind(self, item_id: str, depth: int = 1) -> list[dict]:
        body = f'<d:propfind xmlns:d="{D}" xmlns:c="{C}"><d:prop><d:resourcetype/><d:displayname/><d:getetag/><c:supported-calendar-component-set/></d:prop></d:propfind>'.encode()
        response = await self.request("PROPFIND", item_id, headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"}, content=body)
        return self._xml(response.content)

    async def collections(self, kind: str) -> list[dict]:
        found: dict[str, dict] = {}
        queue = [("", 0)]
        seen = set()
        while queue:
            parent, level = queue.pop(0)
            if parent in seen:
                continue
            seen.add(parent)
            for prop in await self.propfind(parent):
                item_id = prop["id"]
                if item_id == parent or not item_id.endswith("/"):
                    continue
                types = prop.get("types", set())
                if q(C, "calendar") in types or q(A, "addressbook") in types:
                    found[item_id] = prop
                elif q(D, "collection") in types and level < 2:
                    queue.append((item_id, level + 1))
        return [
            {"id": p["id"], "name": p.get("name") or unquote(p["id"].rstrip("/").split("/")[-1]), "components": sorted(p.get("components", []))}
            for p in found.values() if self._supports(p, kind)
        ]

    @staticmethod
    def _supports(prop: dict, kind: str) -> bool:
        types = prop.get("types", set())
        if kind == "contact":
            return q(A, "addressbook") in types
        return q(C, "calendar") in types and (not prop.get("components") or ("VEVENT" if kind == "event" else "VTODO") in prop["components"])

    async def check_collection(self, collection_id: str, kind: str) -> None:
        self.url(collection_id, collection=True)
        props = await self.propfind(collection_id, depth=0)
        prop = next((p for p in props if p["id"] == collection_id), None)
        if not prop or not self._supports(prop, kind):
            raise ValueError(f"Collection does not support {kind}")

    async def check_item(self, item_id: str, kind: str) -> None:
        self.url(item_id, collection=False)
        if "/" not in item_id:
            raise ValueError(f"Invalid {kind} ID")
        await self.check_collection(item_id.rsplit("/", 1)[0] + "/", kind)

    async def report(self, collection_id: str, kind: str, start: str | None = None, end: str | None = None) -> list[dict]:
        await self.check_collection(collection_id, kind)
        if kind == "contact":
            root = ET.Element(q(A, "addressbook-query"))
            prop = ET.SubElement(root, q(D, "prop"))
            ET.SubElement(prop, q(D, "getetag"))
            ET.SubElement(prop, q(A, "address-data"))
            ET.SubElement(root, q(A, "filter"))
        else:
            root = ET.Element(q(C, "calendar-query"))
            prop = ET.SubElement(root, q(D, "prop"))
            ET.SubElement(prop, q(D, "getetag"))
            ET.SubElement(prop, q(C, "calendar-data"))
            filter_node = ET.SubElement(root, q(C, "filter"))
            top = ET.SubElement(filter_node, q(C, "comp-filter"), name="VCALENDAR")
            comp = ET.SubElement(top, q(C, "comp-filter"), name="VEVENT" if kind == "event" else "VTODO")
            if start or end:
                attrs = {}
                if start:
                    attrs["start"] = start
                if end:
                    attrs["end"] = end
                ET.SubElement(comp, q(C, "time-range"), **attrs)
        response = await self.request("REPORT", collection_id, collection=True,
                                      headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                                      content=ET.tostring(root, encoding="utf-8", xml_declaration=True))
        return [p for p in self._xml(response.content) if p["id"] != collection_id and "data" in p]

    async def get(self, item_id: str, kind: str) -> tuple[str, str]:
        await self.check_item(item_id, kind)
        response = await self.request("GET", item_id)
        etag = response.headers.get("etag")
        if not etag:
            raise DavError("DAV item has no ETag")
        self._validate_data(response.text, kind)
        return response.text, etag

    @staticmethod
    def _validate_data(data: str, kind: str) -> None:
        try:
            if kind == "contact":
                if vobject.readOne(data).name.upper() != "VCARD":
                    raise ValueError()
            elif not icalendar.Calendar.from_ical(data).walk("VEVENT" if kind == "event" else "VTODO"):
                raise ValueError()
        except Exception as exc:
            raise DavError(f"DAV item is not a {kind}") from exc

    async def put(self, item_id: str, data: str, *, kind: str, expected_etag: str | None) -> str:
        headers = {"Content-Type": "text/vcard; charset=utf-8" if kind == "contact" else "text/calendar; charset=utf-8",
                   "If-None-Match" if expected_etag is None else "If-Match": "*" if expected_etag is None else expected_etag}
        response = await self.request("PUT", item_id, headers=headers, content=data.encode("utf-8"))
        etag = response.headers.get("etag")
        if etag:
            return etag
        _, etag = await self.get(item_id, kind)
        return etag

    async def delete(self, item_id: str, kind: str, expected_etag: str) -> None:
        await self.get(item_id, kind)
        await self.request("DELETE", item_id, headers={"If-Match": expected_etag})
