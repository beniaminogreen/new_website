import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import generate
import timetracker


@contextmanager
def local_api(responses):
    """Use real curl against a local server; never send test requests externally."""
    requests, release = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({"body": body, "headers": self.headers, "path": self.path})
            response = responses[min(len(requests) - 1, len(responses) - 1)]
            if response.get("interim"):
                self.send_response_only(100)
                self.end_headers()
            self.send_response(response.get("status", 200))
            self.send_header("Content-Type", response.get("type", "application/json"))
            for name, value in response.get("headers", {}).items():
                self.send_header(name, value)
            if not response.get("hold_open"):
                self.send_header("Content-Length", str(len(response["body"])))
            self.end_headers()
            self.wfile.write(response["body"])
            self.wfile.flush()
            if response.get("hold_open"):
                release.wait(5)
                response["finished"].set()
                self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with patch.object(timetracker, "ENDPOINT", f"http://127.0.0.1:{server.server_port}/api/mcp"), \
                patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            yield requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()


def entry(entry_id, name, start, end=None):
    return {"entry_id": entry_id, "activity_name": name, "started_at": start,
            "ended_at": end, "note": "PRIVATE_NOTE", "tags": ["PRIVATE_TAG"]}


class TimetrackerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = generate.read_config(Path(__file__).with_name("config.json"))
        self.now = datetime(2026, 10, 28, 17, tzinfo=timezone.utc)

    def minutes(self, payload):
        return {(r["date"], r["activity"]): r["minutes"] for r in payload["records"]}

    def test_pagination_filters_private_data_and_deduplicates_entries(self):
        first = entry(1, "Deep Work", "2026-10-28T14:00:00Z", "2026-10-28T15:00:00Z")
        pages = [
            {"period": {"timezone": "America/New_York"}, "entries": [first], "next_offset": 1},
            {"period": {"timezone": "America/New_York"}, "entries": [first,
             entry(2, "Sleep", "2026-10-28T04:00:00Z", "2026-10-28T12:00:00Z"),
             entry(3, "Shallow work", "2026-10-28T15:00:00Z", "2026-10-28T15:30:00Z")],
             "next_offset": None},
        ]
        with patch.object(timetracker, "Client") as cls:
            client = cls.return_value
            client.entries.side_effect = pages
            data = timetracker.fetch_daily_totals(self.cfg, "test-credential", self.now)
        self.assertEqual(self.minutes(data), {("2026-10-28", "Deep Work"): 60,
                                              ("2026-10-28", "Shallow work"): 30})
        self.assertEqual([call.args[2] for call in client.entries.call_args_list], [0, 1])
        self.assertEqual(client.entries.call_args_list[0].args[:2], (date(2026, 9, 28), date(2026, 10, 29)))
        page = generate.render(self.cfg, data, self.now.date())
        for value in ("Sleep", "PRIVATE_NOTE", "PRIVATE_TAG", "test-credential", "started_at"):
            self.assertNotIn(value, json.dumps(data) + page)

    def test_midnight_active_entries_and_overlaps(self):
        data = timetracker.daily_totals(self.cfg, [
            entry(1, "Deep Work", "2026-10-28T03:30:00Z", "2026-10-28T04:30:00Z"),
            entry(2, "Shallow work", "2026-10-28T04:15:00Z", "2026-10-28T04:45:00Z"),
            entry(3, "Reading", "2026-10-28T16:00:00Z"),
            entry(4, "Deep Work", "2026-10-29T14:00:00Z", "2026-10-29T15:00:00Z"),
        ], self.now)
        self.assertEqual(self.minutes(data), {
            ("2026-10-27", "Deep Work"): 30, ("2026-10-28", "Deep Work"): 30,
            ("2026-10-28", "Shallow work"): 15, ("2026-10-28", "Reading"): 60})

    def test_dst_fall_back_uses_elapsed_time(self):
        now = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
        data = timetracker.daily_totals(self.cfg, [
            entry(1, "Deep Work", "2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00")], now)
        self.assertEqual(self.minutes(data), {("2026-11-01", "Deep Work"): 60})

    def test_dst_spring_day_is_23_hours(self):
        now = datetime(2026, 3, 9, 5, tzinfo=timezone.utc)
        data = timetracker.daily_totals(self.cfg, [
            entry(1, "Deep Work", "2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00")], now)
        self.assertEqual(self.minutes(data), {("2026-03-08", "Deep Work"): 23 * 60})
        generate.render(self.cfg, data, now.date())

    def test_exactly_30_days_and_completed_days_option(self):
        days = generate.window(self.cfg, date(2026, 10, 28))
        self.assertEqual((len(days), days[0], days[-1]), (30, date(2026, 9, 29), date(2026, 10, 28)))
        page = generate.render(self.cfg, generate.demo_data(self.cfg, self.now.date()), self.now.date())
        self.assertLess(page.index('datetime="2026-10-28"'), page.index('datetime="2026-09-29"'))
        self.cfg["include_today"] = False
        data = timetracker.daily_totals(self.cfg, [
            entry(1, "Deep Work", "2026-10-28T03:30:00Z")], self.now)
        self.assertEqual(self.minutes(data), {("2026-10-27", "Deep Work"): 30})

    def test_fetch_uses_direct_tool_calls_with_curl_headers_json_and_sse(self):
        pages = [
            {"period": {"timezone": "America/New_York"}, "entries": [
                entry(1, "Deep Work", "2026-10-28T14:00:00Z", "2026-10-28T15:00:00Z")], "next_offset": 1},
            {"period": {"timezone": "America/New_York"}, "entries": [], "next_offset": None},
        ]
        results = [{"jsonrpc": "2.0", "id": i + 1, "result": {"content": [
            {"type": "text", "text": json.dumps(page)}], "isError": False}} for i, page in enumerate(pages)]
        sse = b': heartbeat\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        sse += ("event: message\ndata: " + json.dumps(results[1]) + "\n\n").encode()
        finished = threading.Event()
        responses = [{"body": json.dumps(results[0]).encode(), "interim": True},
                     {"body": sse, "type": "text/event-stream", "hold_open": True, "finished": finished}]
        with local_api(responses) as requests, \
                patch.object(timetracker.subprocess, "Popen", wraps=subprocess.Popen) as launch:
            data = timetracker.fetch_daily_totals(self.cfg, "test-credential", self.now)
            self.assertFalse(finished.is_set(), "Client waited for the SSE stream to close")
            for call in launch.call_args_list:
                command = call.args[0]
                self.assertEqual(command[:2], ["curl", "-q"])
                self.assertNotIn("test-credential", " ".join(command))
                self.assertNotIn("--user-agent", command)
                self.assertFalse(call.kwargs.get("shell", False))
        self.assertEqual(self.minutes(data), {("2026-10-28", "Deep Work"): 60})
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request["path"], "/api/mcp")
            self.assertEqual(request["headers"]["Authorization"], "Bearer test-credential")
            self.assertEqual(request["headers"]["Content-Type"], "application/json")
            self.assertEqual(request["headers"]["Accept"], "application/json, text/event-stream")
            self.assertTrue(request["headers"]["User-Agent"].startswith("curl/"))
            self.assertIsNone(request["headers"].get("Mcp-Session-Id"))
        bodies = [request["body"] for request in requests]
        self.assertEqual([body["method"] for body in bodies], ["tools/call", "tools/call"])
        self.assertEqual([body["id"] for body in bodies], [1, 2])
        self.assertEqual(bodies[0]["params"], {"name": "list_time_entries", "arguments": {
            "from": "2026-09-28", "to": "2026-10-29", "limit": 200, "offset": 0}})
        self.assertEqual(bodies[1]["params"]["arguments"]["offset"], 1)

    def test_unexported_key_gives_clear_error_without_request(self):
        with patch.object(timetracker.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "Set/export TIMETRACKER_KEY"):
                timetracker.fetch_daily_totals(self.cfg, "", self.now)
        launch.assert_not_called()

    def test_missing_curl_explains_requirement(self):
        with patch.object(timetracker.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError, "curl is required"):
                timetracker.fetch_daily_totals(self.cfg, "test-credential", self.now)

    def test_structured_tool_result(self):
        client = timetracker.Client("test-credential")
        client.post = Mock(return_value={"structuredContent": {"entries": [], "next_offset": None}})
        self.assertEqual(client.entries(date(2026, 10, 1), date(2026, 10, 28), 0),
                         {"entries": [], "next_offset": None})

    def test_403_stops_without_retry_or_exposing_response(self):
        responses = [{"status": 403, "body": b'{"error_code":1010,"secret":"PRIVATE_ERROR"}'}]
        with local_api(responses) as requests:
            with self.assertRaisesRegex(ValueError, "Cloudflare blocked") as failure:
                timetracker.fetch_daily_totals(self.cfg, "test-credential", self.now)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("PRIVATE_ERROR", str(failure.exception))

    def test_redirect_is_not_followed(self):
        responses = [{"status": 302, "body": b"", "headers": {"Location": "/elsewhere"}}]
        with local_api(responses) as requests:
            with self.assertRaisesRegex(ValueError, "HTTP 302"):
                timetracker.fetch_daily_totals(self.cfg, "test-credential", self.now)
        self.assertEqual(len(requests), 1)

    def test_bad_pagination_is_not_published_as_complete(self):
        with patch.object(timetracker, "Client") as cls:
            cls.return_value.entries.return_value = {
                "period": {"timezone": "America/New_York"}, "entries": [], "next_offset": 0}
            with self.assertRaisesRegex(ValueError, "Pagination did not advance"):
                timetracker.fetch_daily_totals(self.cfg, "test-credential", self.now)

    def test_fetch_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "index.html"
            output.write_text("previous successful page")
            with patch.object(timetracker, "fetch_daily_totals", side_effect=ValueError("offline")):
                with self.assertRaisesRegex(ValueError, "offline"):
                    generate.main(["--fetch", "--output", str(output)])
            self.assertEqual(output.read_text(), "previous successful page")


if __name__ == "__main__":
    unittest.main()
