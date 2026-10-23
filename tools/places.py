"""Adds places to stop at to a road database: NSW rest areas (Transport for NSW) and, from
OpenStreetMap, fast food chains, public toilets and fuel stations. For the REST button.

    python tools/places.py [australia-latest.osm.pbf] [build/roads.db] [build/rest/rest-areas.csv]

The CSV is "NSW Rest Areas - CSV Format" of
https://opendata.transport.nsw.gov.au/data/dataset/nsw-rest-areas (CC BY), downloadable with the
Transport for NSW key. Also run by build_road_db.py.

Adds:
  places(lat, lon, kind, bearing, name, info, hours, ref)
      lat/lon in degrees x 1e6; kind 0 = rest area, 1 = fast food, 2 = toilets, 3 = fuel;
      bearing = direction of travel it's reached from (rest areas on divided roads), -1 = any;
      info = facilities in short words ("WC 2 · TABLES · FOOD"); hours = OSM opening_hours;
      ref = the rest area's number (for the live closures API), 0 for the others
  index on lat
Must agree with RoadDb.kt.
"""
import csv
import os
import sqlite3
import sys

import osmium

REST, FOOD, TOILETS, FUEL = 0, 1, 2, 3

# Chains with toilets and long hours; matched on brand or name.
CHAINS = ["McDonald's", "KFC", "Hungry Jack's", "Subway", "Oporto", "Red Rooster", "Guzman y Gomez",
          "Domino's", "Nando's", "Zambrero", "Carl's Jr.", "Taco Bell", "Burger King"]

BOUND = {"Northbound": 0, "Eastbound": 90, "Southbound": 180, "Westbound": 270}


def chain(tags):
    for key in ("brand", "name"):
        v = (tags.get(key) or "").lower().replace("’", "'")
        for c in CHAINS:
            if c.lower() in v:
                return c
    return None


class Places(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def place(self, tags, lat, lon):
        amenity = tags.get("amenity")
        if amenity == "fast_food":
            name = chain(tags)
            if name:
                self.rows.append((lat, lon, FOOD, -1, name, tags.get("addr:suburb") or tags.get("addr:city") or "",
                                  tags.get("opening_hours") or "", 0))
        elif amenity == "toilets" and tags.get("access") in (None, "yes", "public", "permissive"):
            info = " · ".join(x for x in (
                "ACCESSIBLE" if tags.get("wheelchair") == "yes" else "",
                "BABY CHANGE" if tags.get("changing_table") == "yes" else "",
            ) if x)
            self.rows.append((lat, lon, TOILETS, -1, tags.get("name") or "Public toilets", info, tags.get("opening_hours") or "", 0))
        elif amenity == "fuel":
            name = tags.get("brand") or tags.get("name") or "Fuel"
            info = "SHOP" if tags.get("shop") or tags.get("convenience") == "yes" else ""
            self.rows.append((lat, lon, FUEL, -1, name, info, tags.get("opening_hours") or "", 0))

    def node(self, n):
        if "amenity" in n.tags:
            self.place({t.k: t.v for t in n.tags}, n.location.lat, n.location.lon)

    def way(self, w):
        # Buildings and car parks mapped as areas: their nodes' average is close enough.
        if "amenity" not in w.tags or w.tags.get("amenity") not in ("fast_food", "toilets", "fuel"):
            return
        try:
            pts = [(n.lat, n.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if pts:
            self.place({t.k: t.v for t in w.tags}, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def rest_areas(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("vehicle_type") == "HEAVY VEHICLES ONLY" or r.get("site_status") == "Closed":
                continue
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
            except (KeyError, ValueError):
                continue
            wc = int(r.get("toilets") or 0)
            info = " · ".join(x for x in (
                f"WC {wc}" if wc else "NO WC",
                "ACCESSIBLE WC" if r.get("disabled_toilet") == "TRUE" else "",
                "FOOD" if r.get("food_available") == "TRUE" else "",
                "FUEL" if r.get("fuel_available") == "TRUE" else "",
                "TABLES" if r.get("picnic_table") == "TRUE" else "",
                "BBQ" if r.get("bbq") == "TRUE" else "",
                "PLAYGROUND" if r.get("playground") == "TRUE" else "",
                "DRIVER REVIVER" if r.get("driver_reviver") == "TRUE" else "",
            ) if x)
            bearing = BOUND.get(r.get("access_direction_cardinal", ""), -1)
            name = r.get("rest_area_name") or "Rest area"
            if r.get("road_name"):
                name += f" ({r['road_name'].title()})"
            rows.append((lat, lon, REST, bearing, name, info, "", int(r.get("rest_area_number") or 0)))
    return rows


def add(pbf, db_path, rest_csv):
    handler = Places()
    print(f"Places from {pbf} ...", flush=True)
    handler.apply_file(pbf, locations=True, idx="flex_mem")
    rows = handler.rows
    if rest_csv and os.path.exists(rest_csv):
        rows += rest_areas(rest_csv)
    db = sqlite3.connect(db_path)
    db.executescript("""
        DROP TABLE IF EXISTS places;
        CREATE TABLE places (lat INTEGER NOT NULL, lon INTEGER NOT NULL, kind INTEGER NOT NULL,
                             bearing REAL NOT NULL, name TEXT NOT NULL, info TEXT NOT NULL, hours TEXT NOT NULL,
                             ref INTEGER NOT NULL);
    """)
    db.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?)",
                   [(round(la * 1e6), round(lo * 1e6), k, b, n, i, h, ref) for la, lo, k, b, n, i, h, ref in rows])
    db.executescript("CREATE INDEX places_by_lat ON places (lat);")
    db.commit()
    counts = [sum(1 for r in rows if r[2] == k) for k in range(4)]
    db.close()
    print(f"Places: {counts[0]:,} rest areas, {counts[1]:,} fast food, {counts[2]:,} toilets, {counts[3]:,} fuel")


if __name__ == "__main__":
    add(sys.argv[1] if len(sys.argv) > 1 else "build/osm/australia-latest.osm.pbf",
        sys.argv[2] if len(sys.argv) > 2 else "build/roads.db",
        sys.argv[3] if len(sys.argv) > 3 else "build/rest/rest-areas.csv")
