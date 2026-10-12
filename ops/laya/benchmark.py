"""Warm HTTPS latency for the application's actual 16+4 topic request shape."""
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

def main():
    key = Path('/run/secrets/laya_api_key').read_text().strip()
    latency = []
    instruction = 'Classify only the new message. Does it continue this concrete topic? Use B if the subject differs or evidence is insufficient.'
    for index in range(105):
        started = time.monotonic()
        for offset in (0,16):
            questions = {f'q{i-offset}':{'type':'choice','instructions':instruction,'criteria':{
                'A':f'Объект Бенчмарк {i}: поставка насосов. Переговоры с заказчиком о поставке для Бенчмарк {i}.',
                'B':'Другая тема или недостаточно связи с этой конкретной темой.'}} for i in range(offset,min(offset+16,20))}
            payload = {'state':{'message':'По Бенчмарк 7 поставку перенесли на пятницу. Обсуждение с заказчиком продолжается.','sender':'Менеджер'},'questions':questions,'model':'whatsapp-v1','max_len':2048,'head_max_len':256}
            request = Request('https://laya.kk-minsk.by/v1/systemone',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            with urlopen(request,timeout=20) as response:
                result = json.load(response)
            usage = result['usage']
            if usage['truncated'] or usage['state_tokens_dropped'] or usage['truncated_questions'] or set(result['answers'])!=set(questions):
                raise RuntimeError('Incomplete benchmark input')
        if index>=5:
            latency.append((time.monotonic()-started)*1000)
    report = {'topics':20,'requests_per_message':2,'samples':len(latency),'p95_ms':sorted(latency)[94],'max_ms':max(latency),'weight_sha256':result['routing']['revision'],'calibration_sha256':result['routing']['calibration_revision']}
    Path('/tmp/laya-latency.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)

if __name__=='__main__':
    main()
