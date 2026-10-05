# Test fixtures

`raw_small/` is a hand-made stand-in for `data/raw/`: a few OpenStreetMap elements in Overpass `out geom`
form, six National Bridge Inventory records in ArcGIS JSON, a boundary and a `SOURCES.json`. Nothing here is
real data; names and numbers are invented. The study area that goes with it is
`STUDY_AREA_BBOX=37.7500,-100.0200,37.7600,-100.0100`.

| Element | What it exercises |
|---|---|
| way 1001 | building with `building:levels=3`, civic tags and `addr:*` tags (height from levels, or lidar when the CSV is present) |
| way 1002 | building whose name is the number "7" (name becomes null) |
| way 1003 | building of 20 m2 (dropped, below 25 m2) |
| way 1004 | building with an OSM `height` tag (wins over lidar and levels) |
| ways 1005, 1007, 1008 | buildings without height information (estimated by rule) |
| way 1006 | building whose centroid is outside the study area (dropped) |
| way 1009 | building way that is not closed (dropped) |
| way 1010 | building across the western edge with its centroid inside (kept whole) |
| ways 2001 + 2002 | two contiguous bridge ways sharing node 13 (one merged bridge asset) |
| way 2003 | ordinary road sharing node 14 with the bridge (not merged) |
| way 2005 | road crossing the eastern edge (clipped) |
| way 2008 | road leaving and re-entering through the northern edge (longest part kept) |
| ways 2006, 2007 | service road and footway (base map only, no asset) |
| way 2009 | `bridge=yes` with `area=yes` (not a bridge asset) |
| ways 2010, 2011 | road outside the area; `highway=proposed` |
| way 2012 | single-way highway bridge |
| ways 3001, 3002 | rail line and rail bridge |
| ways 4001, 4002, node 5003 | substation polygon, power line across the northern edge, substation node |
| nodes 5001, 5002 | street lamp inside / outside the area |
| NBI ...011 | matches the merged bridge; inspection `223`, reconstructed 2001, owner 01, condition F |
| NBI ...022 | recorded DMS coordinates far from its point (mismatch: no asset) |
| NBI ...033 | culvert away from any mapped bridge (point asset), owner 04, reconstructed 0 |
| NBI ...044 | bridge away from any mapped bridge (point asset), owner 02, inspection `998` |
| NBI ...055 | valid record outside the study area |
| NBI ...066 | record without usable recorded coordinates |
