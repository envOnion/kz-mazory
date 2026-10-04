"""Run browser → API → durable outbox → Django Q2 → local provider e2e."""

import argparse
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--node", default=shutil.which("node"))
    args = parser.parse_args()
    if not args.node:
        parser.error("Node.js is required; use --node /absolute/path/to/node")
    if not (ROOT / "frontend/dist/index.html").exists():
        parser.error("Build frontend first: cd frontend && npm ci && npm run build")
    task_dir = Path(tempfile.mkdtemp(prefix="mazory-local-e2e-"))
    task_dir.chmod(0o700)
    (task_dir / "isolated-e2e.marker").touch()
    (task_dir / "provider-state.json").write_text("{}")
    cert, key = task_dir / "localhost.crt", task_dir / "localhost.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    from cryptography.fernet import Fernet
    from http.server import ThreadingHTTPServer
    from e2e.providers import ProviderHandler

    ProviderHandler.control = task_dir / "provider-state.json"
    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert), str(key))
    provider.socket = tls.wrap_socket(provider.socket, server_side=True)
    env = {
        **os.environ,
        "MAZORY_ENV": "test",
        "DJANGO_SETTINGS_MODULE": "e2e.settings",
        "MAZORY_E2E_DIR": str(task_dir),
        "MAZORY_E2E_PROVIDER_PORT": str(provider.server_port),
        "REQUESTS_CA_BUNDLE": str(cert),
        "AI_CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "MAZORY_E2E_URL": f"http://127.0.0.1:{args.port}",
        "MAZORY_E2E_OUTPUT": str(task_dir / "playwright"),
    }
    # Ignore production deployment env files; only the local marker selects test settings.
    env.pop("FORCE_POSTGRES_TEST", None)
    env.pop("Q_CLUSTER_NAME", None)
    os.environ.update(env)
    processes, logs = [], []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()

    def spawn(command, name):
        log = (task_dir / f"{name}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(
            command, cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        processes.append(process)
        return process

    try:
        subprocess.run(
            [sys.executable, "manage.py", "migrate", "--noinput"],
            cwd=BACKEND,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        import django

        django.setup()
        from e2e.seed import seed

        seed()
        spawn(
            [
                sys.executable,
                "manage.py",
                "runserver",
                f"127.0.0.1:{args.port}",
                "--noreload",
            ],
            "backend",
        )
        spawn([sys.executable, "manage.py", "qcluster"], "qcluster")
        spawn(
            [
                sys.executable,
                "manage.py",
                "shell",
                "-c",
                "from e2e.dispatch import dispatch_forever; dispatch_forever()",
            ],
            "outbox",
        )
        for _ in range(150):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("A local service exited; inspect logs")
            try:
                with urllib.request.urlopen(
                    env["MAZORY_E2E_URL"] + "/api/health/live/", timeout=1
                ):
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("Local backend did not become ready")
        print(f"Local services ready. Logs and test artifacts: {task_dir}", flush=True)
        result = subprocess.run(
            [
                args.node,
                str(ROOT / "frontend/node_modules/@playwright/test/cli.js"),
                "test",
                "--config=playwright.local.config.ts",
            ],
            cwd=ROOT / "frontend",
            env=env,
        )
        if result.returncode:
            print("Service errors:", flush=True)
            for name in ("backend", "qcluster", "outbox"):
                text = (task_dir / f"{name}.log").read_text()
                print(f"{name}:\n" + "\n".join(text.splitlines()[-30:]))
        return result.returncode
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        provider.shutdown()
        provider.server_close()
        # Session tokens and provider encryption keys are disposable, not test artifacts.
        (task_dir / "session.json").unlink(missing_ok=True)
        key.unlink(missing_ok=True)
        for name in ("e2e.sqlite3", "e2e.sqlite3-shm", "e2e.sqlite3-wal"):
            (task_dir / name).unlink(missing_ok=True)
        print(f"Services stopped; isolated DB removed. Logs: {task_dir}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
