"""Load the deployment's secret file, require CUDA, then start the upstream server."""

import os
import hashlib
import json
from pathlib import Path

import torch


key_path = Path(os.environ.pop("LAYA_API_KEY_FILE"))
key = key_path.read_text().strip()
if not key:
    raise RuntimeError("LAYA API key file is empty")
os.environ["LAYA_API_KEY"] = key
expected_domain_hash = os.environ.get("LAYA_DOMAIN_WEIGHT_SHA256")
expected_config_hash = os.environ.get("LAYA_DOMAIN_CONFIG_SHA256")
domain_path = Path(os.environ.get("LAYA_DOMAIN_MODEL_DIR", "/home/laya/.cache/huggingface/whatsapp-v1"))
if expected_domain_hash:
    weight = domain_path / "model.safetensors"
    if hashlib.sha256(weight.read_bytes()).hexdigest() != expected_domain_hash:
        raise RuntimeError("Domain checkpoint hash mismatch")
if expected_config_hash:
    config = domain_path / 'rl_agent_config.json'
    if hashlib.sha256(config.read_bytes()).hexdigest()!=expected_config_hash:
        raise RuntimeError('Domain calibration configuration hash mismatch')
if not torch.cuda.is_available():
    raise RuntimeError("CUDA is required for this LAYA deployment")
print(f"LAYA CUDA ready: {torch.cuda.get_device_name(0)}; torch={torch.__version__}")
from laya.serve import create_app
from starlette.responses import Response
import uvicorn

app = create_app()

@app.middleware('http')
async def checkpoint_revision(request, call_next):
    response = await call_next(request)
    if response.status_code==200 and request.url.path in ('/v1/systemone','/v1/systemone/batch') and expected_domain_hash:
        body = b''.join([chunk async for chunk in response.body_iterator])
        data = json.loads(body)
        for result in data.get('results',[data]):
            routing = result.get('routing',{})
            if routing.get('model')=='whatsapp-v1':
                routing['revision'] = expected_domain_hash
                routing['calibration_revision'] = expected_config_hash
        headers = dict(response.headers)
        headers.pop('content-length',None)
        return Response(json.dumps(data,ensure_ascii=False),status_code=200,headers=headers,media_type='application/json')
    return response

uvicorn.run(app,host=os.environ.get('LAYA_HOST','0.0.0.0'),port=int(os.environ.get('LAYA_PORT','8000')),log_level=os.environ.get('LAYA_LOG_LEVEL','warning'))
