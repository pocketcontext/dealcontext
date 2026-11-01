#!/usr/bin/env python3
"""Rehearse one-way legacy auth conversion with populated synthetic state."""
import argparse
import contextlib
import socket
import time
import urllib.request
import urllib.error
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
MIGRATION='1790800600_default_users.js'
FIXTURE=r'''
migrate((app)=>{
 const collection=app.findCollectionByNameOrId('agents');
 collection.oauth2.enabled=true;
 collection.oauth2.providers=[{name:'google',clientId:'synthetic-client',clientSecret:'synthetic-secret'}];
 app.save(collection);
 for(let n=0;n<4;n++){
  const r=new Record(collection);r.set('id','legacyaccount0'+n);r.set('name','Synthetic '+n);
  r.set('email','synthetic'+n+'@example.com');r.setPassword('SyntheticPassword123!');
  r.set('verified',n%2===0);r.set('disabled',n>=2);app.save(r);
  const label=new Record(app.findCollectionByNameOrId('agent_directory'));
  label.set('id',r.id);label.set('name',r.getString('name'));app.save(label);
 }
 const owner='legacyaccount00';
 function record(name,id,data){const r=new Record(app.findCollectionByNameOrId(name));r.set('id',id);for(const key in data)r.set(key,data[key]);app.save(r);return r;}
 record('organizations','syntheticorg001',{name:'Synthetic organization',owner,created_by:owner,updated_by:owner});
 record('people','syntheticperson',{name:'Synthetic person',owner,organization:'syntheticorg001',created_by:owner,updated_by:owner});
 record('pipelines','syntheticpipe01',{name:'Synthetic pipeline',active:true,created_by:owner,updated_by:owner});
 record('stages','syntheticstage1',{name:'Synthetic stage',pipeline:'syntheticpipe01',position:0,created_by:owner,updated_by:owner});
 record('deals','syntheticdeal01',{title:'Synthetic deal',stage:'syntheticstage1',organization:'syntheticorg001',person:'syntheticperson',owner,currency:'EUR',status:'open',created_by:owner,updated_by:owner});
 record('notes','syntheticnote01',{body:'Synthetic evidence',deal:'syntheticdeal01',owner,created_by:owner,updated_by:owner});
 record('audit_log','syntheticaudit1',{action:'create',collection:'deals',record:'syntheticdeal01',actor:owner,actor_type:'agent',changes:{synthetic:true}});
 record('_authOrigins','syntheticorigin',{collectionRef:collection.id,recordRef:owner,fingerprint:'synthetic-fingerprint'});
 record('_mfas','syntheticmfa001',{collectionRef:collection.id,recordRef:owner,method:'password'});
 record('_otps','syntheticotp001',{collectionRef:collection.id,recordRef:owner,password:'SyntheticOtp123!'});
 record('_externalAuths','syntheticoauth1',{collectionRef:collection.id,recordRef:owner,provider:'google',providerId:'synthetic-google-subject'});
});
'''


def snapshot(path):
 with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
  names=[r[0] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
  return {name:sorted(db.execute('SELECT * FROM "'+name+'"').fetchall(),key=repr) for name in names if name not in ['_migrations','_params']}


@contextlib.contextmanager
def running(common):
 with socket.socket() as sock:
  sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 process=subprocess.Popen(common+['serve','--http',f'127.0.0.1:{port}'],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 def request(path,body=None,token=None):
  headers={'Content-Type':'application/json'}
  if token:headers['Authorization']=token
  req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
  try:
   with urllib.request.urlopen(req,timeout=10) as response:return response.status,json.load(response)
  except urllib.error.HTTPError as error:return error.code,{}
 try:
  for _ in range(100):
   try:request('/api/health');break
   except OSError:time.sleep(.1)
  else:raise AssertionError('Synthetic server failed to start')
  yield request
 finally:
  process.terminate();process.wait(timeout=15)


def main():
 ap=argparse.ArgumentParser();ap.add_argument('--binary',required=True);args=ap.parse_args();binary=str(Path(args.binary).resolve())
 for conflict in [None,'users','file','index','late']:
  with tempfile.TemporaryDirectory(prefix='deal-users-migration-') as tmp:
   base=Path(tmp);migrations=base/'migrations';shutil.copytree(ROOT/'pb_migrations',migrations);(migrations/MIGRATION).unlink()
   fixture=FIXTURE
   if conflict=='users':(migrations/'1790800551_conflict.js').write_text("\nmigrate((app)=>{const r=new Record(app.findCollectionByNameOrId('users'));r.set('email','conflict@example.com');r.setPassword('SyntheticPassword123!');app.save(r);});")
   if conflict=='file':(migrations/'1790800551_conflict.js').write_text("\nmigrate((app)=>{const c=app.findCollectionByNameOrId('agents');c.fields.add(new FileField({name:'avatar'}));app.save(c);});")
   if conflict=='index':(migrations/'1790800551_conflict.js').write_text("migrate((app)=>{const c=app.findCollectionByNameOrId('agents');c.addIndex('synthetic_custom_index',false,'name','');app.save(c);});")
   (migrations/'1790800550_fixture.js').write_text(fixture)
   hooks=base/'hooks';hooks.mkdir();data=base/'data'
   common=[binary,'--dir',str(data),'--migrationsDir',str(migrations),'--hooksDir',str(hooks)]
   first=subprocess.run(common+['migrate','up'],cwd=ROOT,capture_output=True,text=True)
   assert first.returncode==0 and 'failed to apply' not in first.stderr, first.stderr
   old_token=None
   if conflict is None:
    with running(common) as request:
     status,auth=request('/api/collections/agents/auth-with-password',{'identity':'synthetic0@example.com','password':'SyntheticPassword123!'})
     assert status==200
     old_token=auth['token']
   before=snapshot(data/'data.db')
   with sqlite3.connect(data/'data.db') as db:
    legacy_id=db.execute("select id from _collections where name='agents'").fetchone()[0]
    options_before=json.loads(db.execute("select options from _collections where name='agents'").fetchone()[0])
    keys_before=dict(db.execute('select id,tokenKey from agents'))
    password_before=db.execute('select id,password,verified,disabled,email,name from agents order by id').fetchall()
   shutil.copy(ROOT/'pb_migrations'/MIGRATION,migrations/MIGRATION)
   if conflict=='late':
    target=migrations/MIGRATION;target.write_text(target.read_text().replace('app.delete(legacy);', "throw new Error('Synthetic failure after retarget');"))
   result=subprocess.run(common+['migrate','up'],cwd=ROOT,capture_output=True,text=True)
   after=snapshot(data/'data.db')
   if conflict:
    assert 'failed to apply' in result.stdout+result.stderr, (conflict, result.returncode, result.stdout, result.stderr, len(before.get('users',[])), len(before.get('agents',[])))
    assert before==after, 'Failed conversion changed existing data/schema'
    continue
   assert result.returncode==0 and 'failed to apply' not in result.stderr,result.stderr
   assert 'agents' not in after and len(after['users'])==4
   with sqlite3.connect(data/'data.db') as db:
    assert db.execute('select id,password,verified,disabled,email,name from users order by id').fetchall()==password_before
    options_after=json.loads(db.execute("select options from _collections where name='users'").fetchone()[0])
    assert options_after['oauth2']==options_before['oauth2'], 'Provider settings changed'
    for row in db.execute('select name,fields from _collections'):
     assert all(f.get('collectionId')!=legacy_id for f in json.loads(row[1])),row[0]
    assert db.execute('select collectionRef,recordRef from _externalAuths').fetchall()==[('_pb_users_auth_','legacyaccount00')]
    assert db.execute('select collectionRef,recordRef,fingerprint from _authOrigins').fetchall()==[('_pb_users_auth_','legacyaccount00','synthetic-fingerprint')]
    assert db.execute('select count(*) from _mfas').fetchone()[0]==0
    assert db.execute('select count(*) from _otps').fetchone()[0]==0
    assert all(key!=keys_before[uid] for uid,key in db.execute('select id,tokenKey from users'))
   for name,rows in before.items():
    if name in ['agents','users','_collections','_externalAuths','_authOrigins','_mfas','_otps']:continue
    assert rows==after[name], 'Business rows changed: '+name
   repeated=subprocess.run(common+['migrate','up'],cwd=ROOT,capture_output=True,text=True)
   assert snapshot(data/'data.db')==after, 'Repeated migration altered state'
   with running(common) as request:
    assert request('/api/collections/users/auth-refresh',{},old_token)[0]!=200, 'Legacy token remained valid'
    for n in range(4):
     status,auth=request('/api/collections/users/auth-with-password',{'identity':f'synthetic{n}@example.com','password':'SyntheticPassword123!'})
     if n>=2:assert status!=200, 'Disabled account admitted'
     else:assert status==200 and auth['record']['id']==f'legacyaccount0{n}', 'Password or account ID changed'
 print('Legacy users conversion: populated preservation, provider links, disabled accounts, conflicts and repeat-run passed')

if __name__=='__main__':main()
