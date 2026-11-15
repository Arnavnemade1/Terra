import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Readable, Writable } from 'node:stream';
import handler from '../api/proxy.js';

class ResponseSink extends Writable {
  constructor() { super(); this.headers = {}; this.chunks = []; this.statusCode = 200; this.headersSent = false; }
  _write(chunk, _encoding, next) { this.headersSent = true; this.chunks.push(Buffer.from(chunk)); next(); }
  setHeader(key, value) { this.headers[key.toLowerCase()] = value; }
  status(code) { this.statusCode = code; return this; }
  json(value) { this.end(JSON.stringify(value)); return this; }
  get text() { return Buffer.concat(this.chunks).toString(); }
}
function request(method = 'GET', url = '/api/health', body) {
  return Object.assign(Readable.from([]), { method, url, body, headers: { host: 'terra.example', 'content-type': 'application/json', origin: 'https://terra.example' } });
}
function configure(t, fetcher, origin = 'https://backend.example') {
  const previous = process.env.TERRA_API_ORIGIN, oldFetch = globalThis.fetch;
  if (origin === null) delete process.env.TERRA_API_ORIGIN;
  else process.env.TERRA_API_ORIGIN = origin;
  globalThis.fetch = fetcher;
  t.after(() => { globalThis.fetch = oldFetch; if (previous === undefined) delete process.env.TERRA_API_ORIGIN; else process.env.TERRA_API_ORIGIN = previous; });
}
test('an unconfigured deployment returns a useful JSON error without a network request', async t => {
  configure(t, () => assert.fail('must not fetch'), null);
  const res = new ResponseSink(); await handler(request(), res);
  assert.equal(res.statusCode, 503); assert.match(JSON.parse(res.text).detail, /TERRA_API_ORIGIN/);
});
test('streamed tiles preserve query and cache headers, while browser credentials and upstream cookies stay private', async t => {
  configure(t, async (url, options) => {
    assert.equal(url.href, 'https://backend.example/api/connector/terrain?f=json');
    assert.equal(options.headers.get('x-forwarded-host'), 'terra.example');
    assert.equal(options.headers.get('authorization'), null); assert.equal(options.headers.get('cookie'), null);
    return new Response('tile content', { headers: { 'Content-Type': 'image/png', 'Cache-Control': 'private, max-age=60', 'Set-Cookie': 'private-session=value' } });
  });
  const req = request('GET', '/api/connector/terrain?f=json'); req.headers.authorization = 'private'; req.headers.cookie = 'private=value';
  const res = new ResponseSink(); await handler(req, res);
  assert.equal(res.text, 'tile content'); assert.equal(res.headers['cache-control'], 'private, max-age=60'); assert.equal(res.headers['set-cookie'], undefined);
});
test('Vercel-parsed investigation bodies and idempotency keys reach the backend intact', async t => {
  configure(t, async (url, options) => {
    assert.equal(url.pathname, '/api/investigations'); assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), { question: 'What changed?' });
    assert.equal(options.headers.get('idempotency-key'), 'test-run');
    return new Response('{"id":"job"}', { status: 201, headers: { 'Content-Type': 'application/json' } });
  });
  const req = request('POST', '/api/investigations', { question: 'What changed?' }); req.headers['idempotency-key'] = 'test-run';
  const res = new ResponseSink(); await handler(req, res); assert.equal(res.statusCode, 201); assert.equal(JSON.parse(res.text).id, 'job');
});
test('rewritten nested API routes retain their path and provider query parameters', async t => {
  configure(t, async url => {
    assert.equal(url.href, 'https://backend.example/api/investigations/job/artifacts/before.png?download=1');
    return new Response('image');
  });
  const res = new ResponseSink();
  await handler(request('GET', '/api/proxy?download=1&__terra_route=investigations%2Fjob%2Fartifacts%2Fbefore.png'), res);
  assert.equal(res.statusCode, 200); assert.equal(res.text, 'image');
});
test('invalid rewritten routes cannot escape the API prefix', async t => {
  configure(t, () => assert.fail('must not fetch'));
  for (const route of ['../private', 'connector/../private', '%2e%2e/private', '//external.example', 'connector\\private']) {
    const res = new ResponseSink();
    await handler(request('GET', '/api/proxy?__terra_route=' + encodeURIComponent(route)), res);
    assert.equal(res.statusCode, 400);
  }
});
test('invalid configuration and backend failures do not disclose private error content', async t => {
  configure(t, async () => { throw new Error('sensitive upstream details'); });
  const res = new ResponseSink(); await handler(request(), res);
  assert.equal(res.statusCode, 502); assert.doesNotMatch(res.text, /sensitive/);
  process.env.TERRA_API_ORIGIN = 'http://backend.example';
  const invalid = new ResponseSink(); await handler(request(), invalid); assert.equal(invalid.statusCode, 503);
});
