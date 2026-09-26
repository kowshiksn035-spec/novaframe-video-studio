# NovaFrame GPU worker

This runs open-weight CogVideoX locally. It does not use Higgsfield or paid inference APIs. It generates 49 video frames with diffusion, then encodes an MP4 (about 6 seconds, 720×480, 8 fps). Camera controls are prompt guidance, not guaranteed camera trajectories. Output quality is not guaranteed to match Higgsfield.

The website remains Flask on Vercel, with authentication, jobs and private output in Supabase. This worker polls the existing queue over HTTPS; it requires no public port. Keep it on a trusted computer: its service-role key has administrative database access. Never share or commit worker/.env.

## Local setup (Python 3.11, NVIDIA CUDA)

Run these commands from the repository root in PowerShell or a terminal:

```sh
python -m venv worker/.venv
```

Windows PowerShell:

```powershell
worker/.venv/Scripts/python -m pip install -r worker/requirements.txt
worker/.venv/Scripts/python worker/run.py --check
Copy-Item worker/.env.example worker/.env
```

Linux:

```sh
worker/.venv/bin/python -m pip install -r worker/requirements.txt
worker/.venv/bin/python worker/run.py --check
cp worker/.env.example worker/.env
```

If the check reports no CUDA GPU, install the appropriate CUDA-enabled PyTorch build using the official PyTorch installation selector before continuing. Enter your existing Supabase URL and service-role key in worker/.env on the trusted computer. Start `worker/run.py` using the same virtual-environment Python. Initial generation downloads large model weights from Hugging Face. Review each model's license before use, especially CogVideoX-5b-I2V's model-specific license.

The worker creates a private `generated-videos` bucket on startup if absent. In Vercel set `VIDEO_BACKEND=selfhost` and `SELFHOST_ENABLED=1` only after the worker reports ready, then redeploy main. Existing Supabase and authentication environment variables are still needed. HF_KEY is not needed. SELFHOST_ENABLED enables queuing; it is not a live GPU heartbeat. Keep the worker running while testing.

## Hardware and limitations

Text mode uses CogVideoX-2b with FP16 and CPU offload. Image mode uses the larger CogVideoX-5b-I2V model and requires bfloat16 support; the worker rejects unsupported hardware. CPU offload reduces VRAM use but needs substantial system RAM and can be very slow. A GTX 1660 Ti with 16 GB system RAM is not a verified configuration and cannot run this worker's bfloat16 image mode. No claim of successful real rendering is made until tested on the target GPU. No GPU is rented by this setup.

## First real tests

1. Sign into the existing private beta account. Choose Text to video. Prompt: `A red ball rolls across a wooden table, hits a blue block and knocks it over. Static camera, continuous shot, natural physics.`
2. Generate and watch the worker. Confirm queued → processing → completed. Play the MP4 and check the ball and block actually move. Check history survives reloading.
3. On a bfloat16-capable GPU, upload a starting image and choose Image to video. Describe clear object movement. Confirm the reference influences the generated clip and there is temporal motion.
4. Stop the worker, queue another job, cancel it in the app, restart the worker and confirm it does not render the cancelled job.

The free beta retains the existing 10 jobs per day and one active job limit to protect local resources. There are no payments or purchased credits. Electricity, hardware, and any hosting beyond free allowances remain real costs.

Jobs interrupted after claiming remain `processing` to prevent duplicate work. The owner must inspect output and mark the job `failed` through Supabase before retrying. Do not automatically requeue interrupted jobs. No resumable render or worker heartbeat is implemented yet.

Model documentation: https://huggingface.co/THUDM/CogVideoX-2b and https://huggingface.co/THUDM/CogVideoX-5b-I2V
