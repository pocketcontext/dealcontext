#!/usr/bin/env python3
"""Reader HTTP and real browser smoke using a disposable database and synthetic records."""
import argparse,json,pathlib,re,socket,subprocess,tempfile,time,urllib.request,urllib.error,os
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True);parser.add_argument('--browser',action='store_true');args=parser.parse_args()
 assert (ROOT/'ui/dist/index.html').exists(),'Run pnpm build in ui first'
 with tempfile.TemporaryDirectory(prefix=ROOT.name+'-reader-') as tmp:
  common=[str(pathlib.Path(args.binary).resolve()),'--dir',tmp+'/data','--migrationsDir',str(ROOT/'pb_migrations'),'--hooksDir',str(ROOT/'pb_hooks')]
  r=subprocess.run(common+['superuser','upsert','admin@example.com','SyntheticAdminPassword123!'],cwd=ROOT,capture_output=True,text=True);assert r.returncode==0,r.stderr
  with socket.socket()as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  base=f'http://127.0.0.1:{port}'
  with open(tmp+'/server.log','w')as log:
   proc=subprocess.Popen(common+['serve','--http',f'127.0.0.1:{port}'],cwd=ROOT,stdout=log,stderr=log)
   def request(path,body=None,token=None):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':token}if token else{})})
    with urllib.request.urlopen(req,timeout=15)as response:return response.status,response.headers,response.read()
   def api(path,body=None,token=None):return json.loads(request(path,body,token)[2])
   try:
    for _ in range(150):
     try:request('/api/health');break
     except OSError:time.sleep(.1)
    else:raise AssertionError('startup timeout')
    _,headers,body=request('/');assert 'default-src' in headers['Content-Security-Policy'];assert headers['Cache-Control']=='no-store';assert b'<div id="root">'in body
    for asset in re.findall(rb'(?:src|href)="(/assets/[^\"]+)"',body):assert request(asset.decode())[0]==200
    auth=json.loads((ROOT/'pocketcontext.json').read_text())['authCollection'];admin=api('/api/collections/_superusers/auth-with-password',{'identity':'admin@example.com','password':'SyntheticAdminPassword123!'})['token']
    api('/api/collections/'+auth+'/records',{'email':'reader@example.com','name':'Synthetic reader','password':'SyntheticReaderPassword123!','passwordConfirm':'SyntheticReaderPassword123!'},admin)
    session=api('/api/collections/'+auth+'/auth-with-password',{'identity':'reader@example.com','password':'SyntheticReaderPassword123!'});token=session['token']
    schema=api('/api/context/schema',token=token);snapshot=json.loads((ROOT/'ui/src/schema.snapshot.json').read_text())
    fields={t['name']:{c['name']for c in t['columns']}for t in schema['tables']}
    for table in snapshot['tables']:
     assert {c['name']for c in table['columns']}==fields[table['name']],table['name']
     api('/api/context/query',{'sql':'SELECT * FROM "'+table['name']+'" LIMIT 31'},token)
    table='projects'if ROOT.name=='taskcontext'else'databases'if ROOT.name=='metacontext'else'organizations'
    records=[]
    for i in range(35):
     data={'name':f'Synthetic reader {i:02}'}
     if ROOT.name=='dealcontext':data['owner']=session['record']['id']
     if table=='projects':data['key']='R'+str(i)
     if table=='databases':data['endpoint']='https://synthetic.invalid'
     records.append(api('/api/collections/'+table+'/records',data,token))
    if args.browser:
     env=dict(os.environ,READER_TEST_ORIGIN=base,READER_TEST_AUTH=auth,READER_TEST_TABLE=table,READER_TEST_ID=records[0]['id'])
     subprocess.run(['node','tests/reader-browser.cjs'],cwd=ROOT,env=env,check=True)
    print('PASS reader assets/security headers, authenticated SQL schema/queries'+(', real browser login/deep link/search/pagination/mobile'if args.browser else''))
   finally:proc.terminate();proc.wait(timeout=20)
if __name__=='__main__':main()
