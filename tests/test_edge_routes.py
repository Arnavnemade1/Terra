"""vercel.json serves keyless public tiles from the edge; it must mirror the server's own tile routes.

On Vercel, `/api/connector/maps|gibs|terrain` are edge rewrites straight to the fixed public hosts the server
gateway uses (no serverless function, no backend hop: measured on 2026-11-15, the slowest tile in the first minute
went from 57 s to 6 s). Keyed sources (Google 3D tiles) and everything else still go through api/proxy.js.
"""
import json
from pathlib import Path

from terra import gateway

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())
REWRITES = CONFIG["rewrites"]
EDGE = [r for r in REWRITES if r["destination"].startswith("https://")]


def test_every_map_layer_has_an_edge_route_to_the_same_upstream():
    for layer, service in gateway.SOURCES.items():
        route = next(r for r in EDGE if r["source"].startswith(f"/api/connector/maps/{layer}/"))
        assert route["destination"] == f"{gateway.ESRI}/{service}/MapServer/tile/:z/:y/:x"


def test_edge_routes_only_reach_the_gateways_public_hosts():
    allowed = (gateway.ESRI + "/", gateway.GIBS + "/", gateway.TERRAIN)
    assert EDGE and all(r["destination"].startswith(allowed) for r in EDGE)
    # Only numeric tile coordinates and the two GIBS layers the gateway serves.
    assert all(":z(\\d+)/:y(\\d+)/:x(\\d+)" in r["source"] for r in EDGE if "/maps/" in r["source"])
    gibs = [r for r in EDGE if "/gibs/" in r["source"]]
    assert gibs and all("BlueMarble_ShadedRelief_Bathymetry|VIIRS_Black_Marble" in r["source"] for r in gibs)


def test_edge_routes_come_before_the_function_catch_all():
    first_proxy = next(i for i, r in enumerate(REWRITES) if r["destination"] == "/api/proxy")
    assert all(REWRITES.index(r) < first_proxy for r in EDGE)


def test_keyed_sources_never_go_to_the_edge():
    assert not any("google" in r["source"] or "googleapis" in r["destination"] for r in EDGE)
