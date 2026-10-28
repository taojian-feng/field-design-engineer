# Public reference data for the FDE agent

Real operator data (alignment sheets, PODS databases, ILI runs, MTRs) is confidential, so no public dataset
covers everything a Field Design Engineer works from. This guide lists the public sources that come closest,
what each one is good for, and how it maps onto this repo. For anything the public data cannot provide, use the
synthetic dataset in [`data/synthetic/`](../data/synthetic/README.md).

`scripts/fetch_public_data.py` downloads the sources marked **scripted** into `data/public/` (git-ignored).

## Pipeline statistics and records

| Source | What it gives you | Use in this repo | Access |
|---|---|---|---|
| [PHMSA pipeline source data](https://www.phmsa.dot.gov/data-and-statistics/pipeline/source-data) | Operator annual reports (gas distribution, gas transmission and gathering, hazardous liquid, LNG): miles by material, size, install decade, class location and SMYS band | Realistic system mixes for synthetic data; benchmarks | Free ZIP/Excel downloads |
| [PHMSA incident and accident data](https://www.phmsa.dot.gov/data-and-statistics/pipeline/distribution-transmission-gathering-lng-and-liquid-accident-and-incident-data) | Every reported incident since 1970, with cause, pipe attributes, MAOP and pressure at failure | Test cases for "what could go wrong here" reasoning and risk ranking | Free ZIP/Excel downloads |
| [PUDL PHMSA tables](https://docs.catalyst.coop/pudl/en/latest/data_sources/phmsagas.html) (Catalyst Cooperative) | Cleaned, machine-readable versions of the PHMSA gas distribution annual reports, 2004 to 2025 | **Scripted** (`phmsa`). Sets the pipe-vintage mix in the synthetic generator | Free Parquet on S3, no login |

## Maps and GIS

| Source | What it gives you | Use in this repo | Access |
|---|---|---|---|
| [EIA natural gas interstate and intrastate pipelines](https://catalog-beta.data.gov/dataset/natural-gas-interstate-and-intrastate-pipelines) (also on [HIFLD](https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/22)) | National transmission routes, operator and type. Small-scale and approximate | **Scripted** (`eia-pipelines --bbox`). Real routes to hang synthetic stationing and assets on | Free GeoJSON / ArcGIS REST |
| [NPMS Public Map Viewer](https://www.npms.phmsa.dot.gov/AboutPublicViewer.aspx) | Transmission pipelines, LNG plants and breakout tanks, viewable one county at a time | Visual check only. Bulk GIS data is limited to operators and government officials ([access rules](https://www.npms.phmsa.dot.gov/DataMayAccess.aspx)) | Viewer only for the public |
| [Microsoft Global ML Building Footprints](https://github.com/microsoft/globalmlbuildingfootprints) | Building polygons for the U.S. and most of the world | Real structure counts for `class_location_study` (load as `structure` rows within 660 ft of a route) | Free download (ODbL) |
| OpenStreetMap | Buildings, roads, railways, waterways, schools, churches, playgrounds | Crossings (`point_feature`) and identified sites for class location | Free (ODbL), Overpass API or Geofabrik extracts |
| USGS 3DEP elevation, National Hydrography Dataset | Ground profile and water bodies | Elevation profiles, water crossings, hydrotest section planning | Free from the USGS National Map |
| NRCS SSURGO soils | Soil type, drainage, corrosivity to steel | Context for CP findings and coating choice | Free from Web Soil Survey |

## Codes and data standards

| Source | What it gives you | Use in this repo |
|---|---|---|
| [eCFR 49 CFR Part 192](https://www.ecfr.gov/current/title-49/subtitle-B/chapter-I/subchapter-D/part-192) | Current federal gas pipeline safety regulation, with a JSON/XML API | **Scripted** (`ecfr192`). Source for `fde_agent/codes.py`; can be given to the agent as a reference document |
| [PODS](https://en.wikipedia.org/wiki/Pipeline_Open_Data_Standard) (PODS Association) | Industry data model for pipeline assets. PODS Lite is the free, simplified version | `pods_store.py` follows PODS Lite concepts; swap in a real PODS geodatabase later |
| ASME B31.8, API 5L, ASME B16.5 | Gas piping design, line pipe grades, flange ratings | Paid standards. The repo only encodes the few published values it needs (SMYS by grade, flange class ratings) |

## Limits to keep in mind

- Public map data is approximate (often digitized at 1:24,000 or coarser). Do not use it for stationing, depth or
  alignment work.
- PHMSA annual report data is aggregated by operator and state, not by segment.
- There is no public source for MTRs, pressure test charts, ILI results or as-built drawings. The synthetic dataset
  fills those gaps.
- Some PHMSA and EIA hosts block automated downloads from cloud networks. If a scripted download fails, run the
  script from your own machine.
