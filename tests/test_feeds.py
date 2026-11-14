from terra.feeds import _event_point, _parse_tle, _fleet_role

NISAR_TLE = """NISAR
1 65053U 25163A   26316.95112981  .00000351  00000+0  11912-3 0  9995
2 65053  98.4061 142.4721 0001256  90.8943 269.2391 14.42501511 67837
"""


def test_parse_tle_reads_name_and_catalog_number():
    sats = _parse_tle(NISAR_TLE + "garbage line\n")
    assert len(sats) == 1
    assert sats[0]["name"] == "NISAR"
    assert sats[0]["norad"] == 65053
    assert sats[0]["line1"].startswith("1 65053U")
    assert sats[0]["role"] == "Radar imaging"
    assert sats[0]["element_epoch"].startswith("2026-")


def test_parse_tle_skips_malformed_triplets():
    assert _parse_tle("NAME\nnot a line\nalso not\n") == []


def test_event_point_prefers_latest_point_geometry():
    geometry = [
        {"type": "Point", "coordinates": [10.0, 20.0], "date": "2026-11-01T00:00:00Z"},
        {"type": "Point", "coordinates": [11.0, 21.0], "date": "2026-11-02T00:00:00Z"},
    ]
    assert _event_point(geometry) == (11.0, 21.0, "2026-11-02T00:00:00Z")


def test_event_point_uses_polygon_centroid():
    ring = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    lon, lat, _ = _event_point([{"type": "Polygon", "coordinates": [ring]}])
    assert (lon, lat) == (1.0, 1.0)


def test_event_point_handles_missing_geometry():
    assert _event_point([]) is None


def test_terrasar_is_not_confused_with_nasa_terra():
    assert _fleet_role("TERRASAR-X") == "Radar imaging"
    assert _fleet_role("TERRA") == "Multispectral Earth observations"
    assert _fleet_role("SENTINEL 1A") == "Radar imaging"


async def test_mirror_does_not_accept_a_different_norad_object(monkeypatch):
    import httpx
    from terra import feeds
    async def get(*args, **kwargs):
        lines = NISAR_TLE.strip().splitlines()
        return httpx.Response(200, json={"name": lines[0], "line1": lines[1], "line2": lines[2]})
    monkeypatch.setattr(feeds, "_get", get)
    assert await feeds._mirrored_satellite(39634) is None
    assert (await feeds._mirrored_satellite(65053))["norad"] == 65053
