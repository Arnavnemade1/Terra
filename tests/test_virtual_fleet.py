"""The hosted simulator must take off promptly and recover after redeployment."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('terra_test_bridge', Path(__file__).resolve().parents[1] / 'scripts' / 'drone_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def aircraft(slot=4):
    return bridge.Virtual('D-05', 'Wren', 'quad', {'cruise_mps': 8, 'max_mps': 14, 'endurance_min': 35}, (15.0, 37.7), slot)


def test_the_last_simulated_aircraft_takes_off_within_ten_seconds(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: clock[0])
    drone = aircraft()
    drone.task = {'id': 'mission', 'kind': 'hold', 'params': {'lon': 15.0, 'lat': 37.7, 'agl_m': 90}}
    telemetry, _ = drone.step(0.5, None)
    assert telemetry['landed'] is True
    clock[0] += 9
    telemetry, _ = drone.step(0.5, None)
    assert telemetry['landed'] is False and telemetry['agl_m'] > 0
    assert telemetry['mode'] == 'AUTO'


def test_redeployment_moves_the_simulator_to_its_new_pad_with_a_fresh_battery():
    drone = aircraft()
    drone.battery, drone.agl, drone.landed = 0, 100, False
    drone.relocate((-155.25, 19.4))
    assert drone.landed and drone.agl == 0 and drone.battery == 100
    assert bridge.distance(drone.lon, drone.lat, -155.25, 19.4) == pytest.approx(25, rel=0.02)
