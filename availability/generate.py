#!/usr/bin/env python3
"""Generate a minimal availability page from one or more ICS calendars.

Usage (from the repo root): python availability/generate.py availability/calendars.json
Shows only busy/free, colored by calendar. Event titles are never written out.
All times are US Eastern.
"""
import html
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

TZ = ZoneInfo("America/New_York")
SLOT = 30  # minutes


def load_ics(source):
    if source.startswith("env:"):
        source = os.environ[source[4:]]
    if source.startswith("http"):
        with urllib.request.urlopen(source, timeout=30) as r:
            return r.read()
    with open(source, "rb") as f:
        return f.read()


def to_eastern(dt):
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def busy_events(ics_bytes, start, end):
    cal = icalendar.Calendar.from_ical(ics_bytes)
    for ev in recurring_ical_events.of(cal).between(start, end):
        s, e = ev.get("DTSTART").dt, ev.get("DTEND").dt
        if not isinstance(s, datetime):  # skip all-day events
            continue
        if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper() == "FREE":
            continue
        yield to_eastern(s), to_eastern(e)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "calendars.json")))
    days = cfg.get("days", 30)
    h0, h1 = cfg.get("start_hour", 0), cfg.get("end_hour", 24)
    cals = cfg["calendars"]  # earlier calendars win when events overlap

    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    nslots = (h1 - h0) * 60 // SLOT
    grid = [[None] * nslots for _ in range(days)]

    for ci, c in enumerate(cals):
        data = load_ics(c["source"])
        for s, e in busy_events(data, today, today + timedelta(days=days)):
            for d in range(days):
                day0 = today + timedelta(days=d, hours=h0)
                for i in range(nslots):
                    a = day0 + timedelta(minutes=SLOT * i)
                    b = a + timedelta(minutes=SLOT)
                    if s < b and e > a and grid[d][i] is None:
                        grid[d][i] = ci

    css_colors = "\n".join(f".c{i}{{background:{c['color']}}}" for i, c in enumerate(cals))
    head = "<th></th>" + "".join(f'<th colspan="2">{h}</th>' for h in range(h0, h1))
    rows = []
    for d in range(days):
        day = today + timedelta(days=d)
        cells = "".join(
            f'<td class="c{grid[d][i]}"></td>' if grid[d][i] is not None else "<td></td>"
            for i in range(nslots)
        )
        rows.append(f"<tr><th>{day.strftime('%a %b %d')}</th>{cells}</tr>")

    # text list: free ranges inside the working window, weekdays only
    t0, t1 = cfg.get("text_start_hour", 9) * 60, cfg.get("text_end_hour", 17) * 60
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    items = []
    for d in range(days):
        day = today + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        ranges = []
        for i in range(nslots):
            m = h0 * 60 + SLOT * i
            if grid[d][i] is not None or not (t0 <= m < t1) or day + timedelta(minutes=m) < now:
                continue
            if ranges and ranges[-1][1] == m:
                ranges[-1][1] = m + SLOT
            else:
                ranges.append([m, m + SLOT])
        txt = ", ".join(f"{fmt(a)}\u2013{fmt(b)}" for a, b in ranges) or "No available times"
        items.append(f"<li>{day.strftime('%a %b %d')}: {txt}</li>")

    legend = " ".join(
        f'<span class="k c{i}"></span> {html.escape(c["name"])}' for i, c in enumerate(cals)
    )
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beniamino's Availability</title>
<link rel="stylesheet" href="{cfg.get('stylesheet', '/style.css')}">
<style>
.s{{overflow-x:auto}}
table{{border-collapse:separate;border-spacing:1px;margin:1em 0}}
th{{font-weight:normal;text-align:left;padding:0 6px 0 0;white-space:nowrap}}
thead th{{font-size:10px;padding:0}}
td{{width:10px;height:10px;background:#e4e4e4;padding:0}}
.k{{display:inline-block;width:10px;height:10px;vertical-align:middle;margin-left:1em}}
.k:first-child{{margin-left:0}}
{css_colors}
</style></head><body>
<h1>Beniamino's Availability:</h1>
<p>All times EST. Updated {now.strftime('%Y-%m-%d %H:%M')}.</p>
<div class="s"><table><thead><tr>{head}</tr></thead><tbody>
{chr(10).join(rows)}
</tbody></table></div>
<p><span class="k" style="margin-left:0;background:#e4e4e4"></span> free {legend}</p>
<h3> Business Hours Availability (for copying):</h3>
<ul>
{chr(10).join(items)}
</ul>
<p><a href="/">Home</a></p>
</body></html>
"""
    out = cfg.get("output", "availability.html")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        f.write(page)
    print("wrote", out)


if __name__ == "__main__":
    main()
