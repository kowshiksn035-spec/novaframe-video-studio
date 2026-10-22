"""Trusted outbound-only LTX-Video worker. Never deploy this process to Vercel."""
import argparse
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import httpx

MODEL_ID = "ltx-video-local"
MODEL_LABEL = "LTX-Video 2B Distilled · Local GPU"
WIDTH = 672
HEIGHT = 448
NUM_FRAMES = 121  # 8n+1, required by LTX-Video.
FPS = 24
DURATION = 5
RATIO = "3:2"
CONFIG_PATH = Path(__file__).with_name("ltxv-2b-0.9.8-distilled.yaml")

MOTION_GUIDANCE = {
    "": "Use natural camera movement motivated by the scene.",
    "Dolly In": "The camera slowly dollies forward toward the moving subject in one continuous shot.",
    "Orbit": "The camera makes a smooth cinematic orbit around the moving subject while maintaining temporal continuity.",
    "FPV": "Use dynamic first-person camera movement through the scene with physically coherent motion.",
    "Pan Left": "The camera smoothly pans left while the subject and background continue moving naturally.",
    "Zoom Out": "The camera smoothly pulls back to reveal more of the environment while the subject continues moving.",
    "Static": "Use a locked-off camera. The camera does not move, but the subject and environment must still move naturally over time.",
}

NEGATIVE_PROMPT = (
    "worst quality, inconsistent motion, blurry, jittery, distorted, frozen subject, "
    "still photograph, slideshow, Ken Burns effect, zoom-only motion, repeated identical frames"
)


def call(method, path, **kwargs):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": "Bearer " + key}
    headers.update(kwargs.pop("headers", {}))
    response = httpx.request(
        method,
        os.environ["SUPABASE_URL"].rstrip("/") + path,
        headers=headers,
        timeout=120,
        **kwargs,
    )
    # Never print response bodies or exception URLs: they may contain secrets.
    if response.is_error:
        raise RuntimeError(f"Data service HTTP {response.status_code}")
    return response


def ensure_bucket():
    buckets = call("GET", "/storage/v1/bucket").json()
    bucket = next((b for b in buckets if b["id"] == "generated-videos"), None)
    if bucket:
        if bucket.get("public"):
            raise RuntimeError("generated-videos must be private")
        return
    call(
        "POST",
        "/storage/v1/bucket",
        json={
            "id": "generated-videos",
            "name": "generated-videos",
            "public": False,
            "file_size_limit": 52428800,
            "allowed_mime_types": ["video/mp4"],
        },
    )


def claim():
    rows = call(
        "GET",
        "/rest/v1/generations",
        params={
            "status": "eq.queued",
            "provider_id": "like.selfhost:*",
            "order": "created_at.asc",
            "limit": "1",
            "select": "*",
        },
    ).json()
    if not rows:
        return None
    row = rows[0]
    # Compare-and-set: a concurrent worker or cancellation wins at most once.
    claimed = call(
        "PATCH",
        "/rest/v1/generations",
        params={
            "id": "eq." + row["id"],
            "status": "eq.queued",
            "provider_id": "eq." + row["provider_id"],
        },
        json={"status": "processing", "error": None},
        headers={"Prefer": "return=representation"},
    ).json()
    return claimed[0] if claimed else None


def finish(row, **values):
    rows = call(
        "PATCH",
        "/rest/v1/generations",
        params={
            "id": "eq." + row["id"],
            "user_id": "eq." + row["user_id"],
            "status": "eq.processing",
        },
        json=values,
        headers={"Prefer": "return=representation"},
    ).json()
    if not rows:
        raise RuntimeError("Job state changed; owner review required")


def reference_path(row):
    owner = str(uuid.UUID(row["user_id"]))
    path = row["settings"].get("image_path", "")
    if not isinstance(path, str) or not re.fullmatch(
        re.escape(owner) + r"/[0-9a-f-]{36}\.jpg", path
    ):
        raise ValueError("Invalid reference path")
    uuid.UUID(path.split("/")[1][:-4])
    return path


def validate_settings(settings):
    if settings.get("model") != MODEL_ID:
        raise ValueError("Unsupported local model")
    if settings.get("mode") not in ("text", "image"):
        raise ValueError("Unsupported generation mode")
    if settings.get("duration") != DURATION or settings.get("ratio") != RATIO:
        raise ValueError("Unsupported local video dimensions")
    if settings.get("motion", "") not in MOTION_GUIDANCE:
        raise ValueError("Unsupported camera direction")


def build_prompt(settings):
    validate_settings(settings)
    prompt = settings["prompt"].strip()
    if not prompt:
        raise ValueError("Prompt is required")
    motion = MOTION_GUIDANCE[settings.get("motion", "")]
    return (
        f"{prompt}. {motion} "
        "This is a continuous moving shot with true temporal motion across frames. "
        "Objects change position naturally over time, with coherent physical movement, "
        "changing reflections, parallax and background motion where appropriate. "
        "Do not turn the scene into a still photograph or a pan-and-zoom slideshow."
    )


def seed_for(row):
    return int(uuid.UUID(row["id"]).hex[:8], 16)


def download_reference(row, directory):
    path = reference_path(row)
    raw = call("GET", "/storage/v1/object/authenticated/references/" + path).content
    target = Path(directory) / "reference.jpg"
    target.write_bytes(raw)
    return target


def locate_generated_video(directory):
    outputs = sorted(Path(directory).glob("*.mp4"))
    if len(outputs) != 1:
        raise RuntimeError("LTX-Video did not produce exactly one MP4")
    if outputs[0].stat().st_size < 1024:
        raise RuntimeError("LTX-Video produced an invalid MP4")
    return outputs[0]


def render(row, output):
    """Run real LTX diffusion. There is deliberately no image-zoom fallback."""
    settings = row["settings"]
    validate_settings(settings)

    import torch
    from ltx_video.inference import InferenceConfig, infer

    if not torch.cuda.is_available():
        raise RuntimeError("Real LTX generation requires a CUDA GPU")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("This LTX profile requires a GPU with bfloat16 support")

    with tempfile.TemporaryDirectory(prefix="novaframe-ltx-") as directory:
        conditioning_paths = None
        conditioning_strengths = None
        conditioning_start_frames = None
        if settings["mode"] == "image":
            reference = download_reference(row, directory)
            conditioning_paths = [str(reference)]
            conditioning_strengths = [1.0]
            conditioning_start_frames = [0]

        infer(
            InferenceConfig(
                prompt=build_prompt(settings),
                output_path=directory,
                pipeline_config=str(CONFIG_PATH),
                seed=seed_for(row),
                height=HEIGHT,
                width=WIDTH,
                num_frames=NUM_FRAMES,
                frame_rate=FPS,
                offload_to_cpu=True,
                negative_prompt=NEGATIVE_PROMPT,
                conditioning_media_paths=conditioning_paths,
                conditioning_strengths=conditioning_strengths,
                conditioning_start_frames=conditioning_start_frames,
            )
        )
        generated = locate_generated_video(directory)
        shutil.copyfile(generated, output)


def run_once():
    row = claim()
    if not row:
        return False
    print("Rendering job", row["id"], "with", MODEL_LABEL, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="novaframe-") as directory:
            output = Path(directory) / "video.mp4"
            render(row, output)
            path = (
                f"generated-videos/{uuid.UUID(row['user_id'])}/"
                f"{uuid.UUID(row['id'])}.mp4"
            )
            call(
                "POST",
                "/storage/v1/object/" + path,
                content=output.read_bytes(),
                headers={"Content-Type": "video/mp4", "x-upsert": "false"},
            )
            finish(
                row,
                status="completed",
                video_url="storage://" + path,
                error=None,
            )
        print("Completed job", row["id"], flush=True)
    except Exception as exc:
        print("Job failed:", type(exc).__name__, flush=True)
        finish(
            row,
            status="failed",
            error=(
                "Real local video rendering failed. Check CUDA/bfloat16 support, "
                "available memory and LTX model downloads on the worker."
            ),
        )
    return True


def hardware_report():
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError(
            "No CUDA GPU detected. NovaFrame will not substitute a fake zoom video."
        )
    props = torch.cuda.get_device_properties(0)
    vram_gb = props.total_memory / (1024**3)
    print("GPU:", torch.cuda.get_device_name(0))
    print(f"VRAM: {vram_gb:.1f} GB")
    print("bfloat16 support:", torch.cuda.is_bf16_supported())
    print("Model:", MODEL_LABEL)
    print(f"Output profile: {WIDTH}x{HEIGHT}, {NUM_FRAMES} frames, {FPS} fps")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError(
            "This GPU lacks bfloat16 support required by the selected LTX profile. "
            "Real generation is disabled; no fake fallback will run."
        )


def main():
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check GPU compatibility without downloading models or changing data",
    )
    parser.add_argument("--once", action="store_true", help="Process at most one queued job")
    args = parser.parse_args()

    hardware_report()
    if args.check:
        return

    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.getenv(name):
            raise RuntimeError("Missing " + name)
    if not os.environ["SUPABASE_URL"].startswith("https://"):
        raise RuntimeError("Use HTTPS for Supabase")
    if not CONFIG_PATH.exists():
        raise RuntimeError("Missing pinned LTX pipeline configuration")

    ensure_bucket()
    print("Worker ready. Waiting for real LTX-Video jobs.", flush=True)
    while True:
        try:
            worked = run_once()
        except Exception as exc:
            print(
                "Worker paused after",
                type(exc).__name__,
                "— check connection and job state.",
                flush=True,
            )
            worked = False
        if args.once:
            return
        if not worked:
            time.sleep(8)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Worker stopped. Review any processing job before restarting.")
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise SystemExit(1)
