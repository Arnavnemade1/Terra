"""Optical evidence gates: input bounds, source allowlisting, dated selection."""
from datetime import datetime, timezone
from terra.optical import _bbox_from_result, _candidates, _cog_url
import terra.optical as optical


def test_optical_requires_bounded_real_aoi():
    def result(bbox):
        return {"provenance": {"parameters": {"bbox": bbox}}}
    assert _bbox_from_result(result([-74.8, -5.55, -74.5, -5.25])) == [-74.8, -5.55, -74.5, -5.25]
    assert _bbox_from_result(result([-75, -6, -72, -4])) is None
    assert _bbox_from_result(result([-74.8, -5.25, -74.5, -5.55])) is None


def test_only_public_sentinel_cogs_are_read():
    item = {"assets": {"visual": {"href": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/18/M/WU/2026/6/X/TCI.tif"}}}
    assert _cog_url(item, "visual") is not None
    item["assets"]["visual"]["href"] = "https://example.com/TCI.tif"
    assert _cog_url(item, "visual") is None


def test_date_and_cloud_rank_choose_matching_low_cloud_scene():
    def scene(day, cloud):
        return {"id": day, "properties": {"datetime": f"2026-06-{day}T15:00:00Z", "eo:cloud_cover": cloud},
                "assets": {name: {"href": f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/x/{name}.tif"}
                           for name in ("visual", "scl")}}
    target = datetime(2026, 6, 12, tzinfo=timezone.utc)
    ranked = _candidates([scene("12", 90), scene("15", 1)], target)
    assert ranked[0][0]["id"] == "15"


async def test_collect_does_not_stop_after_first_image(monkeypatch, tmp_path):
    async def search(*_args):
        return []
    def candidates(_items, target, count=6):
        return [[{"id": "scene", "properties": {"datetime": target.isoformat().replace("+00:00", "Z")}}]]
    def render(_items, _bbox):
        return b"PNG", {"obscured_pct": 0.5, "coverage_pct": 100, "items": []}
    monkeypatch.setattr(optical, "_search", search)
    monkeypatch.setattr(optical, "_candidates", candidates)
    monkeypatch.setattr(optical, "_render", render)
    result = {"provenance": {"parameters": {"bbox": [-74.8, -5.55, -74.5, -5.25]},
             "inputs": [{"metadata": {"date": "2026-06-12T00:00:00Z"}},
                        {"metadata": {"date": "2026-10-10T00:00:00Z"}}]}}
    entries = await optical.collect_optical_evidence(result, tmp_path)
    assert [entry["kind"] for entry in entries] == ["before", "after", "recent"]
