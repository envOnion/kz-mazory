"""Independent API evaluation, with extra difficult input beyond training templates."""
import json
import math
import time
from pathlib import Path
from urllib.request import Request, urlopen
from domain_data import corpus, INSTRUCTIONS, OTHER

def evaluation_rows():
    rows = corpus('Независимый объект', 200)
    names = ['Северный парк', 'Каратау', 'Жетысу', 'Лунный остров', 'Арман', 'Көкжиек', 'Красный маяк', 'Күлтегін']
    for i in range(200):
        a, b = names[i % 8] + f' корпус {i}', names[(i + 3) % 8] + f' корпус {i + 1}'
        topic = lambda name: f'Поставка котельного оборудования для {name}. Договор, коммерческое предложение, оплата и сроки оборудования для {name}.'
        target = i % 3
        texts = [f'Заказчик {a} утвердил коммерческое предложение, поставку согласовали на следующую неделю.',
                 f'{b} бойынша бүгін төлем жасалды. Шартқа қол қойылды.',
                 f'На другом объекте {a} склад произошёл ремонт крыши, к поставке котлов это не относится.']
        rows.append({'state': {'message': texts[target], 'sender': 'Менеджер'},
                     'questions': {f'q{k}': {'type': 'choice', 'instructions': INSTRUCTIONS, 'criteria': {'A': topic(name), 'B': OTHER}} for k, name in enumerate([a,b])},
                     'expected_thread': target if target < 2 else None, 'dialogue_id': f'hard:{i}'})
    return rows

def main():
    key = Path('/run/secrets/laya_api_key').read_text().strip()
    rows = evaluation_rows()
    reports = []
    for model in ['multilingual', 'whatsapp-v1']:
        accepted = correct = 0
        latencies, decisions = [], []
        for index, row in enumerate(rows):
            start = time.monotonic()
            questions = {q:{**value, 'criteria':dict(reversed(list(value['criteria'].items()))) if index % 2 else value['criteria']} for q,value in row['questions'].items()}
            request = Request('http://127.0.0.1:8000/v1/systemone', data=json.dumps({'model': model, 'state': row['state'], 'questions': questions, 'max_len': 2048, 'head_max_len': 256}).encode(), headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
            with urlopen(request, timeout=30) as response: data = json.load(response)
            latencies.append((time.monotonic() - start) * 1000)
            ranked = sorted([(int(q[1:]), answer['probabilities']['A']) for q,answer in data['answers'].items()], key=lambda pair: -pair[1])
            selected = ranked[0][0] if ranked[0][1] >= .90 and ranked[0][1] - ranked[1][1] >= .15 else None
            if selected is not None:
                accepted += 1
                correct += selected == row['expected_thread']
            decisions.append({'id':row['dialogue_id'],'expected':row['expected_thread'],'selected':selected,'scores':ranked})
        n = accepted
        p = correct/n if n else 0
        z = 1.96
        centre = (p+z*z/(2*n))/(1+z*z/n) if n else 0
        radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n) if n else 0
        reports.append({'model':model,'total':len(rows),'accepted':accepted,'correct':correct,'precision':p,'coverage':accepted/len(rows),
            'precision_wilson95':[centre-radius,centre+radius],'p95_ms':sorted(latencies)[int(.95*len(latencies))],'decisions':decisions,'pass':p>=.98 and accepted/len(rows)>=.40})
    Path('/tmp/domain-evaluation.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
    print(json.dumps([{k:v for k,v in report.items() if k!='decisions'} for report in reports],ensure_ascii=False),flush=True)

if __name__=='__main__':
    main()
