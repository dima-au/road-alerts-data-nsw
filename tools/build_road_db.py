"""Builds the offline road database Road Alerts reads: speed limits, school zones, speed cameras,
STOP and GIVE WAY signs, all from one OpenStreetMap extract.

    python tools/build_road_db.py [australia-latest.osm.pbf] [build/roads.db]

Download the extract first (~1 GB, Geofabrik updates it daily):
    curl -L -o build/osm/australia-latest.osm.pbf https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf
Needs pyosmium (pip install osmium). Put the result on the head unit with tools/install_db.sh.

SQLite, so Android reads it without a parser of our own:
  roads(id, kmh, implied, kind, name, cond, school, geom)
        kmh NULL when unknown; kind 0 = road, 1 = service road; cond = maxspeed:conditional as
        mapped ("40 @ (Mo-Fr 08:00-09:30,14:30-16:00; PH off; SH off)"); school 1 = a school zone
        mapped without its hours
  tiles(tile, road)        every 0.01 deg cell (~1.1 km x ~0.9 km) a road passes through
  points(tile, kind, lat, lon, bearing, kmh)
        kind 0 = speed camera, 1 = STOP, 2 = GIVE WAY; lat/lon in degrees x 1e6; bearing = the
        direction of travel the sign or camera is for (0 = north), -1 when unknown; kmh = the
        camera's limit, NULL when unknown
  meta(key, value)         source file date, build time, format version, and the NSW school
                           calendar (see school_calendar.py)
geom is the polyline: the first point as two int32 (degrees x 1e6, latitude first), then zigzag
varint deltas in the same units. Must agree with RoadDb.kt.
"""
import math
import os
import re
import sqlite3
import struct
import sys
import time

import osmium

# Next to this script (python -I leaves the script's folder off the path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import school_calendar  # noqa: E402

FORMAT = 1
TILE = 0.01
TILES_PER_ROW = 36_000  # longitude cells round the globe at 0.01 deg

CAR_ROADS = re.compile(
    r"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|road)(_link)?$")

CAMERA, STOP, GIVE_WAY = 0, 1, 2
POINT_KINDS = {"speed_camera": CAMERA, "stop": STOP, "give_way": GIVE_WAY}

# Compass words some mappers use instead of degrees.
COMPASS = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
           "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}


def parse_maxspeed(value):
    """Same rules as the app's parseMaxspeed: "60", "60 km/h", "40 mph", "AU:urban" -> km/h."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if v == "au:urban":
        return 50
    if v == "au:rural":
        return 100
    try:
        if v.endswith("mph"):
            return round(float(v[:-3].strip()) * 1.609)
        if v.endswith("km/h"):
            v = v[:-4].strip()
        return int(v)
    except ValueError:
        return None


def implied_limit(tags):
    """Zone tags, 10 in a shared zone, 50 on a residential street (the Australian urban default)."""
    for key in ("maxspeed:type", "source:maxspeed", "zone:maxspeed"):
        kmh = parse_maxspeed(tags.get(key))
        if kmh is not None:
            return kmh
    return {"living_street": 10, "residential": 50}.get(tags.get("highway"))


def posted_limit(tags):
    kmh = parse_maxspeed(tags.get("maxspeed"))
    if kmh is None:
        # Some roads carry the limit per direction only; use it when both directions agree.
        fwd = parse_maxspeed(tags.get("maxspeed:forward"))
        bwd = parse_maxspeed(tags.get("maxspeed:backward"))
        if fwd is not None and fwd == bwd:
            kmh = fwd
    return kmh


def is_school_zone(tags):
    return tags.get("hazard") == "school_zone" or tags.get("school_zone") in ("yes", "designated")


def parse_direction(value):
    """Degrees, a compass word, or "forward"/"backward" (resolved later along the way); None if unusable."""
    v = (value or "").strip()
    if not v:
        return None
    if v.lower() in ("forward", "backward"):
        return v.lower()
    if v.upper() in COMPASS:
        return COMPASS[v.upper()]
    try:
        return float(v) % 360
    except ValueError:
        return None


def varint(n, out):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def encode(points):
    out = bytearray(struct.pack("<ii", *points[0]))
    for (plat, plon), (lat, lon) in zip(points, points[1:]):
        for d in (lat - plat, lon - plon):
            varint((d << 1) ^ (d >> 63), out)
    return bytes(out)


def tile_of(lat, lon):
    return int((lat + 90) / TILE) * TILES_PER_ROW + int((lon + 180) / TILE)


def tiles_of(points):
    """Cells under every segment's bounding box: a long straight segment crosses cells without a node in them."""
    found = set()
    for (alat, alon), (blat, blon) in zip(points, points[1:] or points):
        lat0, lat1 = sorted((alat / 1e6, blat / 1e6))
        lon0, lon1 = sorted((alon / 1e6, blon / 1e6))
        for r in range(int((lat0 + 90) / TILE), int((lat1 + 90) / TILE) + 1):
            for c in range(int((lon0 + 180) / TILE), int((lon1 + 180) / TILE) + 1):
                found.add(r * TILES_PER_ROW + c)
    return found


def bearing(a, b):
    """Initial bearing from a to b (degrees x 1e6 pairs), 0 = north."""
    lat1, lon1, lat2, lon2 = (math.radians(v / 1e6) for v in (*a, *b))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return math.degrees(math.atan2(y, x)) % 360


class Roads(osmium.SimpleHandler):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.count = 0
        self.with_limit = 0
        self.school = 0
        # Signs and cameras: node id -> [kind, lat, lon, direction, kmh]. A "forward"/"backward"
        # direction is relative to the way the node is on, so it becomes degrees when that way comes
        # by (the file holds all nodes before the ways).
        self.points = {}

    def node(self, n):
        kind = POINT_KINDS.get(n.tags.get("highway"))
        if kind is None:
            return
        direction = parse_direction(n.tags.get("direction") or n.tags.get("camera:direction"))
        kmh = parse_maxspeed(n.tags.get("maxspeed")) if kind == CAMERA else None
        self.points[n.id] = [kind, round(n.location.lat * 1e6), round(n.location.lon * 1e6), direction, kmh]

    def way(self, w):
        highway = w.tags.get("highway")
        if not highway or not CAR_ROADS.match(highway) or w.tags.get("area") == "yes":
            return
        try:
            points = [(round(n.lat * 1e6), round(n.lon * 1e6)) for n in w.nodes]
        except osmium.InvalidLocationError:
            return  # a node outside the extract
        if len(points) < 2:
            return
        for i, n in enumerate(w.nodes):
            p = self.points.get(n.ref)
            if p is not None and p[3] in ("forward", "backward"):
                # The way's own direction at the node: towards it from the previous node, or away
                # from it to the next one at the way's start.
                along = bearing(points[i - 1], points[i]) if i > 0 else bearing(points[0], points[1])
                p[3] = along if p[3] == "forward" else (along + 180) % 360
        tags = {t.k: t.v for t in w.tags}
        posted = posted_limit(tags)
        implied = implied_limit(tags) if posted is None else None
        kmh = posted if posted is not None else implied
        name = tags.get("name") or tags.get("ref")
        kind = 1 if highway == "service" else 0
        cond = tags.get("maxspeed:conditional")
        school = int(cond is None and is_school_zone(tags))
        self.db.execute("INSERT INTO roads VALUES (?,?,?,?,?,?,?,?)",
                        (w.id, kmh, int(posted is None and implied is not None), kind, name, cond, school,
                         encode(points)))
        self.db.executemany("INSERT INTO tiles VALUES (?,?)", ((t, w.id) for t in tiles_of(points)))
        self.count += 1
        if posted is not None:
            self.with_limit += 1
        if cond or school:
            self.school += 1
        if self.count % 200_000 == 0:
            print(f"  {self.count:,} roads", flush=True)


def map_date(src):
    """Date of the OSM data in the extract (Geofabrik's replication timestamp), else the file's date."""
    reader = osmium.io.Reader(src, osmium.osm.osm_entity_bits.NOTHING)
    try:
        stamp = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    return stamp[:10] if stamp else time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(src)))


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "build/osm/australia-latest.osm.pbf"
    dst = sys.argv[2] if len(sys.argv) > 2 else "build/roads.db"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE roads (id INTEGER PRIMARY KEY, kmh INTEGER, implied INTEGER NOT NULL,
                            kind INTEGER NOT NULL, name TEXT, cond TEXT, school INTEGER NOT NULL,
                            geom BLOB NOT NULL);
        CREATE TABLE tiles (tile INTEGER NOT NULL, road INTEGER NOT NULL);
        CREATE TABLE points (tile INTEGER NOT NULL, kind INTEGER NOT NULL, lat INTEGER NOT NULL,
                             lon INTEGER NOT NULL, bearing REAL NOT NULL, kmh INTEGER);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    started = time.time()
    handler = Roads(db)
    print(f"Reading {src} ...", flush=True)
    # flex_mem keeps node locations in memory; Australia needs a few GB.
    handler.apply_file(src, locations=True, idx="flex_mem")
    counts = [0, 0, 0]
    rows = []
    for kind, lat, lon, direction, kmh in handler.points.values():
        # A forward/backward sign on no car road stays unresolved: of no use, left out.
        if isinstance(direction, str):
            continue
        # A STOP or GIVE WAY sign without a direction would warn on the crossing road too.
        if kind != CAMERA and direction is None:
            continue
        rows.append((tile_of(lat / 1e6, lon / 1e6), kind, lat, lon, -1 if direction is None else direction, kmh))
        counts[kind] += 1
    db.executemany("INSERT INTO points VALUES (?,?,?,?,?,?)", rows)
    print("Indexing ...", flush=True)
    db.executescript("""
        CREATE INDEX tiles_by_tile ON tiles (tile);
        CREATE INDEX points_by_tile ON points (tile);
    """)
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("format", str(FORMAT)),
        ("source", os.path.basename(src)),
        ("source_date", map_date(src)),
        ("built", time.strftime("%Y-%m-%d %H:%M", time.localtime())),
    ])
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, dst)
    # School terms and public holidays, so school zones are off in the holidays.
    school_calendar.add(dst)
    # Transport for NSW's official speed zones, when the file has been downloaded (speed_zones.py).
    zones = os.path.join(os.path.dirname(dst) or ".", "szone", "speed_zones.geojson")
    if os.path.exists(zones):
        import speed_zones
        speed_zones.add(zones, dst)
    mb = os.path.getsize(dst) / 1e6
    print(f"{handler.count:,} roads, {handler.with_limit:,} with a posted limit, {handler.school:,} school zones; "
          f"{counts[CAMERA]:,} cameras, {counts[STOP]:,} STOP, {counts[GIVE_WAY]:,} GIVE WAY "
          f"-> {dst} ({mb:.0f} MB) in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
