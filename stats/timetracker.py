"""Read Timetracker's MCP API; return only selected daily activity totals.

The entry schema comes from the supplied Claude handoff. Requests are sent
by the actual curl executable, as in the user's successful command.
"""
from __future__ import annotations

import json
import subprocess
import time as clock
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from email.parser import BytesParser
from zoneinfo import ZoneInfo

from generate import key, window

ENDPOINT = "https://api.timetracker.live/api/mcp"
MAX_BYTES = 10 * 1024 * 1024


class CurlResponse:
    """Read curl's response headers; leave the body streaming for JSON/SSE."""
    def __init__(self, stream):
        self.read, self.readline = stream.read, stream.readline
        size = 0
        for _ in range(10):
            line = stream.readline(65537)
            parts = line.split()
            if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
                raise ValueError("curl did not return a valid HTTP response.")
            self.status = int(parts[1])
            size += len(line)
            lines = []
            while True:
                line = stream.readline(65537)
                size += len(line)
                if size > 65536:
                    raise ValueError("Timetracker response headers exceed 64 KiB.")
                if line in (b"\r\n", b"\n"):
                    break
                if not line:
                    raise ValueError("Timetracker response headers were incomplete.")
                lines.append(line)
            if not 100 <= self.status < 200:
                self.headers = BytesParser().parsebytes(b''.join(lines))
                return
        raise ValueError("Too many informational HTTP responses.")


def matching_message(raw, request_id):
    value = json.loads(raw)
    messages = value if isinstance(value, list) else [value]
    for message in messages:
        if (isinstance(message, dict) and message.get("id") == request_id
                and ("result" in message or "error" in message)):
            return message
    return None


def read_response(response, request_id):
    """Read JSON or a matching SSE message, without waiting for stream closure."""
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    message = None
    if content_type == "application/json":
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Timetracker response exceeds 10 MiB.")
        message = matching_message(raw, request_id)
    elif content_type == "text/event-stream":
        lines, size, deadline = [], 0, clock.monotonic() + 30
        while clock.monotonic() < deadline:
            raw = response.readline(MAX_BYTES + 1)
            size += len(raw)
            if size > MAX_BYTES:
                raise ValueError("Timetracker event stream exceeds 10 MiB.")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line and lines:
                message = matching_message("\n".join(lines), request_id)
                lines = []
                if message is not None:
                    break
            elif line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
            if not raw:
                break
    else:
        raise ValueError("Timetracker returned an unexpected response type.")
    if message is None:
        raise ValueError("No matching MCP response was received before the stream ended or timed out.")
    if "error" in message:
        # Do not print a server-provided body that might echo data or credentials.
        error = message["error"]
        code = error.get("code") if isinstance(error, dict) else None
        detail = f" (JSON-RPC code {code})" if type(code) is int else ""
        raise ValueError(f"Timetracker rejected tools/call{detail}. Check tool arguments and token permissions.")
    result = message.get("result")
    if not isinstance(result, dict):
        raise ValueError("Expected an MCP result object.")
    return result


class Client:
    def __init__(self, token):
        if not token or not token.strip():
            raise ValueError("Set/export TIMETRACKER_KEY before using --fetch. "
                             "Python cannot see a shell variable unless it is exported.")
        if "\r" in token or "\n" in token:
            raise ValueError("TIMETRACKER_KEY must be a single-line token.")
        self.token = token.strip()
        self.request_id = 0

    def post(self, method, params=None):
        self.request_id += 1
        body = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            body["params"] = params
        # Keep the credential out of argv and files: curl reads its header on stdin.
        # -q disables .curlrc; no redirects, retries, custom user agent, or TLS overrides.
        command = ["curl", "-q", "--silent", "--no-buffer", "--include",
                   "--suppress-connect-headers", "--max-time", "30",
                   "--request", "POST", ENDPOINT, "--header", "@-",
                   "--header", "Content-Type: application/json",
                   "--header", "Accept: application/json, text/event-stream",
                   "--data-binary", json.dumps(body)]
        try:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as process:
                try:
                    process.stdin.write(("Authorization: Bearer " + self.token + "\n").encode())
                    process.stdin.close()
                    response = CurlResponse(process.stdout)
                    if response.status != 200:
                        error_body = response.read(8192)
                        if response.status == 403 and b"1010" in error_body:
                            message = "Cloudflare blocked curl too (HTTP 403, error 1010). Contact Timetracker support."
                        elif response.status == 401:
                            message = "Authentication was rejected (HTTP 401). Check TIMETRACKER_KEY."
                        elif response.status == 403:
                            message = "Access denied (HTTP 403). Check permissions or provider firewall rules."
                        else:
                            message = f"Timetracker returned HTTP {response.status}."
                        raise ValueError(message + " No retry or redirect was attempted.")
                    result = read_response(response, self.request_id)
                    if response.headers.get_content_type() != "text/event-stream":
                        code = process.wait(timeout=2)
                        if code:
                            raise ValueError(f"curl transfer failed (exit {code}); no data was published.")
                    return result
                except (ValueError, BrokenPipeError) as error:
                    code = process.poll()
                    if code:
                        message = "curl timed out." if code == 28 else f"curl transfer failed (exit {code})."
                        raise ValueError(message + " No data was published.") from None
                    if isinstance(error, BrokenPipeError):
                        raise ValueError("curl stopped before accepting the request.") from None
                    raise
                finally:
                    # An SSE response can stay open after our complete result arrives.
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        except FileNotFoundError:
            raise ValueError("curl is required for --fetch. Install curl and retry.") from None
        except subprocess.TimeoutExpired:
            raise ValueError("curl did not finish in time; no data was published.") from None

    def entries(self, start, end, offset):
        # Deliberately the only exposed tool call: it cannot modify tracked time.
        result = self.post("tools/call", {"name": "list_time_entries", "arguments": {
            "from": start.isoformat(), "to": end.isoformat(), "limit": 200, "offset": offset}})
        if result.get("isError"):
            raise ValueError("list_time_entries reported an error. Check token permissions and date arguments.")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        content = result.get("content", [])
        if not isinstance(content, list):
            raise ValueError("Expected MCP text content or a structured result.")
        texts = [item["text"] for item in content
                 if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
        if len(texts) != 1:
            raise ValueError("Expected one JSON text result from list_time_entries.")
        return json.loads(texts[0])


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Included entries need ISO timestamps.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Included entry timestamps must include a UTC offset.")
    return parsed.timestamp()


def daily_totals(cfg, entries, now):
    """Split at local midnight; config order resolves overlapping activities."""
    tz = ZoneInfo(cfg["timezone"])
    if now.tzinfo is None:
        raise ValueError("The current time must include a UTC offset.")
    days = window(cfg, now.astimezone(tz).date())
    start = datetime.combine(days[0], time(), tzinfo=tz).timestamp()
    end = min(now.timestamp(), datetime.combine(days[-1] + timedelta(days=1), time(), tzinfo=tz).timestamp())
    activities = [a for a in cfg["activities"] if a["include"]]
    indices = {key(a["name"]): i for i, a in enumerate(activities)}
    events = defaultdict(Counter)
    for entry in entries:
        index = indices.get(key(entry["activity_name"]))
        if index is None:
            continue
        left = timestamp(entry.get("started_at"))
        right = now.timestamp() if entry.get("ended_at") is None else timestamp(entry["ended_at"])
        if entry.get("ended_at") is not None and right < left:
            raise ValueError("An included entry ends before it starts.")
        left, right = max(start, left), min(end, right)
        if right > left:
            events[left][index] += 1
            events[right][index] -= 1

    totals = defaultdict(float)
    active, previous = Counter(), None
    for instant in sorted(events):
        if previous is not None and active:
            index = min(active)
            left = previous
            while left < instant:
                day = datetime.fromtimestamp(left, tz).date()
                midnight = datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).timestamp()
                right = min(instant, midnight)
                totals[(day, index)] += (right - left) / 60
                left = right
        active.update(events[instant])
        active = +active  # Remove zero counts at an entry's end.
        previous = instant
    return {"schema_version": 1, "timezone": cfg["timezone"],
            "updated_at": now.astimezone(timezone.utc).isoformat(),
            "records": [{"date": day.isoformat(), "activity": activities[index]["name"], "minutes": minutes}
                        for (day, index), minutes in sorted(totals.items())]}


def fetch_daily_totals(cfg, token, now):
    client = Client(token)
    days = window(cfg, now.astimezone(ZoneInfo(cfg["timezone"])).date())
    selected = {key(a["name"]) for a in cfg["activities"] if a["include"]}
    # Pad date filters by a day, then clip precisely to the displayed period.
    first, last = days[0] - timedelta(days=1), days[-1] + timedelta(days=1)
    entries, offset = {}, 0
    for _ in range(1000):
        page = client.entries(first, last, offset)
        if not isinstance(page, dict) or not isinstance(page.get("entries"), list) or "next_offset" not in page:
            raise ValueError("Unexpected list_time_entries response; refusing to publish incomplete data.")
        if not isinstance(page.get("period"), dict) or page["period"].get("timezone") != cfg["timezone"]:
            raise ValueError("The Timetracker account and config must use the same timezone.")
        for entry in page["entries"]:
            if (not isinstance(entry, dict) or type(entry.get("entry_id")) is not int
                    or not isinstance(entry.get("activity_name"), str)):
                raise ValueError("An entry is missing its ID or activity name.")
            entry_id = entry["entry_id"]
            if key(entry["activity_name"]) in selected:
                # Never save notes, tags, or unselected activities to the output.
                entries[entry_id] = {field: entry.get(field) for field in
                                     ("activity_name", "started_at", "ended_at")}
            else:
                entries.pop(entry_id, None)
        following = page["next_offset"]
        if following is None:
            return daily_totals(cfg, entries.values(), now)
        if type(following) is not int or following <= offset:
            raise ValueError("Pagination did not advance; refusing to publish incomplete data.")
        offset = following
    raise ValueError("Too many time-entry pages; refusing to publish incomplete data.")
