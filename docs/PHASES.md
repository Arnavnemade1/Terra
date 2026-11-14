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
- **NISAR passes by swath, not proximity**: NISAR's L-band SAR looks left at 33–47° incidence, about 420–680 km from its ground
  track, so its next pass is now the next pass whose swath covers the area (`swathPass`), used by the Satellites KPI, the satellite
  card and the sensor view, which shows IN SWATH / OUTSIDE SWATH. (The closest overpass over Etna, 199 km away, would not image it.)
- New endpoint `/api/terrain?bbox=` (cached on disk 30 days); connector providers `worldcover`, `wikidata`; tiles `hillshade`,
  `transport`, `terrain-base`.
- Cesium detail worth knowing: a globe material whose source mentions the surface-normal input is silently dropped when the terrain
  has no stored normals (ArcGIS heightmaps), so the relief shader derives its local frame from the world position instead.

## Phase 4e — Google Map Tiles API, attribution and phones (2026-11-14)
- **Photorealistic 3D Tiles, keyless in the browser.** `backend/terra/google_tiles.py` serves `/api/connector/google3d/*`:
  the key (`GOOGLE_MAPS_TILES_KEY`, falls back to `VITE_GOOGLE_MAPS_KEY`) goes upstream as `X-Goog-Api-Key`, never in a URL;
  only `root.json` and `datasets/<id>/files/<name>.json|glb?session=` are forwarded; child URIs are rewritten to the gateway;
  Google's `private` Cache-Control and ETags pass through and nothing is stored server-side; 401/403/429 become plain messages.
  `/api/connector/google3d/status` probes availability (cached 10 min); `/api/health` reports `photorealistic_3d`.
  The connector now treats `304 Not Modified` as a cache answer rather than a redirect. 22 tests in `tests/test_google_tiles.py`.
- **Map style "Photoreal"** (map controls and View menu, offered only when the status probe succeeds): Google tiles below 300 km,
  the imagery globe above. While a radar or change layer is shown the tiles step aside so science pixels sit on the terrain globe.
  Non-Google basemaps are hidden while Google tiles render; the pointer readout is off (nothing is measured from Google content).
- **Attribution was invisible everywhere.** Cesium's credit list sat in a zero-size container with `overflow: hidden`. It is now a
  strip along the bottom of the map; the controls sit above it at whatever height it wraps to (ResizeObserver). Google's logo at
  18 px, full opacity, with every data provider.
- **Phones and tablets** (checked at 360, 390, 430, 768, 1100, 1280, 1440 in headless Chrome: no horizontal overflow in any mode or
  panel): View, Analysis, Share, Sources and About were unreachable below 760 px; they are now one More menu (below 1020 px too, where
  the top bar overflowed). Two-row phone top bar with full-width workspace tabs; 44 px touch targets on phones and coarse pointers;
  map controls reduced to style and full screen; timeline ticks thinned by pixel spacing; one-column report; sensor view fits.
- Removed every infinite pulse and blink (running-step dots, the sensor "rec" dot).

## Phase 4f — Drones, camera feeds and real-drone connections (2026-11-14)
- **Timeline and charts** are a drawer (closed by default, **T** or the "Timeline" button) so the globe is unobstructed.
- **Drones workspace**: five simulated aircraft fly over different parts of the area, chosen from the investigation: a fixed-wing
  maps the perimeter; two quadcopters orbit the largest changed patches (or the highest summits / sectors); one flies a lawnmower
  survey; one relays and overwatches the centre. Orbits are sized so the camera looks down at about 40°. Flight heights follow the
  ground (terrain sampled once along each route, smoothed with a moving maximum so aircraft never dip into ridges). Quads pitch with
  speed and fixed wings bank into turns (coordinated-turn roll). Everything simulated is labelled SIM.
- **Map**: low-poly glTF aircraft built in code (unlit, baked shading, nav lights, translucent rotor discs), label tags with call sign,
  height and SIM/LIVE, a line to the ground, the selected aircraft's line of sight and its route.
- **Fleet panel**: list (mission, status, height, battery), detail card (altitude, ground speed, heading, battery and endurance, link,
  flight time; aircraft, mission, task, sensor, position, gimbal), **Full camera view** / **Camera**, and **Assign a task**
  (orbit a patch or summit, survey, perimeter, overwatch, hold, return).
- **Camera** (per aircraft): a second globe rendered from the aircraft's position and gimbal (relief world with trees, or imagery),
  with the heads-up display: REC clock, call sign, sensor, UTC time, mission, heading tape, gimbal scale, ALT ft AGL, GS kt, HDG,
  SPI in degrees and decimal minutes, range, ground elevation, BAT, LNK dBm, zoom and field of view. Drag slews the gimbal, the wheel
  zooms. **IR** drapes measured land-surface temperature (Landsat 8/9 band 10, the most recent mostly cloud-free scene, clouds
  masked) in Ironbow, White-hot or Black-hot, and reads the temperature at the crosshair. **Δ** drapes the radar change.
  **Fly** takes manual control of a simulated aircraft (W/S, A/D, Q/E, R/F, arrows for the gimbal, Shift for speed, Esc to hand
  back; it eases back onto its route).
- **Real drones**: `TERRA_DRONE_TOKEN` on the server enables `/api/drones/telemetry` (bearer token) and task polling;
  `scripts/drone_bridge.py` connects a MAVLink autopilot (PX4/ArduPilot via pymavlink) or simulates connected aircraft.
  Tasks are advisory; orbit, hold and return are forwarded to the autopilot only with `--allow-commands`; the pilot in command
  can always override. A ground station can share an MJPEG/snapshot URL, shown in the camera as live video.
- Cesium details: a second globe can receive undefined eye-space positions in globe materials unless globe lighting is on (the
  relief's fixed light makes that harmless); its atmosphere must not follow the real sun or a night-time camera renders black.

## Phase 4g — Fleet coordination: one central database, durable agents, real aircraft (2026-11-14)
**Architecture.** Every aircraft, real or virtual, talks to Terra through one protocol; every report, task, airspace
reservation, bid and message is a row in the central fleet database (SQLite in WAL mode, the same file as investigations,
shared by the API and the worker). Durable agents (Temporal workflows) coordinate only through that database and signals.

    ground station (MAVLink bridge, or the virtual fleet) ──HTTPS + bearer token──▶ /api/fleet ──▶ central database
    FleetCoordinatorWorkflow ─┬─ coordination_round activity ──▶ central database (jobs, tasks, airspace, messages)
    DroneAgentWorkflow × N  ──┴─ drone_agent_round activity  ──▶ central database (bids, health, conformance, messages)
    workspace (browser) ──same origin──▶ /api/fleet (state once a second; areas, people's tasks, manual setpoints)

- **Protocol** (`backend/terra/fleet/api.py`): register (identity, sensors, relay radio, speeds, endurance, autopilot);
  telemetry twice a second with a sequence number (late or repeated messages are ignored; re-registering starts a new
  session); the reply carries the current task, any manual setpoint and the area; task acknowledgements move only forward
  (assigned → acknowledged → active → completed / failed / rejected).
- **Coordinator** (one durable workflow, every 4 s or at once when signalled): turns monitoring targets (changed patches,
  summits; volcanoes need an IR sensor) into jobs (orbits, a survey, perimeter mapping, a relay when anyone is beyond radio
  range or reports a weak link); assigns aircraft by an exhaustive search that maximises the priority covered, then minimises
  cost, so scarce capabilities (a relay radio) go where only they can help; keeps current holders unless a bid is clearly
  better; sends aircraft home before their battery falls below what they need to return plus a 15 % reserve and hands their
  job to another; reopens jobs whose real aircraft did not acknowledge within 30 s; respects people's tasks (15 minutes)
  and manual flight; starts an agent for every aircraft without a live one. Each task reserves an operation volume (area,
  altitude band above ground, time window), and overlapping volumes get altitude layers at least 40 m apart, after the
  strategic-deconfliction idea in NASA's UAS Traffic Management research.
- **Drone agents** (one durable workflow per aircraft, every 3 s): bid on every job from the aircraft's own position, battery,
  speed and sensors (and say whether it could do the job and still get home with the reserve); report lost or late telemetry,
  low battery, weak links, leaving the reserved airspace, and the nearest traffic inside 60 m / 25 m (departures near the
  launch site excepted); mark targets observed; signal the coordinator when something needs a decision now; retire after ten
  minutes of silence. Messages are posted only when something changes.
- **Real aircraft**: `scripts/drone_bridge.py --mavlink …` (PX4 or ArduPilot via pymavlink) registers the aircraft, relays
  position, battery, terrain height and gimbal attitude, and acknowledges tasks. Tasks are advisory: orbit
  (MAV_CMD_DO_ORBIT), hold and relay (MAV_CMD_DO_REPOSITION) and return (MAV_CMD_NAV_RETURN_TO_LAUNCH) reach the autopilot
  only with `--allow-commands`, survey and perimeter are rejected with a note to plan them in the ground-control software,
  and the pilot in command can always override. Real aircraft are never flown from the browser.
- **Virtual fleet** (`--virtual 5`, started by the dev stack when `TERRA_DRONE_TOKEN` is set): a fixed-wing mapper and four
  quadcopters (IR, lidar, relay radio) that fly their tasks with turn, climb and acceleration limits, drain and swap batteries,
  launch one at a time from separate departure heights, and acknowledge tasks exactly like a real ground station.
- **Interoperability**: reserved airspace is published as ASTM F3548-shaped operational intents at `/api/fleet/operations`
  (altitudes above ground; Terra is not a certified UTM service supplier). DJI aircraft would need an adapter for DJI's Cloud
  API, which requires DJI developer credentials; the protocol has a slot for it but no adapter yet.
- **Workspace**: the fleet is rendered from telemetry 1.2 s in the past with interpolation; reserved airspace is drawn as
  translucent volumes; the panel shows the coordinator's round, each task's lifecycle and reason, the airspace band, the
  agent's status, and the shared coordination log. Manual flight of a virtual aircraft is predicted locally and streamed to
  the server as setpoints.

## Phase 4h — Real autopilot firmware, field notes, mission profiles, adding aircraft (2026-11-14)
- **ArduPilot SITL** (`scripts/sitl.py`): the real ArduCopter and ArduPlane flight firmware on simulated airframes, built
  once into `~/.terra/sitl` (outside the repo) at low priority with a memory guard that pauses the compiler below 1.5 GB
  free. `sitl.py fleet` runs five vehicles (one ArduPlane mapper, four ArduCopter quads) at the monitoring area's launch
  site, one MAVLink bridge each, and relaunches them at the new site when the area moves more than 40 km;
  `sitl.py add --id … --sensors …` adds one more. The dev stack flies this fleet when it is built, otherwise the virtual
  fleet (`TERRA_FLEET=sitl|virtual|off`).
- **MAVLink missions** (`scripts/drone_bridge.py`): every task kind is uploaded with the MAVLink mission protocol: takeoff,
  speed, then LOITER_TURNS for orbits, LOITER_UNLIM for holds and relays, waypoint loops for surveys, perimeters and
  expanding-square searches (DO_JUMP repeats them until re-tasked), RTL for return. The bridge waits for a 3D fix, the EKF
  position estimate and home before registering, retries arming, and reports a task active only when the aircraft is on
  station at the asked height. Arming and GUIDED-mode manual flight need `--allow-arming` / `--allow-manual` and are
  refused for real aircraft.
- **Field notes** (`backend/terra/fleet/observe.py`): on station and every two minutes, each drone agent notes where its
  camera looks (line of sight to the ground) and the measured values there: Landsat 8/9 surface temperature, this
  investigation's radar backscatter change, ESA WorldCover land cover, ground height and Open-Meteo weather. Each value
  names its source; nothing is invented. Warmer than the area's 95th percentile or a change of 3 dB or more becomes an
  observation broadcast to the fleet. Notes appear in the panel, as pins on the map and on the camera's HUD.
- **Communication**: an aircraft that takes over a job tells the one it relieves; observations, hand-offs, joins and
  rejoins go to the shared log. The map draws each aircraft's radio link to the launch site or, beyond direct range,
  through the relay aircraft, coloured by reported signal strength.
- **Mission profiles**: change watch (orbit measured change, then summits), thermal watch (IR aircraft orbit the warmest
  ground in the latest cloud-free Landsat scene; readings at the top of the scene's scale are shown as "≥"), and search
  (four sectors, each an expanding-square search at 90 m). People can also assign a search to one aircraft.
- **Adding aircraft**: the panel's Add a drone issues a token for that aircraft alone (only its SHA-256 is stored; it can
  be revoked) and one command: `sitl.py add` for a simulated aircraft, or the MAVLink bridge for a real one (advisory
  until `--allow-commands`). Enrollment works only from the machine running Terra. Aircraft silent for 15 minutes leave
  the fleet until they register again.
- **Camera**: IR shows measured temperature (ironbow, white or black hot) over a dim view of the ground so terrain stays
  legible, with the scene's temperature scale and bracketed markers on the warmest measured ground; the crosshair reads
  the temperature or says there is none. EO has a sensor-style grade.

## Phase 4i — Map Tiles handoff and drone workspace (2026-11-14)
- Google 3D keeps the terrain preview visible until the first view is ready, then switches basemaps together. Restricted keys use the configured public referrer; keys remain behind the connector.
- Camera header, expand/return controls, simpler HUD, mission-target and downward views, zoom, help, telemetry warnings, and video loading/error states. The toolbar stays above the timeline and fits a 360 px viewport.
- Fleet reporting/task/attention summaries, aircraft filters, and direct camera access. Connected autopilot altitude is preserved instead of being replaced by coarse map terrain during loading.
- IR palettes apply only to measured Landsat pixels and preserve nodata transparency. Dated source context stays visible; changing investigations replaces the camera's radar overlay.
- Validation: production build, 194 backend tests, browser checks at desktop and 360 px, and no configured credentials found in the production bundle. The connected fleet used for UI verification was ArduPilot SITL.

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
