"""Explicit user-authorized file verification; network writes to Bitrix are impossible."""
import json
import os
import re
import hmac
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit

READ_METHODS = frozenset({"crm.deal.get", "crm.deal.list", "crm.company.get", "crm.company.list", "crm.status.list", "user.get"})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+CREDENTIALS['relay_key']):
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 2_000_000:
            self.send_error(400)
            return
        body = self.rfile.read(length)
        match = re.fullmatch(r"/rest/1/fixture/([a-z.]+)\.json", self.path)
        if match:
            method = match[1]
            allowed = method in READ_METHODS
            with (DIRECTORY / "relay-audit.jsonl").open("a") as stream:
                stream.write(json.dumps({"method": method, "allowed": allowed}) + "\n")
            if not allowed:
                self.send_error(403)
                return
            target = CREDENTIALS["webhook"].rstrip("/") + "/" + method + ".json"
            headers = {"Content-Type": "application/json"}
        elif self.path in ("/v1/systemone", "/v1/systemone/batch"):
            target = "https://laya.kk-minsk.by" + self.path
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + CREDENTIALS["laya_key"]}
        else:
            self.send_error(404)
            return
        try:
            with build_opener(NoRedirect()).open(Request(target, data=body, headers=headers), timeout=30) as response:
                content = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except HTTPError as exc:
            self.send_error(exc.code)
        except Exception:
            self.send_error(502)


if __name__ == "__main__":
    if os.environ.get("MAZORY_E2E_READ_ONLY_BITRIX") != "1" or not Path("/.dockerenv").exists():
        raise RuntimeError("Explicit isolated read-only verification required")
    DIRECTORY = Path("/verification")
    CREDENTIALS = json.loads((DIRECTORY / "proxy-credentials.json").read_text())
    parsed = urlsplit(CREDENTIALS["webhook"])
    if parsed.scheme != "https" or parsed.hostname != "aquakip.bitrix24.kz" or parsed.port not in (None, 443) or parsed.username or parsed.query:
        raise RuntimeError("Unexpected portal")
    CREDENTIALS["laya_key"] = Path("/run/secrets/laya.key").read_text().strip()
    CREDENTIALS['relay_key'] = (DIRECTORY / 'relay.key').read_text().strip()
    ThreadingHTTPServer((os.environ["MAZORY_E2E_RELAY_BIND"], 11436), Handler).serve_forever()
