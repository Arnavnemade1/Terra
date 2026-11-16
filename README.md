# Terra

Terra answers questions about how the ground changes, using radar from **NISAR**, the joint NASA and ISRO L-band
synthetic aperture radar mission. Pick any place on Earth, ask a question ("did the wetland flood between these
passes, and where?"), and Terra finds matching NISAR acquisitions, reads only your area from the original products,
measures calibrated backscatter and its change, and shows the result on a 3D globe with a written answer in which
every number is traced to a computed value.

Built for the NASA Space Apps Challenge, challenge **Dancing with the SARs**.

## What it does

- **Investigations.** Original NISAR GCOV (L2, geocoded backscatter) products are read over HTTP byte ranges from
  ASF, cropped to the area, checked for pair compatibility (track, frame, direction, maturity, release), and turned
  into backscatter maps, change maps, histograms, water-like and very bright area, and change hotspots.
- **Checked answers.** A language model plans the method and writes the answer; any number or date that does not
  trace to a computed value is rejected (`backend/terra/agent.py`).
- **Context.** Sentinel-2 true colour of the same dates, Landsat thermal infrared, weather and soil moisture at the
  radar dates, land cover, live hazards (earthquakes, volcanoes, fires, cyclones, floods), and NISAR's next pass that
  actually images the area (by radar swath, not proximity).
- **Views.** 3D globe with terrain, a relief map, a 2D map, Google Photorealistic 3D Tiles (optional), a simulated
  view from the satellite at its next pass, a timeline, and a full report with before and after comparisons.
- **Drones.** A drone workspace and fleet coordination for ground follow-up (see `docs/PHASES.md`, phases 4f and 4g).

## Run it

Requirements: Python 3.11 or newer, Node 20.19 or newer.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
npm ci
cp .env.example .env     # add the keys you have; see below
npm start                # Temporal, worker, API and the web app on http://127.0.0.1:5173
```

Keys (all optional, all stay on the server):

| Variable | Enables |
| --- | --- |
| `EARTHDATA_TOKEN` | Reading original NISAR products (NASA Earthdata Login). Investigations need it. |
| `OPENAI_API_KEY` | Written answers and follow-up questions. Without it, Terra shows the computed summary. |
| `GOOGLE_MAPS_TILES_KEY` | Google Photorealistic 3D Tiles (needs the Map Tiles API enabled). Without it, the option is hidden. |
| `TERRA_DRONE_TOKEN` | Connections from drone ground stations. |

Tests: `npm test` (pytest) and `npm run build` (TypeScript and Vite).

## Deployment

The Vercel site serves the landing page at `/` and the workspace at `/app`. GitHub CI checks Python tests, the API proxy and the production build. See [deployment setup](docs/DEPLOYMENT.md) for connecting the persistent backend through `TERRA_API_ORIGIN`; credentials stay on that backend. The complete product specification is in [docs/PROJECT.md](docs/PROJECT.md).

## NASA data used

- **NISAR L2 GCOV** (NASA and ISRO), searched and read through the Alaska Satellite Facility (ASF DAAC) and NASA Earthdata.
- **NASA FIRMS** active fires, **NASA EONET** natural events, **NASA GIBS** Blue Marble and VIIRS Black Marble imagery.
- **USGS** earthquakes and volcano alert levels; **Landsat 8 and 9** Collection 2 surface temperature (USGS).

## References

Every external resource Terra uses, with its licence or terms. Data services are reached only through
`backend/terra/connector.py`, which lists every permitted host.

**Data and services**

| Resource | Use | Licence or terms |
| --- | --- | --- |
| NISAR GCOV via ASF DAAC and NASA Earthdata | Radar measurements | NASA Earth science data, open use |
| NASA FIRMS, EONET, GIBS | Fires, events, base and night imagery | NASA open data |
| USGS earthquake and volcano feeds | Hazards | US public domain |
| Landsat 8/9 Collection 2 via Microsoft Planetary Computer | Thermal infrared | USGS public domain; Planetary Computer terms of use |
| Copernicus Sentinel-2 via Element 84 Earth Search | True colour evidence | Copernicus open licence ("contains modified Copernicus Sentinel data") |
| ESA WorldCover 2021 | Land cover | CC BY 4.0 |
| GDACS (European Commission JRC and UN) | Alerts | GDACS terms, attribution |
| CelesTrak and a public TLE mirror | Satellite orbits | Public orbital elements |
| Open-Meteo | Weather and soil moisture | CC BY 4.0 |
| Photon geocoder, OpenStreetMap data | Place search | ODbL, © OpenStreetMap contributors |
| Wikidata | Named summits and towns | CC0 |
| Esri World Imagery, Hillshade, Transportation, Boundaries, Elevation 3D | Basemaps and terrain | Esri terms of use, attribution shown on the map |
| Google Map Tiles API (Photorealistic 3D Tiles) | Optional 3D view, display only | Google Maps Platform terms and Map Tiles API policies |
| OpenAI API | Planning and written answers | OpenAI terms of use |

**Software**

| Package | Licence |
| --- | --- |
| CesiumJS, vite-plugin-cesium | Apache-2.0, MIT |
| React, React DOM, Vite, @vitejs/plugin-react | MIT |
| TypeScript | Apache-2.0 |
| satellite.js, mgrs, marked | MIT |
| DOMPurify | MPL-2.0 or Apache-2.0 |
| lucide-react | ISC |
| Archivo, Newsreader, IBM Plex Mono (via Fontsource) | SIL Open Font License 1.1 |
| FastAPI, Pydantic, pyproj, Temporal Python SDK | MIT |
| Uvicorn, HTTPX, python-dotenv, NumPy, rasterio, h5py, fsspec, rio-tiler, SciPy | BSD-3-Clause (NumPy also bundles MIT, Zlib, 0BSD and CC0 parts) |
| Pillow | MIT-CMU |
| aiohttp | Apache-2.0 and MIT |
| Temporal (dev server) | MIT |
| pymavlink, ArduPilot SITL (optional, for drones; installed outside this repo) | LGPL-3.0, GPL-3.0 |

## Use of Super Intelligence (AI)

- **In the product.** A language model (OpenAI; model set by `TERRA_LLM_MODEL`) chooses the method and dates and
  writes the answers. Each answer is labelled with the model that wrote it, and every number and date in it is checked
  against computed values; an answer that fails the check is not shown. Measurements themselves are computed, not
  generated.
- **In building it.** Much of the code and documentation was written with AI coding agents (Anthropic Claude Code and
  OpenAI Codex), directed, reviewed and tested by the team. `docs/FEATURES.md` and `docs/COMPLIANCE.md` were drafted
  with AI assistance.
- TODO (team): state in your own words how the idea and design were chosen, as the submission form asks.
- No AI-generated images or video are part of the product. Any used in the demo must carry a visible watermark.

## Licence

TODO (team decision): choose an OSI-approved licence and add `LICENSE`. Until then, Space Apps applies Apache-2.0
to submitted material. Third-party data and software keep their own licences listed above.
