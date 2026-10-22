import uuid
from pathlib import Path

import pytest

from worker import run as w


UID = '11111111-1111-1111-1111-111111111111'


def settings(**changes):
    value = {
        'prompt': 'A red sports car accelerates along a wet neon city street',
        'model': w.MODEL_ID,
        'mode': 'text',
        'duration': w.DURATION,
        'ratio': w.RATIO,
        'motion': 'Dolly In',
        'image_path': None,
    }
    value.update(changes)
    return value


def test_ltx_profile_is_temporally_valid():
    assert w.MODEL_ID == 'ltx-video-local'
    assert w.NUM_FRAMES > 1
    assert (w.NUM_FRAMES - 1) % 8 == 0
    assert w.WIDTH % 32 == 0
    assert w.HEIGHT % 32 == 0
    assert 4.5 <= w.NUM_FRAMES / w.FPS <= 5.5


def test_motion_prompt_requires_real_subject_motion():
    prompt = w.build_prompt(settings())
    lowered = prompt.lower()
    assert 'true temporal motion' in lowered
    assert 'objects change position' in lowered
    assert 'still photograph' in lowered
    assert 'pan-and-zoom slideshow' in lowered


@pytest.mark.parametrize('motion', ['Dolly In', 'Orbit', 'FPV', 'Pan Left', 'Zoom Out', 'Static'])
def test_each_camera_control_is_prompt_guidance(motion):
    prompt = w.build_prompt(settings(motion=motion))
    assert w.MOTION_GUIDANCE[motion] in prompt


def test_static_camera_still_requires_subject_motion():
    prompt = w.build_prompt(settings(motion='Static')).lower()
    assert 'camera does not move' in prompt
    assert 'subject and environment must still move' in prompt


@pytest.mark.parametrize(
    'changes',
    [
        {'model': 'cogvideox-local'},
        {'model': 'kling-3-standard'},
        {'duration': 10},
        {'ratio': '16:9'},
        {'mode': 'other'},
        {'motion': 'Fake Zoom'},
    ],
)
def test_old_or_fake_generation_settings_are_rejected(changes):
    with pytest.raises(ValueError):
        w.validate_settings(settings(**changes))


def test_seed_is_stable_for_job_id():
    job = {'id': '12345678-1234-5678-1234-567812345678'}
    assert w.seed_for(job) == w.seed_for(job)


def test_reference_path_is_strictly_owner_scoped():
    image_id = uuid.uuid4()
    row = {
        'user_id': UID,
        'settings': {'image_path': f'{UID}/{image_id}.jpg'},
    }
    assert w.reference_path(row) == f'{UID}/{image_id}.jpg'
    row['settings']['image_path'] = f'other-user/{image_id}.jpg'
    with pytest.raises(ValueError):
        w.reference_path(row)


def test_generated_video_must_be_real_mp4_file(tmp_path):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x' * 2048)
    assert w.locate_generated_video(tmp_path) == video


def test_missing_or_ambiguous_video_output_fails(tmp_path):
    with pytest.raises(RuntimeError):
        w.locate_generated_video(tmp_path)
    (tmp_path / 'a.mp4').write_bytes(b'x' * 2048)
    (tmp_path / 'b.mp4').write_bytes(b'x' * 2048)
    with pytest.raises(RuntimeError):
        w.locate_generated_video(tmp_path)


def test_worker_source_has_no_fake_zoom_encoder():
    source = Path(w.__file__).read_text(encoding='utf-8').lower()
    assert 'zoompan' not in source
    assert 'moviepy' not in source
    assert 'from ltx_video.inference import inferenceconfig, infer' in source


class Response:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


def test_claim_is_conditional(monkeypatch):
    row = {'id': str(uuid.uuid4()), 'provider_id': 'selfhost:test'}
    def fake_call(method, path, **kw):
        if method == 'GET':
            return Response([row])
        assert kw['params']['status'] == 'eq.queued'
        return Response([])
    monkeypatch.setattr(w, 'call', fake_call)
    assert w.claim() is None


def test_render_failure_marks_failed_without_leaking_error(monkeypatch):
    row = {'id': str(uuid.uuid4()), 'user_id': str(uuid.uuid4())}
    updates = []
    monkeypatch.setattr(w, 'claim', lambda: row)
    monkeypatch.setattr(
        w,
        'render',
        lambda *a: (_ for _ in ()).throw(RuntimeError('sensitive local detail')),
    )
    monkeypatch.setattr(w, 'finish', lambda row, **values: updates.append(values))
    assert w.run_once() is True
    assert updates[0]['status'] == 'failed'
    assert 'sensitive local detail' not in updates[0]['error']
