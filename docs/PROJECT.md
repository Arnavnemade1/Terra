# TERRA — Project and Final Product Specification

**Purpose:** Shared product, engineering, science and operations reference.
**Status convention:** “Implemented” describes code present in this repository; it does not imply production deployment, field validation, flight certification or independent scientific validation.

## 1. The final goal

Terra is a unified Earth-observation and investigation workspace connecting satellite evidence, autonomous scientific workflows and coordinated drone observations. A person selects a place, asks what changed, sees reproducible evidence, and can direct further observation through the same workspace.

The finished product should answer:

1. What changed, where, and between which observations?
2. How large is the measured change, and which areas deserve attention?
3. Which explanations are supported, contradicted or still uncertain?
4. What additional evidence would resolve the uncertainty?
5. Which available aircraft or satellites could collect useful observations?
6. What did the follow-up observation actually establish?

The final loop is **observe → investigate → verify → task → observe again**. The platform must retain the source, time, method and limitations of every result through that loop.

Terra was conceived for NASA Space Apps. Its purpose extends across floods and wetlands, vegetation disturbance, volcanic surfaces, agriculture, urban change, coastlines, snow and ice. Ground displacement requires an additional interferometric pipeline; it cannot be inferred from the current backscatter method.

## 2. Product principles

- One application for maps, investigations, reports, satellites, hazards and drones.
- Real source data and reproducible computation underpin measured claims.
- Measured findings, contextual models, hypotheses and forecasts are explicitly distinguished.
- Acquisition time is visible. Historical optical and thermal images must never appear to be live camera data.
- Missing or unusable data produces a visible gap, not substituted evidence.
- Agents can plan and explain; they cannot invent observations or override scientific checks.
- Credentials remain on the server and do not enter browser bundles or public artifacts.
- Simulation, autopilot simulation and real aircraft are visibly distinguishable.

## 3. Main user journey

Search for a place, coordinates, reported hazard or saved investigation. Select or draw an area, then ask a question or choose an investigation lens. Terra finds compatible radar observations, reads the required region of the original products, computes measurements and publishes the checked result. The map shows layers and numbered change patches; the dossier shows dated images, measurements, sources and limitations.

The user can inspect individual pixels, compare before and after, draw a terrain profile, examine weather at acquisition time, ask follow-up questions and review the execution record. They can then monitor the area with the drone fleet, inspect aircraft telemetry and camera views, and assign observation tasks.

## 4. Workspace and interaction design

The interface adapts the map-led investigation layout in the supplied reference video: a top command bar, large map, right-hand contextual panel and bottom timeline.

Implemented interface areas include investigation, hazards, satellites and drones; search and saved work; data layers; map styles and 2D/3D controls; before-and-after comparison; charted measurements; terrain profiles; camera views; and evidence reports. The timeline drawer closes completely and opens to approximately 25% of the viewport height. A visible Timeline control reopens it.

Map styles include photographic imagery and stylised relief. The relief renderer adds terrain shading, contours, land cover and trees. These are presentation tools; analytical values retain their own provenance. Display filters and simulated sensor rendering are not independent measurements.

Google Photorealistic 3D Tiles are optional display content, served through a server gateway. Their availability depends on an enabled Google Map Tiles project, billing, key permissions, quota and coverage. Google imagery is not used to derive scientific measurements, and required attribution remains visible.

## 5. Satellite investigation pipeline

### Durable execution

`InvestigationWorkflow` in Temporal coordinates planning, discovery, preparation, computation, verification, synthesis and optional imagery enrichment. Snapshots, status, events, results and artifacts survive process restarts through workflow history and persistent storage. Missing protected inputs can block an investigation until access or data is supplied.

Independent reads and contextual requests run concurrently where possible. Verified measurements publish before optional enrichment finishes. Cached investigations target roughly 10–20 seconds; cold source reads, provider latency, retries and model responses can take longer. Timing must be reported from actual execution rather than a guaranteed duration.

### Current science

The main method uses NISAR GCOV calibrated radar power, converted to decibels. Change is calculated as later minus earlier on pixels valid in both acquisitions. Matching observations must pass geometry and processing compatibility checks. Product exception masks and invalid/nonpositive values are handled before measurement.

Outputs include backscatter summaries and distributions, change distributions, valid pixel counts, coverage and area, threshold-exceedance area, descriptive low-return and bright-return areas, and connected change hotspots. Low-return “water-like” pixels are a heuristic, not independently validated flood classification. Threshold magnitude is not statistical significance.

Where the input budget requires averaging, the reduction in resolution is recorded. Float rasters provide numerical data; coloured previews provide geographic presentation. Percentiles of pixel-wise change are not differences between the two dates’ summary percentiles.

### Language agents

The planner interprets questions and date windows; catalog selection is constrained by actual observations. The writer receives computed evidence and contextual records. Numerical claims and dates are checked against that evidence. Unsupported drafts are retried or replaced with a deterministic computed summary. Follow-up answers use the same evidence boundaries.

Numerical traceability alone does not establish a causal explanation. The final product must also connect important claims to their specific supporting pixels, sources and reasoning.

## 6. Evidence sources

| Source | Role | Interpretation boundary |
| --- | --- | --- |
| NASA/ASF NISAR GCOV | Original radar acquisitions and calibrated surface change | Backscatter is not ground displacement |
| Copernicus Sentinel-2 L2A | Dated true-colour before/after context | Clouds, date offsets and coverage remain visible |
| Landsat 8/9 Collection 2 Level 2 | Land-surface temperature for thermal context | Historical satellite surface temperature, not a live drone thermal sensor |
| Open-Meteo / ERA5 and recent weather models | Precipitation, soil moisture, temperature and wind at radar times | Modelled regional context, not local ground truth or proof of cause |
| Copernicus elevation via Open-Meteo | Sampled terrain profiles | Surface elevations, not displacement measurements |
| USGS, GDACS, NASA EONET, NASA FIRMS | Reported hazards, earthquakes, volcano alerts and fire detections | Spatial proximity alone does not link a hazard to an earlier radar change |
| CelesTrak and an orbital-element mirror | Satellite position propagation and fleet display | Predicted position and radar swath access are not guaranteed tasking opportunities |
| Esri, NASA GIBS, optional Google Map Tiles | Geographic display | Basemaps are separate from investigation evidence |

Optical images are selected using coverage and cloud/shadow estimates inside the area of interest. Snow and ice are distinguished from atmospheric obscuration. Recent imagery remains labelled with its actual acquisition date.

The thermal implementation reads Landsat temperature bands with QA masking and documented scale/offset. Approximate native thermal resolution is coarser than its delivered pixel grid. Applying a thermal colour palette to a simulated drone view does not create new thermal observations.

## 7. Evidence dossier

The dossier is the durable, reviewable output of an investigation. It includes dated optical and radar comparison views; change overlays and maps; the answer and supporting evidence; hotspot locations; quantified measurements; source product metadata; coverage and quality; processing parameters and checks; limitations; execution history; and downloadable artifacts.

Before-and-after images must clearly identify sensor, date and geographic area. Optical date offsets, cloud coverage and missing scenes must remain visible. Forecasts require a separately validated model and uncertainty record; the current system must not extrapolate a numerical forecast from one or two radar images.

Print/PDF and underlying data downloads support review. A standalone shareable evidence export, richer claim-to-pixel navigation and validated forecasting remain future work unless separately implemented and verified.

## 8. Drone system: purpose and operating modes

Drones extend an investigation into targeted local observation: revisiting changed patches, surveying an area, mapping a perimeter, inspecting a summit, maintaining overwatch or providing a communications relay.

Three distinct modes exist:

| Mode | What runs | What it demonstrates |
| --- | --- | --- |
| Virtual fleet | Lightweight aircraft motion and battery models | UI, coordination, task lifecycle and telemetry integration |
| ArduPilot SITL | Actual ArduCopter/ArduPlane firmware on simulated airframes | Autopilot mission handling, bridge integration and simulated flight dynamics |
| Real MAVLink aircraft | Ground-station bridge connected to an autopilot | Real telemetry and explicitly enabled mission forwarding; field operation requires operator responsibility |

SITL support and real-aircraft protocol code do not establish that a real deployment has been flight-tested.

## 9. Central fleet architecture

All fleet state is coordinated through a central SQLite database in WAL mode, shared by the API and workers. Aircraft identities, telemetry sessions, jobs, bids, tasks, acknowledgements, reserved volumes and messages are persisted.

```text
Virtual aircraft / ArduPilot SITL / real MAVLink ground station
                       |
              authenticated fleet protocol
                       |
                  FastAPI /api/fleet
                       |
                central fleet database
                  /             \
     FleetCoordinatorWorkflow   DroneAgentWorkflow × aircraft
                       |
             browser fleet and camera workspace
```

The coordinator runs periodically and responds to signals. Per-aircraft durable agents assess suitability, availability and conformance. Coordination is database-mediated, allowing the UI and bridge to observe the same decisions and lifecycle.

### Task allocation

Monitoring targets become jobs with capability requirements and priority. Aircraft bid using position, sensors, speed, endurance and estimated cost. The coordinator seeks coverage of high-priority work while allocating scarce capabilities appropriately and avoiding unnecessary reassignment.

Jobs include orbit, survey, perimeter mapping, relay, overwatch, hold and return. People’s task assignments and simulated manual flight are respected. Low energy, weak communications, stale telemetry, missed acknowledgements and task failures can trigger replanning or return decisions.

### Lifecycle and separation

Registration records identity and capabilities. Telemetry carries increasing sequence numbers; repeated or late updates are rejected. Task acknowledgements progress through assigned, acknowledged, active and terminal states. Sessions distinguish reconnects from stale messages.

Tasks reserve geographic, altitude and time volumes. The coordinator applies altitude separation to overlapping planned operations and agents report nearby traffic or conformance departures. These are implemented coordination mechanisms, not certified collision avoidance or certified air traffic management.

Operational intents can be exposed in an ASTM F3548-shaped form. Terra is not a certified UTM service supplier; schema resemblance must not be presented as standards certification.

## 10. Drone workspace and cameras

The map displays aircraft models, call signs, origin/status labels, routes, selected sightlines and reserved operation volumes. Fleet details expose mission, task reason, task lifecycle, position, altitude, speed, heading, battery, link and agent status. A coordination log explains assignments and changes.

Camera views support simulated geographic rendering and connected video sources. The HUD shows aircraft and camera attitude, mission information, time, position, ground reference, field of view and available telemetry. Dragging changes the simulated gimbal; zoom adjusts the camera view. Radar-change and historical Landsat thermal overlays provide investigation context.

Manual browser flight is limited to simulated aircraft. Ground-station video may be supplied as an MJPEG or snapshot source. A live video stream must remain distinguishable from a synthetic geographic camera and from dated satellite overlays.

## 11. Ground-station bridge and SITL

`scripts/drone_bridge.py` registers aircraft, relays telemetry, receives tasks and acknowledges their progress. MAVLink integrations target PX4/ArduPilot. Explicit command flags determine whether tasks remain advisory or are sent to the autopilot. Arming/takeoff is separately controlled. The operator retains responsibility and override capability.

`scripts/sitl.py` can set up ArduPilot and launch multiple simulated aircraft with separate parameters and MAVLink ports. It connects each vehicle through the same bridge used by a ground station. Building firmware is a separate, potentially lengthy setup step; it is not required for the lightweight virtual fleet.

A DJI Cloud API adapter is a future integration requiring DJI developer access. A protocol slot is not a completed adapter.

## 12. Technology and repository map

| Area | Main implementation |
| --- | --- |
| Browser application | React, TypeScript, Vite, CesiumJS; `src/App.tsx`, `src/Globe.tsx` |
| Reports and comparisons | `src/Dossier.tsx`, `src/Compare.tsx`, report styles |
| Timeline and charts | `src/Dock.tsx`, `src/charts.tsx` |
| Geographic presentation | `src/relief.ts`, live layers, satellite fleet and sensor views |
| Drone presentation | `src/DronesPanel.tsx`, `src/DroneFeed.tsx`, drone store/layers/models |
| HTTP API | FastAPI; `backend/terra/app.py` |
| Provider boundary | `connector.py`, geographic gateways, Google tile gateway |
| Investigation execution | `workflows.py`, `activities.py`, `worker.py`, Temporal |
| Scientific processing | NumPy, SciPy, Rasterio, h5py, pyproj; `science.py`, acquisition and compute runner |
| Optical, thermal and contextual data | `optical.py`, `thermal.py`, `conditions.py`, terrain and feed modules |
| Fleet coordination | `backend/terra/fleet/`: API, database, planner, activities, workflows and observation logic |
| Local orchestration | `scripts/dev.py`, Temporal development helper, drone bridge, SITL helper |
| Tests | `tests/`: science, agents, workflows, APIs, connector, imagery, weather, terrain and fleet |

## 13. Security and configuration

External data requests use a central provider allowlist and shared connection pools. Redirects, response sizes and returned artifacts are constrained. Scientific execution receives a reduced environment so provider credentials are not passed into computation unnecessarily. Development servers deny sensitive workspace files.

Credential names are documented in `.env.example`; actual values must remain in ignored environment files or deployment secret storage. Relevant variables include `EARTHDATA_TOKEN`, `OPENAI_API_KEY`, `GOOGLE_MAPS_TILES_KEY` and `TERRA_DRONE_TOKEN`. `TERRA_LLM_MODEL` selects the writer/planner model. `TERRA_FLEET` and `TERRA_VIRTUAL_FLEET` configure development aircraft. Legacy variable names may remain supported server-side; they must not become client-side secrets.

Fleet registration, telemetry and task exchange require the configured bearer credential. Production work needs further access control, per-operator/aircraft credentials, secure video access, rate limits, audit retention and deployment hardening. Local connectivity is not equivalent to production security validation.

## 14. Running and verifying the project

With repository dependencies and the Python environment installed:

```sh
npm start
```

The local supervisor starts the required services. The browser normally serves at `http://127.0.0.1:5173`; the API port can vary when the default is occupied. Consult supervisor output for actual addresses.

```sh
npm run build
npm test
```

These verify the TypeScript/production build and Python suite. They do not substitute for browser review, real-provider checks, durable restart tests or hardware flight testing. Use the virtual fleet before SITL, and SITL before a controlled hardware integration exercise.

## 15. Remaining milestones and acceptance criteria

### A. Reliable investigation product

Complete deployment and access control; demonstrate multiple environmental use cases with real observations; keep evidence packets reproducible; expose uncertainty and missing coverage clearly; test restart/resume and provider outages. Report actual first-result, narrative and enrichment timings separately.

### B. Evidence-to-action workflow

Link important claims to exact sources and spatial evidence. Allow an operator to select a hotspot, create an observation objective, see why aircraft were assigned and inspect the resulting records. Persist field imagery and its geographic/time calibration with the originating investigation.

### C. Validated multisensor science

Add appropriately validated water, vegetation and disturbance methods; integrate independent precipitation and ground observations; account for speckle, geometry and terrain effects. Add displacement analysis only with suitable interferometric products and corrections. Validate each method against independent reference data.

### D. Robust fleet operations

Exercise task upload, acknowledgements, reconnects, battery returns, capability scarcity, weak links and failed aircraft in SITL. Validate reservations and operator overrides. Establish explicit deployment and safety boundaries before field operation. Add supported vendor adapters only with real access and end-to-end verification.

### E. Forecasting and monitoring

Introduce watchlists and new-acquisition notifications. Forecast only phenomena with a documented model, training/reference data, backtesting, uncertainty and a suitable decision context. Record contradictions and update conclusions when new evidence arrives.

### F. Final demonstration

A complete demonstration should start with a real area and question, produce checked before-and-after evidence, identify a spatial observation target, coordinate a labelled simulated or approved connected fleet, show telemetry and observation results, and retain the entire record in the dossier. Every displayed measurement must have an identifiable source; every simulated element must be labelled.

## 16. Definition of success

Terra succeeds when a user can understand a real surface change, inspect the evidence, identify what remains uncertain, and coordinate a useful follow-up observation in one coherent workspace. Visual quality supports that understanding. Scientific traceability, dependable execution and honest operational boundaries make the result trustworthy.

## Related references

- [Phase history and implementation notes](PHASES.md)
- [Example environment configuration](../.env.example)
- [Project source](https://github.com/Arnavnemade1/Terra)
- [Interface reference supplied by the team](https://x.com/claudeai/status/2111177834127601902)

This document consolidates repository code and implementation notes as of its update date. Provider availability, configured credentials, deployment state, firmware builds and hardware validation must be checked in the intended runtime environment.
