"""Trusted, outbound-only GPU worker. Never deploy this process to Vercel."""
import argparse
import gc
import io
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
import httpx


def call(method, path, **kwargs):
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    headers = {'apikey': key, 'Authorization': 'Bearer ' + key}
    headers.update(kwargs.pop('headers', {}))
    response = httpx.request(method, os.environ['SUPABASE_URL'].rstrip('/') + path,
                            headers=headers, timeout=120, **kwargs)
    # Never print response bodies or exception URLs: they may contain secrets.
    if response.is_error:
        raise RuntimeError(f'Data service HTTP {response.status_code}')
    return response


def ensure_bucket():
    buckets = call('GET', '/storage/v1/bucket').json()
    bucket = next((b for b in buckets if b['id'] == 'generated-videos'), None)
    if bucket:
        if bucket.get('public'): raise RuntimeError('generated-videos must be private')
    else:
        call('POST', '/storage/v1/bucket', json={
            'id': 'generated-videos', 'name': 'generated-videos', 'public': False,
            'file_size_limit': 52428800, 'allowed_mime_types': ['video/mp4']})


def claim():
    rows = call('GET', '/rest/v1/generations', params={
        'status':'eq.queued', 'provider_id':'like.selfhost:*',
        'order':'created_at.asc', 'limit':'1', 'select':'*'}).json()
    if not rows: return None
    row = rows[0]
    # Compare-and-set: a concurrent worker or cancellation wins at most once.
    claimed = call('PATCH', '/rest/v1/generations', params={
        'id':'eq.' + row['id'], 'status':'eq.queued', 'provider_id':'eq.' + row['provider_id']},
        json={'status':'processing', 'error':None},
        headers={'Prefer':'return=representation'}).json()
    return claimed[0] if claimed else None


def finish(row, **values):
    rows = call('PATCH', '/rest/v1/generations', params={
        'id':'eq.' + row['id'], 'user_id':'eq.' + row['user_id'], 'status':'eq.processing'},
        json=values, headers={'Prefer':'return=representation'}).json()
    if not rows: raise RuntimeError('Job state changed; owner review required')


def reference_path(row):
    owner = str(uuid.UUID(row['user_id']))
    path = row['settings'].get('image_path', '')
    if not isinstance(path, str) or not re.fullmatch(re.escape(owner) + r'/[0-9a-f-]{36}\.jpg', path):
        raise ValueError('Invalid reference path')
    uuid.UUID(path.split('/')[1][:-4])
    return path


def render(row, output):
    import torch
    from diffusers import CogVideoXPipeline, CogVideoXImageToVideoPipeline
    from diffusers.utils import export_to_video
    from PIL import Image, ImageOps
    settings = row['settings']
    if settings['model'] != 'cogvideox-local' or settings['mode'] not in ('text', 'image'):
        raise ValueError('Unsupported model or mode')
    if settings['duration'] != 6 or settings['ratio'] != '3:2':
        raise ValueError('Unsupported video dimensions')
    image_mode = settings['mode'] == 'image'
    if image_mode and not torch.cuda.is_bf16_supported():
        raise RuntimeError('Image-to-video requires a GPU supporting bfloat16')
    cls = CogVideoXImageToVideoPipeline if image_mode else CogVideoXPipeline
    model = 'THUDM/CogVideoX-5b-I2V' if image_mode else 'THUDM/CogVideoX-2b'
    pipe = cls.from_pretrained(model, torch_dtype=torch.bfloat16 if image_mode else torch.float16,
                              use_safetensors=True)
    pipe.enable_sequential_cpu_offload()
    pipe.vae.enable_slicing()
    pipe.vae.enable_tiling()
    args = {}
    if image_mode:
        raw = call('GET', '/storage/v1/object/authenticated/references/' + reference_path(row)).content
        with Image.open(io.BytesIO(raw)) as source:
            args['image'] = ImageOps.fit(source.convert('RGB'), (720, 480))
    prompt = settings['prompt'] + ('. Camera: ' + settings['motion'] if settings.get('motion') else '')
    try:
        frames = pipe(prompt=prompt, num_frames=49, num_inference_steps=50,
                      guidance_scale=6, num_videos_per_prompt=1,
                      generator=torch.Generator(device='cuda').manual_seed(int(row['id'].replace('-', '')[:8], 16)),
                      **args).frames[0]
        export_to_video(frames, str(output), fps=8)
    finally:
        del pipe
        gc.collect()
        torch.cuda.empty_cache()


def run_once():
    row = claim()
    if not row: return False
    print('Rendering job', row['id'], flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='novaframe-') as directory:
            output = Path(directory) / 'video.mp4'
            render(row, output)
            path = f"generated-videos/{uuid.UUID(row['user_id'])}/{uuid.UUID(row['id'])}.mp4"
            call('POST', '/storage/v1/object/' + path, content=output.read_bytes(),
                 headers={'Content-Type':'video/mp4', 'x-upsert':'false'})
            finish(row, status='completed', video_url='storage://' + path, error=None)
        print('Completed job', row['id'], flush=True)
    except Exception as exc:
        print('Job failed:', type(exc).__name__, flush=True)
        finish(row, status='failed', error='Local rendering failed. Check GPU compatibility, available memory and model downloads on the worker.')
    return True


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'))
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check GPU without downloading models or changing data')
    parser.add_argument('--once', action='store_true', help='Process at most one queued job')
    args = parser.parse_args()
    import torch
    if not torch.cuda.is_available(): raise RuntimeError('An NVIDIA CUDA GPU is required; none was detected')
    print('GPU:', torch.cuda.get_device_name(0))
    print('Image-to-video bfloat16 support:', torch.cuda.is_bf16_supported())
    if args.check: return
    for name in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'):
        if not os.getenv(name): raise RuntimeError('Missing ' + name)
    if not os.environ['SUPABASE_URL'].startswith('https://'): raise RuntimeError('Use HTTPS for Supabase')
    ensure_bucket()
    print('Worker ready. Waiting for NovaFrame jobs.', flush=True)
    while True:
        try: worked = run_once()
        except Exception as exc:
            print('Worker paused after', type(exc).__name__, '— check connection and job state.', flush=True)
            worked = False
        if args.once: return
        if not worked: time.sleep(8)


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: print('Worker stopped. Review any processing job before restarting.')
    except Exception as exc:
        # Configuration messages are safe; never serialize HTTP exceptions.
        print(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise SystemExit(1)
