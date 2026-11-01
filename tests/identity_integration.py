#!/usr/bin/env python3
"""Actual application hooks under signed identity exchange, using synthetic TLS/JWKS."""
import argparse
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch
from auth_server import ROOT, server


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


@contextlib.contextmanager
def issuer():
    with tempfile.TemporaryDirectory(prefix='identity-fixture-') as folder:
        key, cert = Path(folder)/'key.pem', Path(folder)/'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        modulus = subprocess.run(['openssl','rsa','-in',str(key),'-noout','-modulus'], check=True,capture_output=True).stdout.decode().strip().split('=')[1]
        jwks = {'keys':[{'kty':'RSA','kid':'synthetic-key','alg':'RS256','use':'sig','n':b64(bytes.fromhex(modulus)), 'e':'AQAB'}]}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_GET(self):
                self.send_response(200 if self.path=='/.well-known/jwks.json' else 404)
                self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps(jwks).encode())
        http = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert,key)
        http.socket = tls.wrap_socket(http.socket,server_side=True)
        thread = threading.Thread(target=http.serve_forever,daemon=True); thread.start()
        origin = f'https://127.0.0.1:{http.server_port}'
        def sign(email='member@example.com', **overrides):
            now=int(time.time())
            claims={'iss':origin,'aud':'https://app.example.com','sub':email,'email':email,'email_verified':True,'hd':'example.com','iat':now,'nbf':now,'exp':now+60,'jti':secrets.token_urlsafe(24),'name':'Synthetic member',**overrides}
            payload=b64(json.dumps({'alg':'RS256','kid':'synthetic-key','typ':'JWT'}).encode())+'.'+b64(json.dumps(claims).encode())
            signed=subprocess.run(['openssl','dgst','-sha256','-sign',str(key)],input=payload.encode(),capture_output=True,check=True).stdout
            return payload+'.'+b64(signed)
        try:
            with patch.dict(os.environ,{'SSL_CERT_FILE':str(cert),'DEALCONTEXT_GOOGLE_WORKSPACE_DOMAIN':'example.com'}):
                yield origin,sign
        finally:
            http.shutdown(); http.server_close(); thread.join()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--binary',required=True);args=ap.parse_args()
    with issuer() as (origin,sign):
        config=json.loads((ROOT/'pocketcontext.json').read_text())
        config['identity']={'issuer':origin,'audience':'https://app.example.com','workspaceDomain':'example.com','allowProvisioning':True}
        with server(args.binary,context_config=config) as req:
            admin=req('POST','/api/collections/_superusers/auth-with-password',{'identity':'admin@example.com','password':'SyntheticAdminPassword123!'})['token']
            def exchange(assertion=None,expected=200):
                return req('POST','/api/context/auth-with-assertion',{'assertion':assertion or sign()},expected=expected)
            for changes in [{'aud':'https://other.example.com'},{'hd':'outside.com'},{'email_verified':False},{'exp':int(time.time())-1}]:
                exchange(sign(**changes),expected=(400,401,403))
            once=sign(); auth=exchange(once); user=auth['record']; token=auth['token']
            assert user['collectionName']=='users' and user['verified'] and not user.get('disabled')
            claims=json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'=='))
            assert 840 <= claims['exp']-time.time() <= 900
            exchange(once,expected=(400,401,403))
            assert exchange()['record']['id']==user['id']
            assert req('POST','/api/collections/users/auth-refresh',token=token)['token']==token
            req('GET','/api/context/schema',token=token)
            # Separate issuer-sub cannot take over an already bound email.
            exchange(sign(sub='other-subject'),expected=(400,401,403))
            directory=req('GET','/api/collections/agent_directory/records/'+user['id'],token=token)
            assert directory['name'], 'Identity-created users need CRM directory labels'
            req('PATCH','/api/collections/users/records/'+user['id'],{'disabled':True},admin)
            exchange(expected=(400,401,403))
            req('GET','/api/context/schema',token=token,expected=(401,403))
            req('PATCH','/api/collections/users/records/'+user['id'],{'disabled':False},admin)
            req('GET','/api/context/schema',token=token,expected=(401,403))
            assert exchange()['record']['id']==user['id']
    print('Hosted identity application integration passed')


if __name__=='__main__':main()
