"""Terrain context: WorldCover tile naming, Wikidata parsing and de-duplication, cache isolation. No network access."""
import base64
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from terra import terrain


def test_worldcover_tiles_cover_the_area_including_tile_edges():
    names = [n for n, _ in terrain._tiles([14.85, 37.6, 15.15, 37.85])]
    assert names == ["N36E012", "N36E015"]
    assert [n for n, _ in terrain._tiles([-0.2, -0.1, 0.1, 0.2])] == ["S03W003", "S03E000", "N00W003", "N00E000"]


def test_grid_shape_follows_ground_aspect():
    h, w = terrain._shape([14.85, 37.6, 15.15, 37.85])
    assert max(h, w) == terrain.SIZE and abs(w / h - (0.3 * np.cos(np.radians(37.725))) / 0.25) < 0.02


def _row(qid, label, lon, lat, kind=None, elev=None, pop=None):
    row = {"item": {"value": f"http://www.wikidata.org/entity/{qid}"}, "itemLabel": {"value": label}, "coord": {"value": f"Point({lon} {lat})"}}
    if kind:
        row["kind"] = {"value": f"http://www.wikidata.org/entity/{kind}"}
    if elev is not None:
        row["elev"] = {"value": str(elev)}
    if pop is not None:
        row["pop"] = {"value": str(pop)}
    return row


class _Response:
    def __init__(self, rows):
        self.rows = rows

    def json(self):
        return {"results": {"bindings": self.rows}}


@pytest.mark.asyncio
async def test_named_features_keep_one_label_per_summit_and_rank_by_height(monkeypatch):
    rows = [
        _row("Q1", "Mount Etna", 14.995, 37.751, "Q169358", 3357), _row("Q1", "Mount Etna", 14.995, 37.751, "Q8502", 3329),
        _row("Q2", "Monte Silvestri", 15.0051, 37.698, "Q8502", 1986), _row("Q3", "Silvestri-Berge", 15.006, 37.699, "Q8502"),
        _row("Q4", "Q12345", 15.0, 37.7, "Q8502", 900),  # unlabelled item
        _row("Q5", "Nicolosi", 14.95, 37.61, pop=7594), _row("Q6", "Gurrida Lake", 14.9, 37.84, "Q23397", 835),
        _row("Q7", "Far Away Peak", 20.0, 40.0, "Q8502", 2000),  # outside the box
    ]

    async def fake_request(provider, method, url, **kwargs):
        assert provider == "wikidata" and "wikibase:box" in kwargs["params"]["query"]
        return _Response(rows)

    monkeypatch.setattr(terrain, "request", fake_request)
    out = await terrain.named_features([14.85, 37.6, 15.15, 37.85])
    names = [f["name"] for f in out]
    assert names[0] == "Mount Etna" and out[0]["kind"] == "volcano" and out[0]["elevation_m"] == 3357
    assert "Silvestri-Berge" not in names and "Monte Silvestri" in names  # same summit within 700 m
    assert "Q12345" not in names and "Far Away Peak" not in names
    assert {"Nicolosi", "Gurrida Lake"} <= set(names)


def test_landcover_shares_use_the_inner_area(monkeypatch):
    def fake_read(url, bbox, grid):
        grid[:, : grid.shape[1] // 2] = 10  # west half forest
        grid[:, grid.shape[1] // 2:] = 60  # east half bare
        return True

    monkeypatch.setattr(terrain, "_read_tile", fake_read)
    lc = terrain.landcover([0.0, 0.0, 1.0, 1.0], inner=[0.0, 0.0, 0.25, 1.0])
    assert lc["shares"] == [{"code": 10, "name": "Tree cover", "pct": 100.0}]
    grid = np.array(Image.open(BytesIO(base64.b64decode(lc["grid_png"]))))
    assert grid.shape == (lc["height"], lc["width"]) and set(np.unique(grid)) == {10, 60}
