import * as C from 'cesium';

/**
 * Where map tiles are requested from.
 *
 * Browsers open at most six HTTP/1.1 connections per host. With every tile and every API call on one host, a few
 * slow tiles held the whole map back: measured, the slowest tiles spent ~53 s queued in the browser and under a
 * second at the server. Names under .localhost always resolve to this machine, so on a local workspace imagery is
 * spread over four of them ({s} = t1…t4) and terrain gets its own, each with its own six connections, while API
 * calls keep the page's host to themselves. One probe at startup checks that the names work; otherwise (another
 * browser setup, or a deployment that is not on localhost) every URL stays same-origin.
 */
const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
const SUBDOMAINS = ['t1', 't2', 't3', 't4'];
let sharded = false;

const origin = (host: string) => `${location.protocol}//${host}.localhost${location.port ? `:${location.port}` : ''}`;

/** Resolves once the tile hosts are known (at most 2 s; same-origin if the probe fails). The probe asks the tile
 * gateway itself, the same path every tile takes, which answers without touching Temporal or the database. */
export const tilesReady: Promise<boolean> = (LOCAL
  ? fetch(`${origin('t1')}/api/connector`, { cache: 'no-store', signal: AbortSignal.timeout(2000) }).then(r => r.ok).catch(() => false)
  : Promise.resolve(false)
).then(ok => { sharded = ok; return ok; });

/** Keep requests in Cesium's own priority queue (nearest tiles first, cancellable) instead of the browser's FIFO. */
export function configureRequests() {
  C.RequestScheduler.maximumRequestsPerServer = 6;
  C.RequestScheduler.maximumRequests = 48;
}

/** An imagery tile template for the gateway, e.g. imagery('maps/imagery/{z}/{y}/{x}'). */
export function imagery(path: string): { url: string; subdomains?: string[] } {
  const p = `/api/connector/${path}`;
  return sharded ? { url: `${location.protocol}//{s}.localhost${location.port ? `:${location.port}` : ''}${p}`, subdomains: SUBDOMAINS } : { url: p };
}

/** The terrain service, on a host of its own: refinement anywhere waits on terrain first. */
export function terrainUrl(): string {
  return sharded ? `${origin('terrain')}/api/connector/terrain` : '/api/connector/terrain';
}

/** Mesh downloads must not occupy the page's connections while camera workers and API calls start. */
export function googleTilesUrl(): string {
  const path='/api/connector/google3d/root.json';
  return sharded ? `${origin('google')}${path}` : path;
}

export const tileHostsInUse = () => sharded;
