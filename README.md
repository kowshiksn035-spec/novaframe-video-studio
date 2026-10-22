# NovaFrame AI Video Studio

NovaFrame is a private-beta AI video creator inspired by modern video-generation studios. The product UI is its own app; **Higgsfield is only a product reference and is not an active generation provider.**

## Current architecture

- Flask/Python web API
- Static responsive frontend in `public/`
- Supabase Auth, Postgres and private Storage
- Local outbound-only GPU worker in `worker/`
- LTX-Video 2B distilled 0.9.8 for real text-to-video and image-to-video
- Vercel-compatible web hosting
- No payments, subscriptions or purchased generation credits

The web app never runs GPU inference. It creates a queued Supabase job. The trusted local worker claims the job, runs LTX diffusion, uploads the generated MP4 into the private `generated-videos` bucket, and marks the job completed. The browser receives only a short-lived signed playback URL.

There is intentionally **no static-photo zoom fallback**. If the GPU/model cannot produce a real video, the job fails.

## Local generation profile

- model ID: `ltx-video-local`
- 672 × 448, 3:2
- 121 generated frames
- 24 fps
- roughly 5 seconds
- text-to-video and image-to-video
- camera buttons become prompt guidance, not post-processing transforms

See `worker/README.md` for GPU requirements and setup.

## Web app setup

Python 3.12 is recommended for the Flask app:

```sh
python -m venv .venv
# activate the environment
pip install -r requirements.txt
pip install python-dotenv
cp .env.example .env
python -m flask --app api.index run --port 5000
```

Web environment:

```text
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
SELFHOST_ENABLED=0
ALLOWED_EMAILS=...
APP_ORIGIN=http://localhost:5000
COOKIE_SECURE=0
```

Set `SELFHOST_ENABLED=1` only after `python worker/run.py --check` succeeds and the worker is ready.

## Supabase

Existing project: `novaframe-video-studio` (`rlkhcenojjsqrfqglvsu`, `ap-south-1`).

The existing migration provides:
- owner-scoped generations
- RLS
- atomic reservation/deduplication
- rolling beta limit
- one active job per user
- private reference-image storage

The worker creates `generated-videos` as a private MP4 bucket if it does not exist.

Historical failed generations from earlier provider experiments are retained as history. Nonterminal legacy provider jobs are never contacted by the current app.

## Safety

- service-role key stays server/worker-side
- reference images and output videos remain private
- generated videos are returned through signed URLs
- generation IDs are deduplicated
- queue claims use compare-and-set
- queued jobs can be cancelled safely
- processing jobs are not blindly retried
- no active paid AI provider integration exists

## Health

`GET /api/health` is non-billable. It reports `ready` when Supabase is reachable and `SELFHOST_ENABLED=1`. The flag confirms queuing is enabled; it is not a live heartbeat from the GPU worker.

## Tests

```sh
pip install pytest
python -m pytest -q
python -m compileall -q api app.py tests worker
node --check public/app.js
```

GitHub Actions runs these checks on pull requests.

## Remaining live milestone

Repository tests can verify the queue, security and worker code, but a **real LTX render must run on the user's GPU machine** because model weights and CUDA inference are intentionally not executed in GitHub CI. A successful live proof is:

prompt → queued → processing → real multi-frame LTX MP4 → private Supabase upload → completed → browser playback/history.
