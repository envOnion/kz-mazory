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
        qwen=os.getenv('MAZORY_E2E_MODEL')=='qwen3.8-27b'
        paths=('/props','/v1/models','/v1/chat/completions','/apply-template','/tokenize') if qwen else ('/api/chat','/api/version','/api/show')
        if self.path not in paths:
            self.send_error(404); return
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        if self.path in ('/api/chat','/api/show','/v1/chat/completions'):
            data=json.loads(body)
            if data.get('model')!=('qwen3.8-27b' if qwen else 'gemma4:e4b'): self.send_error(400); return
        request=Request(('http://127.0.0.1:11571' if qwen else 'http://127.0.0.1:11434')+self.path,data=body if self.command=='POST' else None,
                        headers={'Content-Type':'application/json'},method=self.command)
        try:
            with urlopen(request,timeout=1800 if qwen else 180) as response:
                content=response.read(); self.send_response(response.status)
                self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(content)))
                self.end_headers(); self.wfile.write(content)
        except HTTPError as exc:
            content=exc.read()
            self.send_response(exc.code)
            self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(content)))
            self.end_headers(); self.wfile.write(content)

if __name__=='__main__':
    if os.environ.get('MAZORY_E2E_REAL_MODEL')!='1': raise RuntimeError('Explicit real model profile required')
    ThreadingHTTPServer((os.environ['MAZORY_E2E_RELAY_BIND'],11435),Handler).serve_forever()
