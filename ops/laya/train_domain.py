"""Run inside the LAYA Docker image on kk-minsk; independent evaluation is separate."""
import json
import warnings
import os
from pathlib import Path
from laya.train import finetune, TrainConfig

warnings.filterwarnings("ignore", message="Conflicting schema detected")
base = next(Path("/home/laya/.cache/huggingface/hub").rglob("multilingual/rl_agent_config.json")).parent
report = finetune("/tmp/domain-data/train.jsonl", str(base), os.getenv('LAYA_TRAIN_OUTPUT', '/home/laya/.cache/huggingface/whatsapp-v1'),
                  TrainConfig(epochs=3, micro_batch=2, grad_accum=4, loss="soft-ce", shuffle_options=("choice",),
                              max_len=512, head_max_len=256, log_every=100, calib_frac=0.15,
                              eval_data="/tmp/domain-data/calibration.jsonl", target_error=0.02, seed=12841), device="cuda")
Path("/tmp/domain-data/training-summary.json").write_text(json.dumps(report, indent=2))
