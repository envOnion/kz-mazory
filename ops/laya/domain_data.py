"""Synthetic topic membership only; customer messages never enter this corpus."""
import json
from pathlib import Path

INSTRUCTIONS = "Classify only the new message. Does it continue this concrete topic? Use B if the subject differs or evidence is insufficient."
OTHER = "Другая тема или недостаточно связи с этой конкретной темой."


def corpus(prefix, count, varied_descriptions=False):
    result = []
    for i in range(count):
        names = [f"{prefix} {i * 3 + k}" for k in range(3)]
        domains = ["поставка насосов", "монтаж котельной", "оплата теплообменника"]
        target = i % 3
        templates = [
            "По объекту {name} договор подписан, сегодня выставили счёт на оборудование.",
            "Заказчик {name} перечислил аванс, деньги поступили на расчётный счёт.",
            "Для {name} завтра отправлю коммерческое предложение на согласование.",
            "По {name} поставку перенесли на пятницу. Обсуждение с заказчиком продолжается.",
            "{name} бойынша шартқа қол қойылды, бүгін шот жіберілді.",
            "{name}: төлем түсті, жабдықты келесі аптада жеткіземіз.",
            "По {name} пока оплаты нет. Заказчик ждёт финансирования.",
            "{name} заказ отменил, поставки оборудования не будет.",
        ]
        no_match = i % 5 == 0
        name = f"Посторонний {prefix} {i}" if no_match else names[target]
        message = templates[i % len(templates)].format(name=name)
        topics = [f"Объект {n}: {domains[k]}. Переговоры с заказчиком о договоре, счёте, оплате и сроках поставки оборудования для {n}." for k, n in enumerate(names)]
        if varied_descriptions:
            formats = [
                lambda n,d: f'Объект {n}: {d}. Переговоры с заказчиком о договоре, счёте, оплате и сроках поставки оборудования для {n}.',
                lambda n,d: f'Обновление сроков поставки насосов по объекту {n}. Поставка насосов по объекту {n} согласована на следующую неделю, при этом обсуждение с заказчиком продолжается.',
                lambda n,d: f'Обновление сроков поставки насосов для {n}. Поставка насосов для объекта {n} согласована на следующую неделю. Обсуждение с заказчиком продолжается.',
                lambda n,d: f'Поставка оборудования для {n}. Договор, коммерческое предложение, оплата и сроки оборудования для {n}.',
                lambda n,d: f'Согласование поставки для объекта {n}. Заказчик и менеджер обсуждают поставку насосов и сроки исполнения по объекту {n}.',
                lambda n,d: f'Статус работ по {n}. Обсуждение {d}, условий договора, коммерческого предложения и плана работ для {n}.',
            ]
            topics = [formats[(i+k)%len(formats)](n,domains[k]) for k,n in enumerate(names)]
        questions = {f"q{k}": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": {"A": topic, "B": OTHER}} for k, topic in enumerate(topics)}
        result.append({"state": {"message": message, "sender": "Менеджер"}, "questions": questions,
                       "expected": {f"q{k}": "A" if k == target and not no_match else "B" for k in range(3)},
                       "expected_thread": None if no_match else target, "dialogue_id": f"{prefix}:{i}"})
    return result


if __name__ == "__main__":
    out = Path("/tmp/domain-data")
    out.mkdir(exist_ok=True)
    for name, prefix, count in [("train", "Учебный объект", 1000), ("calibration", "Проверочный объект", 300), ("heldout", "Независимый объект", 200)]:
        (out / f"{name}.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in corpus(prefix, count, varied_descriptions=name!='heldout')) + "\n")
