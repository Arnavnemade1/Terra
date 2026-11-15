import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const requestHeaders = ['accept', 'content-type', 'origin', 'referer', 'sec-fetch-site', 'idempotency-key', 'if-none-match', 'range'];
const responseHeaders = ['content-type', 'cache-control', 'etag', 'content-range', 'accept-ranges', 'last-modified', 'content-disposition'];

/** Same-origin delivery to the persistent backend. Provider credentials stay on that backend. */
export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  const configured = process.env.TERRA_API_ORIGIN;
  if (!configured) return res.status(503).json({ detail: 'Terra’s backend is not connected to this deployment. Configure TERRA_API_ORIGIN on Vercel.' });
  let upstream;
  try {
    upstream = new URL(configured);
    if (upstream.protocol !== 'https:' || upstream.username || upstream.password || upstream.pathname !== '/' || upstream.search || upstream.hash) throw new Error();
  } catch {
    return res.status(503).json({ detail: 'The deployed backend address is invalid. Configure an HTTPS origin in TERRA_API_ORIGIN.' });
  }
  const incoming = new URL(req.url, 'https://terra.invalid');
  if (!incoming.pathname.startsWith('/api/')) return res.status(404).json({ detail: 'API route not found.' });
  upstream.pathname = incoming.pathname;
  upstream.search = incoming.search;
  const headers = new Headers();
  for (const name of requestHeaders) if (req.headers[name]) headers.set(name, req.headers[name]);
  headers.set('x-forwarded-host', req.headers.host || '');
  headers.set('x-forwarded-proto', 'https');
  try {
    const options = { method: req.method, headers, redirect: 'manual', signal: AbortSignal.timeout(25_000) };
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      if (req.body !== undefined) options.body = typeof req.body === 'string' || Buffer.isBuffer(req.body) ? req.body : JSON.stringify(req.body);
      else { options.body = req; options.duplex = 'half'; }
    }
    const response = await fetch(upstream, options);
    res.statusCode = response.status;
    for (const name of responseHeaders) if (response.headers.has(name)) res.setHeader(name, response.headers.get(name));
    if (!response.body || req.method === 'HEAD') return res.end();
    await pipeline(Readable.fromWeb(response.body), res);
  } catch {
    if (res.headersSent) return res.destroy();
    res.status(502).json({ detail: 'The Terra backend could not be reached. Please retry shortly.' });
  }
}
