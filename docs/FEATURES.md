# TERRA: 100 features worth building

Challenge: NASA Space Apps, "Dancing with the SARs". Track and visualize surface change with NISAR radar, hypothesize the
physical drivers, and say which polarizations show the change. Judging: **Impact**, **Creativity**, **Validity**, **Relevance**
(NASA data at the core), **Presentation**.

**What already exists** (see `docs/PHASES.md`, so none of it is proposed again): original GCOV HDF5 subsets read over HTTP byte
ranges (`acquire.py`), one diagonal term per run (HH when present), gamma0 dB and later-minus-earlier change, a fixed 3 dB descriptive
threshold, water-like (below −18 dB) and very bright (above −3 dB) area, up to six hotspots, histograms, COG plus PNG export,
strict pair compatibility (track, frame, direction, maturity, release, grid), an LLM planner and a writer whose numbers and dates are
checked against evidence, Sentinel-2 true colour, Open-Meteo conditions, ESA WorldCover shares, live hazard feeds, a timeline dock,
swipe compare, relief map, sensor view and Google Photorealistic 3D Tiles (display only).

**Tags.** Effort: **[S]** 2 h or less, **[M]** 1 day or less, **[L]** several days. Criteria are listed after the effort tag.
**Sources were checked**: every CMR collection named below was found by short name in
`cmr.earthdata.nasa.gov/search/collections.json`, and every static COG host answered an HTTP range request (206).
Collections confirmed: `NISAR_L2_GCOV_V1` and `_PROVISIONAL_V1`, `NISAR_L2_GUNW_V1`, `NISAR_L2_GOFF_V1`, `NISAR_L2_GSLC_V1`,
`NISAR_L3_SME2_PROVISIONAL_V1`, `OPERA_L2_RTC-S1_V1`, `OPERA_L3_DSWX-S1_V1`, `OPERA_L3_DSWX-HLS_V1`, `OPERA_L3_DIST-ALERT-HLS_V1`,
`OPERA_L3_DIST-ALERT-S1_V1`, `OPERA_L3_DISP-S1_V1`, `SPL3SMP_E`, `GPM_3IMERGDL`. GIBS layers confirmed in the EPSG:3857 capabilities:
`NISAR_L2_Geocoded_Polarimetric_Covariance`, `OPERA_L3_Dynamic_Surface_Water_Extent-Sentinel-1`, `IMERG_Precipitation_Rate`,
`MODIS_Combined_Flood_1-Day`.

Any new external host must be added to `PROVIDERS` in `backend/terra/connector.py`, restricted to the documented path prefix.

---

## 1. SAR science and calibration (12)

**1. Read every polarization the product has** · [S] · Validity, Relevance
`acquire.py` keeps only the first diagonal term (`terms = [...][:1]`). Read all diagonal terms present (`HHHH`, `HVHV`, or `VVVV`, `VHVH`)
plus `numberOfLooks` and `rtcGammaToSigmaFactor` in the same coalesced range prefetch, and let `run_analysis` loop over polarizations.
This unlocks every feature in section 2. Data: `NISAR_L2_GCOV_V1` via ASF. Files: `acquire.py`, `science.py`, `tests/test_science.py`.

**2. Equivalent number of looks (ENL), measured and declared** · [S] · Validity
Estimate ENL as mean² / variance of linear gamma0 over the 7×7 windows in the lowest decile of coefficient of variation (homogeneous
areas), and compare it with the product's `numberOfLooks` layer scaled by the multilook factor `k²`. Report both in provenance; every
statistical test below takes ENL from here. Files: `science.py`, `tests/test_science.py`.

**3. Refined Lee speckle filter, display and classification only** · [M] · Validity, Presentation
Refined Lee filter (Lee 1981, eight edge-aligned 7×7 masks) on linear power, with ENL from feature 2. Measurements stay on unfiltered
values; filtered layers are exported separately and labelled. The provenance `speckle_filter` field records the filter and window,
so the "No speckle filter" limitation becomes conditional. Files: `science.py`, `src/App.tsx` (layer picker).

**4. Multitemporal speckle filter (Quegan and Yu 2001)** · [M] · Validity
For a same-track stack (feature 27): `J_k = (<I_k> / N) · Σ_i I_i / <I_i>`, with local means from a 5×5 boxcar. It reduces speckle
without blurring edges in space, and keeps the date-to-date contrast that change detection needs better than a spatial filter.
Files: new `backend/terra/timeseries.py`, `science.py`.

**5. Sigma0 alongside gamma0** · [S] · Validity, Impact
Multiply gamma0 by the product's `rtcGammaToSigmaFactor` layer to get sigma0. Published flood and vegetation thresholds are often stated
in sigma0, and users can then compare against the literature. Both radiometries appear in the inspector and the COG tags.
Files: `acquire.py` (copy the layer), `science.py`, `src/Analysis.tsx`.

**6. Noise-floor flag for dark cross-pol pixels** · [S] · Validity
If the GCOV calibration metadata carries a noise-equivalent backscatter, flag pixels within 3 dB of it; if it does not, say the
noise floor is unknown and label dark HV pixels "near noise, interpret with care" rather than reporting them as change. This stops
the writer from reading noise in HV over water as a real signal. Files: `science.py`, `agent.py` (limitations).

**7. Layover and shadow mask from DEM plus look geometry** · [M] · Validity
When `layoverShadowMask` is absent (the code already notes the subswath mask is not one), compute slope and aspect from Copernicus DEM
GLO-30 and the line-of-sight vector from the GCOV `metadata/radarGrid` cubes (`losUnitVectorX/Y`, `incidenceAngle`). Mark layover where
the slope facing the radar exceeds the incidence angle, shadow where the back slope exceeds 90° minus incidence. Data: `copernicus-dem-30m.s3.amazonaws.com`.
Files: new `backend/terra/ancillary.py`, `science.py`, `connector.py`.

**8. Residual terrain check on RTC data** · [M] · Validity
GCOV is already terrain-flattened gamma0 (area-projection RTC). Fit gamma0 dB against local incidence angle (feature 7 geometry) within one
WorldCover class, and report the slope with a 95% interval. A slope well away from zero means residual terrain effects; it is then
stated as a limitation and as a reason to distrust hotspots on steep slopes. Files: `science.py`, `terrain.py`.

**9. Incidence-angle normalization across tracks** · [M] · Validity, Impact
Normalize with `γ0_ref = γ0 · (cos θ_ref / cos θ)^n`, fitting `n` per WorldCover class from the area itself. This allows an *opt-in*
ascending/descending or cross-frame comparison, which `check_compatibility` now rightly refuses. Always labelled "normalized,
different geometry", never presented as a same-track result. Files: `science.py`, `agent.py` (`select_acquisitions`).

**10. Effective looks and resolution readout, frequency B selection** · [S] · Validity
Show the per-pixel `numberOfLooks` and multilook factor next to the pixel spacing, which addresses the limitation "pixel spacing is not
effective resolution". Where a granule has a `frequencyB` grid, let the user pick it; compatibility already checks frequency.
Files: `science.py`, `src/Analysis.tsx`, `src/Dossier.tsx`.

**11. Pair radiometric offset on stable targets** · [S] · Validity
Take pixels that WorldCover marks built-up or bare and that are outside every hotspot; their median Δ dB is the pair's relative
calibration offset, with a bootstrap interval. Report it, and warn when it is comparable to the change threshold. This gives the measured
change a reference point that is not assumed. Files: `science.py`, `terrain.py`.

**12. Wind-roughened water warning** · [S] · Validity
For pixels with JRC Global Surface Water occurrence ≥ 90% that are *not* water-like on a date, combine them with the Open-Meteo 10 m wind
already fetched in `conditions.py`. When the wind is high, state that the water-like area is an undercount for that date and why
(Bragg roughening). Data: `storage.googleapis.com/global-surface-water/downloads2021/occurrence/`. Files: `science.py`, `ancillary.py`, `agent.py`.

## 2. Polarimetry and change detection (14)

**13. Cross-pol ratio layer (HV/HH, in dB)** · [S] · Relevance, Validity
`HV_dB − HH_dB` per date and its change. A higher ratio means more volume scattering (canopy); a ratio drop with an HV drop points to
canopy loss. It is exported as a COG with its own legend. Needs feature 1. Files: new `backend/terra/polarimetry.py`, `science.py`.

**14. Dual-pol Radar Vegetation Index** · [S] · Impact, Relevance
`RVI = 4·σHV / (σHH + σHV)` in linear power (the dual-pol adaptation of Kim and van Zyl's RVI), per date and as a difference, with
statistics per WorldCover class. Labelled an index, not biomass. Serves crop and forest themes. Files: `polarimetry.py`.

**15. Dual-pol false-colour composite** · [S] · Presentation, Relevance
RGB = HH, HV, HH/HV, with fixed documented dB stretches, so that water, bare soil, canopy and double bounce separate visually. This is
*not* a Pauli composite, which needs the HH−VV phase. A legend explains what each colour means physically. Files: `polarimetry.py`, `src/App.tsx`.

**16. Dual-pol entropy and alpha, only when the off-diagonal term exists** · [M] · Creativity, Validity
Read `listOfCovarianceTerms`; if the complex `HHHV` term is delivered, build the 2×2 covariance C2, take its eigen-decomposition and map
entropy H, mean alpha and span (Cloude 2007 dual-pol). If only diagonals exist (the usual case), the layer is disabled and the
reason is shown. Files: `acquire.py`, `polarimetry.py`, `science.py` (allow complex terms on this path only).

**17. Freeman-Durden three-component decomposition for quad-pol granules** · [L] · Creativity
Surface, double-bounce and volume power from the 3×3 covariance where a quad-pol GCOV carries `HHVV` and the other off-diagonals.
It runs only for those granules and is detected from metadata, never estimated from diagonals alone. Files: `polarimetry.py`, `catalog.py` (flag quad-pol).

**18. Automatic change threshold (Kittler-Illingworth)** · [S] · Validity
Replace the fixed 3 dB with the minimum-error threshold of Kittler and Illingworth on the log-ratio histogram (the generalized
Gaussian form of Bazi, Bruzzone and Melgani 2005), separately for increase and decrease, with Otsu as a fallback. Report the chosen
thresholds and the class fits. The user's manual threshold stays available. Files: `science.py`, `tests/test_science.py`.

**19. Omnibus change test over a same-track stack (Conradsen 2016)** · [M] · Validity, Creativity
For k acquisitions, the likelihood-ratio test for equal Gamma-distributed intensities, per polarization, using ENL from feature 2.
The dual-pol statistic is the sum of the per-polarization `−2 ln Q` terms (diagonal-only covariance). Output: a per-pixel p-value for "any
change in the stack". Files: `timeseries.py`.

**20. Pairwise likelihood-ratio test with false-discovery control** · [S] · Validity
The two-date case of feature 19: a per-pixel p-value, then a Benjamini-Hochberg FDR mask at q = 0.05. The limitation text keeps the
caveat that neighbouring pixels are correlated. This replaces "descriptive threshold only" with a stated test. Files: `science.py`.

**21. Four-quadrant change classes with hypothesized drivers** · [S] · Impact, Relevance, Creativity
Classify significant pixels by the signs of ΔHH and ΔHV: both down (open water appearing, clearing), HH up and HV flat or down (flooded
vegetation double bounce, new structures), both up (growth, wetting), HH down and HV up (rare, flagged). A documented rule table
assigns *hypotheses*, which the writer may cite only as hypotheses. Files: `polarimetry.py`, `agent.py` (WRITER prompt).

**22. "Which polarization shows it" report** · [S] · Relevance, Validity
For each hotspot and the whole area, the change in each polarization divided by its expected speckle standard deviation (from
ENL, feature 2). The polarization with the larger standardized change is named, with both values. This answers the challenge's
polarization question with numbers. Files: `polarimetry.py`, `agent.py`, `src/Analysis.tsx`.

**23. Object-based change with superpixels** · [M] · Validity
SLIC superpixels (`skimage.segmentation.slic`) on the filtered multi-pol stack, then the test from feature 20 on segment means, with
segment ENL equal to pixel ENL × effective pixel count. Hotspots follow real field and forest edges instead of 3×3 blobs.
Files: `science.py`, `polarimetry.py`.

**24. Persistence rule for change** · [S] · Validity
With three or more dates, keep a change only if it holds in the following acquisition too (two of three). This suppresses one-date
rain wetting and wind effects. The mask states which rule produced it. Files: `timeseries.py`.

**25. User-set minimum mapping unit** · [S] · Impact
Replace the fixed 12-pixel hotspot minimum with an MMU in hectares, converted using the pixel area. Morphological opening then
labelling as now. Land managers think in hectares, not pixels. Files: `science.py`, `src/Analysis.tsx`.

**26. Per-date water map with split-based Otsu** · [S] · Validity, Impact
Martinis et al. (2009) tile selection: keep tiles whose histogram is bimodal (Ashman's D above 2), run Otsu on the pooled tiles, and
apply that threshold to the whole area. The water threshold then adapts to each scene instead of the fixed −18 dB proxy (which stays as
the fallback). Files: `science.py`.

## 3. Time series and interferometric products (12)

**27. Same-track stack acquisition** · [M] · Relevance, Impact
Pull every GCOV on the chosen track, direction and frame in a date window (capped at 8), using the existing range reader with a
bounded parallel pool and the subset cache. NISAR's 12-day repeat makes stacks of comparable dates. This is the basis for features 4, 19 and 24 and
for 28 to 33. Files: `acquire.py`, `agent.py`, `activities.py`, `workflows.py`.

**28. Time-series chart per hotspot and per clicked point** · [M] · Presentation, Impact
Median and IQR of gamma0 per date and per polarization for a hotspot polygon or a clicked 5×5 window, drawn in the dock. It extends
`/api/investigations/{id}/sample` to return a series. Files: `app.py`, `timeseries.py`, `src/charts.tsx`, `src/Dock.tsx`.

**29. Animated stack playback on the map** · [M] · Presentation
Each stack date is its own COG layer with one fixed stretch; the existing timeline scrubber and play button step through the
layers, with the date burned into the legend. Export the animation as an animated WebP for the report. Files: `src/Dock.tsx`, `src/Globe.tsx`, `science.py`.

**30. Change-point date per hotspot (PELT)** · [M] · Validity, Impact
Run PELT (`ruptures`, Gaussian cost) on each hotspot's median-dB series; report the most likely change date with a bootstrap interval on
the breakpoint. Then cross-reference the date against IMERG rain, FIRMS fires and USGS quakes on the timeline. Files: `timeseries.py`, `src/Dock.tsx`.

**31. First-change-date and change-count maps** · [M] · Creativity, Presentation
Use the sequential factorization of the omnibus test (Conradsen 2016, the R_j tests) to map, per pixel, the date of the first significant
change and the number of changes in the stack. Coloured by date with a categorical legend. Files: `timeseries.py`, `src/App.tsx`.

**32. Temporal stability map** · [S] · Impact
The coefficient of variation of linear gamma0 across the stack. A low value marks stable scatterers (rock, buildings); a high value marks
crops, wetlands and floodplains. It doubles as the stable-target mask for feature 11. Files: `timeseries.py`.

**33. Inundation frequency and flooded-forest occurrence** · [S] · Impact, Relevance
For each pixel, the fraction of stack dates classed as open water (feature 26) and the fraction showing HH double bounce under canopy
(feature 52). This is a wetland hydroperiod map for Pacaya-Samiria-type floodplains. Files: `timeseries.py`.

**34. NISAR GUNW line-of-sight displacement (separate product)** · [L] · Relevance, Creativity
Read the unwrapped phase and coherence from `NISAR_L2_GUNW_V1` over the area through the same range reader. Convert to line-of-sight
displacement with `d = −λ / (4π) · φ`, using the product's own wavelength metadata, relative to a user-chosen reference point.
This is labelled as interferometric and kept fully separate from GCOV. Files: new `backend/terra/gunw.py`, `catalog.py`, `src/App.tsx`.

**35. Coherence loss map from GUNW** · [M] · Validity, Impact
The GUNW coherence layer per pair. Coherence that drops where backscatter barely changes indicates disturbance invisible to amplitude
(collapse, harvest, landslide). It is shown next to the GCOV change with a joint 2D histogram. Files: `gunw.py`, `src/charts.tsx`.

**36. Glacier and landslide velocity from NISAR GOFF offsets** · [L] · Relevance, Impact
Read the along-track and slant-range pixel offsets from `NISAR_L2_GOFF_V1`, convert them to metres per day using the pair's time
separation, and draw arrows on the globe, masked by the product's quality layers. This covers the glacier theme with an official
NISAR product. Files: new `backend/terra/goff.py`, `src/Globe.tsx`.

**37. OPERA DISP-S1 subsidence overlay (North America)** · [M] · Impact, Validity
Where the area falls inside `OPERA_L3_DISP-S1_V1` coverage, read the displacement time series for the area and show it beside NISAR
change: mature InSAR displacement from Sentinel-1, clearly labelled as another sensor. Outside its coverage, the layer says so.
Files: `ancillary.py`, `connector.py`.

**38. Next comparable acquisition forecast** · [S] · Impact
From the ASF catalog history of the selected track and frame, and the 12-day repeat, predict the next date a *comparable* image
should exist. Combine it with `swathPass`, and show it as "next same-geometry look". This is a schedule from orbit and catalog,
not a data forecast. Files: `catalog.py`, `src/fleet.ts`, `src/App.tsx`.

## 4. Multi-sensor fusion (12)

**39. L-band versus C-band: Sentinel-1 RTC beside NISAR** · [M] · Creativity, Validity
Fetch the `OPERA_L2_RTC-S1_V1` VV/VH gamma0 COG nearest each NISAR date (ASF, Earthdata login) and compute the same change. C-band
saturates over dense canopy where L-band still responds, so the pair can show *why* NISAR sees the change. Files: `ancillary.py`,
`connector.py` (datapool path), `src/Compare.tsx`.

**40. OPERA DIST-ALERT agreement for vegetation loss** · [M] · Validity
Intersect NISAR decrease hotspots with `OPERA_L3_DIST-ALERT-HLS_V1` (optical) and `OPERA_L3_DIST-ALERT-S1_V1` (radar) disturbance
pixels dated within the pair. Report the share of each hotspot confirmed by each. Files: `ancillary.py`, `agent.py` (evidence).

**41. JRC Global Surface Water context for new water** · [S] · Validity, Impact
Split "newly water-like" area by JRC occurrence: permanent (≥ 90%), seasonal (above 0 and below 90%), and never observed as water (0%).
Water appearing where there was never any is far more likely to be a flood. Data: JRC GSW 2021 occurrence tiles
(`storage.googleapis.com/global-surface-water/`). Files: `ancillary.py`, `science.py`.

**42. Hansen Global Forest Change context** · [S] · Validity, Impact
Read `treecover2000` and `lossyear` from GFC-2024-v1.12 (`storage.googleapis.com/earthenginepartners-hansen/`). Separate NISAR canopy
loss in forest that was still standing at the last Hansen year from areas already cleared earlier. Files: `ancillary.py`.

**43. GPM IMERG rainfall between the radar dates** · [M] · Validity, Impact
Accumulate `GPM_3IMERGDL` (daily, Late run) over the area between and before both dates (OPeNDAP subset through GES DISC with
Earthdata login). Plot on the timeline as measured satellite precipitation, alongside the modelled Open-Meteo rain already shown.
Files: `conditions.py`, `connector.py`, `src/Dock.tsx`.

**44. SMAP enhanced soil moisture at both dates** · [M] · Validity
`SPL3SMP_E` (NSIDC) daily soil moisture for the cells over the area on each radar date. Wetter soil raises L-band HH, so this is the
first alternative driver to rule in or out for any brightening. Files: `conditions.py`, `connector.py`.

**45. NISAR's own soil moisture product (SME2)** · [M] · Relevance, Validity
Read `NISAR_L3_SME2_PROVISIONAL_V1` for the same dates and area; it is a NISAR-derived product, so it keeps NASA/NISAR data at the
core of the explanation. Shown as a layer and as a value per hotspot, with the product's maturity stated. Files: `ancillary.py`, `catalog.py`.

**46. Sentinel-2 indices on the radar grid** · [M] · Validity, Impact
`optical.py` already reads Sentinel-2 L2A COGs for true colour. Add NDVI (B08, B04), NDWI (B03, B08) and NBR (B08, B12), masked with
the SCL band for cloud, shadow and snow, and resampled to the radar grid. Then scatter ΔNDVI against ΔHV per hotspot, and report the
cloud-free fraction. Files: `optical.py`, `src/charts.tsx`.

**47. FIRMS detections dated inside the pair** · [S] · Validity, Impact
The live feed is a global 24 h file. For an investigation, query the FIRMS area API (`/api/area/csv/{MAP_KEY}/VIIRS_SNPP_SP|NRT/{bbox}/{days}/{date}`)
for the full pair interval and count detections inside each decrease hotspot. Files: `feeds.py`, `agent.py`.

**48. Copernicus GLO-30 DEM for analysis** · [S] · Validity
Replace the Open-Meteo GLO-90 point elevations used by profiles with GLO-30 COG windows. The same grid feeds slope, the layover mask
(feature 7) and HAND (feature 51). Files: `ancillary.py`, `conditions.py`.

**49. Change statistics by land-cover class** · [S] · Impact, Validity
The WorldCover window already loads for the relief map. Reproject it to the radar grid and report, per class, the changed area, the
median Δ dB per polarization, and the share of the class that changed. Today only class shares of the area are shown.
Files: `terrain.py`, `science.py`, `src/Analysis.tsx`.

**50. GIBS NISAR GCOV browse layer for wide views** · [S] · Relevance, Presentation
Add the GIBS layer `NISAR_L2_Geocoded_Polarimetric_Covariance` through the existing `/gibs/` gateway as a zoomed-out context layer. The
globe then shows real NISAR imagery everywhere before an investigation starts. It is labelled browse imagery, never measured.
Files: `gateway.py`, `src/liveLayers.ts`, `src/App.tsx`.

## 5. Hazards and applications by challenge theme (14)

**51. Flood extent with HAND filtering** · [M] · Impact, Validity
Flood = water on the later date (feature 26) minus JRC permanent water (feature 41), keeping only pixels with Height Above Nearest
Drainage below a user-set value, from ASF's GLO-30 HAND (`glo-30-hand.s3.amazonaws.com`). Report km² with the interval from
feature 85. Files: `ancillary.py`, `science.py`.

**52. Flooded-vegetation detector (double bounce)** · [S] · Relevance, Impact
Inside WorldCover tree, mangrove and herbaceous wetland classes: an HH increase at or above the threshold, with HV not increasing as
much, is the L-band signature of water under canopy. This is the Pacaya-Samiria story, and plain water thresholds miss it.
Files: `polarimetry.py`, `terrain.py`.

**53. Deforestation detector** · [M] · Impact, Validity
An HV decrease and RVI drop (feature 14) inside WorldCover tree, applied with the MMU (feature 25), cross-checked against Hansen
(feature 42) and DIST-ALERT (feature 40). The output is a polygon list with area, date bracket and agreement per source.
Files: `polarimetry.py`, `ancillary.py`.

**54. Burn scar mapping** · [M] · Impact, Validity
An HV and RVI decrease within a buffer of FIRMS detections from the pair interval (feature 47), compared against Sentinel-2 dNBR (feature 46)
where cloud-free. L-band sees through smoke, and the report states that. Files: `polarimetry.py`, `optical.py`.

**55. Crop monitoring per field** · [M] · Impact, Relevance
The user draws field polygons (feature 72). For each, show the RVI and HV/HH series over the 12-day stack within WorldCover cropland,
marking RVI rises (canopy development) and sharp HV drops (likely harvest) as stated hypotheses. Files: `timeseries.py`, `src/charts.tsx`.

**56. Soil moisture change on bare soil** · [M] · Validity, Relevance
ΔHH restricted to WorldCover bare or sparse and cropland pixels with low RVI. Fit Δσ0 against ΔSME2 or ΔSMAP (features 44 and 45) across
the area and report the slope and R² only from data in that area, never extrapolated or used outside it.
Files: `polarimetry.py`, `conditions.py`.

**57. Sea-ice and glacier surface drift by amplitude feature tracking** · [L] · Relevance, Creativity
Normalized phase correlation (`skimage.registration.phase_cross_correlation`) on matched tiles of two geocoded HH amplitude images
with a short time separation. At high latitudes overlapping swaths revisit often. Vectors with a correlation-peak quality filter,
in metres per day. This is valid from GCOV because it uses amplitude texture, not phase. Files: new `backend/terra/tracking.py`.

**58. Earthquake response mode** · [M] · Impact, Presentation
When a USGS M6+ event falls in or near the area, select the closest pre- and post-event same-track pair automatically, and show the
GCOV change, GUNW coherence loss (feature 35) and the USGS ShakeMap MMI contours (`cont_mmi.json` from the event's `shakemap` product)
together. Files: `feeds.py`, `agent.py`, `src/Globe.tsx`.

**59. Landslide candidates** · [M] · Impact
Hotspots on slopes above a user-set angle from GLO-30, ranked by area, with IMERG rain in the preceding days (feature 43) and the
layover mask applied first. Output is a candidate list, never confirmed landslides. Files: `science.py`, `ancillary.py`.

**60. Volcano flank monitoring** · [M] · Impact, Relevance
For USGS volcanoes with elevated alert levels (already in the hazards feed), offer a one-click investigation of the edifice: GCOV
change, which shows new lava flows as roughness change, plus GUNW where available. Files: `feeds.py`, `agent.py`, `src/App.tsx`.

**61. Shoreline and riverbank change along transects** · [M] · Impact
Extract the water edge per date from the water map (marching squares, `skimage.measure.find_contours`), cast transects at a
user-set spacing, and report the edge movement per transect in metres (the end-point rate, as in DSAS). This covers coastal erosion and
river migration. Files: new `backend/terra/shoreline.py`, `src/Globe.tsx`.

**62. Urban expansion and construction** · [S] · Impact
A persistent HH increase with low temporal variation afterwards (feature 32), in WorldCover bare, grassland or cropland next to built-up
areas. Reported as "new bright, stable scatterers" with a hypothesis of construction. Files: `timeseries.py`, `polarimetry.py`.

**63. Area watch with new-acquisition alerts** · [M] · Impact
Save an area; the worker polls ASF every few hours. When a new GCOV arrives on the watched track, it runs the comparison with the previous
same-track date and raises a browser notification with the measured summary. This uses the existing Temporal worker and SQLite store.
Files: `workflows.py`, `activities.py`, `store.py`, `src/App.tsx`.

**64. Theme presets for each challenge theme** · [S] · Relevance, Presentation
One-click presets (floods and wetlands, forest loss and fire, crops, soil moisture, ice, earthquakes and subsidence). Each sets the
polarizations, threshold method, ancillary layers, change classes and the limitation text for that theme, and is passed to the planner.
Files: `agent.py` (`PLANNER`), `src/Analysis.tsx`.

## 6. Visualization, 3D and interaction (14)

**65. Tiled science layers with rio-tiler** · [M] · Presentation, Validity
Phase 3 open item. Serve the existing float COGs as XYZ tiles through a titiler/rio-tiler endpoint with server-side rescale and
colormap, so detail holds when zoomed in and the 1600 px preview cap goes away. Files: new `backend/terra/tiles.py`, `app.py`, `src/Globe.tsx`.

**66. User stretch and colormap with histogram handles** · [S] · Presentation
Drag min and max handles on the existing histogram to set the display stretch; choose cividis or viridis for gamma0 and a diverging
map for change. The stretch is shown in the legend and in shared links. Values in the COG never change. Files: `src/charts.tsx`, `src/Panels.tsx`.

**67. Polarization and product switcher** · [S] · Relevance, Presentation
Layer picker entries for HH, HV, HV/HH, RVI, composite, change per polarization, p-value and change class, each with its own legend
and units. Files: `src/App.tsx`, `src/Panels.tsx`.

**68. Change-class legend that filters the map** · [S] · Presentation, Impact
Click a class from feature 21 in the legend to show only those pixels and their hotspots, with the class's hypothesized driver and
area beside it. Files: `src/App.tsx`, `src/Globe.tsx`.

**69. Hotspot outlines instead of points** · [S] · Presentation, Validity
Return hotspot polygons (vectorized label masks, `rasterio.features.shapes`, simplified) and draw them ground-clamped with area labels.
The patch extent is then visible, not just its centre. Files: `science.py`, `src/Globe.tsx`.

**70. Synchronized triptych: NISAR, Sentinel-1, Sentinel-2** · [M] · Creativity, Presentation
Three linked 2D views with one shared camera, each with its own date stamp and sensor label. The L-band versus C-band versus optical
contrast in feature 39 then reads at a glance. Files: `src/Compare.tsx`, `src/Dossier.tsx`.

**71. Draw a polygon area of interest** · [M] · Impact
Freehand or vertex polygon AOI, clipped as a mask in `_read` (`rasterio.features.geometry_mask`), with geodesic area from `pyproj.Geod`.
Real study areas (a park, a field, a basin) are rarely rectangles. Files: `science.py`, `app.py`, `src/Globe.tsx`.

**72. Field and feature polygons for per-object statistics** · [S] · Impact
Draw or paste GeoJSON polygons inside the area; each gets its own row of per-polarization change and its own series (used by
feature 55). Files: `app.py`, `src/Analysis.tsx`.

**73. Histogram brushing** · [M] · Presentation, Validity
Brush a dB range on the before, after or change histogram to highlight exactly those pixels on the map and give their area. Users can then
see where the water-like or changed values actually are. Files: `src/charts.tsx`, `src/Globe.tsx`, `app.py`.

**74. Look-geometry overlay** · [S] · Validity, Presentation
On the map, draw the swath edges of the measured pass and an arrow for the radar look direction (from track heading and look side). This
explains layover on slopes facing the radar and shadow on back slopes. Files: `src/fleet.ts`, `src/Globe.tsx`.

**75. Science layers draped on terrain** · [S] · Presentation
Add the tiled layers (feature 65) as Cesium imagery layers over World Terrain in 3D, and hide the Google photorealistic tileset while
a science layer is on, since draping onto 3D Tiles does not render in Cesium 1.147. Files: `src/Globe.tsx`.

**76. Photorealistic 3D context flight to a hotspot** · [S] · Presentation
"See in 3D" on a hotspot flies to it in Google Photorealistic 3D Tiles, with the hotspot outline as a ground-clamped polyline for
orientation. Visual context only: nothing is measured or derived from the tiles. Attribution stays on screen. Files: `src/Globe.tsx`.

**77. Deep links** · [S] · Presentation, Impact
The URL encodes the area, investigation id, active layer, stretch, timeline date and camera. Opening the link restores the view. Files: `src/App.tsx`.

**78. Accessible figures** · [S] · Presentation
Palettes checked for colour-vision deficiency (cividis, plus a diverging palette safe for colour-blind viewers), keyboard traversal of the hotspot list, and a
text alternative for every chart generated from the same evidence JSON the writer uses. Files: `src/charts.tsx`, `src/Analysis.tsx`.

## 7. Verification, uncertainty and provenance (11)

**79. Agreement with OPERA DSWx** · [M] · Validity
Compare NISAR water (feature 26) with `OPERA_L3_DSWX-S1_V1` and `OPERA_L3_DSWX-HLS_V1` on the nearest dates (PO.DAAC). Report
overall agreement, Cohen's kappa and F1 on the common valid pixels, and say plainly when no DSWx date is close. Files: new `backend/terra/validation.py`, `ancillary.py`.

**80. Null test on a stable area** · [S] · Validity
Run the same pipeline on a pair over a reference area expected to be stable (desert rock, using feature 32's low-variation pixels). The share of
pixels flagged there is the empirical false-alarm rate for the chosen threshold, and it is reported with every result.
Files: `validation.py`, `tests/`.

**81. Threshold sensitivity curve** · [S] · Validity
Recompute the changed area for thresholds from 1 to 6 dB in 0.5 dB steps and plot the curve. A claim is called robust only if its
area is stable across the neighbourhood of the chosen threshold. Files: `science.py`, `src/charts.tsx`.

**82. Mask accounting waterfall** · [S] · Validity, Presentation
Count the pixels removed by each step: outside the polygon, subswath mask, exception mask, layover/shadow, non-positive power, and no
common valid pixel. Draw it as a waterfall so missing area is never silent. Files: `science.py`, `src/Dossier.tsx`.

**83. Per-claim evidence links** · [S] · Validity, Presentation
`verify_numbers` already traces numbers. Return the evidence path for each traced value, and render each number in the answer as a link
that highlights its source row and layer. Files: `agent.py`, `src/Analysis.tsx`.

**84. Rule-based confidence per claim** · [M] · Validity
Rate High, Medium or Low from explicit inputs: product maturity, ENL, coverage, significance (feature 20), stable-area offset (feature 11), and
independent agreement (features 40 and 79). The reasons are listed, the writer must state the rating, and nothing comes from the model's opinion.
Files: `validation.py`, `agent.py`.

**85. Block-bootstrap intervals for areas and medians** · [M] · Validity
Estimate the spatial autocorrelation range from the empirical variogram of Δ dB, then use a block bootstrap with blocks of that size to
give 95% intervals on changed km² and median change. The writer must quote intervals, not bare point values. Files: `validation.py`, `science.py`.

**86. Contradiction flags** · [S] · Validity
Explicit checks: new water with no IMERG rain and no upstream reason; canopy loss without DIST or Hansen agreement; brightening during
strong wind over water. Each shows as a flag on the claim it weakens. Files: `validation.py`, `agent.py`.

**87. Reproduce command** · [S] · Validity, Presentation
`python -m terra.reproduce <investigation>` re-runs from `provenance.json` and compares the SHA-256 of every COG against the recorded
hashes, failing loudly on a mismatch. Exposed as a button in the dossier. Files: new `backend/terra/reproduce.py`, `src/Dossier.tsx`.

**88. STAC Item export** · [S] · Presentation, Impact
Write each result as a STAC Item (`pystac`, processing and projection extensions) that links to the source NISAR granule ids and output
COGs, so other tools can open results directly. Files: `activities.py` (`publish_science`).

**89. Writer adversarial tests** · [S] · Validity
Pytest cases asking for displacement from GCOV, causes, biomass in tonnes and future change. Each must refuse or state the limitation,
and each must still pass `verify_numbers`. Files: `tests/test_agent.py`.

## 8. Performance, infrastructure, open source and presentation (11)

**90. Server-sent progress stream** · [M] · Presentation
Phase 4 open item. Stream workflow stages, bytes read and partial results over SSE instead of polling, so the live demo shows every step
as it happens. Files: `app.py`, `workflows.py`, `src/App.tsx`.

**91. Demo cache for featured sites** · [S] · Presentation
`scripts/` task that pre-acquires the Pacaya-Samiria pair and stack and two more featured sites into `.data`, so the judged demo
works even if Earthdata or ASF is slow. The cache is reused only if the hashes match the provenance. Files: new `scripts/warm_demo.py`.

**92. Guided demo story mode** · [M] · Presentation
Phase 7 item. A scripted, skippable walkthrough: globe, area, before and after, polarization report, hotspots, verified answer, with
captions taken from the actual result. Honours `prefers-reduced-motion`. Files: `src/App.tsx`, `src/Globe.tsx`.

**93. Static judge build** · [M] · Presentation, Impact
Export finished investigations (JSON, COG tiles, PNGs) to a static bundle with the frontend in read-only mode, deployable without
Earthdata credentials or a worker. Files: `scripts/`, `vite.config.ts`, `src/App.tsx`.

**94. Standalone HTML report** · [M] · Presentation
Phase 6 open item. A single HTML file with embedded images, charts as inline SVG, the answer, the hotspot table, provenance and limitations,
openable offline. Files: `src/Dossier.tsx`, `activities.py`.

**95. README, licence and citation** · [S] · Presentation, Impact
There is no README. Add one with the problem, a screenshot, quick start, an architecture diagram, a data-source table with links and
licences, the method and its limits, plus an open-source `LICENSE` and `CITATION.cff`. Files: `README.md`, `LICENSE`, `CITATION.cff`.

**96. Reproducible notebook** · [S] · Validity, Presentation
A Jupyter notebook that calls `terra.science.run_analysis` on the cached Pacaya-Samiria inputs and reproduces the published numbers.
Science readers can audit the method without the UI. Files: new `notebooks/pacaya_samiria.ipynb`.

**97. Docker Compose for judges** · [M] · Presentation
API, worker, Temporal dev server and the built frontend in one `docker compose up`, with `.env.example` listing which keys are optional.
Files: new `docker-compose.yml`, `Dockerfile`.

**98. Playwright end-to-end test of the demo flow** · [M] · Validity
Phase 7 item. Search the site, run the investigation, wait for the answer, open the report and download; screenshots at 390 px and 1440 px.
Files: new `e2e/demo.spec.ts`, `package.json`.

**99. Per-user Earthdata Login** · [M] · Impact
Earthdata OAuth so each user's own token reads NISAR data, instead of one server token. This is required before any public
deployment beyond the judged demo. Files: `acquire.py`, `app.py`, `connector.py`.

**100. Parallel, cancellable stack reads with a byte budget** · [S] · Presentation, Impact
For features 1 and 27, a per-investigation budget of bytes and seconds, enforced in `RangeFile`, with partial stacks reported honestly
("6 of 8 dates read") and cancellation honoured mid-read. Files: `connector.py`, `acquire.py`.

---

## Build next (fits the hackathon window)

The ten items with the highest value per hour, in order. Together they turn "an HH change map" into
"which polarization shows which change, how sure we are, and what likely caused it", which is the challenge brief almost word for word.

1. **#1 Read every polarization** [S]. Everything polarimetric depends on it; it is a short change to `acquire.py` plus a loop in
   `run_analysis`. Without it, the challenge's polarization question cannot be answered.
2. **#2 ENL plus #22 "which polarization shows it"** [S + S]. Answers the challenge's polarization question directly, with numbers scaled by
   speckle. This is what judges look for under Relevance.
3. **#13 cross-pol ratio and #15 dual-pol composite** [S + S]. Cheap, physically meaningful layers. The composite is the single best
   presentation image for the canopy and water story.
4. **#21 Four-quadrant change classes with hypothesized drivers** [S]. Directly meets "hypothesize physical drivers" and keeps it
   honest by labelling hypotheses as hypotheses.
5. **#52 Flooded-vegetation detector** [S]. The Pacaya-Samiria demo is a flooded forest. Showing double-bounce brightening that water
   thresholds miss is the strongest L-band specific result.
6. **#18 Kittler-Illingworth threshold** [S]. Replaces an arbitrary 3 dB with a published, scene-adaptive method. A large Validity gain
   for about an hour of work.
7. **#41 JRC surface water context** [S]. One COG window read through the existing pattern in `terrain.py`; it turns "new water" into
   "water where there has never been any", which supports the flood claim.
8. **#81 Threshold sensitivity curve plus #80 null test** [S + S]. Two cheap checks that show the result is not a threshold artefact. Few
   hackathon entries show their false-alarm rate.
9. **#50 GIBS NISAR browse layer** [S]. Real NISAR imagery on the globe from the first second, through the existing gateway. A strong
   Relevance and Presentation gain for minutes of work.
10. **#95 README, licence and citation** [S]. Judges and reviewers open the repository; currently there is no README. Open-source is a
    stated requirement.

Next tier, if time remains: #20 (pairwise test with FDR), #26 (split-based Otsu water), #49 (change per land-cover class), #92 (story mode).

## Ruled out

- **Building heights, volumes or any measurement from Google Photorealistic 3D Tiles.** Google's Map Tiles policies forbid analysis or
  derivation from the tiles. They stay visual context only (feature 76).
- **InSAR displacement or coherence from GCOV.** GCOV is a geocoded covariance (power) product with no interferometric phase.
  Displacement needs GUNW (feature 34) or OPERA DISP-S1 (feature 37), and both are labelled as separate products.
- **H/alpha, Freeman-Durden or Pauli RGB from diagonal-only GCOV.** These need the off-diagonal phase terms. Features 16 and 17 run only
  when those terms are delivered, and are disabled with a reason otherwise.
- **Biomass in tonnes per hectare from HV with a generic published equation.** L-band HV saturates and the equations need site
  calibration. CMR lists no NISAR biomass product today (only `NISAR_L3_SME2` among L3), so any number would be invented.
- **Direct change between different tracks, directions or frames.** Geometry differences produce false change. This is refused today,
  and allowed only as a labelled normalized comparison (feature 9).
- **Wet-snow mapping with C-band ratio thresholds (Nagler and Rott) applied to L-band.** Those thresholds are for C-band, and L-band is far
  less sensitive to liquid water in snow.
- **Training a deep-learning flood or deforestation model during the event.** There are no labelled NISAR training sets and no time to
  validate one; an unvalidated model would undercut the Validity score.
- **Forecasting future surface change.** No validated forecasting model exists, as Phase 6 already states. Feature 38 forecasts
  *acquisitions*, not change.
- **Measuring from ASF browse PNGs or the GIBS NISAR layer.** They are publisher renderings; `catalog.py` already marks browse images
  unsuitable for pixel measurement.
- **Treating pixel counts as independent samples for p-values.** Speckle and multilooking correlate neighbouring pixels. Features 20
  and 85 account for this instead of ignoring it.
