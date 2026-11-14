import { api, type Bounds } from './types';

/**
 * The workspace's view of the fleet. Every aircraft (Terra's virtual fleet and real drones alike) reports to the
 * server, which keeps one central database; durable agents coordinate the fleet from that database. This store
 * polls it once a second and renders each aircraft 1.2 s in the past, interpolating between telemetry samples so
 * motion stays smooth. Flying a virtual aircraft by hand is predicted locally and streamed to the server as
 * setpoints; real aircraft are never flown from the browser.
 */
export type Aircraft = 'quad' | 'fixed-wing';
/** real: a flying aircraft; sitl: real autopilot firmware (ArduPilot SITL) on a simulated airframe; virtual: Terra's own simulator. */
export type Source = 'virtual' | 'sitl' | 'real';
export type Profile = 'change' | 'thermal' | 'search';
export type FieldNote = { id: number; t: number; drone_id: string; task_id: string | null; kind: 'station' | 'observation' | 'warm' | 'change' | string; lon: number; lat: number; text: string;
  data: { surface_c?: number; surface_p95_c?: number; surface_source?: string; cover?: string; change_db?: number; elevation_m?: number; weather?: { temperature_2m?: number; wind_speed_10m?: number; wind_direction_10m?: number } } };
export type Enrollment = { drone_id: string; label: string; created_at: number; revoked: number };
export type EnrollRequest = { id: string; name: string; kind: 'mavlink' | 'sitl'; connection: string; aircraft: 'quad' | 'fixed-wing' | 'vtol'; sensors: string[]; relay: boolean };
export type TaskView = { id: string; kind: string; label: string; params: Record<string, unknown> & { lon?: number; lat?: number; radius_m?: number; agl_m?: number; bbox?: Bounds; inset?: number; lane_m?: number }; status: string; created_by: string; reason: string | null; created_at: number; updated_at: number; acked_at: number | null };
export type Volume = { id: string; drone_id: string; task_id: string; geometry: { type: 'circle'; lon: number; lat: number; radius_m: number } | { type: 'box'; bbox: Bounds }; alt_lower_m: number; alt_upper_m: number; t_start: number; t_end: number; state: string };
export type FleetMessage = { id: number; t: number; sender: string; recipient: string; kind: string; body: { text?: string; [k: string]: unknown } };
export type Mission = { kind: string; label: string; status: string; task: string; centre?: [number, number]; radiusM?: number; box?: Bounds; aglM: number; speedMps: number };
export type DroneState = {
  id: string; callsign: string; name: string; source: Source; aircraft: Aircraft; color: string;
  mission: Mission; lon: number; lat: number; aglM: number; groundM: number | null; amslM: number;
  headingDeg: number; speedMps: number; pitchDeg: number; rollDeg: number;
  gimbal: { headingDeg: number; pitchDeg: number }; batteryPct: number; minutesLeft: number; linkDbm: number; flightS: number;
  sensor: string; sensors: string[]; relay: boolean; protocol: string; autopilot: string | null; videoUrl?: string; ageS?: number;
  status: 'airborne' | 'landed' | 'stale' | 'lost'; task: TaskView | null; operation: Volume | null;
  agent: { status: string; age_s: number; summary: { text?: string; feasible_bids?: number; bids?: number; findings?: string[] } } | null;
};
export type Patch = { rank: number; centre: [number, number]; bounds: Bounds; label: string };
export type Place = { name: string; lon: number; lat: number; kind: string; elevation_m: number | null };
export type GroundAt = (lon: number, lat: number) => number | undefined;
export type FleetInfo = {
  connections: boolean; area: { name: string; bbox: Bounds; launch: [number, number]; profile?: Profile; investigation_id?: string | null } | null;
  coordinator: { running: boolean; summary: { round?: number; text?: string; online?: number; jobs?: number; assigned?: number }; age_s: number | null };
  messages: FleetMessage[]; notes: FieldNote[]; jobs: { key: string; kind: string; label: string; priority: number; assigned_drone: string | null }[];
  targets: { id: string; kind?: string; label: string; lon: number; lat: number; radius_m: number; priority: number; needs?: string[]; last_observed: number | null }[];
  error: string | null;
};
type Sample = { t: number; lon: number; lat: number; agl: number | null; alt: number; heading: number; speed: number; gh: number | null; gp: number | null };
type ServerDrone = {
  id: string; name: string; origin: Source; aircraft: string; age_s: number | null; link_state: 'online' | 'stale' | 'lost'; registered_at: number;
  capabilities: { sensors?: string[]; relay?: boolean; cruise_mps?: number; max_mps?: number; endurance_min?: number; protocol?: string; autopilot?: string | null };
  state: { lon?: number; lat?: number; alt_m?: number; agl_m?: number | null; heading_deg?: number; speed_mps?: number; battery_pct?: number | null; link_dbm?: number | null; mode?: string; landed?: boolean; gimbal?: { heading_deg: number; pitch_deg: number } | null; video_url?: string | null };
  track: { t: number; lon: number; lat: number; agl_m: number | null; alt_m: number; heading: number; speed: number; gimbal_heading: number | null; gimbal_pitch: number | null }[];
  task: TaskView | null; operation: Volume | null; agent: DroneState['agent'];
};
type Manual = { lon: number; lat: number; agl: number; heading: number; gimbalPitch: number; speed: number; sent: number };

const G = 9.81;
const DEG = Math.PI / 180;
const DELAY = 1.2;
const STATUS: Record<string, string> = { orbit: 'Orbit', survey: 'Survey', perimeter: 'Mapping', search: 'Searching', relay: 'Relay', hold: 'Hold', standby: 'Standby', return: 'Returning' };
const SENSOR: Record<string, string> = { eo: 'EO', ir: 'IR', lidar: 'lidar', multispectral: 'multispectral', gas: 'gas', sar: 'SAR' };
export const ft = (m: number) => m * 3.28084;
export const kt = (mps: number) => mps * 1.943844;
const wrap = (d: number) => ((d % 360) + 360) % 360;
const turn = (a: number, b: number) => ((b - a + 540) % 360) - 180;
const ease = (t: number) => t < 0 ? 0 : t > 1 ? 1 : t * t * (3 - 2 * t);

function lanes(bbox: Bounds, laneM = 150): [number, number][] {
  const [w, s, e, n] = bbox; const tall = (n - s) * 110_574; const count = Math.max(3, Math.floor(tall / Math.max(60, laneM)));
  const out: [number, number][] = [];
  for (let i = 0; i <= count; i++) { const lat = s + (n - s) * i / count; out.push(...(i % 2 ? [[e, lat], [w, lat]] : [[w, lat], [e, lat]]) as [number, number][]); }
  return out;
}
/** An expanding-square search from a datum: legs of 1, 1, 2, 2, 3, 3 … lanes, turning right (as the coordinator plans it). */
function expandingSquare(lon: number, lat: number, laneM = 120, sizeM = 1200): [number, number][] {
  const out: [number, number][] = [[lon, lat]]; let x = 0, y = 0, heading = 0, leg = 1;
  const dirs = [[0, 1], [1, 0], [0, -1], [-1, 0]];
  while (Math.max(Math.abs(x), Math.abs(y)) * 2 < sizeM && out.length < 80) {
    for (let k = 0; k < 2; k++) { const [dx, dy] = dirs[heading % 4]; x += dx * leg * laneM; y += dy * leg * laneM; heading++; out.push([lon + x / (111_320 * Math.cos(lat * DEG)), lat + y / 110_574]); }
    leg++;
  }
  return out;
}
const ring = (lon: number, lat: number, r: number, k = 72): [number, number][] =>
  Array.from({ length: k }, (_, i) => { const a = i / k * 2 * Math.PI; return [lon + Math.sin(a) * r / (111_320 * Math.cos(lat * DEG)), lat + Math.cos(a) * r / 110_574] as [number, number]; });

export class DroneStore {
  private meta = new Map<string, ServerDrone>();
  private samples = new Map<string, Sample[]>();
  private offset: number | null = null;
  private manual = new Map<string, Manual>();
  private resume = new Map<string, { from: DroneState; at: number }>();
  private listeners = new Set<() => void>();
  private timer: ReturnType<typeof setInterval> | null = null;
  info: FleetInfo = { connections: false, area: null, coordinator: { running: false, summary: {}, age_s: null }, messages: [], notes: [], jobs: [], targets: [], error: null };
  groundAt: GroundAt | null = null;
  /** Ground height from an open camera view, which has the finest terrain around its aircraft. */
  feedGroundAt: GroundAt | null = null;
  generation = 0;
  /** Changes only when a route changes (new task), not with every telemetry update. */
  routes = 0;
  /** True after the first answer from the server (so the workspace knows whether a monitoring area exists). */
  ready = false;
  private routeKey = '';

  subscribe(l: () => void) { this.listeners.add(l); return () => { this.listeners.delete(l); }; }
  private emit() { this.generation++; this.listeners.forEach(l => l()); }
  private serverNow() { return Date.now() / 1000 + (this.offset ?? 0); }

  start() { if (this.timer) return; void this.poll(); this.timer = setInterval(() => void this.poll(), 1000); }
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; }

  async poll() {
    try {
      const v = await api<{ server_time: number; connections_enabled: boolean; area: FleetInfo['area']; coordinator: FleetInfo['coordinator']; messages: FleetMessage[];
        notes?: FieldNote[]; jobs: FleetInfo['jobs']; targets: FleetInfo['targets']; drones: ServerDrone[] }>('/fleet');
      const offset = v.server_time - Date.now() / 1000; this.offset = this.offset === null ? offset : this.offset * 0.8 + offset * 0.2;
      const seen = new Set<string>();
      for (const d of v.drones) {
        seen.add(d.id); this.meta.set(d.id, d);
        const buf = this.samples.get(d.id) ?? []; const last = buf.at(-1)?.t ?? 0;
        for (const p of d.track) if (p.t > last) buf.push({ t: p.t, lon: p.lon, lat: p.lat, agl: p.agl_m, alt: p.alt_m, heading: p.heading, speed: p.speed, gh: p.gimbal_heading, gp: p.gimbal_pitch });
        const cut = this.serverNow() - 30; while (buf.length > 2 && buf[0].t < cut) buf.shift();
        this.samples.set(d.id, buf);
      }
      for (const id of [...this.meta.keys()]) if (!seen.has(id)) { this.meta.delete(id); this.samples.delete(id); }
      const notes = v.notes ?? []; if (notes[0]?.id !== this.info.notes[0]?.id) this.notesVersion++;
      this.info = { connections: v.connections_enabled, area: v.area, coordinator: v.coordinator, messages: v.messages, notes, jobs: v.jobs, targets: v.targets, error: null };
      this.ready = true;
      const key = v.drones.map(d => `${d.id}:${d.task?.id}:${JSON.stringify(d.task?.params ?? null)}:${this.manual.has(d.id)}`).join('|');
      if (key !== this.routeKey) { this.routeKey = key; this.routes++; }
    } catch (e) {
      this.info = { ...this.info, error: e instanceof Error ? e.message : 'The fleet service did not answer.' };
    }
    this.emit();
  }

  /** Publish the monitoring area and its targets; the coordinator plans jobs from them. The profile says what the
   * fleet is for: watching measured change (targets below), the warmest ground in the latest Landsat thermal scene
   * (the server picks those), or searching the area in four sectors. */
  async publishArea(name: string, bbox: Bounds, patches: Patch[], places: Place[], profile: Profile = this.info.area?.profile ?? 'change', investigationId?: string | null) {
    const targets: Record<string, unknown>[] = [
      ...patches.slice(0, 4).map((p, i) => ({ id: `patch-${p.rank}`, kind: 'change', label: p.label, lon: p.centre[1], lat: p.centre[0], priority: [1, 0.85, 0.7, 0.6][i],
        radius_m: Math.max(150, Math.min(3000, Math.hypot((p.bounds[2] - p.bounds[0]) * 111_320 * Math.cos(p.centre[0] * DEG), (p.bounds[3] - p.bounds[1]) * 110_574) / 2)) })),
      ...places.filter(p => p.kind === 'peak' || p.kind === 'volcano').sort((a, b) => (b.elevation_m ?? 0) - (a.elevation_m ?? 0)).slice(0, 2)
        .map((p, i) => ({ id: `summit-${i}`, kind: 'summit', label: p.name, lon: p.lon, lat: p.lat, radius_m: 500, priority: i ? 0.45 : 0.55, needs: p.kind === 'volcano' ? ['ir'] : [] })),
    ];
    if (!targets.length) targets.push({ id: 'centre', kind: 'point', label: 'the centre of the area', lon: (bbox[0] + bbox[2]) / 2, lat: (bbox[1] + bbox[3]) / 2, radius_m: 400, priority: 0.5 });
    // The same area, targets and profile again is not news: send it once (the coordinator keeps it).
    const body = JSON.stringify({ name: name.slice(0, 80), bbox, targets, profile, investigation_id: investigationId ?? null });
    const current = this.info.area;
    const unchanged = current && current.bbox.every((v, i) => Math.abs(v - bbox[i]) < 1e-9) && (current.profile ?? 'change') === profile;
    if (body === this.published && unchanged) return; this.published = body;
    await api('/fleet/area', { method: 'POST', body }).catch(() => { this.published = ''; });
    void this.poll();
  }

  // ------------------------------------------------------------------ adding aircraft
  /** A per-aircraft token and the command that connects its ground station (the token is shown once). */
  enroll(body: EnrollRequest) { return api<{ id: string; token: string; command: string; server: string }>('/fleet/enroll', { method: 'POST', body: JSON.stringify(body) }); }
  async enrollments() { return (await api<{ enrollments: Enrollment[] }>('/fleet/enrollments')).enrollments; }
  revoke(id: string) { return api<{ revoked: number }>(`/fleet/enrollments/${encodeURIComponent(id)}/revoke`, { method: 'POST' }); }
  /** Changes when a new field note arrives. */
  notesVersion = 0;
  private published = '';

  /** A person's task for one aircraft; held by the coordinator for 15 minutes or until it ends. */
  async assign(id: string, task: { kind: string; label: string; lon?: number; lat?: number; radius_m?: number; agl_m?: number; bbox?: Bounds }) {
    const r = await api<{ advisory: boolean }>('/fleet/tasks', { method: 'POST', body: JSON.stringify({ drone_id: id, ...task }) });
    void this.poll(); return r;
  }

  // ------------------------------------------------------------------ manual flight (virtual aircraft only)
  takeControl(id: string) {
    const s = this.state(id); if (!s || s.source === 'real') return;
    this.manual.set(id, { lon: s.lon, lat: s.lat, agl: Math.max(30, s.aglM), heading: s.headingDeg, gimbalPitch: s.gimbal.pitchDeg, speed: s.speedMps, sent: 0 }); this.routes++; this.emit();
  }
  releaseControl(id: string) {
    const s = this.state(id); this.manual.delete(id);
    if (s) this.resume.set(id, { from: s, at: performance.now() / 1000 });
    void api(`/fleet/drones/${encodeURIComponent(id)}/release`, { method: 'POST' }).catch(() => {});
    this.routes++; this.emit();
  }
  isManual(id: string) { return this.manual.has(id); }
  /** Integrate pilot input for `dt` seconds (inputs −1..1) and stream the resulting setpoint four times a second. */
  fly(id: string, input: { forward: number; right: number; yaw: number; climb: number; gimbal: number; boost: boolean }, dt: number) {
    const m = this.manual.get(id); if (!m) return;
    const top = input.boost ? 16 : 9; m.speed += (input.forward * top - m.speed) * Math.min(1, dt * 2.2);
    m.heading = wrap(m.heading + input.yaw * 55 * dt);
    const h = m.heading * DEG, strafe = input.right * top * 0.6;
    const east = Math.sin(h) * m.speed + Math.cos(h) * strafe, north = Math.cos(h) * m.speed - Math.sin(h) * strafe;
    m.lon += east * dt / (111_320 * Math.cos(m.lat * DEG)); m.lat += north * dt / 110_574;
    m.agl = Math.min(1000, Math.max(15, m.agl + input.climb * (input.boost ? 8 : 4) * dt));
    m.gimbalPitch = Math.min(0, Math.max(-90, m.gimbalPitch + input.gimbal * 40 * dt));
    const now = performance.now();
    if (now - m.sent > 250) {
      m.sent = now;
      void api(`/fleet/drones/${encodeURIComponent(id)}/control`, { method: 'POST', body: JSON.stringify({ lon: m.lon, lat: m.lat, agl_m: m.agl, heading_deg: m.heading, gimbal_pitch_deg: m.gimbalPitch }) }).catch(() => {});
    }
  }

  // ------------------------------------------------------------------ state
  ids() { return [...this.meta.keys()]; }
  states(): DroneState[] { return this.ids().map(id => this.state(id)).filter((s): s is DroneState => Boolean(s)); }
  area(): Bounds | null { return this.info.area?.bbox ?? null; }

  missionPath(id: string): [number, number][] | null {
    const d = this.meta.get(id); const t = d?.task; if (!t || this.manual.has(id)) return null; const p = t.params;
    if (t.kind === 'survey' && p.bbox) return lanes(p.bbox, p.lane_m);
    if (t.kind === 'search' && typeof p.lon === 'number' && typeof p.lat === 'number') return expandingSquare(p.lon, p.lat, Number(p.lane_m ?? 120), Number(p.size_m ?? 1200));
    if (t.kind === 'perimeter' && p.bbox) { const [w, s, e, n] = p.bbox; const k = p.inset ?? 0.08; const dx = (e - w) * k, dy = (n - s) * k; return [[w + dx, s + dy], [e - dx, s + dy], [e - dx, n - dy], [w + dx, n - dy]]; }
    if (typeof p.lon === 'number' && typeof p.lat === 'number') {
      if (t.kind === 'return') { const st = d.state; return typeof st.lon === 'number' && typeof st.lat === 'number' ? [[st.lon, st.lat], [p.lon, p.lat]] : null; }
      return ring(p.lon, p.lat, Math.max(t.kind === 'hold' ? 20 : 25, Number(p.radius_m ?? 40)));
    }
    return null;
  }
  closedPath(id: string) { const k = this.meta.get(id)?.task?.kind; return k !== 'return' && k !== 'survey' && k !== 'search'; }

  state(id: string): DroneState | null {
    const d = this.meta.get(id); if (!d) return null;
    const st = d.state; const caps = d.capabilities; const buf = this.samples.get(id) ?? [];
    if (typeof st.lon !== 'number' || typeof st.lat !== 'number') return null;
    // Interpolate 1.2 s in the past between the two samples around that time; extrapolate briefly if data is late.
    const rt = this.serverNow() - DELAY;
    // Height above ground when the aircraft reports it; otherwise from its altitude and the terrain under it.
    let lon = st.lon, lat = st.lat, agl: number | null = st.agl_m ?? null, alt = st.alt_m ?? 0, heading = st.heading_deg ?? 0, speed = st.speed_mps ?? 0;
    let gh = st.gimbal?.heading_deg ?? heading, gp = st.gimbal?.pitch_deg ?? -35, turnRate = 0;
    if (buf.length) {
      const i = buf.findIndex(s => s.t > rt);
      if (i === -1) {
        const a = buf[buf.length - 1]; const dt = Math.min(2, Math.max(0, rt - a.t)); const h = a.heading * DEG;
        lon = a.lon + Math.sin(h) * a.speed * dt / (111_320 * Math.cos(a.lat * DEG)); lat = a.lat + Math.cos(h) * a.speed * dt / 110_574;
        agl = a.agl; alt = a.alt; heading = a.heading; speed = a.speed; gh = a.gh ?? gh; gp = a.gp ?? gp;
        if (buf.length > 1) { const b = buf[buf.length - 2]; turnRate = turn(b.heading, a.heading) / Math.max(0.2, a.t - b.t); }
      } else if (i === 0) {
        const a = buf[0]; lon = a.lon; lat = a.lat; agl = a.agl; alt = a.alt; heading = a.heading; speed = a.speed; gh = a.gh ?? gh; gp = a.gp ?? gp;
      } else {
        const a = buf[i - 1], b = buf[i]; const f = (rt - a.t) / Math.max(1e-3, b.t - a.t);
        lon = a.lon + (b.lon - a.lon) * f; lat = a.lat + (b.lat - a.lat) * f; alt = a.alt + (b.alt - a.alt) * f;
        agl = a.agl !== null && b.agl !== null ? a.agl + (b.agl - a.agl) * f : null;
        heading = wrap(a.heading + turn(a.heading, b.heading) * f); speed = a.speed + (b.speed - a.speed) * f;
        gh = wrap((a.gh ?? heading) + turn(a.gh ?? heading, b.gh ?? heading) * f); gp = (a.gp ?? gp) + ((b.gp ?? gp) - (a.gp ?? gp)) * f;
        turnRate = turn(a.heading, b.heading) / Math.max(0.2, b.t - a.t);
      }
    }
    const m = this.manual.get(id);
    if (m) { lon = m.lon; lat = m.lat; agl = m.agl; heading = m.heading; speed = Math.abs(m.speed); gh = m.heading; gp = m.gimbalPitch; turnRate = 0; }
    const r = this.resume.get(id);
    const aircraftGround = (m ? this.feedGroundAt?.(lon, lat) : undefined) ?? this.groundAt?.(lon, lat) ?? null;
    let height = agl ?? (aircraftGround !== null ? Math.max(0, alt - aircraftGround) : alt);
    if (r && !m) {
      const k = ease((performance.now() / 1000 - r.at) / 3);
      if (k >= 1) this.resume.delete(id);
      else { lon = r.from.lon + (lon - r.from.lon) * k; lat = r.from.lat + (lat - r.from.lat) * k; height = r.from.aglM + (height - r.from.aglM) * k; heading = wrap(r.from.headingDeg + turn(r.from.headingDeg, heading) * k); }
    }
    const aircraft: Aircraft = d.aircraft === 'fixed-wing' ? 'fixed-wing' : 'quad';
    const ground = aircraftGround;
    const roll = aircraft === 'fixed-wing' ? Math.atan(speed * turnRate * DEG / G) / DEG : Math.max(-12, Math.min(12, turnRate * 0.4));
    const t = d.task; const p = t?.params ?? {};
    const landed = Boolean(st.landed) && !m;
    const status: DroneState['status'] = d.link_state === 'lost' ? 'lost' : d.link_state === 'stale' ? 'stale' : landed ? 'landed' : 'airborne';
    const battery = typeof st.battery_pct === 'number' ? st.battery_pct : NaN;
    const mission: Mission = m ? { kind: 'manual', label: 'Manual flight', status: 'Manual', task: 'Flown by you from the camera view', aglM: height, speedMps: speed }
      : t ? { kind: t.kind, label: t.label, status: STATUS[t.kind] ?? t.kind, task: t.label,
        centre: typeof p.lon === 'number' && typeof p.lat === 'number' ? [p.lon, p.lat] : undefined, radiusM: typeof p.radius_m === 'number' ? p.radius_m : undefined,
        box: p.bbox, aglM: Number(p.agl_m ?? height), speedMps: Number(p.speed_mps ?? speed) }
      : { kind: 'idle', label: landed ? 'On the ground' : 'No task', status: landed ? 'Landed' : 'Idle', task: landed ? 'Waiting at the launch site' : 'Waiting for a task', aglM: height, speedMps: speed };
    return {
      id, callsign: d.id.toUpperCase().slice(0, 10), name: d.name !== d.id ? d.name : '', source: d.origin, aircraft, color: d.origin === 'real' ? '#9fe07a' : d.origin === 'sitl' ? '#8fc8ff' : '#f4f1e8',
      mission, lon, lat, aglM: height, groundM: ground, amslM: agl === null && !m ? alt : (ground ?? 0) + height, headingDeg: heading, speedMps: speed,
      pitchDeg: aircraft === 'quad' ? -Math.min(14, speed * 0.7) : 2, rollDeg: roll, gimbal: { headingDeg: gh, pitchDeg: gp },
      batteryPct: battery, minutesLeft: Number.isFinite(battery) ? (battery - 15) / (100 / Number(caps.endurance_min ?? 30)) : NaN,
      linkDbm: typeof st.link_dbm === 'number' ? st.link_dbm : NaN, flightS: Math.max(0, Date.now() / 1000 - d.registered_at),
      sensor: (caps.sensors ?? ['eo']).map(x => SENSOR[x] ?? x).join(' · ') + (caps.relay ? ' · relay radio' : ''), sensors: caps.sensors ?? ['eo'], relay: Boolean(caps.relay),
      protocol: caps.protocol ?? 'other', autopilot: caps.autopilot ?? null, videoUrl: st.video_url ?? undefined, ageS: d.age_s ?? undefined, status,
      task: t, operation: d.operation, agent: d.agent,
    };
  }

  /** Where the camera's line of sight meets the ground, assuming locally level terrain. */
  sightPoint(s: DroneState): [number, number] | null {
    if (s.gimbal.pitchDeg > -2) return null;
    const d = Math.min(8000, s.aglM / Math.tan(-s.gimbal.pitchDeg * DEG)); const h = s.gimbal.headingDeg * DEG;
    return [s.lon + Math.sin(h) * d / (111_320 * Math.cos(s.lat * DEG)), s.lat + Math.cos(h) * d / 110_574];
  }
}

export const droneStore = new DroneStore();
if (import.meta.env.DEV) Object.assign(window, { __terraDrones: droneStore });
