"""Language agents for TERRA: a planner and a writer, both grounded in computed evidence.

The planner reads the question and decides the *method* (one acquisition or a change
comparison) and which time window matters; acquisitions are then chosen
deterministically from the catalog so the model cannot invent products. The writer turns
measurements into an answer, and every number in that answer is checked against the
computed values before it is shown.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .connector import request as connector_request

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def model_name() -> str:
    return os.getenv("TERRA_LLM_MODEL", "").strip() or "gpt-6-luna"


def _key() -> str | None:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        env = get_settings().root / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key or None


def available() -> bool:
    return bool(_key())


async def _chat(system: str, user: str, schema: dict, name: str, max_tokens: int = 1400, effort: str = "low") -> tuple[dict, dict]:
    key = _key()
    if not key:
        raise RuntimeError("No OpenAI API key is configured.")
    body = {
        "model": model_name(), "reasoning_effort": effort, "max_completion_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
    }
    response = await connector_request("openai", "POST", OPENAI_URL,
                                       headers={"Authorization": f"Bearer {key}"}, json=body, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Language model request failed (HTTP {response.status_code}).")
    payload = response.json()
    content = payload["choices"][0]["message"].get("content") or "{}"
    usage = payload.get("usage", {})
    return json.loads(content), {"model": payload.get("model", model_name()), "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}


# ---------------------------------------------------------------- geometry helpers
def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * 6371 * math.asin(min(1, math.sqrt(a)))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    p = math.pi / 180
    y = math.sin((lon2 - lon1) * p) * math.cos(lat2 * p)
    x = math.cos(lat1 * p) * math.sin(lat2 * p) - math.sin(lat1 * p) * math.cos(lat2 * p) * math.cos((lon2 - lon1) * p)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]


def _overlap(a: list[float] | None, b: list[float]) -> float:
    """Fraction of box b covered by box a (lon/lat degrees)."""
    if not a:
        return 0.0
    w, s, e, n = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if w >= e or s >= n:
        return 0.0
    return (e - w) * (n - s) / max(1e-12, (b[2] - b[0]) * (b[3] - b[1]))


# ---------------------------------------------------------------- context
async def context_near(bbox: list[float], radius_km: float = 150, timeout: float = 6) -> dict:
    """Live context around an area, from the cached public feeds. Missing feeds are reported, not filled."""
    from . import feeds
    lat, lon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
    out: dict[str, Any] = {"centre": [round(lat, 4), round(lon, 4)], "radius_km": radius_km, "unavailable": [], "sources": {}}
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    # Context is independent of the radar selection; fetch providers together.
    # A slow global archive must not withhold the area-specific measurements.
    names = ("earthquakes", "events", "fires", "nisar")
    loaders = (feeds.earthquakes, feeds.events, feeds.fires, feeds.nisar_recent)
    responses = await asyncio.gather(*(asyncio.wait_for(loader(), timeout=timeout) for loader in loaders), return_exceptions=True)
    available_feeds = {}
    for name, response in zip(names, responses):
        if isinstance(response, BaseException):
            out["unavailable"].append(name)
        else:
            available_feeds[name] = response
            out["sources"][name] = {k: response.get(k) for k in ("source", "status", "fetched_at")}
    try:
        quakes = available_feeds["earthquakes"]["items"]
        near = []
        for q in quakes:
            d = _km(lat, lon, q["lat"], q["lon"])
            if d <= radius_km * 2:
                near.append({"magnitude": q["mag"], "place": q["place"], "distance_km": round(d), "direction": _bearing(lat, lon, q["lat"], q["lon"]),
                             "days_ago": round((now_ms - q["time"]) / 86400000, 1), "depth_km": q.get("depth"), "url": q.get("url")})
        out["earthquakes"] = sorted(near, key=lambda q: (-q["magnitude"], q["distance_km"]))[:6]
    except Exception:
        if "earthquakes" not in out["unavailable"]:
            out["unavailable"].append("earthquakes")
    try:
        events = available_feeds["events"]["items"]
        near = []
        for e in events:
            d = _km(lat, lon, e["lat"], e["lon"])
            if d <= radius_km * 3:
                near.append({"title": e["title"], "category": e["category_title"], "distance_km": round(d), "direction": _bearing(lat, lon, e["lat"], e["lon"]),
                             "last_update": (e.get("date") or "")[:10], "url": e.get("source"), "provider": e.get("provider"),
                             "alert": e.get("alert"), "severity": e.get("severity")})
        out["events"] = sorted(near, key=lambda e: e["distance_km"])[:6]
    except Exception:
        if "events" not in out["unavailable"]:
            out["unavailable"].append("events")
    try:
        fires = available_feeds["fires"]["items"]
        near = [(f, _km(lat, lon, f[1], f[0])) for f in fires]
        near = [(f, d) for f, d in near if d <= radius_km]
        out["fires_24h"] = {"count": len(near), "max_frp_mw": max((f[2] for f, _ in near), default=0),
                            "nearest_km": round(min((d for _, d in near), default=0)) if near else None}
    except Exception:
        if "fires" not in out["unavailable"]:
            out["unavailable"].append("fires")
    try:
        frames = available_feeds["nisar"]["items"]
        hits = [f for f in frames if _overlap([min(c[0] for c in f["ring"]), min(c[1] for c in f["ring"]), max(c[0] for c in f["ring"]), max(c[1] for c in f["ring"])], bbox) > 0]
        out["nisar_frames_4d"] = len(hits)
    except Exception:
        if "nisar" not in out["unavailable"]:
            out["unavailable"].append("nisar")
    out["interpretation"] = "Nearby events are contextual reports, not causes of radar changes. Empty results apply only to the named feed's coverage and time window."
    return out


# ---------------------------------------------------------------- acquisition selection
def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def select_acquisitions(mode: str, observations: list[dict], bbox: list[float], preference: str = "widest_span",
                        date_from: str = "", date_to: str = "") -> tuple[list[str], str]:
    """Deterministic choice among catalog products; returns (ids, explanation) or ([], reason)."""
    usable = []
    for o in observations:
        if o.get("product") != "GCOV" or not o.get("date"):
            continue
        cover = _overlap(o.get("bbox"), bbox)
        pols = o.get("polarizations") or []
        if cover < 0.5 or not any(p in ("HH", "HHHH", "VV", "VVVV") for p in pols):
            continue
        usable.append({**o, "_cover": cover, "_original": bool(o.get("download_url")) and not o.get("local")})
    originals = [o for o in usable if o["_original"]]
    pool = originals or usable
    if not pool:
        return [], "No GCOV acquisition covers at least half of this area with HH or VV polarisation."
    rank = {"VALIDATED": 0, "PROVISIONAL": 1, "BETA": 2}
    if mode == "backscatter":
        best = sorted(pool, key=lambda o: (o["date"], -rank.get(str(o.get("maturity")).upper(), 3)), reverse=True)
        pick = next((o for o in best if o["_cover"] >= 0.95), best[0])
        return [pick["id"]], f"Most recent acquisition covering {round(pick['_cover'] * 100)}% of the area ({pick['date'][:10]}, track {pick.get('track')} {str(pick.get('direction', '')).lower()})."
    # A fair comparison needs identical geometry and processing: same track, direction, frame,
    # product maturity and processing release (the science step fails closed otherwise).
    groups: dict[tuple, list[dict]] = {}
    for o in pool:
        polarizations = tuple(sorted({p[:2] if len(p) == 4 else p for p in o.get("polarizations", []) if p in ("HH", "HHHH", "VV", "VVVV")}))
        groups.setdefault((o.get("track"), o.get("direction"), o.get("frame"), str(o.get("maturity")).upper(), o.get("product_version"), polarizations), []).append(o)
    candidates = []
    for key, items in groups.items():
        items = sorted({o["date"]: o for o in items}.values(), key=lambda o: o["date"])
        if len(items) < 2:
            continue
        if date_from or date_to:
            start = _ts(f"{date_from}T00:00:00+00:00") if date_from else _ts(items[0]["date"])
            end = _ts(f"{date_to}T00:00:00+00:00") if date_to else _ts(items[-1]["date"])
            a = min(items, key=lambda o: abs(_ts(o["date"]) - start))
            later = [o for o in items if o["date"] > a["date"]]
            if not later:
                continue
            b = min(later, key=lambda o: abs(_ts(o["date"]) - end))
        elif preference == "recent_pair":
            a, b = items[-2], items[-1]
        else:
            a, b = items[0], items[-1]
        days = (datetime.fromisoformat(b["date"].replace("Z", "+00:00")) - datetime.fromisoformat(a["date"].replace("Z", "+00:00"))).days
        candidates.append((min(a["_cover"], b["_cover"]), days, key, a, b))
    if not candidates:
        return [], "No two acquisitions of this area share a track, direction and frame, which a fair change comparison requires."
    if date_from or date_to:
        target = (_ts(f"{date_to}T00:00:00+00:00") if date_to else 0) - (_ts(f"{date_from}T00:00:00+00:00") if date_from else 0)
        candidates.sort(key=lambda c: (c[0] >= 0.95, -abs(c[1] * 86400 - target) if date_from and date_to else c[1]), reverse=True)
    else:
        candidates.sort(key=lambda c: (c[0] >= 0.95, c[1] if preference != "recent_pair" else -c[1]), reverse=True)
    cover, days, key, a, b = candidates[0]
    return [a["id"], b["id"]], (f"Same track ({key[0]}, {str(key[1] or '').lower()}, frame {key[2]}) and processing ({str(key[3]).lower()}, {key[4]}) "
                                f"on {a['date'][:10]} and {b['date'][:10]}, {days} days apart. Native alignment and quality masks will be checked before comparison.")


def heuristic_mode(question: str) -> str:
    q = question.lower()
    return "change" if re.search(r"chang|since|before|after|increas|decreas|compare|trend|grow|shrink|loss|gain|differen|over (the )?(past|last)", q) else "backscatter"


def investigation_scope(question: str) -> dict:
    """A capability boundary, not an event detector or a causal classifier."""
    q = question.lower()
    scopes = [
        ("deformation", r"earthquake|seismic|subsid|deform|uplift|sink|displacement|landslide",
         "GCOV backscatter does not measure ground motion or earthquake likelihood.",
         ["Appropriate interferometric products with quality checks and displacement corrections", "Independent geodetic measurements or authoritative event reports"]),
        ("volcanic_surface", r"volcan|lava|erupt|crater|caldera|ash",
         "Backscatter shows changes in surface roughness and cover such as new lava or ash, but not eruption rates, gas or ground deformation.",
         ["Interferometric deformation products and thermal observations", "Volcano observatory reports for the same dates"]),
        ("glacier_motion", r"glacier|ice.?flow|ice.?velocity",
         "Backscatter differences do not provide glacier velocity or displacement.",
         ["Suitable offset-tracking products with temporal baseline and uncertainty", "Independent glacier observations"]),
        ("fire_and_vegetation", r"fire|burn|deforest|vegetation|forest|logging",
         "Backscatter can identify surface differences, but cannot by itself establish burned area, vegetation loss, or a cause.",
         ["Dated optical imagery with cloud/quality masks", "Validated burn or vegetation-change analysis and independent reference data"]),
        ("flood_and_water", r"flood|water|inundat|river|lake",
         "Backscatter responds to water and surrounding surface conditions; this pipeline does not classify flood extent.",
         ["Matched pre-event and post-event acquisitions", "Water masks, terrain, hydrologic context, and independent validation"]),
        ("coastal_change", r"coast|shore|erosion|beach",
         "Backscatter changes alone do not establish shoreline position or erosion rates.",
         ["Tide-matched imagery and a validated shoreline extraction method", "Independent elevation or shoreline observations"]),
        ("urban_and_infrastructure", r"urban|city|construct|building|infrastructure|expan|sprawl|mining|\bmine\b|port|airport",
         "Bright returns indicate buildings and structures, but backscatter alone does not count buildings or establish land use.",
         ["Dated high-resolution optical imagery", "Official land-use or building records"]),
        ("agriculture", r"crop|farm|agricult|harvest|irrigat|field",
         "Backscatter follows crop structure and soil moisture through a season, but does not identify crop types or yields by itself.",
         ["A dense time series across the growing season", "Field reference data or crop maps"]),
        ("ice_and_snow", r"\bice\b|sea ice|snow|permafrost|thaw|frozen|freeze",
         "L-band backscatter responds to ice structure and melt, but this pipeline does not classify ice type, thickness or snow water equivalent.",
         ["Ice charts or optical imagery from the same dates", "Temperature records to separate melt from structural change"]),
        ("weather", r"storm|hurricane|cyclone|tornado|weather|drought|rain|temperature",
         "Radar backscatter is not a direct weather, rainfall, or temperature measurement.",
         ["Authoritative meteorological observations for the requested dates", "Validated surface-impact analysis if a radar comparison is appropriate"]),
    ]
    for kind, pattern, boundary, needed in scopes:
        if re.search(pattern, q):
            return {"topic": kind, "capability": "backscatter_evidence_with_context", "boundary": boundary,
                    "additional_evidence_needed": needed, "event_classification": "not_performed"}
    return {"topic": "surface_observation", "capability": "backscatter_evidence_with_context",
            "boundary": "The implemented measurement is calibrated radar backscatter; event type and cause are not inferred.",
            "additional_evidence_needed": ["A task-specific method and independent validation for causal or event-specific conclusions"],
            "event_classification": "not_performed"}


PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["mode", "time_preference", "date_from", "date_to", "interpretation", "can_answer", "cannot_answer"],
    "properties": {
        "mode": {"type": "string", "enum": ["backscatter", "change"]},
        "time_preference": {"type": "string", "enum": ["widest_span", "recent_pair", "latest"]},
        "date_from": {"type": "string", "description": "YYYY-MM-DD the question starts from, or empty"},
        "date_to": {"type": "string", "description": "YYYY-MM-DD the question ends at, or empty"},
        "interpretation": {"type": "string"},
        "can_answer": {"type": "string"},
        "cannot_answer": {"type": "string"},
    },
}

PLANNER = """You plan Earth-observation investigations that use NISAR L-band radar GCOV backscatter (calibrated gamma-naught, dB).
Choose:
- mode "change" when the question is about change, trends, before/after or comparison over time; otherwise "backscatter".
- time_preference: "widest_span" by default for change questions; "recent_pair" only if the user asks about the latest or most recent change;
  "latest" for single-acquisition questions.
- date_from / date_to: if the question names months, seasons or dates, give the start and end as YYYY-MM-DD (use the 15th for a month,
  and the years of the available date range); otherwise empty strings.
Write three short sentences (max 30 words each): how you interpret the question; what L-band backscatter can tell us here; what it cannot.
Be concrete and honest: backscatter responds to surface roughness, moisture, vegetation structure and open water; it does not measure
elevation or displacement, and it cannot attribute causes by itself."""


async def plan(question: str, region: str, bbox: list[float], observations: list[dict], context: dict) -> dict:
    """Coordinator: interpret the question, then pick acquisitions deterministically."""
    usage = None
    try:
        scope = investigation_scope(question)
        brief = {"question": question, "area": region, "bbox": bbox, "capability_boundary": scope, "acquisitions_available": len(observations),
                 "date_range": [min((o["date"] for o in observations if o.get("date")), default=None), max((o["date"] for o in observations if o.get("date")), default=None)],
                 "nearby": {k: context.get(k) for k in ("earthquakes", "events", "fires_24h")}}
        # Planning is a short structured choice; no extended reasoning keeps it under ~2 s.
        decided, usage = await _chat(PLANNER, json.dumps(brief, default=str), PLAN_SCHEMA, "plan", 600, effort="none")
        source = "language_model"
    except Exception as exc:
        decided = {"mode": heuristic_mode(question), "time_preference": "widest_span", "date_from": "", "date_to": "", "interpretation": "Interpreted with keyword rules because the language model was unavailable.",
                   "can_answer": "Calibrated radar backscatter statistics for the area.", "cannot_answer": "Causes cannot be attributed from backscatter alone."}
        source = "rules (language model unavailable)"
    def clean(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return ""
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return ""
        return value
    ids, why = select_acquisitions(decided["mode"], observations, bbox, decided.get("time_preference", "widest_span"),
                                   clean(decided.get("date_from")), clean(decided.get("date_to")))
    if decided["mode"] == "change" and not ids:
        fallback, why2 = select_acquisitions("backscatter", observations, bbox)
        if fallback:
            decided = {**decided, "mode": "backscatter", "cannot_answer": decided["cannot_answer"] + " A change comparison was not possible: " + why}
            ids, why = fallback, why2
    scope = investigation_scope(question)
    decided["cannot_answer"] = (decided.get("cannot_answer", "") + " " + scope["boundary"]).strip()
    return {**decided, "scope": scope, "observation_ids": ids, "selection": why, "planner": source, "usage": usage, "model": model_name() if usage else None}


# ---------------------------------------------------------------- writer + number verification
ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "answer", "evidence", "possible_explanations", "caveats", "next_steps"],
    "properties": {
        "headline": {"type": "string"},
        "answer": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "possible_explanations": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}

WRITER = """You are the synthesis agent of a scientific Earth-observation tool. Answer the user's question using ONLY the JSON evidence given.
Rules:
- Every number you write must appear in the evidence (you may round to fewer decimals). Never invent numbers, dates or places.
- Write numbers for people: at most 2 decimals for dB, 1 for percentages, thousands separators for counts and areas, use a real minus sign (−).
- Describe magnitudes, not significance: do not call a change "real", "significant" or "confirmed"; the method has no significance test.
- Use plain language for a curious non-specialist; define dB once if useful. No hype.
- headline: one sentence, max 18 words, the direct answer.
- answer: 2 short paragraphs, max 140 words total, markdown allowed (no headings).
- evidence: 2–4 bullets, each citing a specific computed value.
- possible_explanations: 0–3 hypotheses phrased as possibilities ("could", "may"), grounded in L-band physics (open water is dark/low;
  flooded forest and buildings can be bright via double bounce; wetter or rougher surfaces brighten) or in the nearby events given.
  Never state a cause as fact.
- caveats: 1–3 of the most important limitations from the evidence.
- next_steps: 1–2 concrete follow-ups (e.g. compare another date on the same track, check optical imagery).
- If the evidence cannot answer the question, say so plainly in the headline.
- Use the most decision-relevant measures for the question: for water or flooding, the low-return (water-like) area before and after and
  the newly low-return area; for buildings or urban growth, the high-return area; for any change question, the largest hotspots
  (direction, area and mean change) with their locations as "near <lat>, <lon>" using the centre values given. Say where change is
  concentrated rather than only quoting area-wide medians, which can hide localized change.
- When change.polarizations lists more than one polarization, say which one shows the change more clearly (derived.leading_polarization), citing each
  polarization's mean_abs_standardized_change (change in speckle standard deviations) or a hotspot's by_polarization values. These scale by speckle only.
  A polarization with null standardized values could not be scaled; do not rank it.
- water_context (when status is "available") splits the newly water-like area by how often JRC mapped it as water from 1984 to 2021: permanent, seasonal, or never.
  Cite the split for water or flood questions; water-like returns where it was never mapped are the stronger new-wetting signal. It is history, not proof of flooding.
- Low-return area is a proxy, not a water map: call it "water-like" or "very low return" and never "flooded area".
- surface_conditions are modelled weather at the radar acquisition dates, at one grid point near the area's centre, not ground measurements.
  If relevant, report the dated precipitation or soil moisture as a possible confounder, never as a proven cause of radar change.
  Precipitation includes snow water equivalent, not only liquid rain; its supplied window ends at the last completed model hour before acquisition.
  A null value or unavailable date means unknown, never zero. A 72-hour precipitation total is usable only when coverage is complete.
  Do not compare soil moisture between different model products or soil layers; use the supplied comparison status.
  Today's weather or a recent event is not a substitute for conditions at the radar acquisition time."""


def _numbers(value: Any, out: list[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and math.isfinite(value):
        out.append(float(value))
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers(v, out)
    elif isinstance(value, str):
        # Hyphens in a calendar date separate fields; they are not minus signs.
        text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", r"\1 \2 \3", value)
        for m in re.findall(r"-?\d+(?:\.\d+)?", text):
            out.append(float(m))


_MONTHS = {name: i for i, names in enumerate([("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
                                                ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
                                                ("october", "oct"), ("november", "nov"), ("december", "dec")], 1) for name in names}
_MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
_DATE_PATTERNS = [
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("md", re.compile(_MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", re.I)),
    ("dm", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?(?:,?\s+(\d{4})\b)?", re.I)),
    ("my", re.compile(_MONTH + r"\s+(\d{4})\b", re.I)),
]


def _check_dates(text: str, evidence: Any) -> tuple[str, int, list[str]]:
    """Dates are checked as dates ("October 25", "25 Oct 2026", "2026-10-25"), not as loose numbers."""
    known = {(int(y), int(m), int(d)) for y, m, d in re.findall(r"(\d{4})-(\d{2})-(\d{2})", json.dumps(evidence, default=str))}
    checked, unmatched = 0, []
    for kind, pattern in _DATE_PATTERNS:
        def check(match: re.Match) -> str:
            nonlocal checked
            g = match.groups()
            if kind == "iso":
                y, m, d = int(g[0]), int(g[1]), int(g[2])
            elif kind == "md":
                m, d, y = _MONTHS[g[0].lower().rstrip(".")], int(g[1]), int(g[2]) if g[2] else None
            elif kind == "dm":
                d, m, y = int(g[0]), _MONTHS[g[1].lower().rstrip(".")], int(g[2]) if g[2] else None
            else:
                m, y, d = _MONTHS[g[0].lower()], int(g[1]), None
            checked += 1
            if not any((y is None or y == ky) and m == km and (d is None or d == kd) for ky, km, kd in known):
                unmatched.append(match.group(0))
            return " " * len(match.group(0))
        text = pattern.sub(check, text)
    return text, checked, unmatched


def verify_numbers(text: str, evidence: Any) -> dict:
    """Check every number and date in generated text against the evidence, allowing display rounding."""
    known: list[float] = []
    _numbers(evidence, known)
    text, checked, unmatched = _check_dates(text, evidence)
    found = re.findall(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?", text)
    for raw in found:
        token = raw.replace("−", "-").replace(",", "")
        try:
            value = float(token)
        except ValueError:
            continue
        decimals = len(token.split(".")[1]) if "." in token else 0
        tolerance = 0.5 * 10 ** (-decimals) + 1e-9
        checked += 1
        # Preserve signs: a decrease must never pass verification as an increase.
        # Only actual display rounding is allowed, not a relative error margin.
        if not any(abs(k - value) <= tolerance for k in known):
            unmatched.append(raw)
    return {"numbers_checked": checked, "unmatched": unmatched, "status": "all_traced" if not unmatched else "untraced_numbers",
            "scope": "Numeric consistency only; not validation of meaning, causation, or scientific significance."}


def _rounded(value: Any) -> Any:
    """Round evidence for display so the model cannot print false precision."""
    if isinstance(value, bool) or not isinstance(value, (float, dict, list)):
        return value
    if isinstance(value, float):
        return round(value, 2) if abs(value) < 1000 else round(value, 1)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return [_rounded(v) for v in value]


def _evidence(investigation: dict, context: dict | None) -> dict:
    result = investigation.get("result") or {}
    measurements = result.get("measurements", {})
    inputs = [{"date": (i.get("metadata") or {}).get("date"), "track": (i.get("metadata") or {}).get("track"), "direction": (i.get("metadata") or {}).get("direction"),
               "polarization": (i.get("metadata") or {}).get("selected_polarization"), "maturity": (i.get("catalog") or {}).get("maturity"),
               "original_product": not (i.get("metadata") or {}).get("derivative", False)} for i in (result.get("provenance") or {}).get("inputs", [])]
    trimmed = json.loads(json.dumps(measurements))
    for key in ("observations",):
        for o in trimmed.get(key, []) or []:
            o.pop("histogram", None)
    if isinstance(trimmed.get("change"), dict):
        trimmed["change"].pop("histogram", None)
    # Facts derived deterministically from the inputs, so a written answer may cite them.
    derived: dict[str, Any] = {}
    dates = sorted(d for d in (i.get("date") for i in inputs) if d)
    if len(dates) >= 2:
        derived["acquisition_interval_days"] = (datetime.fromisoformat(dates[-1].replace("Z", "+00:00")) - datetime.fromisoformat(dates[0].replace("Z", "+00:00"))).days
    metas = [(i.get("metadata") or {}) for i in (result.get("provenance") or {}).get("inputs", [])]
    if metas:
        derived["multilook_factor"] = max(int(m.get("multilook") or 1) for m in metas)
        if metas[0].get("pixel_spacing"):
            derived["pixel_spacing_m"] = metas[0]["pixel_spacing"][0]
    # Net changes people naturally ask for, so a written answer never has to do its own arithmetic.
    change = measurements.get("change") or {}
    before, after = change.get("low_return_before_km2"), change.get("low_return_after_km2")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        derived["low_return_net_change_km2"] = round(after - before, 2)
        derived["low_return_net_change_size_km2"] = round(abs(after - before), 2)
        if before > 0:
            derived["low_return_net_change_pct_of_before"] = round((after - before) / before * 100, 1)
            derived["low_return_net_change_size_pct_of_before"] = round(abs(after - before) / before * 100, 1)
    up, down = change.get("increasing_km2"), change.get("decreasing_km2")
    if isinstance(up, (int, float)) and isinstance(down, (int, float)):
        derived["changed_by_threshold_km2"] = round(up + down, 2)
    if change.get("leading_polarization"):
        derived["leading_polarization"] = change["leading_polarization"]
    if change.get("hotspots") is not None:
        derived["hotspot_count"] = len(change["hotspots"])
        # Prose can legitimately say "darker by 5 dB" for a signed -5 dB
        # change. Make that magnitude an explicit derived fact; never turn
        # the numeric verifier into a blanket absolute-value comparison.
        derived["hotspot_change_magnitudes_db"] = [
            round(abs(spot["mean_change_db"]), 2)
            for spot in change["hotspots"]
            if isinstance(spot.get("mean_change_db"), (int, float)) and math.isfinite(spot["mean_change_db"])
        ]
    return {"question": investigation.get("question"), "area": investigation.get("title"), "bbox": investigation.get("bbox"), "method": investigation.get("mode"),
            "measurements": _rounded(trimmed), "inputs": inputs, "limitations": (result.get("validation") or {}).get("limitations", []),
            "observation_count": len(inputs), "derived": derived, "capability_boundary": investigation_scope(investigation.get("question") or ""),
            "plan": {k: (investigation.get("plan") or {}).get(k) for k in ("interpretation", "can_answer", "cannot_answer", "selection")},
            "nearby_context": context or {}, "surface_conditions": result.get("surface_conditions") or investigation.get("surface_conditions") or {},
            "water_context": result.get("water_context") or investigation.get("water_context"),
            "context_timing_note": "Recent event feeds may be months apart from the radar acquisitions. Spatial proximity alone is not temporal coincidence or causal evidence."}


async def write_answer(investigation: dict, context: dict | None) -> dict:
    evidence = _evidence(investigation, context)
    brief = json.dumps(evidence, default=str)
    for attempt in range(2):
        answer, usage = await _chat(WRITER, brief, ANSWER_SCHEMA, "answer", 1800)
        required_caveats = [evidence["capability_boundary"]["boundary"]]
        if evidence.get("surface_conditions", {}).get("acquisitions"):
            required_caveats.append("Weather values are modelled at the area centre, not ground measurements or evidence of causation.")
        answer["caveats"] = [*required_caveats, *answer["caveats"]][:4]
        text = " ".join([answer["headline"], answer["answer"], *answer["evidence"], *answer["possible_explanations"], *answer["caveats"], *answer["next_steps"]])
        verification = verify_numbers(text, _verification_evidence(evidence))
        if not verification["unmatched"]:
            break
        # One corrective rewrite: name the untraced values instead of silently discarding the answer.
        brief = json.dumps({**evidence, "rewrite_instruction": (
            "A previous draft used values that are not in this evidence: " + ", ".join(verification["unmatched"][:8]) +
            ". Write the answer again using only values that appear in the evidence (including 'derived'); do not compute new numbers.")}, default=str)
    if verification["unmatched"]:
        raise ValueError("The generated answer included numbers that could not be traced to evidence. The computed report remains available.")
    return {**answer, "verification": verification, "model": usage.get("model"), "usage": usage,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _verification_evidence(evidence: dict) -> dict:
    # A user's question, an earlier model answer, or a plan is not evidence.
    verified = {k: evidence[k] for k in ("measurements", "inputs", "observation_count", "derived", "nearby_context", "water_context") if k in evidence}
    conditions = evidence.get("surface_conditions") or {}
    # Source prose/URLs are provenance, not a pool of numbers for the writer.
    if conditions:
        verified["surface_conditions"] = {"acquisitions": [{
            key: value for key, value in item.items()
            if key in {"date", "model_time", "model_time_offset_minutes", "grid_centre", "resolution",
                       "precipitation_72h_mm", "precipitation_window_start", "precipitation_window_end",
                       "rain_72h_mm", "rain_observed_hours", "rain_expected_hours", "rain_coverage_pct",
                       "soil_moisture_m3m3", "soil_layer", "temperature_c", "wind_kmh", "relative_humidity_pct"}
        } for item in conditions.get("acquisitions", [])], "comparison": conditions.get("comparison", {})}
    return verified


def condition_bullets(conditions: dict | None) -> list[str]:
    """Dated model values; never infer wet/dry conditions from missing data."""
    if not conditions:
        return []
    bullets = []
    for item in conditions.get("acquisitions", [])[:2]:
        values = []
        rain = item.get("rain_72h_mm")
        if isinstance(rain, (int, float)) and math.isfinite(rain) and item.get("rain_observed_hours") == item.get("rain_expected_hours") == 72:
            values.append(f"preceding 72-hour precipitation {rain:.1f} mm")
        soil = item.get("soil_moisture_m3m3")
        if isinstance(soil, (int, float)) and math.isfinite(soil):
            values.append(f"soil moisture {soil:.3f} m³/m³ ({item.get('soil_layer', 'layer unspecified')})")
        if values:
            bullets.append(f"Modelled conditions on {str(item.get('date', ''))[:10]}: {'; '.join(values)}.")
    return bullets


def measured_answer(investigation: dict, context: dict | None = None) -> dict:
    """Immediate deterministic answer from measurements, also used on LLM failure."""
    evidence = _evidence(investigation, context)
    measurements = evidence["measurements"]
    change = measurements.get("change")
    observations = measurements.get("observations") or []
    scope = evidence["capability_boundary"]
    if change:
        headline = "The radar measured a backscatter difference; its cause remains unresolved."
        answer = (f"Median later-minus-earlier backscatter was {change['median_db']:.2f} dB across {change['valid_pixels']:,} common valid pixels. "
                  f"{change['threshold_exceedance_pct']:.1f}% exceeded the descriptive magnitude threshold. This is not a significance test.\n\n" + scope["boundary"])
        bullets = [f"Comparison uses {change['valid_pixels']:,} common valid pixels.",
                   f"Median difference: {change['median_db']:.2f} dB."]
        spots = change.get("hotspots") or []
        if spots:
            top = spots[0]
            word = "brighter" if top["direction"] == "increase" else "darker"
            where = f"near {top['centre'][0]:.3f}, {top['centre'][1]:.3f}"
            area = f"{top['area_km2']:.2f} km² " if top.get("area_km2") is not None else ""
            answer += (f"\n\nThe largest changed patch is {area}{where}, {word} by {abs(top['mean_change_db']):.1f} dB on average; "
                       f"{len(spots)} patches are listed in the report.")
            bullets.append(f"Largest patch: {area}{where}, mean change {top['mean_change_db']:.1f} dB.")
        water = evidence.get("water_context") or {}
        if water.get("status") == "available" and water["mapped_pixels"]:
            parts = [f"{water[k]['pct_of_mapped']:.1f}% {label}" for k, label in (("permanent", "permanent water"), ("seasonal", "seasonal water"), ("never", "never mapped as water"))
                     if water[k].get("pct_of_mapped") is not None]
            bullets.append("Newly water-like area by JRC water history (1984-2021): " + ", ".join(parts) + ".")
        scored = [r for r in change.get("polarizations") or [] if r.get("mean_abs_standardized_change") is not None]
        if change.get("leading_polarization") and len(scored) > 1:
            bullets.append("Change in speckle standard deviations (mean absolute): " + ", ".join(f"{r['polarization']} {r['mean_abs_standardized_change']:.1f}" for r in scored)
                           + f"; {change['leading_polarization']} shows it more clearly.")
        if change.get("low_return_before_km2") is not None and change.get("low_return_after_km2") is not None:
            bullets.append(f"Water-like (very low return) area: {change['low_return_before_km2']:.2f} km² before, {change['low_return_after_km2']:.2f} km² after.")
    elif observations:
        observation = observations[0]
        headline = "One radar acquisition describes this surface; temporal change is not established."
        answer = (f"Median backscatter was {observation['median_db']:.2f} dB across {observation['valid_pixels']:,} valid pixels. "
                  "A matched second acquisition is needed to measure change.\n\n" + scope["boundary"])
        bullets = [f"Valid pixels: {observation['valid_pixels']:,}.", f"Median backscatter: {observation['median_db']:.2f} dB."]
    else:
        headline, answer, bullets = "No computed radar measurement is available.", scope["boundary"], []
    bullets.extend(condition_bullets(evidence.get("surface_conditions")))
    narrative = {"headline": headline, "answer": answer, "evidence": bullets, "possible_explanations": [],
                 "caveats": evidence["limitations"][:3], "next_steps": scope["additional_evidence_needed"],
                 "model": "deterministic computed summary", "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if evidence.get("surface_conditions", {}).get("acquisitions"):
        narrative["caveats"].insert(0, "Weather values are modelled at the area centre, not ground measurements or evidence of causation.")
    narrative["verification"] = verify_numbers(" ".join([headline, answer, *bullets]), _verification_evidence(evidence))
    return narrative


FOLLOWUP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}
FOLLOWUP = """Answer a follow-up question about a completed radar investigation using ONLY the JSON evidence (measurements, inputs,
limitations, earlier answer, nearby context). Max 120 words, plain language, markdown allowed. Every number must come from the evidence.
Weather in surface_conditions is modelled context at the radar dates, not ground observations or proof of a cause. Do not compare different
soil layers/models. Null weather values are unknown, never zero. If the evidence cannot answer, say what additional analysis would."""


async def follow_up(investigation: dict, question: str, context: dict | None) -> dict:
    evidence = {**_evidence(investigation, context), "earlier_answer": (investigation.get("result") or {}).get("narrative")}
    answer, usage = await _chat(FOLLOWUP, json.dumps({"follow_up": question, "evidence": evidence}, default=str), FOLLOWUP_SCHEMA, "followup", 900)
    verification = verify_numbers(answer["answer"], _verification_evidence(evidence))
    if verification["unmatched"]:
        raise ValueError("The follow-up included numbers that could not be traced to evidence.")
    return {"question": question, "answer": answer["answer"], "verification": verification, "model": usage.get("model"),
            "asked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
