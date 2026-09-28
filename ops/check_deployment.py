"""Validate production settings with ephemeral keys, without printing secrets or deploying."""

import os
import secrets
import base64
import subprocess
import sys
from pathlib import Path

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
    cwd=Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(__file__).resolve().parents[1] / "backend",
    env=environment,
    check=True,
)
