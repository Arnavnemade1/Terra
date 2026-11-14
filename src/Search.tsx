import { useEffect, useMemo, useRef, useState } from 'react';
import { CornerDownLeft, LoaderCircle, Search as SearchIcon, X } from 'lucide-react';
import { decimal, dmsPretty, mgrs, parseCoordinates } from './geo';
import { api, prettyDate, REGIONS, type Bounds, type GeoResult, type Investigation, type Region } from './types';

export type Hazard = {
  id: string; title: string; category: string; category_title: string; lon: number; lat: number; date: string;
  provider?: string; alert?: string | null; severity?: string | null; country?: string | null; source?: string; summary?: string;
};

type Option = { key: string; group: string; title: string; detail: string; aside?: string; dot?: string; choose: () => void };
type Props = {
  inputRef: React.RefObject<HTMLInputElement | null>;
  hazards: Hazard[]; jobs: Investigation[]; hazardColor: (h: Hazard) => string;
  onRegion: (region: Region, fly: Bounds | null) => void; onJob: (job: Investigation) => void;
  /** Rendered inside a popover: always open, results in normal flow, focused on mount. */
  inline?: boolean;
};

const RECENT_KEY = 'terra.recentPlaces';
export function boxAround(lon: number, lat: number, half = 0.15): Bounds {
  return [Math.max(-180, lon - half), Math.max(-89, lat - half * 0.85), Math.min(180, lon + half), Math.min(89, lat + half * 0.85)];
}
function readRecent(): Region[] {
  try { const v = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); return Array.isArray(v) ? v.filter(r => r && Array.isArray(r.bbox)).slice(0, 5) : []; } catch { return []; }
}
function remember(region: Region) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([region, ...readRecent().filter(r => r.name !== region.name)].slice(0, 5))); } catch { /* per-viewer convenience only */ }
}
const words = (s: string) => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');

/** One field for places, coordinates in any common format, live hazards, featured sites and saved work. */
export default function Search({ inputRef, hazards, jobs, hazardColor, onRegion, onJob, inline = false }: Props) {
  const [q, setQ] = useState('');
  const [openState, setOpen] = useState(false);
  const open = inline || openState;
  useEffect(() => { if (inline) inputRef.current?.focus(); }, [inline, inputRef]);
  const [places, setPlaces] = useState<GeoResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [recent, setRecent] = useState<Region[]>(readRecent);
  const list = useRef<HTMLDivElement>(null);
  const query = q.trim();
  const parsed = useMemo(() => parseCoordinates(query), [query]);

  useEffect(() => {
    setCursor(0); setFailed(false);
    if (parsed || query.length < 2) { setPlaces([]); setBusy(false); return; }
    const controller = new AbortController(); setBusy(true);
    const timer = setTimeout(() => {
      api<{ items: GeoResult[] }>(`/geocode?q=${encodeURIComponent(query)}`, { signal: controller.signal })
        .then(v => setPlaces(v.items))
        .catch(() => { if (!controller.signal.aborted) { setPlaces([]); setFailed(true); } })
        .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    }, 220);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, parsed]);

  const close = () => { setOpen(false); setQ(''); inputRef.current?.blur(); };
  const go = (region: Region, fly: Bounds | null) => { remember(region); setRecent(readRecent()); onRegion(region, fly); close(); };

  const options: Option[] = useMemo(() => {
    const out: Option[] = [];
    const needle = words(query);
    if (parsed) {
      const name = decimal(parsed.lat, parsed.lon, 4);
      out.push({ key: 'coord', group: 'Coordinates', title: name, detail: `${parsed.format} · ${dmsPretty(parsed.lat, parsed.lon)} · ${mgrs(parsed.lat, parsed.lon)}`,
        aside: parsed.note ? 'Swapped' : undefined,
        choose: () => go({ name, context: parsed.note || parsed.format, bbox: boxAround(parsed.lon, parsed.lat), tag: 'Coordinates' }, boxAround(parsed.lon, parsed.lat)) });
      return out;
    }
    if (!needle) {
      recent.forEach((r, i) => out.push({ key: `recent-${i}`, group: 'Recent', title: r.name, detail: r.context, choose: () => go(r, r.bbox) }));
      REGIONS.slice(0, 6).forEach(r => out.push({ key: `site-${r.name}`, group: 'Featured sites', title: r.name, detail: `${r.tag} · ${r.context}`, choose: () => go(r, r.bbox) }));
      return out;
    }
    places.forEach((g, i) => {
      const small = g.bbox && g.bbox[2] - g.bbox[0] <= 0.6 && g.bbox[3] - g.bbox[1] <= 0.6 && g.bbox[2] - g.bbox[0] > 0.02;
      const bbox: Bounds = small ? g.bbox! : boxAround(g.lon, g.lat, 0.2);
      out.push({ key: `place-${i}`, group: 'Places', title: g.name, detail: g.context || decimal(g.lat, g.lon, 2), aside: g.kind,
        choose: () => go({ name: g.name, context: g.context || decimal(g.lat, g.lon, 3), bbox, tag: g.kind || 'Place' }, g.bbox && !small ? g.bbox : bbox) });
    });
    hazards.filter(h => words(`${h.title} ${h.country || ''} ${h.category_title}`).includes(needle)).slice(0, 4).forEach(h => out.push({
      key: `hazard-${h.id}`, group: 'Live hazards', title: h.title, dot: hazardColor(h),
      detail: [h.alert ? `${h.alert[0].toUpperCase()}${h.alert.slice(1)} alert` : null, h.category_title, h.date ? prettyDate(h.date) : null].filter(Boolean).join(' · '),
      choose: () => go({ name: h.title.slice(0, 60), context: [h.category_title, h.provider].filter(Boolean).join(' · '), bbox: boxAround(h.lon, h.lat, 0.15), tag: h.category_title }, boxAround(h.lon, h.lat, 0.15)) }));
    REGIONS.filter(r => words(`${r.name} ${r.context} ${r.tag}`).includes(needle)).forEach(r => out.push({
      key: `site-${r.name}`, group: 'Featured sites', title: r.name, detail: `${r.tag} · ${r.context}`, choose: () => go(r, r.bbox) }));
    jobs.filter(j => words(`${j.question} ${j.title}`).includes(needle)).slice(0, 3).forEach(j => out.push({
      key: `job-${j.id}`, group: 'Saved investigations', title: j.question || j.title, detail: `${j.title.split(' · ')[0]} · ${prettyDate(j.created_at)}`,
      choose: () => { onJob(j); close(); } }));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, parsed, places, hazards, jobs, recent]);

  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }); }, [cursor]);
  const empty = !busy && query.length >= 2 && !options.length;
  let lastGroup = '';

  return <div className={`search ${open ? 'open' : ''} ${inline ? 'inline' : ''}`}>
    <div className="search-field">
      <SearchIcon size={16} aria-hidden="true" />
      <input id="place-search" ref={inputRef} value={q} role="combobox" aria-expanded={open} aria-controls="search-results" aria-autocomplete="list"
        aria-activedescendant={open && options[cursor] ? `search-${options[cursor].key}` : undefined} autoComplete="off" spellCheck={false}
        placeholder="Search a place, coordinates or a hazard" aria-label="Search places, coordinates, hazards and saved investigations"
        onChange={e => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={e => {
          if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setCursor(c => Math.min(c + 1, Math.max(0, options.length - 1))); }
          else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor(c => Math.max(0, c - 1)); }
          else if (e.key === 'Enter') { e.preventDefault(); options[cursor]?.choose(); }
          else if (e.key === 'Escape') close();
        }} />
      {busy ? <LoaderCircle size={15} className="spin quiet" aria-label="Searching" />
        : q ? <button className="icon-btn" onMouseDown={e => e.preventDefault()} onClick={() => { setQ(''); inputRef.current?.focus(); }} aria-label="Clear search"><X size={14} /></button>
        : <kbd aria-hidden="true">/</kbd>}
    </div>
    {open && <div className="search-results" id="search-results" role="listbox" ref={list} aria-label="Search results">
      {options.map((o, i) => {
        const header = o.group !== lastGroup ? o.group : null; lastGroup = o.group;
        return <div key={o.key} role="presentation">
          {header && <div className="search-group" role="presentation">{header}</div>}
          <button id={`search-${o.key}`} role="option" aria-selected={cursor === i} className="search-option"
            onMouseEnter={() => setCursor(i)} onMouseDown={e => e.preventDefault()} onClick={o.choose}>
            {o.dot && <i className="dot" style={{ background: o.dot }} aria-hidden="true" />}
            <span className="search-text"><span className="search-title">{o.title}</span><span className="search-detail">{o.detail}</span></span>
            {o.aside && <span className="search-aside">{o.aside}</span>}
            {cursor === i && <CornerDownLeft size={13} className="search-enter" aria-hidden="true" />}
          </button>
        </div>;
      })}
      {failed && <p className="search-note">Place search is unavailable right now. Coordinates and featured sites still work.</p>}
      {empty && !failed && <p className="search-note">Nothing matches “{query}”. Check the spelling, try a nearby town, or paste coordinates.</p>}
      <p className="search-help">Coordinates work in any common form: <span className="num">19.41, −155.28</span> · <span className="num">19°24′N 155°17′W</span> · <span className="num">5QKB 6021 4743</span> · a map link.</p>
    </div>}
  </div>;
}
