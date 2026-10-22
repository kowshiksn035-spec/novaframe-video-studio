import io
import uuid

import pytest

from api import index as m

UID = '11111111-1111-1111-1111-111111111111'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setenv('SUPABASE_ANON_KEY', 'anon')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'service')
    monkeypatch.setenv('ALLOWED_EMAILS', 'creator@example.com')
    monkeypatch.setenv('APP_ORIGIN', 'http://localhost')
    monkeypatch.setenv('SELFHOST_ENABLED', '1')
    m.app.config['TESTING'] = True
    c = m.app.test_client()
    c.set_cookie('nf_access', 'test', path='/api')
    return c


def auth(monkeypatch, handler):
    def fake(method, path, **kw):
        if path == '/auth/v1/user':
            return {'id': UID, 'email': 'creator@example.com'}
        return handler(method, path, **kw)
    monkeypatch.setattr(m, 'sb', fake)


def payload(**kw):
    data = {
        'id': str(uuid.uuid4()),
        'prompt': 'A red sports car drives quickly down a wet neon city street',
        'model': 'ltx-video-local',
        'mode': 'text',
        'duration': 5,
        'ratio': '3:2',
        'motion': 'Dolly In',
    }
    data.update(kw)
    return data


def post(c, data):
    return c.post('/api/generations', json=data, headers={'Origin': 'http://localhost'})


def test_csrf(client):
    assert client.post(
        '/api/generations',
        json=payload(),
        headers={'Origin': 'https://evil.example'},
    ).status_code == 403


def test_anonymous(client):
    client.delete_cookie('nf_access', path='/api')
    assert client.get('/api/generations').status_code == 401


def test_config_is_local_ltx_only(client):
    config = client.get('/api/config').json
    assert config['backend'] == 'selfhost'
    assert config['default_model'] == 'ltx-video-local'
    assert config['models'] == [
        {'id': 'ltx-video-local', 'label': 'LTX-Video 2B Distilled · Local GPU'}
    ]
    assert config['payments'] is False
    assert config['worker_enabled'] is True


def test_health_setup_required(client, monkeypatch):
    monkeypatch.delenv('SUPABASE_URL', raising=False)
    r = client.get('/api/health')
    assert r.status_code == 200
    assert r.json['status'] == 'setup_required'
    assert r.json['ok'] is False


def test_health_ready(client, monkeypatch):
    monkeypatch.setattr(m, 'sb', lambda *a, **k: [])
    r = client.get('/api/health')
    assert r.status_code == 200
    assert r.json['status'] == 'ready'
    assert r.json['supabase_reachable'] is True


def test_health_requires_worker_enable(client, monkeypatch):
    monkeypatch.delenv('SELFHOST_ENABLED', raising=False)
    monkeypatch.setattr(m, 'sb', lambda *a, **k: [])
    r = client.get('/api/health')
    assert r.status_code == 200
    assert r.json['status'] == 'setup_required'
    assert r.json['generation_ready'] is False


def test_health_degraded_when_supabase_unreachable(client, monkeypatch):
    monkeypatch.setattr(
        m,
        'sb',
        lambda *a, **k: (_ for _ in ()).throw(m.Problem('db down', 502)),
    )
    r = client.get('/api/health')
    assert r.status_code == 503
    assert r.json['status'] == 'degraded'
    assert r.json['supabase_reachable'] is False


@pytest.mark.parametrize(
    'changes',
    [
        {'duration': True},
        {'duration': 10},
        {'prompt': 'short'},
        {'ratio': '16:9'},
        {'model': 'cogvideox-local'},
        {'model': 'kling-3-standard'},
        {'mode': 'image', 'image_path': 'other/file.jpg'},
    ],
)
def test_validation(client, monkeypatch, changes):
    auth(monkeypatch, lambda *a, **k: pytest.fail('Invalid request reached DB'))
    assert post(client, payload(**changes)).status_code == 400


def test_owner_scope(client, monkeypatch):
    def handler(method, path, **kw):
        assert kw['params']['user_id'] == 'eq.' + UID
        return []
    auth(monkeypatch, handler)
    assert client.get('/api/generations').json == []


def test_active_generation_is_owner_scoped(client, monkeypatch):
    job = {'id': str(uuid.uuid4()), 'status': 'queued', 'provider_id': 'selfhost:x'}
    def handler(method, path, **kw):
        params = kw['params']
        assert params['user_id'] == 'eq.' + UID
        assert params['status'] == 'in.(submitting,queued,processing)'
        assert params['limit'] == '1'
        return [job]
    auth(monkeypatch, handler)
    assert client.get('/api/generations/active').json == job


def test_active_generation_returns_null_when_idle(client, monkeypatch):
    auth(monkeypatch, lambda *a, **k: [])
    assert client.get('/api/generations/active').json is None


def test_duplicate_does_not_queue_again(client, monkeypatch):
    data = payload()
    settings = {k: v for k, v in data.items() if k != 'id'}
    settings['image_path'] = None
    existing = {'id': data['id'], 'settings': settings, 'status': 'queued',
                'provider_id': 'selfhost:' + data['id']}
    auth(monkeypatch, lambda *a, **k: {'created': False, 'job': existing})
    r = post(client, data)
    assert r.status_code == 200
    assert r.json['id'] == data['id']


def test_quota(client, monkeypatch):
    auth(monkeypatch, lambda *a, **k: {'error': 'Daily beta limit reached.'})
    assert post(client, payload()).status_code == 429


def test_local_submission_queues_without_external_provider(client, monkeypatch):
    job_id = str(uuid.uuid4())
    data = payload(id=job_id)

    def handler(method, path, **kw):
        if path.endswith('reserve_generation'):
            assert kw['json']['p_settings']['model'] == 'ltx-video-local'
            return {'created': True}
        assert method == 'PATCH'
        assert kw['json'] == {
            'provider_id': 'selfhost:' + job_id,
            'status': 'queued',
        }
        return [{'id': job_id, 'user_id': UID, 'settings': data, **kw['json']}]

    auth(monkeypatch, handler)
    r = post(client, data)
    assert r.status_code == 202
    assert r.json['status'] == 'queued'
    assert r.json['provider_id'] == 'selfhost:' + job_id


def test_local_requires_explicit_enable(client, monkeypatch):
    monkeypatch.delenv('SELFHOST_ENABLED', raising=False)
    auth(monkeypatch, lambda *a, **k: pytest.fail('Disabled queue touched'))
    assert post(client, payload()).status_code == 503


def test_local_poll_never_contacts_legacy_provider(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'processing',
        'provider_id': 'selfhost:abc',
    }
    auth(monkeypatch, lambda *a, **k: [job])
    assert client.get('/api/generations/' + job['id']).json == job


def test_nonterminal_legacy_provider_job_is_not_contacted(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'processing',
        'provider_id': 'legacy-provider-id',
    }
    auth(monkeypatch, lambda *a, **k: [job])
    assert client.get('/api/generations/' + job['id']).status_code == 409


def test_terminal_legacy_history_remains_readable(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'failed',
        'provider_id': 'legacy-provider-id',
        'error': 'Historical failed generation.',
    }
    auth(monkeypatch, lambda *a, **k: [job])
    r = client.get('/api/generations/' + job['id'])
    assert r.status_code == 200
    assert r.json['status'] == 'failed'


def test_local_cancel(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'queued',
        'provider_id': 'selfhost:abc',
    }
    def handler(method, path, **kw):
        if method == 'GET':
            return [job]
        assert kw['params']['status'] == 'eq.queued'
        assert kw['json'] == {'status': 'cancelled', 'error': None}
        return [{**job, 'status': 'cancelled', 'error': None}]
    auth(monkeypatch, handler)
    r = client.post(
        '/api/generations/' + job['id'] + '/cancel',
        headers={'Origin': 'http://localhost'},
    )
    assert r.status_code == 200
    assert r.json['status'] == 'cancelled'


def test_local_cancel_race(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'queued',
        'provider_id': 'selfhost:abc',
    }
    def handler(method, path, **kw):
        if method == 'GET':
            return [job]
        return []
    auth(monkeypatch, handler)
    assert client.post(
        '/api/generations/' + job['id'] + '/cancel',
        headers={'Origin': 'http://localhost'},
    ).status_code == 409


def test_legacy_provider_cancel_is_disabled(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'queued',
        'provider_id': 'legacy-provider-id',
    }
    auth(monkeypatch, lambda *a, **k: [job])
    assert client.post(
        '/api/generations/' + job['id'] + '/cancel',
        headers={'Origin': 'http://localhost'},
    ).status_code == 409


def test_other_user_job(client, monkeypatch):
    auth(monkeypatch, lambda *a, **k: [])
    assert client.get('/api/generations/' + str(uuid.uuid4())).status_code == 404


def test_invalid_image(client, monkeypatch):
    auth(monkeypatch, lambda *a, **k: pytest.fail('Bad upload saved'))
    r = client.post(
        '/api/uploads',
        data={'image': (io.BytesIO(b'<script>bad</script>'), 'x.jpg')},
        headers={'Origin': 'http://localhost'},
    )
    assert r.status_code == 400


def test_output_signed_only_for_owner_path(client, monkeypatch):
    job = {
        'id': str(uuid.uuid4()),
        'user_id': UID,
        'status': 'completed',
        'video_url': 'storage://generated-videos/another-user/file.mp4',
    }
    auth(monkeypatch, lambda *a, **k: [job])
    assert client.get('/api/generations/' + job['id']).status_code == 502


def test_local_output_signed(client, monkeypatch):
    job = {'id': str(uuid.uuid4()), 'user_id': UID, 'status': 'completed'}
    job['video_url'] = f"storage://generated-videos/{UID}/{job['id']}.mp4"

    def handler(method, path, **kw):
        if method == 'GET':
            return [job]
        assert path == '/storage/v1/object/sign/' + job['video_url'][10:]
        return {'signedURL': '/object/sign/private?token=test'}

    auth(monkeypatch, handler)
    r = client.get('/api/generations/' + job['id'])
    assert r.json['video_url'].startswith(
        'https://example.supabase.co/storage/v1/'
    )
