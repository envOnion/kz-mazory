"""Validate production settings with ephemeral keys, without printing secrets or deploying."""

import os
import secrets
import base64
import subprocess
import sys
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("backend", nargs="?", default=str(Path(__file__).resolve().parents[1] / "backend"))
parser.add_argument("--runtime-context", help="Check running server containers using this Docker context")
args = parser.parse_args()
if args.runtime_context:
    names = ["mazory-backend", "mazory-qcluster", "mazory-qcluster-history",
             "mazory-qcluster-delivery", "mazory-qcluster-crm", "mazory-outbox"]
    result = subprocess.run(
        ["docker", "--context", args.runtime_context, "inspect", "--type", "container", *names],
        capture_output=True, text=True, check=True,
    )
    containers = json.loads(result.stdout)
    images = {container["Image"] for container in containers}
    if len(images) != 1 or not all(container["State"]["Running"] for container in containers):
        for container in containers:
            print(container["Name"], container["Config"]["Image"], container["State"]["Status"])
        raise SystemExit("Deployment mismatch: backend and all workers must run the same image.")
    print("Backend and all workers are running the same image.")
    raise SystemExit(0)

environment = {
    **os.environ,
    "MAZORY_ENV": "production",
    "SECRET_KEY": secrets.token_urlsafe(64),
    "JWT_SIGNING_KEY": secrets.token_urlsafe(64),
    "MFA_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "ALLOWED_HOSTS": "localhost",
    "CSRF_TRUSTED_ORIGINS": "https://localhost",
    "WAHA_API_KEY": secrets.token_urlsafe(32),
    "BITRIX_INBOUND_TOKEN": secrets.token_urlsafe(32),
}
subprocess.run(
    [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "ERROR"],
    cwd=Path(args.backend),
    env=environment,
    check=True,
)
