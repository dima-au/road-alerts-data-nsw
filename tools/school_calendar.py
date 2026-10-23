"""Adds NSW school terms and public holidays to a road database, so school zones are off in the
holidays without internet in the car.

    python tools/school_calendar.py [build/roads.db]

Also run by build_road_db.py. Sources:
  terms     https://education.nsw.gov.au/schooling/calendars/<year>  (Eastern division: first school
            development day to last day for students, each term; school zones run on those days)
  holidays  https://date.nager.at/api/v3/PublicHolidays/<year>/AU    (national ones and AU-NSW)
Stored in meta as comma-separated ISO dates: school_terms_NSW "2026-01-27..2026-04-02,...",
public_holidays_NSW "2026-01-01,...", and calendar_years_NSW "2026,2027" (the years covered).
Must agree with SchoolCalendar.kt.
"""
import datetime
import html
import json
import re
import sqlite3
import sys
import urllib.request

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"])}
DATE = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (\d{1,2}) (" + "|".join(MONTHS) + r")(?: (\d{4}))?"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (RoadAlerts calendar)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def page_text(year):
    t = fetch(f"https://education.nsw.gov.au/schooling/calendars/{year}")
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t))


def day(year, d, month, explicit_year):
    return datetime.date(int(explicit_year or year), MONTHS[month], int(d))


def terms(year):
    """[(start, end)] per term: from the first school development day to the last day for students."""
    t = page_text(year)
    east = t[t.index("Eastern division"):t.index("Western division")]
    students = east[:east.index("School development days")]
    development = east[east.index("School development days"):east.index("School holidays")]
    out = []
    for n in range(1, 5):
        m = re.search(rf"Term {n}\s*:\s*{DATE} to {DATE}", students)
        if not m:
            raise ValueError(f"{year} term {n}: no dates on the page")
        first = day(year, *m.groups()[0:3])
        last = day(year, *m.groups()[3:6])
        dev = re.search(rf"Term {n}\s*:\s*{DATE}", development)
        if dev:
            first = min(first, day(year, *dev.groups()[0:3]))
        out.append((first, last))
    return out


def holidays(year):
    data = json.loads(fetch(f"https://date.nager.at/api/v3/PublicHolidays/{year}/AU"))
    return sorted({h["date"] for h in data if not h.get("counties") or "AU-NSW" in h["counties"]})


def add(db_path, years=None):
    this = datetime.date.today().year
    years = years or [this, this + 1]
    got_terms, got_holidays, covered = [], [], []
    for y in years:
        try:
            got_terms += terms(y)
            got_holidays += holidays(y)
            covered.append(y)
        except Exception as e:  # a year not published yet: zones fall back to every weekday then
            print(f"  calendar {y}: {e}", flush=True)
    db = sqlite3.connect(db_path)
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("school_terms_NSW", ",".join(f"{a}..{b}" for a, b in got_terms)),
        ("public_holidays_NSW", ",".join(got_holidays)),
        ("calendar_years_NSW", ",".join(map(str, covered))),
    ])
    db.commit()
    db.close()
    print(f"NSW calendar {covered}: {len(got_terms)} terms, {len(got_holidays)} public holidays")


if __name__ == "__main__":
    add(sys.argv[1] if len(sys.argv) > 1 else "build/roads.db")
