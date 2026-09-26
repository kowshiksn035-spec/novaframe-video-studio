# Self-hosted video generation

NovaFrame now defaults to a local CogVideoX GPU worker. No Higgsfield API key or payments are required for this backend. See [worker setup and real-video test checklist](worker/README.md). Generation stays disabled until `SELFHOST_ENABLED=1`; this switch does not verify worker availability. Real GPU rendering has not yet been validated.

The instructions below describe the original optional Higgsfield backend. To use it explicitly, set `VIDEO_BACKEND=higgsfield`. Existing Flask/Supabase architecture and historical jobs are preserved.

# NovaFrame AI Video Studio MVP

A Vercel-compatible Flask app with a responsive studio, Supabase authentication and persistence, private reference-image storage, and Higgsfield Kling 3.0 submission/status integration. No payments, fake credits, simulated videos, or fake progress.

## Current delivery status

- Implemented: invitation-only password sign-in with HttpOnly cookies and refresh, text/image generation forms, reference upload and image re-encoding, camera prompt guidance, generation history, status polling, video playback and open/save link.
- Implemented: owner checks, RLS migration, atomic reservation and per-user rolling 24-hour limit (10), one active job per user, request deduplication, no retries of billable submissions.
- Automated verification: 17 mocked backend tests pass. They do not establish live provider or database integration.
- Figma design: https://www.figma.com/design/XW8kKwAVNyF5A7BdGhME0K
- A Vercel preview was submitted, but inspection is blocked by account scope permissions and browser preview requires Vercel sign-in. Do not treat it as a verified working deployment.
- Supabase project created: `novaframe-video-studio`, ref `rlkhcenojjsqrfqglvsu`, region `ap-south-1`, quoted cost $0/month. Migration `create_video_studio` applied successfully.
- Still needed: server environment credentials, Higgsfield Cloud API key, Vercel environment configuration. No real generation has run.
- Source repository: https://github.com/kowshiksn035-spec/novaframe-video-studio (private).

## Run locally

Python 3.12 recommended. From this folder:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install python-dotenv
cp .env.example .env
python -m flask --app api.index run --port 5000
```

On Windows use `Copy-Item .env.example .env` instead of `cp` if needed. Flask loads `.env` when python-dotenv is installed. Open http://localhost:5000. Without credentials the studio shows a setup notice and disables generation.

## Supabase setup

1. Existing project: `rlkhcenojjsqrfqglvsu` in the owner's selected organization.
2. The migration is already applied to that project. Apply `supabase/migrations/202609180001_studio.sql` only when provisioning another fresh project.
3. Create the invited beta user through Supabase Auth, with an email/password. User invitation email/password flows are managed in Supabase, not implemented as public signup in this app.
4. Set `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` on the server. Never put the service-role key in browser code.
5. Set `ALLOWED_EMAILS` to a comma-separated list of invited users. An empty list denies access.

## Higgsfield setup

Set `HF_KEY` to the Higgsfield Cloud API `key-id:key-secret`, using Vercel's encrypted environment settings. A ChatGPT Higgsfield connector does not provide this app with a reusable secret. Provider generations can incur provider charges even though this MVP has no payments.

Verified model references:
- https://console.higgsfield.ai/models/kling-video/v3.0/std/text-to-video/api-reference
- https://console.higgsfield.ai/models/kling-video/v3.0/std/image-to-video/playground
- https://github.com/higgsfield-ai/higgsfield-client

The MVP uses 5/10 seconds, silent output, text ratios 16:9/9:16/1:1. Image-to-video follows image proportions. Camera direction is included in the prompt, not a guaranteed camera-control parameter.

## GitHub / Vercel

Create a dedicated private repository (do not use the unrelated Toji-cooks repository). Commit this folder, excluding `.env` and caches. Import it into Vercel with the root directory set to this folder. `vercel.json` routes requests to the Python API, which also serves `public/` assets.

Set all six values in `.env.example` on Vercel, replacing `APP_ORIGIN` with the exact deployed HTTPS origin and setting `COOKIE_SECURE=1`. `VERCEL` also enables secure cookies automatically. Keep the app invitation-only until a real user isolation test and real generation succeed.

## Health check

Use `GET /api/health` to verify a deployment without creating a billable generation.

- `ready`: Supabase is reachable and the Higgsfield credential is configured.
- `setup_required`: one or more required environment values are missing or malformed.
- `degraded`: Supabase was configured but could not be reached.

The health check never submits a Higgsfield generation and never returns secret values.

## Tests

```sh
pip install pytest
python -m pytest -q
node --check public/app.js
```

GitHub Actions also runs these checks automatically from `.github/workflows/verify.yml`; continuation changes should pass this workflow before merging.

## Operational limitations

- Results are provider-hosted URLs, not copied into permanent video storage. Save videos before provider URLs expire.
- Status refresh is on demand. Closing the browser does not cancel the provider job; reopen it from Generations to refresh.
- A submission timeout or crash can leave `needs_review` or `submitting`. Check the provider dashboard before changing that record; never blindly resubmit. After confirming the provider request ID, set `provider_id` and status `queued` in the admin database. If no request was accepted, mark `failed`.
- Uploaded reference images remain in the private bucket. Storage cleanup and account deletion UX are not part of this MVP.
- Provider errors are deliberately generic; no secrets are returned or printed.
- Public registration, payments, social sharing, audio editing, and multi-scene timeline editing are out of scope.

Latest submitted preview (requires Vercel access; build unverified):
https://novaframe-video-studio-9suay8r96-kowshiksn035-5936.vercel.app
