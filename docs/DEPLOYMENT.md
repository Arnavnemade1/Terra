# Deploying Terra

The frontend runs on Vercel. The FastAPI backend, Temporal server and scientific worker require a persistent host with writable storage for SQLite, investigations and artifacts. ArduPilot SITL is a local development tool and is not started by the Vercel deployment.

## Vercel

- Install: `npm ci`; build: `npm run build`; output: `dist`.
- `/` serves the landing page; `/app` and `/app.html` open the workspace.
- `/api/*` is streamed through the server-side function to `TERRA_API_ORIGIN`, an HTTPS origin such as `https://api.example.com` without an `/api` suffix.
- Set that variable for Production and Preview in Vercel, then redeploy. When it is absent, API requests return an explicit 503 configuration message.
- Provider keys belong on the backend. Never use `TERRA_PUBLIC_*` or a Vite client variable for a credential. Local secrets and data are excluded from deployment uploads.

## Backend host

Install the Python dependencies from `pyproject.toml` and mount persistent storage at `TERRA_DATA_DIR`. The API and scientific worker must share that filesystem and SQLite database; `python -m terra.runtime` supervises both. Supply `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` for the separate Temporal service. Use `PYTHONPATH=backend` when running directly from the repository.

Configure `TERRA_ALLOWED_HOSTS` with the backend hostname and `TERRA_ALLOWED_ORIGINS` with the Vercel production URL. Keep `EARTHDATA_TOKEN`, `OPENAI_API_KEY`, `GOOGLE_MAPS_TILES_KEY` and `TERRA_DRONE_TOKEN` on that host. If the Google key uses website restrictions, set `GOOGLE_MAPS_TILES_REFERER` to an allowed public page URL.

### Railway

The backend Docker image starts `python -m terra.runtime`: a supervised API and scientific worker in one container, sharing `/data`. Attach a Railway volume at `/data` and keep this service at one replica. The image excludes local credentials, datasets, frontend dependencies and desktop/SITL tooling. Set `TEMPORAL_ADDRESS` to the private Temporal service address; the production runtime requires it.

Temporal runs as a separate private service backed by PostgreSQL. Its database credentials should use Railway variable references. Expose only the Terra HTTP service publicly on port 8000. No Temporal development server is started in the application image.

`.railway/railway.ts` declares the API, its Docker build, readiness check, restart policy and existing 500 MB volume. Run `npm ci` before `railway config plan`; review the plan before `railway config apply`. The partial configuration preserves the separate Temporal and PostgreSQL services, and `variables.managed: false` keeps credentials outside source control. A volume change must never detach or replace the existing data volume. The older `railway.json` format is no longer used.

The current public API is `https://terra-production-b426438a.up.railway.app`. Both `https://terra132.vercel.app` and `https://terra1326.vercel.app` proxy to it. The production tile cache is capped at 64 MB and warming is disabled, leaving room on the trial volume for scientific artifacts. Increase persistent storage before retaining many investigations.

The deployment readiness path is `/api/ready`. It returns 200 only when Temporal responds and the worker has a recent heartbeat from the same container boot. Startup retries the worker while Temporal initializes; SIGTERM stops both child processes. Worker concurrency is bounded to three activities and four workflow tasks in the image, with single-threaded native-library defaults.

Add provider credentials through Railway's secret-variable interface or `railway variable set KEY --stdin --skip-deploys`, never as build arguments or command-line values. Set `TERRA_ALLOWED_ORIGINS` to both production Vercel origins. After the public Railway domain is ready, set Vercel's `TERRA_API_ORIGIN` to that HTTPS origin and redeploy both sites. `/api/health` should then report `temporal.connected: true` through the Vercel domain.

Verify `/api/health` through Vercel: `temporal.connected` must be true for durable investigations. Verify `/api/connector/google3d/status` for Google imagery. The frontend loading successfully does not establish that these backend services are available.

## GitHub

The CI workflow runs Python tests, proxy tests, the TypeScript/Vite build, a public-build credential scan and the backend Docker build, including native-library imports. Vercel deployments use the repository's `main` branch. Contributors whose commits are blocked by the Vercel team-membership check need an owner-created merge commit or membership in the Vercel team.
