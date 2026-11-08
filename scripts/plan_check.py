#!/usr/bin/env python3
"""Plan gate for docs/plan/implementation-plan.md.

Reads three machine-readable markdown tables delimited by HTML comments:

    <!-- plan:days:start -->    | Day | Date | Weekday | Start | Cap h | Lunch after h |
    <!-- plan:lanes:start -->   | Lane | Owner | Files |
    <!-- plan:tickets:start --> | ID | Title | Pri | Type | Lane | Day | Start | Hours |
                                | Depends | Trace | Acceptance |

and one settings line:  <!-- plan:config freeze=Tue deadline=Wed max_lane_day=8 -->

Checks (each failure is one line starting with "ERROR"):
  * weekday of every date is recomputed with datetime and must match the table;
  * at most 6 lanes; file ownership of lanes is disjoint (no path equal to or
    a prefix of another lane's path);
  * per lane-day: hours sum <= max_lane_day and <= the day's cap, no overlap,
    start + hours <= cap;
  * every dependency exists (missing -> clear error), no cycle, and each
    dependency ends no later than the dependent starts (same day: by hour);
  * a MUST never depends on a SHOULD;
  * build/test/spike tickets finish on or before the freeze day; the last
    ticket finishes on or before the deadline day;
  * every ticket has a non-empty Trace and Acceptance.

Prints a lane x day calendar with wall-clock times. Exit 0 = ok, 1 = errors,
2 = the file could not be parsed at all.

Usage: python3 scripts/plan_check.py [path/to/implementation-plan.md]
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_PLAN = Path(__file__).resolve().parent.parent / "docs" / "plan" / "implementation-plan.md"
MAX_LANES = 6
PRIORITIES = {"MUST", "SHOULD"}
TYPES = {"spike", "build", "test", "docs", "review", "fix", "release"}
FROZEN_TYPES = {"spike", "build", "test"}
EPS = 1e-9


def table(text: str, name: str) -> list[dict[str, Any]]:
    m = re.search(rf"<!-- plan:{name}:start -->(.*?)<!-- plan:{name}:end -->", text, re.S)
    if not m:
        raise ValueError(f"missing table markers <!-- plan:{name}:start/end -->")
    rows = [ln.strip() for ln in m.group(1).splitlines() if ln.strip().startswith("|")]
    if len(rows) < 2:
        raise ValueError(f"table {name!r} has no rows")

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header = cells(rows[0])
    out = []
    for ln in rows[2:]:  # rows[1] is the |---| separator
        values = cells(ln)
        if len(values) != len(header):
            raise ValueError(
                f"table {name!r}: row has {len(values)} cells, header has {len(header)}: {ln[:80]}"
            )
        out.append(dict(zip(header, values, strict=False)))
    return out


def config(text: str) -> dict[str, str]:
    m = re.search(r"<!-- plan:config ([^>]*?)-->", text)
    if not m:
        raise ValueError("missing <!-- plan:config ... --> line")
    return dict(kv.split("=", 1) for kv in m.group(1).split())


def num(value: str, what: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{what}: {value!r} is not a number") from None


def wall(day: dict[str, Any], offset: float, *, is_end: bool = False) -> str:
    """Wall-clock time of a working-hour offset; the lunch hour is skipped.

    A start exactly at the lunch boundary begins after lunch; an end exactly at
    it finishes before lunch. Times past midnight are shown as HH:MM+1d."""
    start_h, start_m = (int(x) for x in day["Start"].split(":"))
    lunch_after = day.get("Lunch after h", "-")
    extra = 0.0
    if lunch_after not in ("-", ""):
        boundary = float(lunch_after)
        if offset > boundary + EPS or (not is_end and offset >= boundary - EPS):
            extra = 1.0
    minutes = start_h * 60 + start_m + round((offset + extra) * 60)
    days, minutes = divmod(minutes, 24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}" + (f"+{days}d" if days else "")


def check(text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    report: list[str] = []
    cfg = config(text)
    days = table(text, "days")
    lanes = table(text, "lanes")
    tickets = table(text, "tickets")
    max_lane_day = num(cfg.get("max_lane_day", "8"), "max_lane_day")

    # Days: recompute weekdays.
    day_by_name: dict[str, dict[str, Any]] = {}
    day_index: dict[str, int] = {}
    for i, d in enumerate(days):
        try:
            date = dt.date.fromisoformat(d["Date"])
        except ValueError:
            errors.append(f"ERROR day {d['Day']}: bad date {d['Date']!r}")
            continue
        real = date.strftime("%a")
        if real != d["Weekday"]:
            errors.append(
                f"ERROR day {d['Day']}: {d['Date']} is a {real}, table says {d['Weekday']}"
            )
        if d["Day"] != real:
            errors.append(
                f"ERROR day label {d['Day']!r} does not match computed "
                f"weekday {real} of {d['Date']}"
            )
        d["_cap"] = num(d["Cap h"], f"day {d['Day']} cap")
        day_by_name[d["Day"]] = d
        day_index[d["Day"]] = i
    for key in ("freeze", "deadline"):
        if cfg.get(key) not in day_by_name:
            errors.append(f"ERROR config {key}={cfg.get(key)!r} is not a day in the days table")

    # Lanes: count and disjoint file ownership.
    if len(lanes) > MAX_LANES:
        errors.append(f"ERROR {len(lanes)} lanes; the limit is {MAX_LANES}")
    lane_names = {ln["Lane"] for ln in lanes}
    owned: list[tuple[str, str]] = []
    for ln in lanes:
        for path in (p.strip().strip("`") for p in ln["Files"].split(",")):
            if path:
                owned.append((ln["Lane"], path))
    for i, (la, pa) in enumerate(owned):
        for lb, pb in owned[i + 1 :]:
            if la == lb:
                continue
            a, b = pa.rstrip("/"), pb.rstrip("/")
            if a == b or b.startswith(a + "/") or a.startswith(b + "/"):
                errors.append(f"ERROR file ownership overlaps: {pa} ({la}) vs {pb} ({lb})")

    # Tickets: fields.
    by_id: dict[str, dict[str, Any]] = {}
    for t in tickets:
        tid = t["ID"]
        if tid in by_id:
            errors.append(f"ERROR duplicate ticket id {tid}")
        by_id[tid] = t
        if t["Pri"] not in PRIORITIES:
            errors.append(f"ERROR {tid}: priority {t['Pri']!r} not in {sorted(PRIORITIES)}")
        if t["Type"] not in TYPES:
            errors.append(f"ERROR {tid}: type {t['Type']!r} not in {sorted(TYPES)}")
        if t["Lane"] not in lane_names:
            errors.append(f"ERROR {tid}: lane {t['Lane']!r} is not defined in the lanes table")
        if t["Day"] not in day_by_name:
            errors.append(f"ERROR {tid}: day {t['Day']!r} is not defined in the days table")
        for field in ("Trace", "Acceptance", "Title"):
            if t[field] in ("", "-"):
                errors.append(f"ERROR {tid}: empty {field}")
        try:
            t["_start"] = num(t["Start"], f"{tid} start")
            t["_hours"] = num(t["Hours"], f"{tid} hours")
        except ValueError as exc:
            errors.append(f"ERROR {exc}")
            t["_start"], t["_hours"] = 0.0, 0.0
        if t["_hours"] <= 0:
            errors.append(f"ERROR {tid}: hours must be > 0")
        t["_end"] = t["_start"] + t["_hours"]
        t["_deps"] = [d.strip() for d in t["Depends"].split(",") if d.strip() not in ("", "-")]

    # Capacity and overlap per lane-day.
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in tickets:
        cells[(t["Lane"], t["Day"])].append(t)
    for (lane, day), items in sorted(cells.items()):
        if day not in day_by_name:
            continue
        cap = day_by_name[day]["_cap"]
        total = sum(t["_hours"] for t in items)
        if total > max_lane_day + EPS:
            errors.append(
                f"ERROR capacity {lane} {day}: {total:g} h > {max_lane_day:g} h per lane-day"
            )
        if total > cap + EPS:
            errors.append(f"ERROR capacity {lane} {day}: {total:g} h > day cap {cap:g} h")
        items.sort(key=lambda t: t["_start"])
        for t in items:
            if t["_start"] < -EPS or t["_end"] > cap + EPS:
                errors.append(
                    f"ERROR {t['ID']}: runs {t['_start']:g}-{t['_end']:g} h, "
                    f"outside {day} 0-{cap:g} h"
                )
        for a, b in zip(items, items[1:], strict=False):
            if b["_start"] < a["_end"] - EPS:
                errors.append(
                    f"ERROR overlap {lane} {day}: {a['ID']} "
                    f"({a['_start']:g}-{a['_end']:g}) "
                    f"and {b['ID']} ({b['_start']:g}-{b['_end']:g})"
                )

    # Dependencies: existence, MUST/SHOULD, order.
    for t in tickets:
        for dep in t["_deps"]:
            if dep not in by_id:
                errors.append(
                    f"ERROR {t['ID']} depends on {dep}, which is not defined in the tickets table"
                )
                continue
            d = by_id[dep]
            if t["Pri"] == "MUST" and d["Pri"] == "SHOULD":
                errors.append(f"ERROR {t['ID']} is MUST but depends on SHOULD {dep}")
            if t["Day"] in day_index and d["Day"] in day_index:
                dep_day, own_day = day_index[d["Day"]], day_index[t["Day"]]
                if dep_day > own_day or (dep_day == own_day and d["_end"] > t["_start"] + EPS):
                    errors.append(
                        f"ERROR {t['ID']} starts {t['Day']} "
                        f"+{t['_start']:g} h before its dependency "
                        f"{dep} ends ({d['Day']} +{d['_end']:g} h)"
                    )

    # Cycles (iterative DFS, report the path).
    WHITE, GREY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in by_id}
    reported: set[frozenset] = set()
    for root in by_id:
        if color[root] != WHITE:
            continue
        stack = [(root, iter(by_id[root]["_deps"]))]
        path = [root]
        color[root] = GREY
        while stack:
            node, it = stack[-1]
            nxt = next((d for d in it if d in by_id), None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if color[nxt] == GREY:
                cyc = path[path.index(nxt) :] + [nxt]
                key = frozenset(cyc)
                if key not in reported:
                    reported.add(key)
                    errors.append("ERROR dependency cycle: " + " -> ".join(cyc))
            elif color[nxt] == WHITE:
                color[nxt] = GREY
                stack.append((nxt, iter(by_id[nxt]["_deps"])))
                path.append(nxt)

    # Freeze and deadline.
    if cfg.get("freeze") in day_index and cfg.get("deadline") in day_index:
        fz, dl = day_index[cfg["freeze"]], day_index[cfg["deadline"]]
        for t in tickets:
            if t["Day"] not in day_index:
                continue
            if t["Type"] in FROZEN_TYPES and day_index[t["Day"]] > fz:
                errors.append(
                    f"ERROR {t['ID']} ({t['Type']}) is scheduled {t['Day']}, "
                    f"after the freeze ({cfg['freeze']})"
                )
            if day_index[t["Day"]] > dl:
                errors.append(
                    f"ERROR {t['ID']} is scheduled {t['Day']}, "
                    f"after the deadline day ({cfg['deadline']})"
                )

    # Report.
    musts = sum(1 for t in tickets if t["Pri"] == "MUST")
    report.append(
        f"plan_check: {len(tickets)} tickets "
        f"({musts} MUST, {len(tickets) - musts} SHOULD), "
        f"{len(lanes)} lanes, {len(days)} days, "
        f"{sum(t['_hours'] for t in tickets):g} planned hours"
    )
    for d in days:
        if "_cap" in d:
            weekday = dt.date.fromisoformat(d["Date"]).strftime("%A")
            report.append(
                f"  {d['Day']} {d['Date']} (computed {weekday}), "
                f"starts {d['Start']}, cap {d['_cap']:g} h"
            )
    report.append("calendar (lane / day: ticket start-end wall clock):")
    for ln in lanes:
        for d in days:
            items = sorted(cells.get((ln["Lane"], d["Day"]), []), key=lambda t: t["_start"])
            if not items or "_cap" not in d:
                continue
            total = sum(t["_hours"] for t in items)
            spans = ", ".join(
                f"{t['ID']} {wall(d, t['_start'])}-{wall(d, t['_end'], is_end=True)}" for t in items
            )
            room = min(d["_cap"], max_lane_day)
            report.append(f"  {ln['Lane']} {d['Day']} [{total:g}/{room:g} h] {spans}")
    return errors, report


def main(argv: list[str]) -> int:
    if len(argv) <= 1 and not DEFAULT_PLAN.is_file():
        # The default plan is internal and absent from the published tree: nothing to check.
        print(f"SKIP plan check: {DEFAULT_PLAN.name} is not in this tree")
        return 0
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PLAN
    try:
        text = path.read_text(encoding="utf-8")
        errors, report = check(text)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR cannot check {path}: {exc}")
        return 2
    for line in report:
        print(line)
    for line in errors:
        print(line)
    print(f"RESULT: {'FAIL' if errors else 'OK'} ({len(errors)} error(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
