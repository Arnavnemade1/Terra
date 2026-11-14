"""Drone fleet coordination: one central database, a ground-station protocol, and durable agents.

    ground stations (real drones over MAVLink, or the virtual fleet)  --HTTPS + bearer token-->  Terra API
    Terra API  --writes/reads-->  central fleet database (SQLite in WAL mode, shared with the worker)
    durable agents (Temporal): one coordinator + one agent per drone  --activities-->  the same database
    the workspace (browser)  --same-origin API-->  the same database

Everything a drone reports, every task, every airspace reservation and every message between agents is a
row in that database, so the system can be audited, replayed and resumed after any process restarts.
"""
