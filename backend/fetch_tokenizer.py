"""Fetch pinned, checksum-verified tokenizer data at build time, never model code/weights."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def fetch_manifest(root, manifest):
    target = root / manifest["revision"]
    target.mkdir(exist_ok=True)
    for name, checksum in manifest["files"].items():
        path = target / name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == checksum:
            continue
        url = f"https://huggingface.co/{manifest['repo']}/resolve/{manifest['revision']}/{name}"
        with urlopen(url, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError(f"Tokenizer checksum mismatch: {name}")
        path.write_bytes(data)
    print(f"Verified tokenizer: {manifest['repo']}@{manifest['revision']}")


def main():
    root = Path(__file__).resolve().parent / "tokenizers"
    for path in sorted(root.glob("*manifest.json")):
        fetch_manifest(root, json.loads(path.read_text()))


if __name__ == "__main__":
    main()
