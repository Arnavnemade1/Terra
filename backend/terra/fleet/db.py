"""The central fleet database: the single relay point between ground stations, agents and the workspace.

SQLite in WAL mode, in the same file as investigations, so the API process (telemetry, tasks from people)
and the worker process (durable agents) share it safely. Every write is a short transaction.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_area (
    id TEXT PRIMARY KEY, name TEXT, bbox TEXT NOT NULL, launch TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_targets (
    id TEXT PRIMARY KEY, area_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, lon REAL NOT NULL, lat REAL NOT NULL,
    radius_m REAL NOT NULL, priority REAL NOT NULL, needs TEXT, source TEXT, last_observed REAL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_drones (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, aircraft TEXT NOT NULL, capabilities TEXT NOT NULL,
    registered_at REAL NOT NULL, last_seen REAL, last_seq INTEGER DEFAULT -1, state TEXT
);
CREATE TABLE IF NOT EXISTS fleet_telemetry (
    drone_id TEXT NOT NULL, t REAL NOT NULL, lon REAL, lat REAL, alt_m REAL, agl_m REAL, heading REAL, speed REAL,
    battery REAL, link REAL, gimbal_heading REAL, gimbal_pitch REAL
);
CREATE INDEX IF NOT EXISTS fleet_telemetry_drone_t ON fleet_telemetry (drone_id, t);
CREATE TABLE IF NOT EXISTS fleet_jobs (
    key TEXT PRIMARY KEY, area_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, params TEXT NOT NULL,
    priority REAL NOT NULL, needs TEXT, assigned_drone TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_tasks (
    id TEXT PRIMARY KEY, drone_id TEXT NOT NULL, job_key TEXT, kind TEXT NOT NULL, label TEXT NOT NULL, params TEXT NOT NULL,
    status TEXT NOT NULL, created_by TEXT NOT NULL, reason TEXT, priority REAL NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, acked_at REAL, hold_until REAL
);
CREATE INDEX IF NOT EXISTS fleet_tasks_drone ON fleet_tasks (drone_id, status);
CREATE TABLE IF NOT EXISTS fleet_operations (
    id TEXT PRIMARY KEY, drone_id TEXT NOT NULL, task_id TEXT NOT NULL, geometry TEXT NOT NULL,
    alt_lower_m REAL NOT NULL, alt_upper_m REAL NOT NULL, t_start REAL NOT NULL, t_end REAL NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_bids (
    drone_id TEXT NOT NULL, job_key TEXT NOT NULL, cost REAL NOT NULL, feasible INTEGER NOT NULL, detail TEXT, t REAL NOT NULL,
    PRIMARY KEY (drone_id, job_key)
);
CREATE TABLE IF NOT EXISTS fleet_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_agents (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, workflow_id TEXT, status TEXT NOT NULL, heartbeat REAL NOT NULL, summary TEXT
);
CREATE TABLE IF NOT EXISTS fleet_control (
    drone_id TEXT PRIMARY KEY, setpoint TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, drone_id TEXT NOT NULL, task_id TEXT, kind TEXT NOT NULL,
    lon REAL NOT NULL, lat REAL NOT NULL, text TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fleet_notes_drone ON fleet_notes (drone_id, t);
CREATE TABLE IF NOT EXISTS fleet_credentials (
    token_hash TEXT PRIMARY KEY, drone_id TEXT NOT NULL, label TEXT, created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
"""
MIGRATIONS = ["ALTER TABLE fleet_area ADD COLUMN investigation_id TEXT", "ALTER TABLE fleet_area ADD COLUMN profile TEXT DEFAULT 'change'"]
ACTIVE = ("assigned", "acknowledged", "active")
TELEMETRY_KEEP_S = 20 * 60
MESSAGE_KEEP = 5000

_pruned = {"at": 0.0}
_ready: set[str] = set()
_ready_lock = threading.Lock()


def now() -> float:
    return time.time()


def _loads(value: str | None, default: Any = None) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


class FleetDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        key = str(self.path.resolve())
        with _ready_lock:
            if key not in _ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.connect() as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript(SCHEMA)
                    for statement in MIGRATIONS:  # columns added after the first release of this schema
                        try:
                            db.execute(statement)
                        except sqlite3.OperationalError:
                            pass
                _ready.add(key)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    # ------------------------------------------------------------------ area and targets
    def set_area(self, name: str, bbox: list[float], targets: list[dict], investigation_id: str | None = None, profile: str = "change") -> dict:
        launch = [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]  # launch from the centre: shortest reach to every target
        t = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT bbox, profile FROM fleet_area WHERE id='active'").fetchone()
            db.execute("INSERT INTO fleet_area (id, name, bbox, launch, updated_at, investigation_id, profile) VALUES ('active', ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name, bbox=excluded.bbox, launch=excluded.launch, updated_at=excluded.updated_at, investigation_id=excluded.investigation_id, profile=excluded.profile",
                       (name, json.dumps(bbox), json.dumps(launch), t, investigation_id, profile))
            changed_profile = previous is not None and (previous["profile"] or "change") != profile
            db.execute("DELETE FROM fleet_targets WHERE area_id='active' AND source != 'operator'")
            for item in targets:
                db.execute("INSERT INTO fleet_targets VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) ON CONFLICT(id) DO UPDATE SET label=excluded.label, lon=excluded.lon, lat=excluded.lat, radius_m=excluded.radius_m, priority=excluded.priority, needs=excluded.needs, updated_at=excluded.updated_at",
                           (item["id"], item["kind"], item["label"], item["lon"], item["lat"], item["radius_m"], item["priority"], json.dumps(item.get("needs", [])), item.get("source", "investigation"), t))
            moved = not previous or json.loads(previous["bbox"]) != bbox or changed_profile
            if moved:
                # A new area: every automatic task and reservation is void; people's tasks stay until they end.
                db.execute("UPDATE fleet_tasks SET status='superseded', updated_at=?, reason='The monitoring area changed.' WHERE status IN ('assigned','acknowledged','active') AND created_by != 'operator'", (t,))
                db.execute("UPDATE fleet_operations SET state='ended', updated_at=? WHERE state != 'ended'", (t,))
                db.execute("DELETE FROM fleet_jobs")
            db.execute("COMMIT")
        self.message("operator", "all", "area", {"text": f"Monitoring area set: {name} with {len(targets)} targets.", "bbox": bbox})
        return self.area()

    def area(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM fleet_area WHERE id='active'").fetchone()
        return {"name": row["name"], "bbox": json.loads(row["bbox"]), "launch": json.loads(row["launch"]), "updated_at": row["updated_at"],
                "investigation_id": row["investigation_id"], "profile": row["profile"] or "change"} if row else None

    def targets(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_targets WHERE area_id='active' ORDER BY priority DESC").fetchall()
        return [{**dict(r), "needs": _loads(r["needs"], [])} for r in rows]

    def mark_observed(self, target_id: str, t: float) -> None:
        with self.connect() as db:
            db.execute("UPDATE fleet_targets SET last_observed=? WHERE id=?", (t, target_id))

    # ------------------------------------------------------------------ drones and telemetry
    def register(self, drone: dict) -> None:
        t = now()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM fleet_drones WHERE id=?", (drone["id"],)).fetchone()
            # Registering starts a new session: a restarted ground station counts its messages from 1 again.
            db.execute("INSERT INTO fleet_drones (id, name, origin, aircraft, capabilities, registered_at) VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name, origin=excluded.origin, aircraft=excluded.aircraft, capabilities=excluded.capabilities, last_seq=-1, registered_at=excluded.registered_at",
                       (drone["id"], drone["name"], drone["origin"], drone["aircraft"], json.dumps(drone["capabilities"]), t))
        self.message(f"drone:{drone['id']}", "all", "joined" if not existing else "rejoined",
                     {"text": f"{drone['name']} {'joined' if not existing else 'reconnected to'} the fleet ({drone['origin']}, {drone['aircraft']}, sensors: {', '.join(drone['capabilities'].get('sensors', []))})."})

    def record(self, telemetry: dict) -> bool:
        """Store one telemetry message. Out-of-order and duplicate messages (by sequence number) are ignored."""
        t = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT last_seq FROM fleet_drones WHERE id=?", (telemetry["id"],)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(telemetry["id"])
            if telemetry["seq"] <= row["last_seq"]:
                db.execute("ROLLBACK")
                return False
            state = {k: v for k, v in telemetry.items() if k not in ("id",)}
            db.execute("UPDATE fleet_drones SET last_seen=?, last_seq=?, state=? WHERE id=?", (t, telemetry["seq"], json.dumps(state), telemetry["id"]))
            g = telemetry.get("gimbal") or {}
            db.execute("INSERT INTO fleet_telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (telemetry["id"], t, telemetry["lon"], telemetry["lat"], telemetry["alt_m"], telemetry.get("agl_m"), telemetry["heading_deg"], telemetry["speed_mps"],
                        telemetry.get("battery_pct"), telemetry.get("link_dbm"), g.get("heading_deg"), g.get("pitch_deg")))
            if t - _pruned["at"] > 60:  # by time, whatever sequence numbers a ground station uses
                _pruned["at"] = t
                db.execute("DELETE FROM fleet_telemetry WHERE t < ?", (t - TELEMETRY_KEEP_S,))
            db.execute("COMMIT")
        return True

    def drones(self, retire_after_s: float = 15 * 60) -> list[dict]:
        """Aircraft in the fleet. One silent for 15 minutes has left it (its record stays; registering again rejoins)."""
        t = now()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_drones WHERE last_seen IS NULL OR last_seen > ? OR registered_at > ? ORDER BY id", (t - retire_after_s, t - retire_after_s)).fetchall()
        out = []
        for r in rows:
            age = t - r["last_seen"] if r["last_seen"] else None
            out.append({"id": r["id"], "name": r["name"], "origin": r["origin"], "aircraft": r["aircraft"], "capabilities": _loads(r["capabilities"], {}),
                        "registered_at": r["registered_at"], "last_seen": r["last_seen"], "age_s": None if age is None else round(age, 1),
                        "link_state": "online" if age is not None and age <= 15 else "stale" if age is not None and age <= 60 else "lost",
                        "state": _loads(r["state"], {})})
        return out

    def track(self, drone_id: str, seconds: float = 30) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT t, lon, lat, alt_m, agl_m, heading, speed, gimbal_heading, gimbal_pitch FROM fleet_telemetry WHERE drone_id=? AND t > ? ORDER BY t",
                              (drone_id, now() - seconds)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ jobs, bids, tasks, operations
    def set_jobs(self, jobs: list[dict]) -> None:
        t = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM fleet_jobs")
            for j in jobs:
                db.execute("INSERT INTO fleet_jobs VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?)",
                           (j["key"], j["kind"], j["label"], json.dumps(j["params"]), j["priority"], json.dumps(j.get("needs", [])), j.get("assigned_drone"), t))
            db.execute("COMMIT")

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_jobs ORDER BY priority DESC").fetchall()
        return [{**dict(r), "params": _loads(r["params"], {}), "needs": _loads(r["needs"], [])} for r in rows]

    def put_bids(self, drone_id: str, bids: list[dict]) -> None:
        t = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM fleet_bids WHERE drone_id=?", (drone_id,))
            db.executemany("INSERT INTO fleet_bids VALUES (?, ?, ?, ?, ?, ?)",
                           [(drone_id, b["job_key"], b["cost"], 1 if b["feasible"] else 0, json.dumps(b.get("detail", {})), t) for b in bids])
            db.execute("COMMIT")

    def bids(self, fresh_s: float = 15) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_bids WHERE t > ?", (now() - fresh_s,)).fetchall()
        return [{**dict(r), "feasible": bool(r["feasible"]), "detail": _loads(r["detail"], {})} for r in rows]

    def tasks(self, active_only: bool = True, limit: int = 200) -> list[dict]:
        query = "SELECT * FROM fleet_tasks" + (" WHERE status IN ('assigned','acknowledged','active')" if active_only else "") + " ORDER BY created_at DESC LIMIT ?"
        with self.connect() as db:
            rows = db.execute(query, (limit,)).fetchall()
        return [{**dict(r), "params": _loads(r["params"], {})} for r in rows]

    def benched(self, within_s: float = 120) -> set[str]:
        """Aircraft that rejected two or more tasks recently, or rejected one for a vehicle-level reason: not tasked for a while."""
        with self.connect() as db:
            rows = db.execute("SELECT drone_id, COUNT(*) AS n, MAX(CASE WHEN reason LIKE 'vehicle:%' THEN 1 ELSE 0 END) AS vehicle FROM fleet_tasks "
                              "WHERE status='rejected' AND updated_at > ? GROUP BY drone_id", (now() - within_s,)).fetchall()
        return {r["drone_id"] for r in rows if r["n"] >= 2 or r["vehicle"]}

    def refusals(self, within_s: float = 600) -> set[tuple[str, str]]:
        """(aircraft, job) pairs the aircraft rejected recently: not offered to it again for a while."""
        with self.connect() as db:
            rows = db.execute("SELECT drone_id, job_key FROM fleet_tasks WHERE status='rejected' AND job_key IS NOT NULL AND updated_at > ?", (now() - within_s,)).fetchall()
        return {(r["drone_id"], r["job_key"]) for r in rows}

    def current_task(self, drone_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM fleet_tasks WHERE drone_id=? AND status IN ('assigned','acknowledged','active') ORDER BY created_at DESC LIMIT 1", (drone_id,)).fetchone()
        return {**dict(row), "params": _loads(row["params"], {})} if row else None

    def create_task(self, drone_id: str, kind: str, label: str, params: dict, created_by: str, reason: str, priority: float,
                    job_key: str | None = None, hold_until: float | None = None) -> dict:
        """Assign a task, superseding whatever the drone was doing. Returns the new task."""
        t = now(); task_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE fleet_tasks SET status='superseded', updated_at=? WHERE drone_id=? AND status IN ('assigned','acknowledged','active')", (t, drone_id))
            db.execute("UPDATE fleet_operations SET state='ended', updated_at=? WHERE drone_id=? AND state != 'ended'", (t, drone_id))
            db.execute("INSERT INTO fleet_tasks VALUES (?, ?, ?, ?, ?, ?, 'assigned', ?, ?, ?, ?, ?, NULL, ?)",
                       (task_id, drone_id, job_key, kind, label, json.dumps(params), created_by, reason, priority, t, t, hold_until))
            db.execute("COMMIT")
        return {"id": task_id, "drone_id": drone_id, "job_key": job_key, "kind": kind, "label": label, "params": params, "status": "assigned",
                "created_by": created_by, "reason": reason, "priority": priority, "created_at": t, "updated_at": t, "acked_at": None, "hold_until": hold_until}

    def update_task_params(self, task_id: str, params: dict) -> None:
        with self.connect() as db:
            db.execute("UPDATE fleet_tasks SET params=?, updated_at=? WHERE id=?", (json.dumps(params), now(), task_id))

    def ack(self, task_id: str, drone_id: str, status: str, detail: str | None) -> dict | None:
        t = now()
        order = {"assigned": 0, "acknowledged": 1, "active": 2, "completed": 3, "failed": 3, "rejected": 3}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM fleet_tasks WHERE id=? AND drone_id=?", (task_id, drone_id)).fetchone()
            if not row:
                db.execute("ROLLBACK"); return None
            # Lifecycle only moves forward; a superseded task stays superseded.
            if row["status"] in ("superseded", "cancelled") or order.get(status, -1) < order.get(row["status"], 0):
                db.execute("ROLLBACK"); return {**dict(row), "params": _loads(row["params"], {})}
            db.execute("UPDATE fleet_tasks SET status=?, updated_at=?, acked_at=COALESCE(acked_at, ?), reason=CASE WHEN ? IN ('rejected','failed') AND ? IS NOT NULL THEN ? ELSE reason END WHERE id=?",
                       (status, t, t, status, detail, detail, task_id))
            if status in ("completed", "failed", "rejected"):
                db.execute("UPDATE fleet_operations SET state='ended', updated_at=? WHERE task_id=?", (t, task_id))
            elif status == "active":
                db.execute("UPDATE fleet_operations SET state='activated', updated_at=? WHERE task_id=? AND state='accepted'", (t, task_id))
            db.execute("COMMIT")
        if status != "acknowledged":
            self.message(f"drone:{drone_id}", "coordinator", f"task-{status}", {"text": f"{row['label']}: {status}{f' ({detail})' if detail else ''}.", "task_id": task_id})
        return {**dict(row), "status": status, "params": _loads(row["params"], {})}

    def put_operation(self, drone_id: str, task_id: str, geometry: dict, lower: float, upper: float, t_end: float) -> None:
        t = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE fleet_operations SET state='ended', updated_at=? WHERE drone_id=? AND task_id != ? AND state != 'ended'", (t, drone_id, task_id))
            existing = db.execute("SELECT id FROM fleet_operations WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                db.execute("UPDATE fleet_operations SET geometry=?, alt_lower_m=?, alt_upper_m=?, t_end=?, updated_at=? WHERE id=?",
                           (json.dumps(geometry), lower, upper, t_end, t, existing["id"]))
            else:
                db.execute("INSERT INTO fleet_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)",
                           (uuid.uuid4().hex[:12], drone_id, task_id, json.dumps(geometry), lower, upper, t, t_end, t))
            db.execute("COMMIT")

    def operations(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_operations WHERE state != 'ended'").fetchall()
        return [{**dict(r), "geometry": _loads(r["geometry"], {})} for r in rows]

    def set_operation_state(self, drone_id: str, state: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE fleet_operations SET state=?, updated_at=? WHERE drone_id=? AND state IN ('accepted','activated','nonconforming')", (state, now(), drone_id))

    # ------------------------------------------------------------------ manual control (virtual aircraft only)
    def set_control(self, drone_id: str, setpoint: dict | None) -> None:
        with self.connect() as db:
            if setpoint is None:
                db.execute("DELETE FROM fleet_control WHERE drone_id=?", (drone_id,))
            else:
                db.execute("INSERT INTO fleet_control VALUES (?, ?, ?) ON CONFLICT(drone_id) DO UPDATE SET setpoint=excluded.setpoint, updated_at=excluded.updated_at",
                           (drone_id, json.dumps(setpoint), now()))

    def control(self, drone_id: str | None = None, fresh_s: float = 5) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_control WHERE updated_at > ?" + (" AND drone_id=?" if drone_id else ""),
                              (now() - fresh_s, drone_id) if drone_id else (now() - fresh_s,)).fetchall()
        return {r["drone_id"]: _loads(r["setpoint"], {}) for r in rows}

    # ------------------------------------------------------------------ messages and agents
    def message(self, sender: str, recipient: str, kind: str, body: dict) -> None:
        with self.connect() as db:
            cur = db.execute("INSERT INTO fleet_messages (t, sender, recipient, kind, body) VALUES (?, ?, ?, ?, ?)", (now(), sender, recipient, kind, json.dumps(body)))
            if cur.lastrowid and cur.lastrowid % 500 == 0:
                db.execute("DELETE FROM fleet_messages WHERE id < ?", (cur.lastrowid - MESSAGE_KEEP,))

    def messages(self, limit: int = 80, after: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_messages WHERE id > ? ORDER BY id DESC LIMIT ?", (after, limit)).fetchall()
        return [{**dict(r), "body": _loads(r["body"], {})} for r in rows]

    def heartbeat(self, agent_id: str, kind: str, workflow_id: str | None, status: str, summary: dict) -> dict:
        """Record an agent's heartbeat; returns the previous summary (agents report only what changed)."""
        with self.connect() as db:
            previous = db.execute("SELECT summary FROM fleet_agents WHERE id=?", (agent_id,)).fetchone()
            db.execute("INSERT INTO fleet_agents VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, heartbeat=excluded.heartbeat, summary=excluded.summary, workflow_id=excluded.workflow_id",
                       (agent_id, kind, workflow_id, status, now(), json.dumps(summary)))
        return _loads(previous["summary"], {}) if previous else {}

    def agents(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fleet_agents ORDER BY id").fetchall()
        t = now()
        return [{**dict(r), "summary": _loads(r["summary"], {}), "age_s": round(t - r["heartbeat"], 1)} for r in rows]


# ---------------------------------------------------------------- field notes and credentials (methods added to FleetDB)
def _add_note(self, drone_id: str, task_id: str | None, kind: str, lon: float, lat: float, text: str, data: dict) -> int:
    with self.connect() as db:
        cur = db.execute("INSERT INTO fleet_notes (t, drone_id, task_id, kind, lon, lat, text, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (now(), drone_id, task_id, kind, lon, lat, text, json.dumps(data)))
        return cur.lastrowid


def _notes(self, limit: int = 60, drone_id: str | None = None, since: float = 0) -> list[dict]:
    query = "SELECT * FROM fleet_notes WHERE t > ?" + (" AND drone_id=?" if drone_id else "") + " ORDER BY id DESC LIMIT ?"
    with self.connect() as db:
        rows = db.execute(query, (since, drone_id, limit) if drone_id else (since, limit)).fetchall()
    return [{**dict(r), "data": _loads(r["data"], {})} for r in rows]


def _last_note(self, drone_id: str, task_id: str | None) -> dict | None:
    with self.connect() as db:
        row = db.execute("SELECT * FROM fleet_notes WHERE drone_id=? AND task_id IS ? ORDER BY id DESC LIMIT 1", (drone_id, task_id)).fetchone()
    return dict(row) if row else None


def _add_credential(self, token_hash: str, drone_id: str, label: str) -> None:
    with self.connect() as db:
        db.execute("INSERT INTO fleet_credentials VALUES (?, ?, ?, ?, 0)", (token_hash, drone_id, label, now()))


def _credential(self, token_hash: str) -> dict | None:
    with self.connect() as db:
        row = db.execute("SELECT * FROM fleet_credentials WHERE token_hash=? AND revoked=0", (token_hash,)).fetchone()
    return dict(row) if row else None


def _credentials(self) -> list[dict]:
    with self.connect() as db:
        rows = db.execute("SELECT drone_id, label, created_at, revoked FROM fleet_credentials ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def _revoke(self, drone_id: str) -> int:
    with self.connect() as db:
        return db.execute("UPDATE fleet_credentials SET revoked=1 WHERE drone_id=? AND revoked=0", (drone_id,)).rowcount


FleetDB.add_note, FleetDB.notes, FleetDB.last_note = _add_note, _notes, _last_note
FleetDB.add_credential, FleetDB.credential, FleetDB.credentials, FleetDB.revoke = _add_credential, _credential, _credentials, _revoke


def fleet_db() -> FleetDB:
    from ..config import get_settings
    return FleetDB(get_settings().database)
