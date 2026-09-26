import uuid
from worker import run as w
class Response:
 def __init__(self,value):self.value=value
 def json(self):return self.value

def test_claim_is_conditional(monkeypatch):
 row={'id':str(uuid.uuid4()),'provider_id':'selfhost:test'}
 def call(method,path,**kw):
  if method=='GET':return Response([row])
  assert kw['params']['status']=='eq.queued'
  return Response([])
 monkeypatch.setattr(w,'call',call)
 assert w.claim() is None

def test_reference_rejects_other_owner():
 import pytest
 with pytest.raises(ValueError):w.reference_path({'user_id':str(uuid.uuid4()),'settings':{'image_path':'other/file.jpg'}})

def test_render_failure_marks_failed(monkeypatch):
 row={'id':str(uuid.uuid4()),'user_id':str(uuid.uuid4())};updates=[]
 monkeypatch.setattr(w,'claim',lambda:row)
 monkeypatch.setattr(w,'render',lambda *a:(_ for _ in ()).throw(RuntimeError('GPU unavailable')))
 monkeypatch.setattr(w,'finish',lambda row,**values:updates.append(values))
 assert w.run_once() is True
 assert updates[0]['status']=='failed'
 assert 'GPU unavailable' not in updates[0]['error']
