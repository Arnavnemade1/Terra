"""Hazard feeds: GDACS parsing and cross-source de-duplication, without network access."""
import asyncio

import pytest

from terra import feeds


class _Response:
    def __init__(self, data):
        self._data, self.content = data, b"x"

    def json(self):
        return self._data


def _gdacs(code, eventid, lon, lat, alert, current=True, todate="2026-11-12T00:00:00"):
    return {"geometry": {"coordinates": [lon, lat]}, "properties": {
        "eventtype": code, "eventid": eventid, "name": f"Event {eventid}  in ,  Place", "alertlevel": alert,
        "iscurrent": "true" if current else "false", "fromdate": "2026-11-01T00:00:00", "todate": todate,
        "severitydata": {"severitytext": " Medium impact "}, "country": "Somewhere", "url": {"report": f"https://www.gdacs.org/report.aspx?eventid={eventid}"}}}


@pytest.mark.asyncio
async def test_gdacs_parses_alerts_and_drops_old_closed_events(monkeypatch):
    feeds._cache.pop("gdacs", None)
    pages = {"TC": [_gdacs("TC", 1, 120.0, 15.0, "Red")], "FL": [_gdacs("FL", 2, 105.0, 16.0, "Green"), _gdacs("FL", 3, 0, 0, "Green", False, "2025-01-01T00:00:00")],
             "VO": [], "DR": [], "EQ": []}

    async def fake_get(url, params=None, timeout=30):
        return _Response({"features": pages[params["eventlist"]]})
    monkeypatch.setattr(feeds, "_get", fake_get)
    result = await feeds.gdacs()
    items = {i["id"]: i for i in result["items"]}
    assert set(items) == {"gdacs-TC-1", "gdacs-FL-2"}
    assert items["gdacs-TC-1"]["category"] == "severeStorms" and items["gdacs-TC-1"]["alert"] == "red"
    assert items["gdacs-TC-1"]["title"] == "Event 1 in, Place" and items["gdacs-TC-1"]["severity"] == "Medium impact"
    feeds._cache.pop("gdacs", None)


def test_merge_prefers_alerting_source_and_keeps_links():
    usgs = [{"id": "v1", "category": "volcanoes", "lat": 19.42, "lon": -155.29, "alert": "yellow", "current": True, "date": "2026-11-13", "source": "https://usgs/v1", "provider": "USGS"}]
    eonet = [{"id": "e1", "category": "volcanoes", "lat": 19.40, "lon": -155.28, "alert": None, "current": True, "date": "2026-11-10", "source": "https://eonet/e1", "provider": "NASA EONET"},
             {"id": "e2", "category": "wildfires", "lat": 19.40, "lon": -155.28, "alert": None, "current": True, "date": "2026-11-11", "source": "https://eonet/e2", "provider": "NASA EONET"}]
    merged = feeds._merge_hazards([usgs, eonet])
    assert [m["id"] for m in merged] == ["v1", "e2"]  # same volcano merged; a fire nearby stays separate
    assert merged[0]["also"] == [{"provider": "NASA EONET", "url": "https://eonet/e1"}]
