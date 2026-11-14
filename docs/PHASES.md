# TERRA — Execution Phases

Status as of 2026-11-13, taking over from Codex's initial scaffold.

## Phase 0 — Foundations (mostly done)
- [x] FastAPI backend (`backend/terra/app.py`): health, inputs, observations, investigation CRUD, cancel, resume, replay, report, artifacts
- [x] Temporal workflow (`workflows.py`): discover → prepare (waits until inputs arrive) → analyze → verify → publish, with checkpoints
- [x] Science core (`science.py`): GCOV HDF5/GeoTIFF inspection, bounded window reads, compatibility checks, backscatter change, color mapping
- [x] ASF/CMR catalog search (`catalog.py`), SQLite store, isolated compute runner subprocess
- [x] Reference data fetch (`scripts/fetch_reference.py`); Pacaya‑Samiria HH/HV inputs in `.data/inputs`
- [x] 15 passing pytest tests

## Phase 1 — Make it run end to end ✅
- [x] Fixed the Cesium terrain‑provider type error in `src/Globe.tsx`
- [x] `src/styles.css`: the full observatory stylesheet (it was missing, so the build failed). Covers the shell, globe overlays, panels, modals and responsive layout down to phone width
- [x] `scripts/dev.py` (`npm start`): Temporal → worker → API → Vite with prefixed logs and clean Ctrl+C. Uses the `temporal` CLI if installed, otherwise `scripts/temporal_dev.py` downloads the dev server through the Python SDK. Picks a free API port if 8000 is taken and passes it to Vite's proxy (`TERRA_API_PORT`)
- [x] Fixed the Pacaya–Samiria preset bbox: it overlapped the local GCOV data by only 0.0002°, so analyses covered 0.15 km². It now matches the data footprint (1,102 km², 2.75M valid pixels)
- [x] Fixed "Download results & provenance": it linked to the private `result.json` (404); it now uses `public-result.json`
- [x] Smoke test: catalog search (34 real ASF acquisitions) → investigation via Temporal → calibrated backscatter computed → PNG layer drawn on the globe

## Phase 2 — Globe visual quality ✅
- [x] Real solar position: lighting, the day–night terminator and atmosphere follow the current time (the clock refreshes every minute)
- [x] NASA VIIRS Black Marble city lights on the night side; faint moonlit relief instead of a black disc
- [x] The orbital home view frames the terminator next to the region when the region is in darkness
- [x] Star field, per‑fragment sky atmosphere, ACES tone mapping in HDR, tuned imagery saturation
- [x] Below 5,000 km, sun lighting, the sun‑driven atmosphere and the night layer switch off (checked every frame in `preRender`), so analysis views are always daylit whatever the local hour
- [x] Cinematic 4 s intro descent from 42,000 km, then a slow idle orbit that stops on any interaction; region fly‑tos use an oblique 58° view with eased arcs; `prefers-reduced-motion` turns all of this off
- [x] Progressive detail: Esri place names and borders fade in below about 13,000 km and stay above science layers. New layer toggles: "Places & borders" and "Day & night"
- [x] Region marker hidden by the planet when it is on the far side; tile preloading and camera inertia tuned
- Dev builds expose `window.__terraViewer` / `window.__terraCesium` for inspection

## Phase 2b — God's Eye‑inspired rebuild ✅
Research: studied `bilawalsidhu/gods-eye-view` (MIT) in its viewer, HUD, scope/style shaders, CSS tokens and demo footage. What makes it work:
an inset viewport inside a graph‑paper frame, a corner‑bracket "intelligence HUD" of live monospace telemetry, dense real data drawn as
callouts and tracks on the globe, a bottom command dock, slim glass side panels, sensor display modes, and Esri imagery plus keyless terrain.
TERRA adopts that language with its own purpose (investigating change), and every readout is real:
- **Frame and HUD** (`src/Hud.tsx`): UTC clock, MGRS/DMS at screen centre, altitude, scale (m/px measured on the ellipsoid), solar elevation,
  live NISAR sub‑point (SGP4), next ≤350 km nadir approach to the AOI, feed health, and a typed summary of what is in view
- **Live layers** (`src/liveLayers.ts`, backend `terra/feeds.py`, cached, stale‑marked on failure):
  NISAR live orbit, ground track and orbit (CelesTrak NORAD 65053), 167 Earth‑resources satellites, NISAR GCOV coverage for the past 4 days
  (ASF), USGS M2.5+ quakes over 7 days, NASA EONET open events, and NASA FIRMS 24 h active fires. Everything is clickable, opens a context card,
  and can become the AOI with "Investigate here"
- **Anywhere on Earth**: Locate palette (`/`) with Photon/OSM geocoding, coordinates, and 12 featured change sites. Right‑click marks any point as the AOI
- **Display modes** (`src/displayModes.ts`, own GLSL): NORMAL / NVG / FLIR / CRT / MONO, labelled as display filters, not data
- **Rendering**: procedural fine star field (replaces Cesium's blocky Tycho sky), LDR true‑colour imagery (HDR tone mapping washed colours out),
  outline‑only acquisition footprints (stacked translucent fills had tinted the imagery yellow), and ground‑clamped AOI corner brackets
- Fixed renderer crashes: primitives created at the zero vector, and looped ground polylines with a duplicated closing vertex

## Phase 2c — Deslop pass, and putting the new keys to work ✅
**Design research.** Practitioner write‑ups on "AI slop" UI agree that the tells are unexamined defaults. TERRA had most of them: cyan on black,
glow everywhere, "monospace tech theater" (uppercase, wide letter‑spacing), glassmorphism on every panel, everything boxed into chips and cards,
icons on every button, gradient dots, fake jargon ("FEEDS 5/5 NOMINAL") and fade‑up animation everywhere.
Sources: wystack anti‑pattern catalog, 21st.dev, uxdesign.cc "How to de‑slopify your designs", The Fountain Institute "Signs a UI has been vibe coded".
**Decisions** (what a person would choose for a radar survey instrument):
- **Palette:** warm graphite neutrals and a single accent, International Orange (aerospace), reserved for NISAR, the area of interest and the one primary action
- **Type:** Archivo for the interface, Newsreader serif only for names and headline figures, IBM Plex Mono only for numbers that must align;
  true minus signs; sentence‑case copy in plain language
- **Structure:** solid panels with hairline rules (no blur, no glow, no chips); one investigation column; a map‑control strip; popovers for layers and sources
- **Map:** cartographic halo labels instead of boxed callouts, a legend whose glyphs match the map symbols, a real graphic scale bar, and a status strip
  (coordinates, MGRS, eye altitude, sun, NISAR's next pass over the area, source health, UTC)
- **Removed:** inset frame and graph paper, HUD corners, fake classification line, typewriter summary, command dock, display‑mode tray (now in Layers)

**Keys.**
- **Earthdata:** `terra/acquire.py` reads only the area of interest from the *original* NISAR GCOV HDF5 at ASF using HTTP byte ranges (about 15 s, about 3–30 MB),
  then writes a cropped HDF5 with the original layout, masks and identification unchanged, plus a provenance sidecar. Investigations now run on validated
  originals anywhere NISAR has coverage. Before this, only the bundled third‑party beta GeoTIFF worked.
- **Science fix:** current GCOV products encode per‑polarization validity bits in `inputDataExceptionMask` (256 = HH fully focused). The old `== 0` test
  rejected every valid pixel of every original product; `exception_quality()` now follows the product's own documentation (test added).
- **Cesium ion:** Cesium World Terrain, plus Google Photorealistic 3D Tiles through ion (asset 2275207) below 300 km. The Google Maps key returns
  `403 Permission denied on the service`: enable the **Map Tiles API** on that key's project and TERRA will use it directly.
  Science layers stay on the imagery globe, because single‑image draping onto 3D tiles does not render in Cesium 1.147.
- **Featured sites** are resized to boxes of 40 km or less, so each one can be investigated at native resolution.

## Phase 3 — NISAR visualization and time travel (§9–12)
- Tiled (COG/XYZ) science layers instead of a single PNG; legend, units, value inspector on hover
- Observation timeline with scrubbing across acquisitions; swipe/compare view
- HH/HV/ratio composites, change maps, footprint styling

## Phase 4 — Autonomous agents (§13–17) — core done
- [x] **Coordinator** (`plan_investigation`): `gpt-6-luna` (`TERRA_LLM_MODEL`) reads the question, picks the method and the time window
  (named months and dates become target dates). Acquisitions are then chosen deterministically from the ASF catalog, so the model
  cannot invent products. Change pairs must share track, direction, frame, maturity and processing release.
- [x] **Data acquisition**: AOI subsets of the *original* GCOV HDF5 are read over HTTP ranges with Earthdata. Areas larger than the
  2048² native-pixel budget (about 20 km for 10 m / 40 MHz products, 40 km for 20 m) are multilooked up to 4×; this is recorded in
  provenance and stated in the limitations.
- [x] **Analysis and verification**: calibrated backscatter and change, histograms, and an exception-mask fix for current GCOV releases.
- [x] **Synthesis**: a written answer where every number *and date* is traced to computed evidence (`verify_numbers`); any untraced
  value rejects the answer and the computed summary is shown instead. "Write a fuller answer" re-runs the writer on a completed result.
  Follow-up questions use the same checks.
- [x] Live context near the area (earthquakes, events, fires, NISAR passes), each feed capped at 6 s so planning never stalls.
- [x] Codex additions (taken over): connector allowlist (`connector.py`), same-origin tile gateway (`gateway.py`), dated optical evidence
  (`optical.py`), evidence dossier, incremental results.
- [ ] Separate child workflows per role; live event stream (SSE); scientific memory across investigations.

## Phase 4b — Speed, breadth and the finished interface ✅ (2026-11-13)
**Speed** (cold area, two original products): first result about 13 s, checked written answer about 18–19 s (was 30–40 s; 270–313 s before Phase 4).
- Range reader (`connector.RangeFile`): one shared, allowlisted transport for HDF5 and Cloud-Optimized GeoTIFF. NISAR reads prefetch the file's
  head and tail (HDF5 metadata) in parallel and reuse the size from the signing request; signed Earthdata links are cached until shortly before expiry,
  and the most likely pair is signed while the language model plans.
- Sentinel-2 optical evidence: 186 s → about 11–25 s. GDAL was probing sibling paths (`.aux.xml`, "test") through the opener and reading the whole
  170 MB COG; the opener now serves only the COG, reads the right overview level, and prefetches exactly the tiles under the area.
- The worker keeps every live feed warm in the background; planning never waits more than 1 s for context. Feed copies persist on disk
  (`.data/cache/feed-*.json`) so restarts do not re-download unchanged data. CelesTrak rate-limits by address; NISAR's elements fall back to a public mirror.
- The writer names untraced values and retries once instead of dropping to the computed summary; net changes are supplied as derived facts.

**More useful answers** (`science.py`): water-like (below −18 dB) and very bright (above −3 dB) area on each date, newly water-like and no-longer
water-like area, area brighter/darker by the threshold, and up to six change hotspots (connected patches after a 3×3 average) with area, mean and
peak change, centre and bounds. The writer is told to say where change is concentrated. New capability boundaries: volcanic surface, urban,
agriculture, ice and snow.

**More than wildfires**: hazards merge USGS volcano alert levels, GDACS (tropical cyclones, floods, droughts, volcanoes, orange/red earthquakes, with
alert levels and severity) and NASA EONET, de-duplicated across sources; USGS M4.5+ earthquakes are listed too. Alerts appear on the globe, in the
Hazards tab with category filters, in search, and in the context given to the writer.

**Interface**: docked panel with search at the top; a single search for places, coordinates (decimal, DMS, MGRS, map links, `geo:`), live hazards,
featured sites, saved investigations and recent places. Results show the answer, before/after thumbnails, key figures, a hotspot list that flies to
each patch (numbered on the globe), a histogram, and a layer picker that includes true-colour images. The report is a full-screen document that opens
on a large before/after viewer (true colour, radar, change; side by side or a draggable swipe), then the answer, hotspot table, measurements,
sources and method.

**Security**: all external calls go through `connector.py` (allowlisted hosts, credentials attached server-side only, signed links never logged
or returned). Checked: the production bundle, API responses, artifacts and logs contain no key or signed URL; the dev server refuses `.env`, `.data`,
`backend` and similar paths.

## Phase 4c — Analysis workspace (reference layout, dark) ✅ (2026-11-14)
Layout adapted from a wildfire forensics prototype (top bar, map, right panel, timeline dock), kept dark and re-purposed for radar change,
hazards and satellites:
- **Top bar**: area picker (search popover for places, coordinates, hazards, saved work), radar date range, three workspaces with badges
  (Investigation; Hazards with orange/red alert count; Satellites with passes over the area in 48 h), View / Analysis / Share menus, live sources.
- **Map**: compass, 2D relief map (dark hillshade, graticule tags) or 3D terrain and imagery, zoom presets relative to the area, scale bar,
  full screen, coordinate readout, legend with click-to-read values, numbered change-hotspot callouts, terrain-profile path.
- **Right panel**: Layers (thumbnails from the actual layers, per-layer opacity, terrain exaggeration), Analysis (question, progress, answer,
  hotspots, follow-ups), Evidence (swipe comparison, sources, limitations), Notes (kept in the browser, shown on the timeline);
  Investigation overview and Weather and surface (Open-Meteo rain, soil moisture, wind, temperature, humidity at both radar dates) cards.
  Hazards and Satellites workspaces have their own KPI rows, grouped lists and detail cards.
- **Dock**: a scrubbable, playable timeline (catalog acquisitions, the measured pair, true-colour dates, nearby hazards, notes; hazard reports
  over 7 days; predicted passes over the next 48 h). Radar and true-colour layers follow the timeline. Three charts per workspace
  (radar histograms before/after, change per pixel, terrain profile; earthquakes per day, largest magnitude, reports by type; altitude,
  distance to the area, passes by satellite) and a key-events / hazard-log / pass-log list.
- Taken over from Codex: modelled conditions (`conditions.py`), profile endpoint, topographic tiles, satellite fleet with roles and mirror.
- Fixed: dates shown in local time (shifted across midnight); 2D morph errors; NISAR's next pass searched over its 12-day cycle.

## Phase 4d — Terrain mapping and sensor views
- **3D relief map** (map controls: Photo / Relief): sand-coloured globe with Esri World Hillshade; faceted shading and contour lines
  computed on the GPU from the terrain mesh (screen-space derivative normals, NW light, index contour every fifth line, spacing follows
  zoom); ESA WorldCover 2021 land cover (10 m, read as a bounded COG window through the connector, padded beyond the area for oblique
  views) as a muted tint; low-poly trees and shrubs placed where land cover has them, sized to the zoom and re-planted only when the
  drawn terrain under them changes; named summits, volcanoes, lakes and towns from Wikidata with stated elevations, de-duplicated and
  decluttered on screen; roads as pale lines (Esri World Transportation).
- **Pointer readout** in 3D: elevation, slope and facing direction from the rendered terrain, whether the point is inside a changed
  patch with the measured change once the pointer rests, land-cover class, and position.
- **Land cover card**: class shares for the area itself, with a note on how cover affects L-band backscatter.
- **Sensor view** (top-right "NISAR view", View menu, or "Sensor view at next pass" for any satellite): a second globe looking from the
  satellite's predicted position at its next pass, with a heads-up display (heading tape, look-angle scale, altitude, ground speed,
  track heading, slant range, off-nadir and incidence angles, look side, field of view, ground elevation at the crosshair).
  EO drapes Sentinel-2 or the basemap; SAR drapes the investigation's NISAR layer in grey or iron-bow false colour; Δ drapes the change
  layer. It is labelled SIM: it shows look geometry, not imagery the satellite took. Drag slews, wheel zooms.
- New endpoint `/api/terrain?bbox=` (cached on disk 30 days); connector providers `worldcover`, `wikidata`; tiles `hillshade`,
  `transport`, `terrain-base`.
- Cesium detail worth knowing: a globe material whose source mentions the surface-normal input is silently dropped when the terrain
  has no stored normals (ArcGIS heightmaps), so the relief shader derives its local frame from the world position instead.

## Phase 5 — Advanced science (§18–21)
- Physics‑aware checks: speckle filtering, incidence angle and terrain normalization, statistical significance
- Multisensor fusion (Sentinel‑1, Sentinel‑2, precipitation via GIBS/CMR)
- Integrity engine: every claim links to evidence and gets a confidence rating; contradictions are flagged

## Phase 6 — Unified UX and reports (§22–24)
- [x] Integrated evidence dossier with dated radar figures, before/after slider, opacity overlay, change layer, computed metrics, acquisition metadata, provenance, checks, limitations, print/PDF, and artifact downloads.
- [x] Preserve ASF browse composites with new investigations when the publisher image is publicly accessible. If unavailable, the dossier explicitly says so; calibrated computed figures remain available.
- [x] Forecast section states when no validated forecasting model exists. Numerical predictions require a separately implemented model, uncertainty, and validation record.
- [ ] Standalone shareable HTML export and model-backed forecast workflow.

## Phase 7 — Demo, hardening, docs (§25–34)
- Scripted Pacaya‑Samiria flooding investigation, end to end
- Performance budget, security review, Playwright E2E, README and architecture docs
