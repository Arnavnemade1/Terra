# Deploying Terra

The frontend runs on Vercel. The FastAPI backend, Temporal server and scientific worker require a persistent host with writable storage for SQLite, investigations and artifacts. ArduPilot SITL is a local development tool and is not started by the Vercel deployment.

## Vercel

- Install: `npm ci`; build: `npm run build`; output: `dist`.
- `/` serves the landing page; `/app` and `/app.html` open the workspace.
- `/api/*` is streamed through the server-side function to `TERRA_API_ORIGIN`, an HTTPS origin such as `https://api.example.com` without an `/api` suffix.
- Set that variable for Production and Preview in Vercel, then redeploy. When it is absent, API requests return an explicit 503 configuration message.
- Provider keys belong on the backend. Never use `TERRA_PUBLIC_*` or a Vite client variable for a credential. Local secrets and data are excluded from deployment uploads.

## Backend host

Install the Python dependencies from `pyproject.toml`, mount persistent storage at `TERRA_DATA_DIR`, and run the API and `python -m terra.worker` as separate services. Supply `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` for the Temporal service. Use `PYTHONPATH=backend` when running directly from the repository.

Configure `TERRA_ALLOWED_HOSTS` with the backend hostname and `TERRA_ALLOWED_ORIGINS` with the Vercel production URL. Keep `EARTHDATA_TOKEN`, `OPENAI_API_KEY`, `GOOGLE_MAPS_TILES_KEY` and `TERRA_DRONE_TOKEN` on that host. If the Google key uses website restrictions, set `GOOGLE_MAPS_TILES_REFERER` to an allowed public page URL.

Verify `/api/health` through Vercel: `temporal.connected` must be true for durable investigations. Verify `/api/connector/google3d/status` for Google imagery. The frontend loading successfully does not establish that these backend services are available.

## GitHub

The CI workflow runs Python tests, proxy tests, the TypeScript/Vite build and a public-build credential scan. Vercel deployments use the repository's `main` branch. Contributors whose commits are blocked by the Vercel team-membership check need an owner-created merge commit or membership in the Vercel team.
