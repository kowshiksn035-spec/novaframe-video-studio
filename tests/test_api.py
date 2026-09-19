import io, uuid
from types import SimpleNamespace
import pytest
from api import index as m
UID='11111111-1111-1111-1111-111111111111'
@pytest.fixture
def client(monkeypatch):
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

def test_health_setup_required(client,monkeypatch):
 monkeypatch.delenv('SUPABASE_URL',raising=False)
 r=client.get('/api/health');assert r.status_code==200;assert r.json['status']=='setup_required';assert r.json['ok'] is False

def test_health_ready(client,monkeypatch):
 for key,value in {'SUPABASE_URL':'https://example.supabase.co','SUPABASE_ANON_KEY':'anon','SUPABASE_SERVICE_ROLE_KEY':'service','ALLOWED_EMAILS':'creator@example.com','HF_KEY':'test:secret'}.items():monkeypatch.setenv(key,value)
 monkeypatch.setattr(m,'sb',lambda *a,**k:[])
 r=client.get('/api/health');assert r.status_code==200;assert r.json['status']=='ready';assert r.json['supabase_reachable'] is True
@pytest.mark.parametrize('changes',[{'duration':True},{'duration':300},{'prompt':'short'},{'ratio':'bad'},{'mode':'image','image_path':'other/file.jpg'}])
def test_validation(client,monkeypatch,changes):
 auth(monkeypatch,lambda *a,**k:pytest.fail('Invalid request reached DB'));assert post(client,payload(**changes)).status_code==400

def test_owner_scope(client,monkeypatch):
 def handler(method,path,**kw):
  assert kw['params']['user_id']=='eq.'+UID;return []
 auth(monkeypatch,handler);assert client.get('/api/generations').json==[]
def test_duplicate(client,monkeypatch):
 data=payload();settings={k:v for k,v in data.items() if k!='id'};settings['image_path']=None
 auth(monkeypatch,lambda *a,**k:{'created':False,'job':{'id':data['id'],'settings':settings,'status':'queued'}})
 monkeypatch.setattr(m,'submit_provider',lambda *a,**k:pytest.fail('Duplicate provider call'));assert post(client,data).status_code==200

def test_quota(client,monkeypatch):
 auth(monkeypatch,lambda *a,**k:{'error':'Daily beta limit reached.'});monkeypatch.setattr(m,'submit_provider',lambda *a,**k:pytest.fail('Quota bypass'));assert post(client,payload()).status_code==429

def test_submit(client,monkeypatch):
 def handler(method,path,**kw):
  if 'reserve' in path:return {'created':True}
  assert kw['json']=={'provider_id':'p1','status':'queued'};return [{'id':'job',**kw['json']}]
 auth(monkeypatch,handler)
 def submit(model,arguments):
  assert model=='text';assert arguments['duration']==5;return 'p1'
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

def test_provider_wire_contract(monkeypatch):
 monkeypatch.setenv('HF_KEY','test:secret')
 calls=[]
 def fake(url,**kw):
  calls.append((url,kw));return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'request_id':'provider-1'})
 monkeypatch.setattr(m.httpx,'post',fake)
 assert m.submit_provider('image',{'image_url':'https://example.com/input.jpg'})=='provider-1'
 assert calls[0][0]=='https://api.higgsfield.ai/kling-video/v3.0/std/image-to-video'
 assert calls[0][1]['headers']['Authorization']=='Key test:secret'
 assert len(calls)==1
