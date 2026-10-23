# Road Alerts map data — NSW

The offline road database of the Road Alerts app for **New South Wales**: speed limits (official
NSW speed zones over OpenStreetMap), school zones on NSW school days, speed cameras, STOP / GIVE
WAY signs. The OpenStreetMap part covers all of Australia, but only NSW has the official zones and
school calendar. Built here once a month by GitHub Actions and published as
the latest release: `roads.db.gz` and `roads.json` (dates, size, SHA-256 of the database).

The build scripts in `tools/` are copies of the app's; the database format must match the app.

## Sources and licences

- **OpenStreetMap** contributors, via the Geofabrik Australia extract. © OpenStreetMap
  contributors, available under the [Open Database Licence](https://opendatacommons.org/licenses/odbl/).
  The database published here is a derived database under the same licence.
- **Transport for NSW**, [Speed Zones](https://opendata.transport.nsw.gov.au/data/dataset/speed-zones),
  Creative Commons Attribution 4.0.
- **NSW Department of Education**, school terms; **date.nager.at**, public holidays.

## Build it yourself

```
pip install osmium
python tools/build_road_db.py australia-latest.osm.pbf roads.db
```

The workflow needs the repository secret `TFNSW_API_KEY` (a Transport for NSW Open Data key) to
download the speed zones.
