"""Calendar module: file-based config, CalDAV round trip, and tool surface."""

from __future__ import annotations

import contextlib
import html
import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "harness" / "modules" / "calendar" / "server.py"

ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//test//calendar//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-1\r\n"
    "DTSTAMP:20261101T120000Z\r\n"
    "DTSTART:20261120T150000Z\r\n"
    "DTEND:20261120T160000Z\r\n"
    "SUMMARY:Planning\\, with team\r\n"
    "DESCRIPTION:Line one\\nLine two that is folded \r\n"
    " continuation\r\n"
    "LOCATION:Room 1\r\n"
    "RRULE:FREQ=WEEKLY\r\n"
    "BEGIN:VALARM\r\n"
    "TRIGGER:-PT15M\r\n"
    "ACTION:DISPLAY\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

NO_END_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-noend\r\n"
    "DTSTAMP:20261101T120000Z\r\n"
    "DTSTART:20261121T090000Z\r\n"
    "SUMMARY:No end\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("calendar_server_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(path: Path, **overrides) -> Path:
    data = {
        "url": "http://127.0.0.1:1/dav.php",
        "username": "test",
        "password": "secret",
        "calendar": "Personal",
        "timezone": "UTC",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Config file and iCalendar parsing
# ---------------------------------------------------------------------------

def test_config_file_wins_over_environment(tmp_path, monkeypatch):
    module = _load_module()
    path = _config(
        tmp_path / "calendar.json",
        url="http://file.test/dav.php",
        username="file-user",
        password="file-pass",
        calendar="Work",
    )
    monkeypatch.setenv("HARNESS_CALENDAR_URL", "http://env.test/dav.php")
    monkeypatch.setenv("HARNESS_CALENDAR_USER", "env-user")
    monkeypatch.setenv("HARNESS_CALENDAR_PASSWORD", "env-pass")
    module.configure(str(path))

    assert module.base_url() == "http://file.test/dav.php/"
    assert module.username() == "file-user"
    assert module.password() == "file-pass"
    assert module.configured_calendar() == "Work"


def test_missing_config_file_is_a_clean_error(tmp_path):
    module = _load_module()
    try:
        module.configure(str(tmp_path / "nope.json"))
    except module.CalendarError as exc:
        assert "cannot read calendar config file" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("a missing config file must fail")


def test_parse_ics_unfolds_and_keeps_recurrence():
    module = _load_module()
    events = module.parse_ics(ICS)

    assert len(events) == 1
    event = events[0]
    assert event["uid"] == "evt-1"
    assert event["summary"] == "Planning, with team"
    assert "Line two that is folded continuation" in event["description"]
    assert event["start"].startswith("2026-11-20T15:00:00")
    assert event["end"].startswith("2026-11-20T16:00:00")
    assert event["recurring"] is True
    assert event["location"] == "Room 1"


def test_update_event_ics_preserves_untouched_properties():
    module = _load_module()
    updated = module.update_event_ics(
        ICS, {"SUMMARY": "SUMMARY:Renamed"}
    )

    assert "SUMMARY:Renamed" in updated
    assert "RRULE:FREQ=WEEKLY" in updated
    assert "BEGIN:VALARM" in updated
    assert "TRIGGER:-PT15M" in updated
    event = module.parse_ics(updated)[0]
    assert event["summary"] == "Renamed"
    assert event["recurring"] is True


def test_read_only_hides_write_tools(tmp_path):
    module = _load_module()
    module.configure(str(_config(tmp_path / "calendar.json", read_only=True)))

    names = [tool["name"] for tool in module.tool_schemas()]
    assert "calendar_list_events" in names
    assert "calendar_create_event" not in names
    assert "calendar_update_event" not in names
    assert "calendar_delete_event" not in names


def test_read_only_refuses_write_calls(tmp_path):
    module = _load_module()
    module.configure(str(_config(tmp_path / "calendar.json", read_only=True)))

    result = module.handle_call("calendar_delete_event", {"uid": "evt-1"})

    assert result["isError"] is True
    assert "read_only=true" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Minimal CalDAV stub
# ---------------------------------------------------------------------------

EVENT_PATH = "/dav.php/calendars/test/personal/evt-1.ics"


class _DavHandler(BaseHTTPRequestHandler):
    server_version = "CalDavStub/1.0"

    def _authorized(self) -> bool:
        expected = self.server.auth  # type: ignore[attr-defined]
        return self.headers.get("Authorization") == expected

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _xml(self, body: str) -> None:
        self._send(
            207, body.encode("utf-8"), "application/xml; charset=utf-8"
        )

    @staticmethod
    def _response(href: str, props: str) -> str:
        return (
            "<d:response>"
            f"<d:href>{html.escape(href)}</d:href>"
            "<d:propstat><d:prop>"
            f"{props}"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )

    def do_PROPFIND(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._send(401)
            return
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path.rstrip("/") or "/"
        if path in ("/dav.php", ""):
            props = (
                "<d:current-user-principal>"
                "<d:href>/dav.php/principals/test/</d:href>"
                "</d:current-user-principal>"
            )
            body = self._response("/dav.php/", props)
        elif path == "/dav.php/principals/test":
            props = (
                "<d:displayname>Test User</d:displayname>"
                "<c:calendar-home-set>"
                "<d:href>/dav.php/calendars/test/</d:href>"
                "</c:calendar-home-set>"
                "<c:calendar-user-address-set>"
                "<d:href>mailto:test@example.test</d:href>"
                "</c:calendar-user-address-set>"
            )
            body = self._response("/dav.php/principals/test/", props)
        elif path == "/dav.php/calendars/test":
            home = self._response(
                "/dav.php/calendars/test/",
                "<d:resourcetype><d:collection/></d:resourcetype>",
            )
            calendar = self._response(
                "/dav.php/calendars/test/personal/",
                "<d:displayname>Personal</d:displayname>"
                "<d:resourcetype><d:collection/><c:calendar/>"
                "</d:resourcetype>"
                "<d:owner><d:href>/dav.php/principals/test/</d:href>"
                "</d:owner>"
                "<d:current-user-privilege-set>"
                "<d:privilege><d:read/></d:privilege>"
                "<d:privilege><d:write/></d:privilege>"
                "</d:current-user-privilege-set>"
                "<c:supported-calendar-component-set>"
                "<c:comp name=\"VEVENT\"/>"
                "</c:supported-calendar-component-set>"
                "<a:calendar-color>#336699</a:calendar-color>",
            )
            body = home + calendar
        else:
            self._send(404)
            return
        self._xml(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            "<d:multistatus xmlns:d=\"DAV:\" "
            "xmlns:c=\"urn:ietf:params:xml:ns:caldav\" "
            "xmlns:a=\"http://apple.com/ns/ical/\">"
            f"{body}</d:multistatus>"
        )

    def do_REPORT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._send(401)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        events = self.server.events  # type: ignore[attr-defined]
        records = ""
        if self.path.rstrip("/") == "/dav.php/calendars/test/personal":
            for href, item in events.items():
                if b"evt-1" in body and "evt-1" not in href:
                    continue
                records += self._response(
                    href,
                    "<d:getetag>"
                    f"\"{item['etag']}\""
                    "</d:getetag>"
                    "<c:calendar-data>"
                    f"{html.escape(item['ics'])}"
                    "</c:calendar-data>",
                )
        self._xml(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            "<d:multistatus xmlns:d=\"DAV:\" "
            "xmlns:c=\"urn:ietf:params:xml:ns:caldav\">"
            f"{records}</d:multistatus>"
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._send(401)
            return
        item = self.server.events.get(self.path)  # type: ignore[attr-defined]
        if item is None:
            self._send(404)
            return
        self._send(
            200, item["ics"].encode("utf-8"),
            "text/calendar; charset=utf-8",
        )

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._send(401)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        events = self.server.events  # type: ignore[attr-defined]
        status = 204 if self.path in events else 201
        etag = str(int(events.get(self.path, {}).get("etag", "0")) + 1)
        events[self.path] = {
            "ics": body.decode("utf-8"),
            "etag": etag,
        }
        self.send_response(status)
        self.send_header("ETag", f"\"{etag}\"")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._send(401)
            return
        events = self.server.events  # type: ignore[attr-defined]
        if self.path not in events:
            self._send(404)
            return
        del events[self.path]
        self._send(204)

    def log_message(self, *args) -> None:  # keep the test output clean
        return


@contextlib.contextmanager
def _caldav_stub(username: str = "test", password: str = "secret"):
    import base64

    server = ThreadingHTTPServer(("127.0.0.1", 0), _DavHandler)
    server.events = {  # type: ignore[attr-defined]
        EVENT_PATH: {"ics": ICS, "etag": "1"},
    }
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    server.auth = f"Basic {token}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/dav.php", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# CalDAV round trip
# ---------------------------------------------------------------------------

def test_list_calendars_and_events(tmp_path):
    module = _load_module()
    with _caldav_stub() as (url, _server):
        module.configure(str(_config(tmp_path / "calendar.json", url=url)))

        listing = module.calendar_list_calendars()
        assert listing["count"] == 1
        assert listing["calendars"][0]["name"] == "Personal"
        assert listing["calendars"][0]["read_only"] is False

        result = module.calendar_list_events({
            "calendar": "Personal",
            "start": "2026-11-01T00:00:00Z",
            "end": "2026-12-01T00:00:00Z",
        })
        assert result["calendar"] == "Personal"
        assert result["count"] == 1
        event = result["events"][0]
        assert event["uid"] == "evt-1"
        assert event["summary"] == "Planning, with team"
        assert event["recurring"] is True
        assert event["href"].endswith("/evt-1.ics")
        assert result["sources"] == [event["href"]]


def test_write_round_trip_uses_etags(tmp_path):
    module = _load_module()
    with _caldav_stub() as (url, server):
        module.configure(str(_config(tmp_path / "calendar.json", url=url)))

        created = module.calendar_create_event({
            "summary": "New event",
            "start": "2026-12-01T10:00:00Z",
            "end": "2026-12-01T11:00:00Z",
            "calendar": "Personal",
            "description": "Created by the test",
        })
        assert created["status"] == "created"
        uid = created["uid"]
        href = created["href"]
        event_path = urllib.parse.urlsplit(href).path
        assert href.endswith(".ics")
        stored = server.events[event_path]
        assert "New event" in stored["ics"]

        updated = module.calendar_update_event({
            "uid": uid,
            "calendar": "Personal",
            "summary": "Renamed event",
        })
        assert updated["status"] == "updated"
        stored = server.events[event_path]
        assert "Renamed event" in stored["ics"]

        deleted = module.calendar_delete_event({
            "uid": uid,
            "calendar": "Personal",
        })
        assert deleted["status"] == "deleted"
        assert event_path not in server.events


def test_field_update_survives_a_missing_dtend(tmp_path):
    """RFC 5545 allows a timed VEVENT without DTEND; a rename must still work."""
    module = _load_module()
    path = "/dav.php/calendars/test/personal/evt-noend.ics"
    with _caldav_stub() as (url, server):
        server.events[path] = {"ics": NO_END_ICS, "etag": "1"}
        module.configure(str(_config(tmp_path / "calendar.json", url=url)))

        updated = module.calendar_update_event({
            "uid": "evt-noend",
            "calendar": "Personal",
            "summary": "Renamed",
        })

        assert updated["status"] == "updated"
        assert updated["start"] == ""
        assert updated["end"] == ""
        assert "SUMMARY:Renamed" in server.events[path]["ics"]
        assert "DTSTART:20261121T090000Z" in server.events[path]["ics"]


def test_unknown_uid_never_targets_another_event(tmp_path):
    module = _load_module()
    with _caldav_stub() as (url, server):
        module.configure(str(_config(tmp_path / "calendar.json", url=url)))
        try:
            module.calendar_delete_event({
                "uid": "missing-uid",
                "calendar": "Personal",
            })
        except module.CalendarError as exc:
            assert "no event with UID" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("an unknown UID must not delete anything")
    assert EVENT_PATH in server.events
