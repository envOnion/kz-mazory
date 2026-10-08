"""Docker-only relay to the existing kk-minsk model engine; synthetic E2E only."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self): self.forward()
    def do_POST(self): self.forward()
    def forward(self):
        if self.path not in ('/api/chat','/api/version','/api/show'):
            self.send_error(404); return
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        if self.path in ('/api/chat','/api/show'):
            data=json.loads(body)
            if data.get('model')!='gemma4:e4b': self.send_error(400); return
        request=Request('http://127.0.0.1:11434'+self.path,data=body if self.command=='POST' else None,
                        headers={'Content-Type':'application/json'},method=self.command)
        try:
            with urlopen(request,timeout=180) as response:
                content=response.read(); self.send_response(response.status)
                self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(content)))
                self.end_headers(); self.wfile.write(content)
        except HTTPError as exc: self.send_error(exc.code)

if __name__=='__main__':
    if os.environ.get('MAZORY_E2E_REAL_MODEL')!='1': raise RuntimeError('Explicit real model profile required')
    ThreadingHTTPServer((os.environ['MAZORY_E2E_RELAY_BIND'],11435),Handler).serve_forever()
