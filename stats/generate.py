#!/usr/bin/env python3
"""Generate /stats/ as static HTML. Python 3.12; no third-party dependencies.

Use --fetch with an exported TIMETRACKER_KEY to read Timetracker, or --input
for offline daily-totals JSON. Fetching requires curl. See README.md for setup.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent


def fail(message):
    raise ValueError(message)


def key(name):
    return name.strip().casefold()


def read_config(path):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for field, lo, hi in (("days", 1, 366), ("minutes_per_block", 5, 60),
                          ("minimum_hours_per_row", 1, 26)):
        value = cfg.get(field)
        if type(value) is not int or not lo <= value <= hi:
            fail(f"{field} must be an integer from {lo} to {hi}.")
    if 60 % cfg["minutes_per_block"]:
        fail("minutes_per_block must divide evenly into 60.")
    ZoneInfo(cfg["timezone"])
    if type(cfg.get("include_today")) is not bool:
        fail("include_today must be true or false.")
    if not isinstance(cfg.get("title"), str) or not cfg["title"].strip():
        fail("title must be a nonempty string.")
    for field in ("data_file", "output"):
        if not isinstance(cfg.get(field), str) or not cfg[field].strip():
            fail(f"{field} must be a file path.")
    seen = set()
    if not isinstance(cfg.get("activities"), list):
        fail("activities must be a list.")
    for activity in cfg["activities"]:
        name = activity.get("name")
        if not isinstance(name, str) or not name.strip() or key(name) in seen:
            fail("Activity names must be nonempty and unique, ignoring case.")
        seen.add(key(name))
        if type(activity.get("include")) is not bool:
            fail("Every activity must have include set to true or false.")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", activity.get("color", "")):
            fail("Activity colors must be six-digit hex values such as #43815c.")
    if not any(a["include"] for a in cfg["activities"]):
        fail("Enable at least one activity in the config.")
    return cfg


def window(cfg, today):
    last = today if cfg["include_today"] else today - timedelta(days=1)
    return [last - timedelta(days=i) for i in reversed(range(cfg["days"]))]


def aggregate(cfg, payload, days):
    """Apply the inclusion list before calculating any public totals."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        fail("Expected daily-totals JSON with schema_version: 1. See stats/README.md.")
    if payload.get("timezone") != cfg["timezone"]:
        fail("Input dates must use the same timezone as stats/config.json.")
    if not isinstance(payload.get("records"), list):
        fail("Input must contain a records list.")
    activities = [a for a in cfg["activities"] if a["include"]]
    indices = {key(a["name"]): i for i, a in enumerate(activities)}
    totals = {day: [0.0] * len(activities) for day in days}
    for record in payload["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("activity"), str):
            fail("Each input record must contain an activity name.")
        index = indices.get(key(record["activity"]))
        if index is None:
            continue
        try:
            day = date.fromisoformat(record["date"])
        except (KeyError, TypeError, ValueError):
            fail("Included records must have dates in YYYY-MM-DD format.")
        if day not in totals:
            continue
        minutes = record.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            fail("Included records must contain numeric minutes.")
        if not math.isfinite(minutes) or minutes < 0:
            fail("Included minutes must be finite and nonnegative.")
        totals[day][index] += minutes
    tz = ZoneInfo(cfg["timezone"])
    for day, values in totals.items():
        start = datetime.combine(day, time(), tzinfo=tz)
        end = datetime.combine(day + timedelta(days=1), time(), tzinfo=tz)
        day_minutes = (end.timestamp() - start.timestamp()) / 60
        if sum(values) > day_minutes + 1e-6:
            fail(f"Included time exceeds the length of {day}. Check for duplicate or overlapping records.")
    return activities, totals


def duration(minutes):
    minutes = int(math.floor(minutes + 0.5))
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h {remainder:02d}m" if hours and remainder else f"{hours}h" if hours else f"{remainder}m"


def blocks(values, block_minutes, count):
    """Pack activities in config order. A partial block preserves exact duration."""
    segments, position = [], 0.0
    for index, value in enumerate(values):
        segments.append((index, position, position + value))
        position += value
    cells = []
    for cell in range(count):
        left, right = cell * block_minutes, (cell + 1) * block_minutes
        fills = []
        for index, start, end in segments:
            overlap = max(0, min(right, end) - max(left, start))
            if overlap:
                offset = 100 * (max(left, start) - left) / block_minutes
                fills.append(f'<span class="fill c{index}" style="left:{offset:.6f}%;width:{100 * overlap / block_minutes:.6f}%"></span>')
        cells.append('<td class="block" aria-hidden="true">' + ''.join(fills) + '</td>')
    return ''.join(cells)


def render(cfg, payload, today, demo=False):
    days = window(cfg, today)
    esc = html.escape
    activities = [a for a in cfg["activities"] if a["include"]]
    colors = '\n'.join(f'.time-stats .c{i}{{background:{a["color"]}}}' for i, a in enumerate(activities))
    css = (HERE / "stats.css").read_text(encoding="utf-8") + '\n' + colors
    site_style = (f'<style>{(ROOT / "static/style.css").read_text(encoding="utf-8")}</style>'
                  if demo else '<link rel="stylesheet" href="/style.css">')
    legend = '<p class="legend" aria-label="Activities">' + ' '.join(
        f'<span class="k c{i}" aria-hidden="true"></span> {esc(a["name"])}'
        for i, a in enumerate(activities)) + '</p>'
    period = f'{days[0].strftime("%d %b")}–{days[-1].strftime("%d %b %Y")}'
    if demo:
        period += ' · Sample data'
    content = '<p class="empty">No time data published yet.</p>'
    if payload is not None:
        activities, totals = aggregate(cfg, payload, days)
        longest = max(sum(v) for v in totals.values())
        hours = max(cfg["minimum_hours_per_row"], math.ceil(longest / 60))
        count = hours * 60 // cfg["minutes_per_block"]
        ticks = ''.join(f'<th colspan="{60 // cfg["minutes_per_block"]}" scope="colgroup">{hour}</th>' for hour in range(hours))
        header = f'<thead><tr><th scope="col" aria-label="Day"></th>{ticks}</tr></thead>'
        rows = []
        for day in reversed(days):
            values = totals[day]
            total = sum(values)
            detail = day.strftime('%a %d %b') + ' · ' + ' · '.join(
                f'{a["name"]}: {duration(v)}' for a, v in zip(activities, values))
            detail += f' · Total: {duration(total)}'
            rows.append(
                f'<tr class="day" title="{esc(detail, quote=True)}" aria-label="{esc(detail, quote=True)}">'
                f'<th scope="row"><time datetime="{day.isoformat()}">{day.strftime("%a %b %d")}</time></th>'
                + blocks(values, cfg["minutes_per_block"], count)
                + '</tr>')
        content = (
            '<div class="s"><table aria-label="Daily time totals, hours grouped by activity">'
            + header + '<tbody>' + '\n'.join(rows) + '</tbody></table></div>')
    title = esc(cfg["title"])
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · beniamino.org</title>
{site_style}
<style>{css}</style>
</head><body>
<main class="time-stats" id="time-stats">
<h1>{title}</h1>
<p class="period">{esc(period)}</p>
{legend}
{content}
</main>
</body></html>
'''


def demo_data(cfg, today):
    """Explicit demo mode only; never used by the deployment workflow."""
    patterns = [(210, 90, 60), (240, 120, 30), (150, 120, 90), (270, 60, 30),
                (180, 150, 60), (0, 30, 120), (0, 0, 90)]
    records = []
    for day in window(cfg, today):
        for activity, minutes in zip(("Deep Work", "Shallow work", "Reading"), patterns[day.weekday()]):
            records.append({"date": day.isoformat(), "activity": activity, "minutes": minutes})
        records.append({"date": day.isoformat(), "activity": "Sleep", "minutes": 480})
    return {"schema_version": 1, "timezone": cfg["timezone"], "records": records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path, default=HERE / "config.json")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="Internal daily-totals JSON; see README")
    source.add_argument("--fetch", action="store_true", help="Fetch from Timetracker using TIMETRACKER_KEY")
    source.add_argument("--demo", action="store_true", help="Preview invented data outside static/")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--today", type=date.fromisoformat, help="Override date for previews/tests")
    args = parser.parse_args(argv)
    if args.fetch and args.today:
        parser.error("--today is only for offline previews/tests.")
    cfg = read_config(args.config)
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    today = args.today or now.date()
    if args.demo:
        output = args.output or HERE / "preview.html"
        if (ROOT / "static").resolve() in output.resolve().parents:
            fail("Demo output must be outside static/ so it cannot be published accidentally.")
        payload = demo_data(cfg, today)
    elif args.fetch:
        from timetracker import fetch_daily_totals
        payload = fetch_daily_totals(cfg, os.environ.get("TIMETRACKER_KEY", ""), now)
        output = args.output or ROOT / cfg["output"]
    else:
        source = args.input or ROOT / cfg["data_file"]
        output = args.output or ROOT / cfg["output"]
        if args.input and not source.exists():
            fail("The specified input file does not exist.")
        payload = json.loads(source.read_text(encoding="utf-8")) if source.exists() else None
    page = render(cfg, payload, today, demo=args.demo)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(output)
    print(f"Wrote {output}" + (" (sample data)" if args.demo else ""))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print(f"Stats generation failed: {error}", file=sys.stderr)
        sys.exit(1)
