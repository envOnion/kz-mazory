"""Docker-only launcher; no application dependencies or databases on the host."""
import subprocess
from pathlib import Path

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2]
    compose = ['docker', '--context', 'kk-minsk', 'compose', '-f', 'compose.e2e.yml']
    try:
        subprocess.run([*compose, 'up', '-d', '--build', 'nginx', 'qcluster', 'outbox'], cwd=root, check=True)
        result = subprocess.run([*compose, 'up', '--build', '--no-deps', '--abort-on-container-exit', '--exit-code-from', 'browser', 'browser'], cwd=root)
        raise SystemExit(result.returncode)
    finally:
        subprocess.run([*compose, 'down', '--volumes', '--remove-orphans'], cwd=root)
