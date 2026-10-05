# tools/ - optional offline tools

## derive_building_heights.py

Replaces *estimated* building heights with heights **measured from public USGS 3DEP lidar**.

For every OSM building footprint in `data/raw/osm.json` the tool reads a 2 m height-above-ground raster derived
from USGS 3DEP lidar and takes the 90th percentile of the pixels whose centre lies inside the footprint. It writes

| File | Content |
|---|---|
| `data/raw/building_heights_3dep.csv` | `osm_id,height_m,n_pixels,lidar_project,collected` - one row per footprint that got a usable measurement, sorted by `osm_id`, LF line endings |
| `data/raw/building_heights_3dep.meta.json` | provenance record (`source_id`, `url`, `retrieved_at`, `license`, `attribution_text`, `vintage`, `n_buildings`, `n_footprints_total`, `method`) that `download_data` merges into `data/raw/SOURCES.json` |

Both files are committed. The pipeline (`process_data`) only joins the CSV by `osm_id`; when the CSV is absent
nothing changes and every building keeps its OSM-tag or rule-based height. A footprint without a usable
measurement simply has no row - **the tool never estimates or guesses a value**.

The tool is optional. You only need to run it again when the study area or the OSM building footprints change.

### Why it lives outside the application environment

Reading the rasters needs GDAL (LERC/ZSTD-compressed Cloud Optimized GeoTIFFs over HTTPS, reprojection with
PROJ). The application environment deliberately has no GDAL, rasterio, shapely or geopandas, so the Docker image,
CI and tests stay small and never touch the network. The tool therefore has its own environment and its own
requirements file, and its *output* (a small CSV) is what the application consumes.

### Setup and run

```bash
# once: separate environment (Python 3.12; about 45 MB of wheels - rasterio bundles GDAL and PROJ)
uv venv --python 3.12 .venv-tools
uv pip install --python .venv-tools/Scripts/python.exe -r tools/requirements-heights.txt

# run (about 15 s; reads roughly 1 000 x 950 pixels from four remote rasters, no account or key needed)
.venv-tools/Scripts/python.exe tools/derive_building_heights.py

# then rebuild the processed data so the heights are picked up
.venv/Scripts/python.exe scripts/process_data.py        # or run_pipeline.py --skip-download
```

On Linux/macOS the interpreter is `.venv-tools/bin/python`. Without `uv`: `python -m venv .venv-tools` and
`pip install -r tools/requirements-heights.txt` inside it. Verified with rasterio 1.5.2 (GDAL 3.12.2) and
numpy 2.5.3 on Python 3.12 / Windows 11.

| Option | Meaning |
|---|---|
| `--bbox S,W,N,E` | study area, south,west,north,east. Default: `STUDY_AREA_BBOX` from the environment, else the `STUDY_AREA_BBOX` line of `.env`, else `37.745,-100.030,37.762,-100.005` |
| `--out PATH` | output CSV (default `data/raw/building_heights_3dep.csv`); the meta JSON is written next to it |
| `--osm PATH` | Overpass JSON with the building ways (default `data/raw/osm.json`) |
| `--surface ndsm\|hag` | which height-above-ground surface to use, see below (default `ndsm`) |
| `--report PATH` | optional diagnostics CSV: one row per footprint, including rejected ones, with status, pixel count, p50/p90/max and footprint area |
| `-v` | debug logging |

Exit code 0 = files written; 1 = nothing written (the reason is logged; files from an earlier run are left untouched).

### Data source, vintage, licence

- **Lidar:** USGS 3D Elevation Program (3DEP), project `USGS_LPC_KS_Area1_2014_LAS_2015` ("KS_Area1_2014"),
  collected **12 Dec 2013 - 22 Jan 2014**, quality level 3 (fewer than 2 points per m2).
- **Rasters:** 2 m Cloud Optimized GeoTIFFs produced from that point cloud and hosted by the Microsoft Planetary
  Computer (NAD83 / UTM zone 14N, float32, LERC_ZSTD): collections
  [`3dep-lidar-dsm`](https://planetarycomputer.microsoft.com/dataset/3dep-lidar-dsm) (surface model) and
  [`3dep-lidar-dtm-native`](https://planetarycomputer.microsoft.com/dataset/3dep-lidar-dtm-native) (terrain model
  from the lidar vendor's ground classification). Items are found through the STAC API
  (`https://planetarycomputer.microsoft.com/api/stac/v1/search`) and read with an anonymous, short-lived SAS token
  (`https://planetarycomputer.microsoft.com/api/sas/v1/token/<account>/<container>`); only the window covering the
  footprints is downloaded.
- When several lidar projects cover the area the most recent one is used; an older one (here the sparse
  `KS_DodgeCity_2010` collection) is read only for footprints the newer one does not cover at all. In the default
  area that never happens.
- **Licence:** USGS 3DEP data are in the US public domain. The Planetary Computer's own terms apply to the hosting
  service.
- **Credit line:** "U.S. Geological Survey, 3D Elevation Program".

### Method

1. Footprints = closed OSM ways with a `building` tag whose extent overlaps the bbox.
2. Height above ground = surface model minus terrain model, pixel by pixel. The terrain model has no value where
   no ground return exists (beneath large roofs, 0.75 % of the pixels in the default window); those gaps are
   interpolated from the surrounding ground (GDAL FillNodata, inverse distance, at most 100 m). Hiding 344 known
   40 m x 40 m ground blocks and re-interpolating them reproduced the real terrain with an RMSE of 0.31 m.
3. Each footprint ring is reprojected to the raster CRS; the pixels whose **centre** is inside the polygon are
   selected (`rasterio.features.geometry_mask`, GDAL's pixel-centre rule).
4. At least **4** valid pixels are required. Height = **90th percentile** of those pixels.
5. Only values with **2.5 m <= height <= 60 m** are kept; the kept value is rounded to 0.1 m.

### Why the default surface is DSM minus DTM, not the ready-made `3dep-lidar-hag` product

The project specification named the Planetary Computer's ready-made height-above-ground collection
(`3dep-lidar-hag`). It was tried first and is still available as `--surface hag`, but it is not the default,
because in this study area it does not describe building roofs reliably. Both findings below come from the pixel
values themselves (4 Oct 2026):

- **Tree canopy and masts are reported as building height.** The product behaves like a per-cell maximum over all
  returns within a few metres, so a few returns from bare winter branches, a mast or a taller neighbour lift every
  nearby cell. Example: a gable-roofed house whose roof shape in the surface model peaks at 6 m is reported at
  12-15 m. With `--surface hag` 12 of 58 houses come out above 10 m (none with the default), 54 footprints smaller
  than 300 m2 come out above 10 m (2 with the default), and 143 of 447 values (32 %) are more than 2 m above the
  default surface's value (median difference +1.1 m).
- **Large flat roofs are reported as ground.** The product's own ground filter (SMRF) classifies the interior of
  big roofs as ground, so the height there is exactly 0 and only a rim remains: 16 of the 41 footprints of
  1 000 m2 or more have a median pixel of 0 m, and three large buildings are rejected outright although the
  surface model shows them 4-9 m high.

The default surface avoids both: the surface model is not driven by isolated high returns, and the terrain comes
from the vendor's (USGS-accepted) ground classification. The method, thresholds, file format and lidar collection
are otherwise identical.

### Result for the default study area (run of 4 Oct 2026)

| | |
|---|---|
| OSM building footprints | 485 |
| with a measured height | **424 (87.4 %)** |
| rejected: fewer than 4 pixels | 8 (footprints of 3-11 m2) |
| rejected: below 2.5 m | 53 - of these 26 show no structure at all (p90 below 0.5 m: built after January 2014, or an open canopy), 27 are low or very small structures (0.5-2.5 m) |
| rejected: above 60 m | 0 |
| heights: min / 25 % / median / 75 % / 90 % / 95 % / max | 2.5 / 4.2 / 5.2 / 6.1 / 8.0 / 9.8 / 23.9 m |

Of the 459 footprints of 25 m2 or more 420 have a measured height. The pipeline keeps 458 of them (one has its
centroid outside the study area); 419 of those carry a measured height and 39 keep the rule-based estimate.

Spot checks: First National Bank Building (6 storeys) 23.9 m; Ford County Government Center (5 storeys) 18.2 m;
Ford County Courthouse 13.4 m; Saint Cornelius Episcopal Church 7.5 m; Taco Bell 5.2 m; Love's 5.1 m; Sonic 3.7 m;
Miller Elementary School 4.4 m. McDonald's and the KOA office have **no row**: the 2013-14 lidar shows no building
covering those footprints.

### Limitations - read before quoting these heights

- **Vintage.** The lidar is from December 2013 - January 2014. Buildings erected later are normally rejected by the
  2.5 m rule (26 footprints here), but when the new footprint overlaps an older structure a wrong value passes.
  Known case: *Holiday Inn Express & Suites* (way 1003769677) - the lidar shows open ground under about two thirds
  of the footprint and a 6 m structure on the rest, so the 6.0 m in the CSV is not the hotel's height. Buildings
  altered or replaced since 2014 keep their old height.
- **Parts of a building complex.** A small footprint next to a much taller part can take the neighbour's height,
  because up to 10 % of its pixels may fall on the neighbour (OSM outlines are traced from imagery and are a few
  metres off). Known cases: the annex of the First National Bank Building (way 965214125, OSM `building:levels=1`,
  17.8 m) and a two-level part of the Ford County Government Center (way 965217140, 17.8 m).
- **Resolution.** 2 m pixels from a sparse (QL3) point cloud. Small sheds are smoothed towards the ground and are
  mostly rejected (they keep the pipeline's estimate). Heights are roof-surface heights: parapets, roof equipment
  and ridge lines can be 1-2 m higher than the value given.
- **Registration.** No shift is applied between OSM and the raster. A search over shifts shows the lidar roofs sit
  about 2 m east and 1 m south of the OSM outlines on average (one pixel; datum difference NAD83/WGS84, imagery
  offset and raster registration cannot be separated). Applying that shift changes 16 of 421 heights by more than
  0.5 m and 3 by more than 1 m, so the result is not sensitive to it.
- **Height, not shape.** One number per footprint, suitable for flat-topped extrusions only - not a 3D building
  model. A newer and denser collection (KS_StatewideFordGray_2018, QL1) exists as raw point clouds but not as a
  ready raster; using it would need PDAL and about 2.6 GB of downloads.
- The tool reads closed ways only (no multipolygon relations; the default area has none).

### Troubleshooting

| Symptom | What to do |
|---|---|
| `needs rasterio and numpy ...` | You started it with the application interpreter. Use `.venv-tools/Scripts/python.exe`. |
| `request to https://planetarycomputer.microsoft.com/... failed` | Network, proxy or service problem. The tool retries three times; run it again later. Behind a proxy set `HTTPS_PROXY`. |
| `... not recognized as being in a supported file format` or a LERC/ZSTD decoding error | The GDAL build lacks LERC_ZSTD. Reinstall the pinned wheels: `uv pip install --python .venv-tools/Scripts/python.exe --reinstall -r tools/requirements-heights.txt`. |
| HTTP 403 while reading a raster | The SAS token expired (about 45 minutes). Just run the tool again. |
| `the STAC collection ... has no item for this area` | No 3DEP raster covers the bbox. No measured heights are available; the pipeline keeps its estimates. |
| `no closed building ways overlapping bbox` | `--bbox`/`STUDY_AREA_BBOX` does not match `data/raw/osm.json`; run `scripts/download_data.py --refresh` first. |

A failed run writes nothing and leaves the files of an earlier successful run in place.
