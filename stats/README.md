# Time stats

One row per day, with 30-minute blocks grouped by activity. The page at `/stats/`
shows **30 days ending today, newest first**, in US Eastern time. It is a daily summary, not a
timeline of when activities happened. Python 3.12 and curl for fetching;
no third-party Python packages.

The page uses the same `/style.css` as `/availability/`: the site's 14px
monospace font, default headings, and 650px page width. Its table follows the
availability page's compact 10px-wide cells, 1px gaps, and full date labels.
Dates are plain text. The legend sits above the grid; nothing appears below it.
There are no buttons, selection states, or page scripts.
The standalone demo embeds that same stylesheet so it also works offline.

## Choose what appears

Edit `stats/config.json`. Set each activity's `include` to `true` or `false` and
edit its hex `color`. List order sets the order of colors in each row.
Names match without regard to capitalization. Unlisted and disabled activities
contribute nothing to public totals.

Initially enabled: **Deep Work, Shallow work, Reading**. These are Timetracker
activity names, not its broader categories such as Work or Health. In particular,
Shallow work works even if you have not assigned it a category in Timetracker.

`days: 30` controls the rolling window. `include_today: true` includes today so
far; set it to `false` for the last 30 completed days. Each row covers **24 hours**
(48 half-hour blocks), with room for sleep. To include sleep, change its
`include` value to `true`. The scale can expand to fit a 25-hour daylight-saving
day. Partial blocks retain shorter durations. If included
activities overlap, the first one in the config wins for that stretch of time;
minutes are never counted twice.

## Connect the hourly build

1. In Timetracker, create a personal token with **Read your tracked time**.
2. In the GitHub repository, add an Actions secret named **TIMETRACKER_KEY**.
3. Copy the files in this package into the repository. The included workflow
   runs the fetcher on the existing hourly schedule, pushes, and manual runs.

Your successful curl request confirms personal-token Bearer authentication at
`https://api.timetracker.live/api/mcp`. The previous Python HTTP client was
blocked with Cloudflare error 1010. The fetcher now invokes the **actual curl
executable** using the same Authorization, Content-Type, and Accept headers.
It sends `tools/call` directly, without an initialization handshake.

For this fix, replace only `stats/timetracker.py`; keep your edited config.
Curl must be on PATH (the same `curl` command you already used). The token is
passed on stdin and is never put in curl's command-line arguments or a file.
Curl uses its native client identity and TLS defaults. No shell is invoked.

The script uses `list_time_entries` for daily totals; `where_my_time_went`
returns totals for the whole requested period. The entry tool's arguments,
fields, and pagination come from the supplied Claude handoff.

With no secret configured, the workflow generates the honest no-data page.
With a secret configured, a failed fetch **stops deployment**, preserving the
previously published site. This prevents incomplete fetches from silently
becoming an empty chart.

To run locally with the same shell variable that worked in curl:

```sh
env TIMETRACKER_KEY="$TIMETRACKER_KEY" python3 stats/generate.py --fetch
```

Your shell expands variables in a curl command; Python only inherits exported variables.
The `env` command above passes the key explicitly. Alternatively, export it
before running the script. If you are already inside `stats/`, use `generate.py`
instead of `stats/generate.py`.

If fetching still fails, the error distinguishes a missing key, HTTP 401,
HTTP 403, a JSON-RPC error, and a tool error. A 403 may be a permission or
firewall issue. The client stops on access errors without retries or alternate
authentication. Report the error text, not the token.

No token is embedded in the files. Raw entries, notes, tags, and entry timestamps
are not written to the public page or to a local data cache. Only selected daily
totals reach the HTML.

## Preview without connecting

```sh
python stats/generate.py --demo
```

Open `stats/preview.html`. This uses invented data and cannot write inside the
published `static/` directory. The ZIP also includes this as `preview.html`.

For your own offline daily totals, the renderer accepts this internal format
(it is not a native Timetracker export):

```json
{
  "schema_version": 1,
  "timezone": "America/New_York",
  "records": [
    {"date": "2026-10-27", "activity": "Deep Work", "minutes": 240},
    {"date": "2026-10-27", "activity": "Reading", "minutes": 45}
  ]
}
```

Save as `stats/daily-totals.json`, then run `python stats/generate.py`.
The input and output are gitignored.

## API details and limits

The fetcher calls only the read-only `list_time_entries` tool through curl,
directly as in the working curl request. It supports plain JSON and SSE replies,
and JSON text or structured tool results. A complete SSE result is returned
without waiting for the connection to close. Requests time out after 30 seconds.
It follows `next_offset` until null with `limit: 200`, and deduplicates entry IDs.

Dates are requested in the account timezone, which must match the config.
UTC timestamps determine elapsed time, including daylight-saving changes.
Entries split at local midnight; open entries stop at the fetch time. The query
includes a day of padding on each side, then clips to exactly 30 displayed days.
The provider's handling of entries that start more than a day before the queried
window and continue into it has not been verified. Returned multi-day entries
are handled correctly.

The revised fetcher has not been run against your live account here. A full
Zola build also has not been tested here. The tests run the actual curl binary
against a local HTTP server to check headers, request bodies, JSON/SSE responses,
pagination, 403 handling, and refusal to follow redirects. Other checks cover
privacy filtering, overlap handling, midnight, daylight-saving changes, and
preserving the last page on failure. All 18 checks pass.

```sh
python -m unittest discover -s stats -p 'test_*.py'
```
