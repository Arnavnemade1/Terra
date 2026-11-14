"""Fetch a bounded, native-resolution crop of an explicitly unverified derivative.

Run: .venv/bin/python scripts/fetch_reference.py
No Earthdata account is used. This is NOT a validated original mission product.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / ".data" / "inputs"
REVISION = "1dad471a86e0aa68c15bcc12b8d55c61cf83cc26"
DATASET = "https://huggingface.co/datasets/nicksteiner/sardine-demo-data"
SOURCE_GRANULE = "NISAR_L2_PR_GCOV_015_147_A_175_2005_DHDH_A_20260320T104408_20260320T104443_X05013_N_F_J_001"
AOI = [-74.80, -5.55, -74.50, -5.25]
SOURCE_CHECKSUMS = {
    "hh": "41fb48376ad0906de71ce0a0584924a4b032cad16c259c5ca97659eab9aaff9e",
    "hv": "9469f1c7010f59764becaf2f14ea37be2216fa4d2169e6a538ec634bef59c827",
}


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=40) as client:
        response = client.get("https://api.daac.asf.alaska.edu/services/search/param", params={
            "dataset": "NISAR", "processingLevel": "GCOV", "output": "geojson",
            "start": "2026-03-20T00:00:00Z", "end": "2026-03-21T00:00:00Z",
            "relativeOrbit": "147", "frame": "175", "maxResults": 10,
        })
        response.raise_for_status()
        catalog = response.json()
        (DESTINATION / "pacaya-official-catalog.json").write_text(json.dumps(catalog, indent=2))

    for pol in ("hh", "hv"):
        url = f"{DATASET}/resolve/{REVISION}/pacaya_full_{pol}.tif"
        target = DESTINATION / f"pacaya-samiria-{pol}.tif"
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                          CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                          GDAL_HTTP_TIMEOUT="60", GDAL_HTTP_MAX_RETRY="2"):
            with rasterio.open(url) as source:
                projected = transform_bounds("EPSG:4326", source.crs, *AOI, densify_pts=21)
                window = from_bounds(*projected, source.transform).round_offsets().round_lengths()
                window = window.intersection(Window(0, 0, source.width, source.height))
                if window.width > 2048 or window.height > 2048:
                    raise ValueError("Reference crop exceeds the native-pixel resource budget.")
                pixels = source.read(1, window=window)
                profile = source.profile.copy()
                profile.update(driver="GTiff", height=pixels.shape[0], width=pixels.shape[1],
                               transform=source.window_transform(window), count=1,
                               compress="deflate", tiled=True, blockxsize=256, blockysize=256)
                with rasterio.open(target, "w", **profile) as cropped:
                    cropped.write(pixels, 1)
                    cropped.update_tags(**source.tags(), TERRA_DERIVATIVE="Third-party beta GCOV crop")
                geographic = transform_bounds(source.crs, "EPSG:4326",
                                               *rasterio.windows.bounds(window, source.transform),
                                               densify_pts=21)
                tags = source.tags()
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        provenance = {
            "source_url": url, "source_dataset": DATASET,
            "source_revision": REVISION,
            "source_granule": SOURCE_GRANULE,
            "source_published_sha256": SOURCE_CHECKSUMS[pol],
            "source_checksum_verified": False,
            "local_sha256": digest,
            "maturity": "BETA", "date": "2026-03-20T10:44:08Z",
            "product": "GCOV", "polarization": pol.upper() * 2,
            "frequency": "A", "track": 147, "frame": 175,
            "direction": "ASCENDING", "product_version": "X05013",
            "mask_applied": True, "mask_source": "Derivative publisher reports subswath masking",
            "radiometry": "gamma0_power", "derivative": True,
            "source_notes": "Third-party beta derivative; not independently validated against ASF original. "
                            "Official catalog confirms the acquisition with a newer P05023 processing version. "
                            "Not suitable for validated environmental conclusions or temporal change claims.",
            "bbox": list(geographic), "pixel_spacing_m": 20,
            "native_tags": tags,
            "crop": {"requested_bbox": AOI, "resampled": False,
                     "width": int(pixels.shape[1]), "height": int(pixels.shape[0])},
        }
        Path(str(target) + ".provenance.json").write_text(json.dumps(provenance, indent=2))
        valid = np.isfinite(pixels) & (pixels > 0)
        print(json.dumps({"file": str(target.relative_to(ROOT)), "sha256": digest,
                          "shape": list(pixels.shape), "valid_fraction": float(valid.mean()),
                          "bbox": list(geographic), "maturity": "BETA", "derivative": True}))


if __name__ == "__main__":
    main()
