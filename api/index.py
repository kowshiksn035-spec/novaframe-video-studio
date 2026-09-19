"""NovaFrame: stateless Vercel-compatible API; Supabase owns durable state."""
import io, os, uuid
from pathlib import Path
from functools import wraps
from urllib.parse import urlparse
import httpx
from flask import Flask, request, jsonify, send_from_directory, g
from werkzeug.exceptions import HTTPException
from PIL import Image, UnidentifiedImageError
import higgsfield_client as hf

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, static_folder=str(ROOT / 'public'), static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
TERMINAL = {'completed', 'failed', 'cancelled', 'needs_review'}

class Problem(Exception):
    def __init__(self, message, status=400): self.message, self.status = message, status

def env(name):
    value = os.getenv(name, '')
    if not value: raise Problem('Studio setup is incomplete. Contact the studio owner.', 503)
    return value

def readiness():
    auth_keys = ['SUPABASE_URL', 'SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY', 'ALLOWED_EMAILS']
    auth_ready = all(os.getenv(name) for name in auth_keys)
    hf_key = os.getenv('HF_KEY', '')
    provider_configured = ':' in hf_key and all(hf_key.split(':', 1))
    return auth_ready, provider_configured, auth_ready and provider_configured

def sb(method, path, *, admin=True, token=None, **kwargs):
    key = env('SUPABASE_SERVICE_ROLE_KEY' if admin else 'SUPABASE_ANON_KEY')
    headers = {'apikey': key, 'Authorization': 'Bearer ' + (token or key)}
    headers.update(kwargs.pop('headers', {}))
    try:
        r = httpx.request(method, env('SUPABASE_URL').rstrip('/') + path, headers=headers, timeout=20, **kwargs)
    except httpx.RequestError:
        raise Problem('The data service could not complete this request.', 502)
    if r.is_error:
        if r.status_code == 401: raise Problem('Please sign in again.', 401)
        raise Problem('The data service could not complete this request.', 502)
    return r.json() if r.content else None

def signed_in(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = request.cookies.get('nf_access')
        if not token: raise Problem('Sign in to continue.', 401)
        user = sb('GET', '/auth/v1/user', admin=False, token=token)
        g.user = user['id']
        allow = {x.strip().lower() for x in os.getenv('ALLOWED_EMAILS', '').split(',') if x.strip()}
        if not allow or user.get('email', '').lower() not in allow:
            raise Problem('This private beta is invitation-only.', 403)
        g.email = user['email']
        return fn(*args, **kwargs)
    return wrapped

@app.before_request
def protect():
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('Origin', '')
        expected = os.getenv('APP_ORIGIN') or request.host_url.rstrip('/')
        if origin != expected: raise Problem('Invalid request origin.', 403)

@app.after_request
def security(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data: https:; media-src 'self' https:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    if request.path.startswith('/api/'): response.headers['Cache-Control'] = 'no-store'
    return response

@app.errorhandler(Problem)
def problem(e): return jsonify(error=e.message), e.status

@app.errorhandler(HTTPException)
def http_error(e): return jsonify(error='File too large (maximum 4 MB).' if e.code == 413 else e.description), e.code

@app.errorhandler(Exception)
def unexpected(e):
    # Log type only: upstream exception strings can contain signed URLs or secrets.
    app.logger.error('Request failed: %s', type(e).__name__)
    return jsonify(error='Request could not be completed. Please try refreshing.'), 502

@app.get('/')
def index(): return send_from_directory(ROOT / 'public', 'index.html')

@app.get('/api/config')
def config():
    auth_ready, provider_configured, generation_ready = readiness()
    return jsonify(auth_ready=auth_ready, generation_ready=generation_ready,
                   provider_configured=provider_configured,
                   model='Kling 3.0 Standard', payments=False)

@app.get('/api/health')
def health():
    auth_ready, provider_configured, generation_ready = readiness()
    supabase_reachable = None
    if auth_ready:
        try:
            sb('GET', '/rest/v1/generations', params={'select':'id', 'limit':'1'})
            supabase_reachable = True
        except Problem:
            supabase_reachable = False

    if auth_ready and provider_configured and supabase_reachable:
        status, code = 'ready', 200
    elif auth_ready and supabase_reachable is False:
        status, code = 'degraded', 503
    else:
        status, code = 'setup_required', 200

    return jsonify(
        ok=status == 'ready',
        status=status,
        service='novaframe-video-studio',
        auth_ready=auth_ready,
        generation_ready=generation_ready,
        provider_configured=provider_configured,
        supabase_reachable=supabase_reachable,
        payments=False,
    ), code

def cookies(response, data):
    for name, value, age in [('nf_access', data['access_token'], data.get('expires_in', 3600)), ('nf_refresh', data['refresh_token'], 604800)]:
        response.set_cookie(name, value, max_age=age, httponly=True, secure=bool(os.getenv('VERCEL') or os.getenv('COOKIE_SECURE') == '1'), samesite='Strict', path='/api')
    return response

@app.post('/api/auth/login')
def login():
    data = request.get_json(silent=True) or {}
    email, password = data.get('email'), data.get('password')
    if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or not 1 <= len(password) <= 256:
        raise Problem('Enter your email and password.')
    allowed = {x.strip().lower() for x in env('ALLOWED_EMAILS').split(',')}
    if email.lower() not in allowed: raise Problem('This private beta is invitation-only.', 403)
    # Supabase applies its own authentication rate limits.
    r = httpx.post(env('SUPABASE_URL') + '/auth/v1/token?grant_type=password', headers={'apikey': env('SUPABASE_ANON_KEY')}, json={'email': email, 'password': password}, timeout=20)
    if r.is_error: raise Problem('Sign-in failed. Check your credentials or try again later.', 401)
    return cookies(jsonify(ok=True), r.json())

@app.post('/api/auth/refresh')
def refresh():
    token = request.cookies.get('nf_refresh')
    if not token: raise Problem('Please sign in again.', 401)
    data = sb('POST', '/auth/v1/token?grant_type=refresh_token', admin=False, json={'refresh_token': token})
    return cookies(jsonify(ok=True), data)

@app.post('/api/auth/logout')
def logout():
    token = request.cookies.get('nf_access')
    if token:
        try: sb('POST', '/auth/v1/logout', admin=False, token=token)
        except Problem: pass
    response = jsonify(ok=True)
    for name in ['nf_access', 'nf_refresh']: response.delete_cookie(name, path='/api')
    return response

@app.get('/api/me')
@signed_in
def me(): return jsonify(email=g.email)

def uuid_string(value):
    try: return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError): raise Problem('Invalid identifier.')

def get_job(job_id):
    rows = sb('GET', '/rest/v1/generations', params={'id': 'eq.' + uuid_string(job_id), 'user_id': 'eq.' + g.user, 'select': '*'})
    if not rows: raise Problem('Generation not found.', 404)
    return rows[0]

def update_job(job_id, **values):
    rows = sb('PATCH', '/rest/v1/generations', params={'id':'eq.' + job_id, 'user_id':'eq.' + g.user}, json=values, headers={'Prefer':'return=representation'})
    return rows[0]

@app.get('/api/generations')
@signed_in
def history():
    return jsonify(sb('GET', '/rest/v1/generations', params={'user_id':'eq.' + g.user, 'select':'*', 'order':'created_at.desc', 'limit':'100'}))

def submit_provider(mode, arguments):
    endpoint = 'https://api.higgsfield.ai/kling-video/v3.0/std/' + ('image-to-video' if mode == 'image' else 'text-to-video')
    # httpx does not retry requests by default. No SDK retry wrapper for POST.
    response = httpx.post(endpoint, headers={'Authorization':'Key ' + env('HF_KEY')}, json=arguments, timeout=20)
    response.raise_for_status()
    return response.json()['request_id']

@app.post('/api/generations')
@signed_in
def generate():
    env('HF_KEY')
    data = request.get_json(silent=True) or {}
    prompt = data.get('prompt', '')
    if not isinstance(prompt, str) or not 10 <= len(prompt.strip()) <= 1200: raise Problem('Write a prompt between 10 and 1,200 characters.')
    mode = data.get('mode', 'text')
    duration, ratio, motion = data.get('duration', 5), data.get('ratio', '16:9'), data.get('motion', '')
    if mode not in ['text', 'image'] or type(duration) is not int or duration not in [5,10] or ratio not in ['16:9','9:16','1:1'] or motion not in ['', 'Dolly In', 'Orbit', 'FPV', 'Pan Left', 'Zoom Out', 'Static']:
        raise Problem('Unsupported generation settings.')
    image_path = data.get('image_path')
    if mode == 'image':
        if not isinstance(image_path, str) or not image_path.startswith(g.user + '/') or '..' in image_path or len(image_path.split('/')) != 2:
            raise Problem('Upload a reference image first.')
    job_id = uuid_string(data.get('id'))
    settings = dict(prompt=prompt.strip(), mode=mode, duration=duration, ratio=ratio, motion=motion, image_path=image_path if mode == 'image' else None)
    reserved = sb('POST', '/rest/v1/rpc/reserve_generation', json={'p_id':job_id, 'p_user':g.user, 'p_settings':settings})
    if reserved.get('error'): raise Problem(reserved['error'], 429)
    if not reserved['created']:
        if reserved['job']['settings'] != settings: raise Problem('Request identifier already used for different settings.', 409)
        return jsonify(reserved['job']), 200
    args = {'prompt': prompt.strip() + ('. Camera: ' + motion if motion else ''), 'duration': duration, 'sound':'off', 'multi_shots':False, 'cfg_scale':0.5}
    try:
        if mode == 'image':
            signed = sb('POST', '/storage/v1/object/sign/references/' + image_path, json={'expiresIn':86400})
            args['image_url'] = env('SUPABASE_URL') + '/storage/v1' + signed['signedURL']
        else: args['aspect_ratio'] = ratio
        # Submit exactly once; never blindly retry a possibly accepted billable request.
        provider_id = submit_provider(mode, args)
        row = update_job(job_id, provider_id=provider_id, status='queued')
    except Exception:
        # A timeout can occur after upstream accepted the job. Keep the reservation.
        row = update_job(job_id, status='needs_review', error='Submission could not be confirmed. Contact the owner before generating again.')
    return jsonify(row), 202

@app.get('/api/generations/<job_id>')
@signed_in
def status(job_id):
    row = get_job(job_id)
    if row['status'] in TERMINAL or not row.get('provider_id'): return jsonify(row)
    client = hf.SyncClient(api_key=env('HF_KEY'), timeout=20)
    state = client.status(row['provider_id'])
    if isinstance(state, hf.Completed):
        result = client.result(row['provider_id'])
        video = result.get('video', {})
        url = video.get('url') if isinstance(video, dict) else video
        if not isinstance(url, str) or urlparse(url).scheme != 'https': raise Problem('Provider returned an invalid video result.', 502)
        row = update_job(row['id'], status='completed', video_url=url)
    elif isinstance(state, hf.Cancelled):
        row = update_job(row['id'], status='cancelled', error=None)
    elif isinstance(state, (hf.Failed, hf.NSFW)):
        row = update_job(row['id'], status='failed', error='The provider could not generate this video. Try a different prompt.')
    else:
        row = update_job(row['id'], status='processing' if isinstance(state, hf.InProgress) else 'queued')
    return jsonify(row)

@app.post('/api/generations/<job_id>/cancel')
@signed_in
def cancel_generation(job_id):
    row = get_job(job_id)
    if row['status'] in TERMINAL:
        return jsonify(row)
    if row['status'] != 'queued' or not row.get('provider_id'):
        raise Problem('This generation can no longer be cancelled.', 409)

    try:
        hf.SyncClient(api_key=env('HF_KEY'), timeout=20).cancel(row['provider_id'])
    except Exception:
        # Do not mark a job cancelled unless the provider confirms the cancel request.
        raise Problem('The provider could not cancel this generation. Check its status instead.', 409)

    row = update_job(row['id'], status='cancelled', error=None)
    return jsonify(row)

@app.post('/api/uploads')
@signed_in
def upload():
    file = request.files.get('image')
    if not file: raise Problem('Choose an image.')
    raw = file.read()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ['PNG','JPEG','WEBP'] or img.width * img.height > 20000000: raise Problem('Use a PNG, JPG or WEBP under 20 megapixels.')
            img.load()
            clean = io.BytesIO()
            img.convert('RGB').save(clean, format='JPEG', quality=90)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError): raise Problem('This file is not a supported image.')
    path = g.user + '/' + str(uuid.uuid4()) + '.jpg'
    sb('POST', '/storage/v1/object/references/' + path, content=clean.getvalue(), headers={'Content-Type':'image/jpeg'})
    return jsonify(path=path)

if __name__ == '__main__': app.run(host='0.0.0.0', port=5000)
