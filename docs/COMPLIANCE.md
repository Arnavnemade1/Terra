# Space Apps 2026 compliance: "Dancing with the SARs"

Checked on 2026-11-14 against spaceappschallenge.org: the challenge page, the Participant Terms and Conditions
(/legal/, updated 7 Aug 2026), the Project Submission Guide, the Judging and Awards Guide, the Team Formation Guide and
the Participant FAQs. Quotes are from those pages.

Challenge: https://www.spaceappschallenge.org/2026/challenges/dancing-with-the-sars/
"Build an interactive application that uses radar remote sensing data from the NASA-ISRO Synthetic Aperture Radar
(NISAR) mission to track and visualize one or more types of surface change at locations around the world."

## Status

| Rule | Where it stands |
| --- | --- |
| NISAR data is the core, not an add-on (Relevance) | Met. Every investigation reads original NISAR GCOV over ASF/Earthdata. |
| At least one NASA data source, for Global Awards | Met: NISAR (ASF), FIRMS, EONET, GIBS, CMR. |
| Team of 6 or fewer, all confirmed at the same event | Check on the team page. |
| **Work starts no earlier than 14 Nov 2026, 9:00 local** | **Risk.** `docs/PHASES.md` describes work done on 13 Nov ("Status as of 2026-11-13", Phase 4b dated 2026-11-13), and the repo's first commit is dated 12 Nov 22:28. The team should decide how to handle this with their Local Lead before submitting. Do not rewrite dates. |
| Original work; every external resource cited in References | To do: a References list in the README and on the submission (libraries, APIs, datasets, fonts, each with its licence). |
| Third-party material "freely and publicly available without restriction" or open source | **Judgement call.** Google Map Tiles, the language-model API and Esri basemaps are key-gated or proprietary. Terra works without each of them (each is optional and degrades cleanly), cite each with its terms, and ask the Local Lead if in doubt. |
| Public links with "no logins, permissions, or registrations" | The demo and project links must open without accounts. Judges must never need their own keys. |
| Demo: video 30 s or less, or up to 7 slides; English | To do. |
| Super Intelligence (AI) disclosure | Required: say where SI was used in the form, in the README and in code metadata; SI-generated images and video carry a visible watermark. Terra's written answers are SI-generated; each is labelled "written by <model>" under the answer (`src/Analysis.tsx`). "SI should not be used to come up with your idea." |
| No NASA logo, flags or mission insignia, especially in SI-generated content | Met in the app (no NASA marks). Keep it that way in slides and video. |
| No minors' likeness, personal information or promotional content | Keep the demo free of them. |
| Licence | Choose an OSI licence (otherwise NASA applies Apache-2.0). |

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
