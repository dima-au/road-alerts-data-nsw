"""Adds Transport for NSW's official speed zones to a road database, so NSW roads get the posted
(or default) limit from the source rather than from OpenStreetMap.

    python tools/speed_zones.py [speed_zones.geojson] [build/roads.db]

The file is the "Speed Zones - GeoJSON Format" resource of
https://opendata.transport.nsw.gov.au/data/dataset/speed-zones (CC BY; ~400 MB, needs a login or
the API key: curl -L -H "Authorization: <key>" -o build/szone/speed_zones.geojson <resource url>).
Also run by build_road_db.py when the file is there.

Adds to the database:
  zones(id, kmh, kind, oneway, geom)   kind 0 = default (unsigned: 50 town / 100 country),
                                       1 = signposted (ordinary, high pedestrian, local traffic,
                                       shared, variable), 2 = school (40 on school days in
                                       school hours, over the zone underneath);
                                       oneway 1 = for travel along the line's direction only;
                                       geom as in roads
  zone_tiles(tile, zone)
  meta zones_date = the dataset's file date
Must agree with RoadDb.kt.
"""
import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_road_db import encode, tiles_of  # noqa: E402

KINDS = {
    "Default": 0,
    "Ordinary Permanent": 1, "High Pedestrian": 1, "Local Traffic": 1, "Shared": 1, "Variable": 1,
    "School": 2,
}
# Not for a car's sign: trucks and buses only, wet weather only, a school bus zone, a toll booth.
SKIP = {"Truck & bus", "Wet Weather", "School Bus", "Toll Plaza"}


def add(src, db_path):
    print(f"Reading {src} ...", flush=True)
    with open(src, encoding="utf-8-sig") as f:
        features = json.load(f)["features"]
    db = sqlite3.connect(db_path)
    db.executescript("""
        DROP TABLE IF EXISTS zones;
        DROP TABLE IF EXISTS zone_tiles;
        CREATE TABLE zones (id INTEGER PRIMARY KEY, kmh INTEGER NOT NULL, kind INTEGER NOT NULL,
                            oneway INTEGER NOT NULL, geom BLOB NOT NULL);
        CREATE TABLE zone_tiles (tile INTEGER NOT NULL, zone INTEGER NOT NULL);
    """)
    zone_id = 0
    counts = {0: 0, 1: 0, 2: 0}
    skipped = 0
    for feat in features:
        p = feat.get("properties") or {}
        g = feat.get("geometry")
        kind = KINDS.get(p.get("Type"))
        if g is None or p.get("Type") in SKIP or kind is None or p.get("Status") not in (None, "Existing"):
            skipped += 1
            continue
        try:
            kmh = int(str(p.get("Speed", "")).split()[0])
        except (ValueError, IndexError):
            skipped += 1
            continue
        oneway = int(p.get("Direction") == "One Way")
        lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        for line in lines:
            points = [(round(lat * 1e6), round(lon * 1e6)) for lon, lat, *_ in line]
            if len(points) < 2:
                continue
            zone_id += 1
            db.execute("INSERT INTO zones VALUES (?,?,?,?,?)", (zone_id, kmh, kind, oneway, encode(points)))
            db.executemany("INSERT INTO zone_tiles VALUES (?,?)", ((t, zone_id) for t in tiles_of(points)))
            counts[kind] += 1
    db.executescript("CREATE INDEX zone_tiles_by_tile ON zone_tiles (tile);")
    # The dataset's own date when known (ZONES_DATE, from the portal), else the file's.
    date = os.environ.get("ZONES_DATE") or datetime.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('zones_date', ?)", (date,))
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"Speed zones: {counts[1]:,} signposted, {counts[0]:,} default, {counts[2]:,} school; {skipped:,} skipped")


if __name__ == "__main__":
    add(sys.argv[1] if len(sys.argv) > 1 else "build/szone/speed_zones.geojson",
        sys.argv[2] if len(sys.argv) > 2 else "build/roads.db")
