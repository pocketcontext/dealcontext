#!/usr/bin/env python3
"""Synthetic REST/SQL tests for demo source lifecycle, ownership and erasure."""
import argparse,json,pathlib,socket,subprocess,tempfile,time,urllib.request,urllib.error
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True);args=parser.parse_args()
 with tempfile.TemporaryDirectory(prefix='demo-sync-model-') as tmp:
  common=[str(pathlib.Path(args.binary).resolve()),'--dir',tmp+'/data','--migrationsDir',str(ROOT/'pb_migrations'),'--hooksDir',str(ROOT/'pb_hooks')]
  result=subprocess.run(common+['superuser','upsert','admin@example.com','SyntheticAdminPassword123!'],cwd=ROOT,capture_output=True,text=True)
  assert result.returncode==0,result.stderr
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  base=f'http://127.0.0.1:{port}'
  with open(tmp+'/server.log','w+') as log:
   proc=subprocess.Popen(common+['serve','--http',f'127.0.0.1:{port}'],cwd=ROOT,stdout=log,stderr=log)
   def req(method,path,body=None,token=None,expected=200):
    request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':token} if token else {})},method=method)
    try:
     with urllib.request.urlopen(request,timeout=15) as response:status=response.status;raw=response.read()
    except urllib.error.HTTPError as error:status=error.code;raw=error.read()
    assert status==expected,(status,expected,raw)
    return json.loads(raw) if raw else None
   def post(collection,body,token,expected=200):return req('POST','/api/collections/'+collection+'/records',body,token,expected)
   def patch(collection,id,body,token,expected=200):return req('PATCH','/api/collections/'+collection+'/records/'+id,body,token,expected)
   try:
    for _ in range(150):
     try:req('GET','/api/health');break
     except OSError:time.sleep(.1)
    else:raise AssertionError('startup timeout')
    admin=req('POST','/api/collections/_superusers/auth-with-password',{'identity':'admin@example.com','password':'SyntheticAdminPassword123!'})['token']
    sessions=[]
    for who in ['one','two']:
     post('agents',{'email':who+'@example.com','name':who,'password':'SyntheticPassword123!','passwordConfirm':'SyntheticPassword123!'},admin)
     sessions.append(req('POST','/api/collections/agents/auth-with-password',{'identity':who+'@example.com','password':'SyntheticPassword123!'}))
    a,b=[s['token'] for s in sessions]
    source='vaultcontext-demo';runbody={'source':source,'snapshot_id':'snapshot-one','generated_at':100,'status':'running','counts':{},'expected_run_count':0}
    post('demo_sync_runs',runbody,None,400)
    for invalid_count in [None,-1,True,1.5,'0']:
     post('demo_sync_runs',{**runbody,'expected_run_count':invalid_count},a,400)
    post('demo_sync_runs',{**runbody,'generated_at':int(time.time())+3600},a,400)
    run=post('demo_sync_runs',runbody,a);assert run['sync_revision']==1
    post('demo_sync_runs',{**runbody,'snapshot_id':'concurrent','expected_run_count':1},b,400)
    rowbody={'source':source,'subject':'synthetic-subject','email':'private@example.com','sales':True,'newsletter':False,'revision':1,'last_used':99,'review_due':999,'terms':'terms-1','consent':'consent-1','sync_run':run['id']}
    post('demo_contacts',rowbody,b,403)
    post('demo_contacts',{**rowbody,'source':'wikicontext-demo'},a,400)
    row=post('demo_contacts',{**rowbody,'created_by':sessions[1]['record']['id'],'sync_revision':900},a)
    assert row['created_by']==sessions[0]['record']['id'] and row['sync_revision']==1
    post('demo_contacts',rowbody,a,400)
    patch('demo_contacts',row['id'],{'sales':False,'expected_sync_revision':0},a,409)
    patch('demo_contacts',row['id'],{'subject':'changed','expected_sync_revision':1},a,400)
    row=patch('demo_contacts',row['id'],{'sales':False,'expected_sync_revision':1},a);assert row['sync_revision']==2
    event=post('demo_consent_events',{'source':source,'source_id':1,'subject':rowbody['subject'],'at':99,'sales':True,'revision':1,'terms':'terms-1','consent':'consent-1','sync_run':run['id']},a)
    post('demo_suppressions',{'source':source,'email':'suppressed@example.com','sales':True,'newsletter':True,'source_updated':100,'sync_run':run['id']},a)
    post('demo_review_events',{'source':source,'source_id':1,'subject':rowbody['subject'],'at':100,'decision':'retain','reason':'synthetic private reason','revision':2,'sync_run':run['id']},a)
    patch('demo_sync_runs',run['id'],{'status':'complete','expected_sync_revision':1},b,403)
    run=patch('demo_sync_runs',run['id'],{'status':'complete','counts':{'source':{'contacts':1}},'expected_sync_revision':1},a);assert run['completed_at'] and run['sync_revision']==2
    patch('demo_contacts',row['id'],{'sales':True,'expected_sync_revision':2},a,400)
    patch('demo_sync_runs',run['id'],{'status':'running','expected_sync_revision':2},a,400)
    post('demo_sync_runs',{**runbody,'snapshot_id':'stale','generated_at':99,'expected_run_count':1},a,409)
    # Both exports have generated_at=100, but the baseline predates the committed run.
    post('demo_sync_runs',{**runbody,'snapshot_id':'same-second-stale'},a,409)
    run2=post('demo_sync_runs',{**runbody,'snapshot_id':'snapshot-two','generated_at':100,'expected_run_count':1},a)
    req('DELETE','/api/collections/demo_contacts/records/'+row['id'],{'sync_run':run2['id'],'expected_sync_revision':1},a,409)
    req('DELETE','/api/collections/demo_contacts/records/'+row['id'],{'sync_run':run2['id'],'expected_sync_revision':2},b,403)
    # A stale item makes the entire batch roll back, including the first delete.
    req('POST','/api/batch',{'requests':[
     {'method':'DELETE','url':'/api/collections/demo_contacts/records/'+row['id'],'body':{'sync_run':run2['id'],'expected_sync_revision':2}},
     {'method':'DELETE','url':'/api/collections/demo_consent_events/records/'+event['id'],'body':{'sync_run':run2['id'],'expected_sync_revision':0}},
    ]},a,400)
    req('GET','/api/collections/demo_contacts/records/'+row['id'],token=a)
    req('DELETE','/api/collections/demo_contacts/records/'+row['id'],{'sync_run':run2['id'],'expected_sync_revision':2},a,204)
    req('GET','/api/collections/demo_contacts/records/'+row['id'],token=a,expected=404)
    data=req('POST','/api/context/query',{'sql':"SELECT changes FROM audit_log WHERE collection LIKE 'demo_%'"},a)
    serialized=json.dumps(data)
    for secret in ['private@example.com','synthetic-subject','terms-1','consent-1','suppressed@example.com','synthetic private reason']:assert secret not in serialized,secret
    patch('demo_sync_runs',run2['id'],{'status':'failed','expected_sync_revision':1},a)
    post('demo_sync_runs',{**runbody,'snapshot_id':'after-failed-stale','generated_at':102,'expected_run_count':1},a,409)
    run3=post('demo_sync_runs',{**runbody,'snapshot_id':'after-failed-fresh','generated_at':102,'expected_run_count':2},a)
    patch('demo_sync_runs',run3['id'],{'status':'complete','expected_sync_revision':1},a)
    print('PASS demo sync model: auth, owner, source uniqueness, run lock, CAS, stale snapshots, batch rollback and PII-free audit erasure')
   finally:proc.terminate();proc.wait(timeout=20)
if __name__=='__main__':main()
