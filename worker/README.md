# NovaFrame real local AI-video worker

This worker runs **LTX-Video 2B distilled 0.9.8** locally. It performs real multi-frame video diffusion for both text-to-video and image-to-video. It does not call Higgsfield or another paid generation API, and there is deliberately **no still-image zoom / Ken Burns / MoviePy fallback**.

The web app stays lightweight on Flask/Vercel. Supabase stores authentication, generation jobs, private reference images and private generated MP4s. The worker polls queued jobs outbound over HTTPS, performs GPU inference, uploads the MP4, then marks the job completed.

## Output profile

- Model: LTX-Video 2B distilled 0.9.8
- Model ID in NovaFrame: `ltx-video-local`
- Resolution: 672 × 448 (3:2)
- Frames: 121
- Frame rate: 24 fps
- Duration: about 5 seconds
- Text-to-video: yes
- Image-to-video: yes
- Prompt enhancer: disabled to avoid downloading extra LLM/image-caption models
- CPU offload: enabled to reduce VRAM pressure

121 frames follows LTX-Video's required `8n+1` temporal shape. 672 × 448 is divisible by 32.

## Important hardware rule

This pinned native LTX profile uses bfloat16. Run the hardware check first:

```sh
python worker/run.py --check
```

It prints GPU model, VRAM, bfloat16 support and the selected model/profile. If CUDA or bfloat16 is unavailable, NovaFrame stops with a clear error. It does **not** manufacture an animated still image as a substitute.

## Setup

Use Python 3.10 or 3.11 for the worker. Create a separate environment from the Flask app.

```sh
python -m venv worker/.venv
```

Install a CUDA-enabled PyTorch build appropriate for your GPU using the official PyTorch selector first. Then install the pinned worker requirements:

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

Put only these values in `worker/.env` on the trusted GPU computer:

```text
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
```

Never commit or share that file. The service-role key is administrative.

The worker dependency is pinned to official Lightricks LTX-Video revision:
`4b2d053057623ddd4d0a1d3e9cd28890e9ef487f`.

On first real generation, LTX model weights and the spatial upscaler are downloaded from the official `Lightricks/LTX-Video` model repository.

## Start

After `--check` succeeds, enable queuing in the web app environment:

```text
SELFHOST_ENABLED=1
```

Then start the worker:

```sh
python worker/run.py
```

For one queued job only:

```sh
python worker/run.py --once
```

The worker creates the private `generated-videos` bucket if needed.

## Motion behavior

Camera buttons are converted to literal LTX prompt guidance. They never apply post-generation zoom/crop animation. Even `Static` means a locked camera while the **subject still moves over time**.

NovaFrame also adds explicit temporal language asking for changing subject position, coherent physics, parallax/background movement and changing reflections. The negative prompt rejects frozen subjects, still photographs, slideshows and zoom-only motion.

## First proof test

Use a short, objectively moving scene:

```text
A red sports car accelerates down a wet neon-lit city street at night. The wheels visibly rotate, the road moves beneath the car, reflections slide across the bodywork, water sprays from the tires, and the background passes continuously. Smooth cinematic side tracking shot with natural motion blur.
```

The test passes only when the MP4 contains actual generated temporal motion. If inference fails because of hardware, memory or model download issues, the job becomes `failed`; do not replace it with a fake animation.

For image-to-video, upload a reference frame and describe the physical movement you expect. The image is passed into LTX as conditioning at frame 0.

## Queue safety

- queued jobs are claimed with compare-and-set
- two workers cannot intentionally claim the same queued row
- queued jobs can be cancelled before a worker claims them
- processing jobs are not automatically requeued after a crash
- completed videos are private and exposed through short-lived signed URLs
- old failed provider rows remain historical only; NovaFrame no longer calls those providers
