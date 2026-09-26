import io, uuid
from types import SimpleNamespace
import pytest
from api import index as m
UID='11111111-1111-1111-1111-111111111111'
@pytest.fixture
def client(monkeypatch):
 monkeypatch.setenv('VIDEO_BACKEND','higgsfield')
 monkeypatch.setenv('ALLOWED_EMAILS','creator@example.com');monkeypatch.setenv('HF_KEY','test:secret');monkeypatch.setenv('APP_ORIGIN','http://localhost')
 m.app.config['TESTING']=True;c=m.app.test_client();c.set_cookie('nf_access','test',path='/api');return c

def auth(monkeypatch,handler):
 def fake(method,path,**kw):
  if path=='/auth/v1/user':return {'id':UID,'email':'creator@example.com'}
  return handler(method,path,**kw)
 monkeypatch.setattr(m,'sb',fake)
def payload(**kw):return {'id':str(uuid.uuid4()),'prompt':'A quiet forest under moonlight','mode':'text','duration':5,'ratio':'16:9','motion':'',**kw}
def post(c,d):return c.post('/api/generations',json=d,headers={'Origin':'http://localhost'})
def test_csrf(client):assert client.post('/api/generations',json=payload(),headers={'Origin':'https://evil.example'}).status_code==403
def test_anonymous(client):
 client.delete_cookie('nf_access',path='/api');assert client.get('/api/generations').status_code==401
def test_unconfigured(client,monkeypatch):
 monkeypatch.delenv('SUPABASE_URL',raising=False);assert client.get('/api/config').json['generation_ready'] is False

def test_config_exposes_model_registry(client):
 config=client.get('/api/config').json
 assert config['default_model']=='kling-3-standard'
 assert config['models']==[{'id':'kling-3-standard','label':'Kling 3.0 Standard'}]

def test_health_setup_required(client,monkeypatch):
 monkeypatch.delenv('SUPABASE_URL',raising=False)
 r=client.get('/api/health');assert r.status_code==200;assert r.json['status']=='setup_required';assert r.json['ok'] is False

def test_health_ready(client,monkeypatch):
 for key,value in {'SUPABASE_URL':'https://example.supabase.co','SUPABASE_ANON_KEY':'anon','SUPABASE_SERVICE_ROLE_KEY':'service','ALLOWED_EMAILS':'creator@example.com','HF_KEY':'test:secret'}.items():monkeypatch.setenv(key,value)
 monkeypatch.setattr(m,'sb',lambda *a,**k:[])
 r=client.get('/api/health');assert r.status_code==200;assert r.json['status']=='ready';assert r.json['supabase_reachable'] is True

def test_health_rejects_malformed_provider_key(client,monkeypatch):
 for key,value in {'SUPABASE_URL':'https://example.supabase.co','SUPABASE_ANON_KEY':'anon','SUPABASE_SERVICE_ROLE_KEY':'service','ALLOWED_EMAILS':'creator@example.com','HF_KEY':'not-a-paired-key'}.items():monkeypatch.setenv(key,value)
 monkeypatch.setattr(m,'sb',lambda *a,**k:[])
 r=client.get('/api/health');assert r.status_code==200;assert r.json['status']=='setup_required';assert r.json['provider_configured'] is False

def test_health_degraded_when_supabase_unreachable(client,monkeypatch):
 for key,value in {'SUPABASE_URL':'https://example.supabase.co','SUPABASE_ANON_KEY':'anon','SUPABASE_SERVICE_ROLE_KEY':'service','ALLOWED_EMAILS':'creator@example.com','HF_KEY':'test:secret'}.items():monkeypatch.setenv(key,value)
 monkeypatch.setattr(m,'sb',lambda *a,**k:(_ for _ in ()).throw(m.Problem('db down',502)))
 r=client.get('/api/health');assert r.status_code==503;assert r.json['status']=='degraded';assert r.json['supabase_reachable'] is False
@pytest.mark.parametrize('changes',[{'duration':True},{'duration':300},{'prompt':'short'},{'ratio':'bad'},{'model':'unknown-model'},{'mode':'image','image_path':'other/file.jpg'}])
def test_validation(client,monkeypatch,changes):
 auth(monkeypatch,lambda *a,**k:pytest.fail('Invalid request reached DB'));assert post(client,payload(**changes)).status_code==400

def test_owner_scope(client,monkeypatch):
 def handler(method,path,**kw):
  assert kw['params']['user_id']=='eq.'+UID;return []
 auth(monkeypatch,handler);assert client.get('/api/generations').json==[]

def test_active_generation_is_owner_scoped(client,monkeypatch):
 job={'id':str(uuid.uuid4()),'status':'queued','provider_id':'p1'}
 def handler(method,path,**kw):
  params=kw['params'];assert params['user_id']=='eq.'+UID;assert params['status']=='in.(submitting,queued,processing)';assert params['limit']=='1';return [job]
 auth(monkeypatch,handler);assert client.get('/api/generations/active').json==job

def test_active_generation_returns_null_when_idle(client,monkeypatch):
 auth(monkeypatch,lambda *a,**k:[]);assert client.get('/api/generations/active').json is None
def test_duplicate(client,monkeypatch):
 data=payload();settings={k:v for k,v in data.items() if k!='id'};settings['model']='kling-3-standard';settings['image_path']=None
 auth(monkeypatch,lambda *a,**k:{'created':False,'job':{'id':data['id'],'settings':settings,'status':'queued'}})
 monkeypatch.setattr(m,'submit_provider',lambda *a,**k:pytest.fail('Duplicate provider call'));assert post(client,data).status_code==200

def test_quota(client,monkeypatch):
 auth(monkeypatch,lambda *a,**k:{'error':'Daily beta limit reached.'});monkeypatch.setattr(m,'submit_provider',lambda *a,**k:pytest.fail('Quota bypass'));assert post(client,payload()).status_code==429

def test_submit(client,monkeypatch):
 def handler(method,path,**kw):
  if 'reserve' in path:return {'created':True}
  assert kw['json']=={'provider_id':'p1','status':'queued'};return [{'id':'job',**kw['json']}]
 auth(monkeypatch,handler)
 def submit(model_id,mode,arguments):
  assert model_id=='kling-3-standard';assert mode=='text';assert arguments['duration']==5;return 'p1'
 monkeypatch.setattr(m,'submit_provider',submit);assert post(client,payload()).json['provider_id']=='p1'

def test_ambiguous_submit(client,monkeypatch):
 def handler(method,path,**kw):
  if 'reserve' in path:return {'created':True}
  assert kw['json']['status']=='needs_review';return [kw['json']]
 auth(monkeypatch,handler);calls=[]
 def submit(*a,**k):calls.append(1);raise TimeoutError()
 monkeypatch.setattr(m,'submit_provider',submit);assert post(client,payload()).json['status']=='needs_review';assert len(calls)==1

def test_other_user_job(client,monkeypatch):
 auth(monkeypatch,lambda *a,**k:[]);assert client.get('/api/generations/'+str(uuid.uuid4())).status_code==404

def test_invalid_image(client,monkeypatch):
 auth(monkeypatch,lambda *a,**k:pytest.fail('Bad upload saved'));r=client.post('/api/uploads',data={'image':(io.BytesIO(b'<script>bad</script>'),'x.jpg')},headers={'Origin':'http://localhost'});assert r.status_code==400

def test_completed(client,monkeypatch):
 job=str(uuid.uuid4())
 def handler(method,path,**kw):return [{'id':job,'status':'queued','provider_id':'p1'}] if method=='GET' else [{'id':job,**kw['json']}]
 auth(monkeypatch,handler);monkeypatch.setattr(m.hf,'SyncClient',lambda **k:SimpleNamespace(status=lambda _:m.hf.Completed(),result=lambda _:{'video':{'url':'https://cdn.example/video.mp4'}}));r=client.get('/api/generations/'+job);assert r.json['status']=='completed';assert r.json['video_url'].endswith('.mp4')

def test_provider_cancelled_status(client,monkeypatch):
 job=str(uuid.uuid4())
 def handler(method,path,**kw):return [{'id':job,'status':'queued','provider_id':'p1'}] if method=='GET' else [{'id':job,**kw['json']}]
 auth(monkeypatch,handler);monkeypatch.setattr(m.hf,'SyncClient',lambda **k:SimpleNamespace(status=lambda _:m.hf.Cancelled()));r=client.get('/api/generations/'+job);assert r.json['status']=='cancelled'

def test_cancel_queued_generation(client,monkeypatch):
 job=str(uuid.uuid4());cancelled=[]
 def handler(method,path,**kw):
  if method=='GET':return [{'id':job,'status':'queued','provider_id':'p1'}]
  assert kw['json']=={'status':'cancelled','error':None};return [{'id':job,'status':'cancelled','provider_id':'p1','error':None}]
 auth(monkeypatch,handler);monkeypatch.setattr(m.hf,'SyncClient',lambda **k:SimpleNamespace(cancel=lambda provider_id:cancelled.append(provider_id)))
 r=client.post('/api/generations/'+job+'/cancel',headers={'Origin':'http://localhost'});assert r.status_code==200;assert r.json['status']=='cancelled';assert cancelled==['p1']

def test_cancel_processing_generation_is_rejected(client,monkeypatch):
 job=str(uuid.uuid4())
 auth(monkeypatch,lambda method,path,**kw:[{'id':job,'status':'processing','provider_id':'p1'}])
 monkeypatch.setattr(m.hf,'SyncClient',lambda **k:pytest.fail('Provider cancel should not be called'))
 r=client.post('/api/generations/'+job+'/cancel',headers={'Origin':'http://localhost'});assert r.status_code==409

def test_provider_wire_contract(monkeypatch):
 monkeypatch.setenv('HF_KEY','test:secret')
 calls=[]
 def fake(url,**kw):
  calls.append((url,kw));return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'request_id':'provider-1'})
 monkeypatch.setattr(m.httpx,'post',fake)
 assert m.submit_provider('kling-3-standard','image',{'image_url':'https://example.com/input.jpg'})=='provider-1'
 assert calls[0][0]=='https://api.higgsfield.ai/kling-video/v3.0/std/image-to-video'
 assert calls[0][1]['headers']['Authorization']=='Key test:secret'
 assert len(calls)==1

def test_local_submission_never_calls_provider(client,monkeypatch):
 monkeypatch.setenv('VIDEO_BACKEND','selfhost');monkeypatch.setenv('SELFHOST_ENABLED','1');monkeypatch.delenv('HF_KEY',raising=False)
 monkeypatch.setattr(m,'submit_provider',lambda *a:pytest.fail('Paid provider called'))
 def handler(method,path,**kw):
  if path.endswith('reserve_generation'):return {'created':True}
  assert method=='PATCH';assert kw['json']['provider_id'].startswith('selfhost:');return [kw['json']]
 auth(monkeypatch,handler)
 assert post(client,payload(duration=6,ratio='3:2')).json['status']=='queued'

def test_local_requires_explicit_enable(client,monkeypatch):
 monkeypatch.setenv('VIDEO_BACKEND','selfhost');monkeypatch.delenv('SELFHOST_ENABLED',raising=False)
 auth(monkeypatch,lambda *a,**k:pytest.fail('Disabled queue touched'))
 assert post(client,payload(duration=6,ratio='3:2')).status_code==503

def test_local_poll_does_not_contact_higgsfield(client,monkeypatch):
 job={'id':str(uuid.uuid4()),'status':'processing','provider_id':'selfhost:abc'}
 auth(monkeypatch,lambda *a,**k:[job]);monkeypatch.delenv('HF_KEY',raising=False)
 assert client.get('/api/generations/'+job['id']).json==job

def test_local_cancel_race(client,monkeypatch):
 job={'id':str(uuid.uuid4()),'status':'queued','provider_id':'selfhost:abc'}
 def handler(method,path,**kw):
  if method=='GET':return [job]
  assert kw['params']['status']=='eq.queued';return []
 auth(monkeypatch,handler)
 assert client.post('/api/generations/'+job['id']+'/cancel',headers={'Origin':'http://localhost'}).status_code==409

def test_output_signed_only_for_owner_path(client,monkeypatch):
 job={'id':str(uuid.uuid4()),'user_id':UID,'status':'completed','video_url':'storage://generated-videos/another-user/file.mp4'}
 auth(monkeypatch,lambda *a,**k:[job])
 assert client.get('/api/generations/'+job['id']).status_code==502

def test_local_output_signed(client,monkeypatch):
 job={'id':str(uuid.uuid4()),'user_id':UID,'status':'completed'}
 job['video_url']=f"storage://generated-videos/{UID}/{job['id']}.mp4"
 monkeypatch.setenv('SUPABASE_URL','https://test.supabase.co')
 def handler(method,path,**kw):
  if method=='GET':return [job]
  assert path=='/storage/v1/object/sign/'+job['video_url'][10:];return {'signedURL':'/object/sign/private?token=test'}
 auth(monkeypatch,handler)
 assert client.get('/api/generations/'+job['id']).json['video_url'].startswith('https://test.supabase.co/storage/v1/')
