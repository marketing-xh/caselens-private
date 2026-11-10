import sys,os,tempfile,threading,json,base64
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).parent))
import server
from http.server import ThreadingHTTPServer
with tempfile.TemporaryDirectory() as d:
  server.DB=Path(d)/'temp.sqlite'
  server.DATABASE_URL=''
  os.environ['CASELENS_USER']='team';os.environ['CASELENS_PASSWORD']='local-test-password'
  srv=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
  t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
  base=f'http://127.0.0.1:{srv.server_port}'
  auth='Basic '+base64.b64encode(b'team:local-test-password').decode()
  def ask(path, method='GET',payload=None, headers=None):
    raw=None if payload is None else json.dumps(payload).encode()
    h=dict(headers or {})
    if payload is not None:h['Content-Type']='application/json'
    q=Request(base+path,data=raw,headers=h,method=method)
    try:
      with urlopen(q,timeout=8) as res:return res.status,res.read()
    except HTTPError as e:return e.code,e.read()
  assert ask('/')[0]==401
  assert ask('/healthz',method='HEAD')[0]==200
  assert ask('/',method='HEAD',headers={'Authorization':auth})[0]==200
  assert ask('/',headers={'Authorization':auth})[0]==200
  url='https://www.tiktok.com/@pydlifeblanks'
  status,_=ask('/api/profile?url='+url,headers={'Authorization':auth})
  assert status==200,status
  st,body=ask('/api/verify-public',method='POST',payload={'url':url,'fields':{'followers':'89900','total_likes':'583900','video_count':'214','account_country':''}},headers={'Authorization':auth})
  assert st==200,(st,body)
  status,body=ask('/api/cases',headers={'Authorization':auth})
  assert status==200
  item=json.loads(body)['items'][0]['data']
  assert item['stats']['followers']==89900
  assert item['stats']['total_likes']==583900
  assert item['manual_verified_at']
  status,body=ask('/api/profile?url='+url,headers={'Authorization':auth})
  assert status==200,(status,body)
  d=json.loads(body)
  assert d['stats']['total_likes']==583900
  assert d['manual_verified']['total_likes']==583900
  assert ask('/api/cases')[0]==401
  srv.shutdown()
print('HTTP QA PASS: auth, HEAD health, profile scenario, verified public stats persisted, refresh retains stats')
