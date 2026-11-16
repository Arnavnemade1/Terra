# Data and API terms

## Google Map Tiles API (Photorealistic 3D Tiles)

Policies: https://developers.google.com/maps/documentation/tile/policies

- **Display only.** Nothing is measured, traced or derived from the tiles. While Google tiles are on screen the pointer
  readout (height, slope, land cover) is switched off; all science uses NISAR, Sentinel-2 and Terra's own terrain.
- **No caching beyond the headers.** Tiles arrive `Cache-Control: private`, so the server keeps no copy
  (`backend/terra/google_tiles.py`); the browser caches within `max-age` and revalidates with ETags.
- **Attribution.** The Google Maps logo (white, for busy backgrounds) at its native 18 px, full opacity, plus every data
  provider from the tiles, in a strip below the map controls that nothing overlaps. Covered by the browser checks.
- **Hybrid scenes.** Terra's own overlays (area brackets, hotspots, hazards) sit on top of the tiles; the attribution
  line names Google separately from Esri and Copernicus. Non-Google basemaps are hidden while Google tiles render.
- **Key handling.** Server-side only (`GOOGLE_MAPS_TILES_KEY`), sent as `X-Goog-Api-Key`, never in a URL, a log,
  a response or the bundle (tests in `tests/test_google_tiles.py`; `dist/` grep after build).
