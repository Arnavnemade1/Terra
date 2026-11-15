import { Fragment, useEffect, useMemo, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Activity, ArrowRight, Check, ChevronDown, Clock3, Cpu, GitBranch, Layers, Plane, RefreshCw, Search, ShieldCheck, X } from 'lucide-react';
import { api } from './types';
import './agentOperations.css';

type Availability = 'working' | 'ready' | 'waiting' | 'blocked' | 'stale' | 'unavailable' | 'finished' | 'failed';
type Agent = { id: string; run_id: string; kind: 'coordinator' | 'aircraft' | 'watch' | 'investigation'; label: string; status: string; availability: Availability; stage: string; progress: number | null; started_at: string; updated_at: string | null; closed_at: string | null; heartbeat_age_s: number | null; next_s: number | null; next_at: string | null; round: number | null; task: string | null; eligible_jobs: number | null; offered_jobs: number | null; details_live: boolean; events: {stage?: string; status: string; at: string}[]; activities: {name: string; label: string; state: string; attempt: number; heartbeat_age_s: number | null}[] };
type Snapshot = { observed_at: string; stale: boolean; limited: boolean; temporal: {connected: boolean; queue: string; worker_available: boolean; workflow_pollers: number | null; activity_pollers: number | null; worker_heartbeat: boolean}; summary: {total: number; running: number; working: number; ready: number; waiting: number; attention: number}; agents: Agent[] };
const titles = {coordinator: 'Fleet coordination', aircraft: 'Aircraft agent', watch: 'Area watch', investigation: 'Investigation'};
const icons = {coordinator: GitBranch, aircraft: Plane, watch: Clock3, investigation: Layers};
const labels: Record<Availability, string> = {working: 'Working', ready: 'Available', waiting: 'Waiting', blocked: 'Needs input', stale: 'Heartbeat late', unavailable: 'Unavailable', finished: 'Finished', failed: 'Failed'};
const words = (s: string) => s.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const age = (s: number | null) => s === null ? '—' : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`;
const time = (s: string | null) => s ? new Date(s).toLocaleString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';

export default function AgentOperations() {
  const [open, setOpen] = useState(false);
  useEffect(() => { document.dispatchEvent(new CustomEvent('terra:operations', { detail: open })); return () => { document.dispatchEvent(new CustomEvent('terra:operations', { detail: false })); }; }, [open]);
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><button className="agents-trigger" aria-label="Open agent operations" title="Agent operations"><Activity size={15}/><span>Agents</span></button></Dialog.Trigger>
    {open && <Dialog.Portal><Dialog.Overlay className="agents-scrim"/><Dialog.Content className="agent-operations" aria-describedby="agents-description"><Operations/><Dialog.Close className="agents-close icon-btn" aria-label="Close agent operations"><X size={19}/></Dialog.Close></Dialog.Content></Dialog.Portal>}
  </Dialog.Root>;
}

function Operations() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [filter, setFilter] = useState('active');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const update = async () => {
      if (document.hidden) { timer = setTimeout(update, 5000); return; }
      try { const value = await api<Snapshot>('/agents', {signal: controller.signal}); if (!controller.signal.aborted) { setData(value); setError(''); } }
      catch { if (!controller.signal.aborted) setError('Agent status could not be refreshed. The last snapshot is retained.'); }
      if (!controller.signal.aborted) timer = setTimeout(update, 5000);
    };
    void update(); return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);
  const counts = useMemo(() => {
    const n = {coordinator:0,aircraft:0,watch:0,investigation:0};
    data?.agents.filter(a => a.status === 'running').forEach(a => n[a.kind]++); return n;
  }, [data]);
  const rows = (data?.agents || []).filter(a => filter === 'active' ? a.status === 'running' : filter === 'attention' ? ['blocked','stale','unavailable','failed'].includes(a.availability) : true)
    .filter(a => `${a.label} ${a.id} ${titles[a.kind]} ${a.stage}`.toLowerCase().includes(query.toLowerCase()));
  const t = data?.temporal;
  const detail = data?.agents.find(a => `${a.id}:${a.run_id}` === selected);
  return <>
    <header className="agents-heading"><div className="agents-eyebrow"><Activity size={14}/> LIVE OPERATIONS <span>Temporal</span></div>
      <Dialog.Title>Agent operations</Dialog.Title>
      <Dialog.Description id="agents-description">Follow durable workflows from satellite acquisition to verified evidence and fleet coordination.</Dialog.Description>
      <div className={`agents-service ${t?.worker_available && !error ? 'available' : 'unavailable'}`}><i/>
        <strong>{!data ? 'Connecting to Temporal…' : t?.worker_available ? 'Workers accepting work' : t?.connected ? 'No available workers confirmed' : 'Temporal unavailable'}</strong>
        {data && <span>Observed {new Date(data.observed_at).toLocaleTimeString()} · updates every 5s</span>}
      </div>
    </header>
    {(error || data?.stale) && <div role="status" className="agents-alert">{error || 'Temporal is unreachable. These are the last observed statuses; current availability is unknown.'}<button onClick={() => setRefresh(r => r + 1)}>Retry</button></div>}
    <div className="agents-metrics" aria-label="Agent summary">{[['Durable workflows', data?.summary.running],['Working',data?.summary.working],['Available',data?.summary.ready],['Waiting',data?.summary.waiting],['Need attention',data?.summary.attention]].map(([label,value]) => <div key={label}><span>{label}</span><strong>{value ?? '—'}</strong></div>)}</div>
    <div className="agents-flow" aria-label="Durable workflow architecture">
      <Flow icon={Clock3} name="Area watches" count={counts.watch} sub="Pass → acquisition"/><ArrowRight/>
      <Flow icon={Layers} name="Investigations" count={counts.investigation} sub="Measure → verify → report"/><ArrowRight/>
      <Flow icon={ShieldCheck} name="Human approval" sub="Required for watch dispatch"/><ArrowRight/>
      <Flow icon={GitBranch} name="Coordinator" count={counts.coordinator} sub={`${counts.aircraft} aircraft agents`}/>
    </div>
    <div className={`agents-workspace ${selected ? 'has-selection' : ''}`}>
      <section className="agents-registry" aria-label="Agent registry">
        <div className="agents-toolbar"><div className="agents-filters" aria-label="Filter agents">{[['active','Active'],['attention','Needs attention'],['all','Recent history']].map(([id,label]) => <button key={id} aria-pressed={filter === id} onClick={() => setFilter(id)}>{label}</button>)}</div>
          <button className="icon-btn" onClick={() => setRefresh(r => r + 1)} aria-label="Refresh agent statuses"><RefreshCw size={15}/></button>
        </div>
        <label className="agents-search"><Search size={15}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Find an agent, area, or workflow…" aria-label="Find an agent"/></label>
        <div className="agents-columns" aria-hidden="true"><span>Agent / workflow</span><span>Status</span><span>Heartbeat</span></div>
        <div className="agents-rows">{rows.map(a => { const Icon = icons[a.kind]; return <Fragment key={`${a.id}-${a.run_id}`}><button className={`agent-row ${selected === `${a.id}:${a.run_id}` ? 'selected' : ''}`} onClick={() => setSelected(selected === `${a.id}:${a.run_id}` ? null : `${a.id}:${a.run_id}`)} aria-expanded={selected === `${a.id}:${a.run_id}`}>
          <span className={`agent-glyph ${a.kind}`}><Icon size={18}/></span><span className="agent-identity"><b>{a.label}</b><small>{titles[a.kind]} · {words(a.stage)}</small></span>
          <span className={`agent-status ${a.availability}`}><i/>{labels[a.availability]}</span><span className="agent-heartbeat">{age(a.heartbeat_age_s)}</span><ChevronDown size={14}/>
        </button>{selected === `${a.id}:${a.run_id}` && <div className="agent-inline-details"><Details agent={a}/></div>}</Fragment>; })}
        {!rows.length && <div className="agents-empty"><Cpu size={26}/><b>{!data ? 'Reading workflow states…' : 'No agents in this view'}</b><p>{!data ? 'Connecting to the task queue and execution registry.' : filter === 'active' ? 'Launch a fleet mission, start an investigation, or watch an area to create durable work.' : 'Try another filter or search.'}</p></div>}
        </div>
        {data?.limited && <p className="agents-footnote">Showing the first 200 active workflows. Narrow this view as the registry grows.</p>}
        <p className="agents-footnote">Recent history includes up to 30 executions, including completed runs. Heartbeats apply to fleet agents; other workflows report activity state.</p>
      </section>
      <aside className="agent-inspector">{detail ? <Details agent={detail}/> : <><div className="inspector-title"><Cpu size={18}/><h3>Worker availability</h3></div><p>Available means Temporal has recently observed workers polling for both workflows and activities.</p><dl><dt>Workflow pollers</dt><dd>{t?.workflow_pollers ?? 'Unknown'}</dd><dt>Activity pollers</dt><dd>{t?.activity_pollers ?? 'Unknown'}</dd><dt>Runtime heartbeat</dt><dd>{t?.worker_heartbeat ? 'Fresh' : 'Not confirmed'}</dd><dt>Task queue</dt><dd>{t?.queue || '—'}</dd></dl><div className="agent-durability"><ShieldCheck size={20}/><b>Work survives restarts</b><p>Temporal persists progress, retries failed activities, and resumes waiting workflows. Select an agent to inspect its execution.</p></div><RenderReadout/></> }</aside>
    </div>
  </>;
}
function Flow({icon: Icon,name,sub,count}:{icon:typeof Activity;name:string;sub:string;count?:number}) { return <div className="agents-flow-step"><Icon size={17}/><span><b>{name}{count !== undefined && <em>{count}</em>}</b><small>{sub}</small></span></div>; }
function Details({agent:a}:{agent:Agent}) {
  const stages = a.kind === 'investigation' ? ['discover','prepare','analyze','verify','synthesize','complete'] : a.kind === 'watch' ? ['waiting_pass','checking','investigating','proposal','dispatched'] : [];
  const current = stages.indexOf(a.stage);
  return <><div className="inspector-title"><Activity size={18}/><h3>{a.label}</h3></div><span className={`agent-status ${a.availability}`}><i/>{labels[a.availability]}</span>
    <dl><dt>Workflow state</dt><dd>{words(a.status)}</dd><dt>Current step</dt><dd>{words(a.stage)}</dd><dt>Started</dt><dd>{time(a.started_at)}</dd>{a.task && <><dt>Assignment</dt><dd>{a.task}</dd></>}{a.eligible_jobs !== null && <><dt>Eligible jobs</dt><dd>{a.eligible_jobs} / {a.offered_jobs}</dd></>}{a.round !== null && <><dt>Coordination round</dt><dd>{a.round}</dd></>}{a.next_s !== null && <><dt>Next check</dt><dd>Within {age(a.next_s)}</dd></>}{a.next_at && <><dt>Next satellite pass</dt><dd>{time(a.next_at)}</dd></>}</dl>
    {stages.length > 0 && <ol className="agent-steps" aria-label="Workflow stages">{stages.map((s,i) => <li key={s} className={i === current ? 'current' : i < current ? 'done' : ''}><i>{i < current ? <Check size={11}/> : i + 1}</i><span>{words(s)}</span></li>)}</ol>}
    <h4>Current activities</h4>{a.activities.length ? a.activities.map((v,i) => <div key={`${v.name}-${i}`} className="agent-activity"><b>{v.label}</b><small>{words(v.state)} · attempt {v.attempt}{v.heartbeat_age_s !== null && ` · heartbeat ${age(v.heartbeat_age_s)} ago`}</small></div>) : <p>{!a.details_live && a.status === 'running' ? 'Live activity details could not be confirmed yet.' : a.status === 'running' ? 'No activity executing at this instant. The workflow may be waiting on a timer, signal, or child workflow.' : 'This execution has ended.'}</p>}
    <details className="agent-execution"><summary>Execution identifiers</summary><label>Workflow</label><code>{a.id}</code><label>Run</label><code>{a.run_id}</code></details>
    {a.events.length > 0 && <><h4>Recent checkpoints</h4><ul className="agent-checkpoints">{a.events.slice().reverse().map((e,i) => <li key={i}><span>{words(e.stage || e.status)}</span><time>{time(e.at)}</time></li>)}</ul></>}
  </>;
}
function RenderReadout() {
  const [marks,setMarks] = useState<PerformanceEntry[]>([]);
  useEffect(() => {const read=()=>setMarks(performance.getEntriesByType('mark').filter(m=>m.name.startsWith('terra:')));read();const timer=setInterval(read,2000);return()=>clearInterval(timer);},[]);
  return <div className="agents-render"><h4>Scene performance</h4>{[['terra:first-frame','First frame'],['terra:map-ready','Map ready'],['terra:terrain-ready','3D terrain']].map(([id,label]) => {const m=marks.find(x=>x.name===id);return <div key={id}><span>{label}</span><b>{m ? `${(m.startTime/1000).toFixed(1)}s` : '—'}</b></div>;})}<div><span>Photoreal load</span><b>{performance.getEntriesByName('terra:photoreal-load').length ? `${(performance.getEntriesByName('terra:photoreal-load').at(-1)!.duration / 1000).toFixed(1)}s` : '—'}</b></div><small>Measured in this browser, from navigation (Photoreal: from selection). Target: map ready within 15s.</small></div>;
}
