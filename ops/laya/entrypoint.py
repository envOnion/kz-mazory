"""Load the deployment's secret file, require CUDA, then start the upstream server."""

import os
from pathlib import Path

import torch


key_path = Path(os.environ.pop("LAYA_API_KEY_FILE"))
key = key_path.read_text().strip()
if not key:
    raise RuntimeError("LAYA API key file is empty")
os.environ["LAYA_API_KEY"] = key
if not torch.cuda.is_available():
    raise RuntimeError("CUDA is required for this LAYA deployment")
print(f"LAYA CUDA ready: {torch.cuda.get_device_name(0)}; torch={torch.__version__}")
os.execvp("laya-serve", ["laya-serve"])
