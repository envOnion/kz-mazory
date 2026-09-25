import os
import json
import logging
import re
from typing import List, Dict, Any, Optional
import requests
from django.utils import timezone
from api.models import AISettings

logger = logging.getLogger(__name__)

class AIService:
    @staticmethod
    def get_embedding(text: str) -> List[float]:
        """
        Генерация плотного вектора (Dense Embedding) через настроенную в AISettings модель OpenRouter.
        По умолчанию: liquid/lfm-2.5-embedding-350m:free (размерность 1024).
        """
        cfg = AISettings.get_active()
        if not text or not text.strip():
            return [0.0] * cfg.embedding_dimension

        url = f"{cfg.embedding_provider_url.rstrip('/')}/embeddings"
        api_key = cfg.embedding_api_key or os.getenv('OPENROUTER_API_KEY', '')
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": cfg.embedding_model_name,
            "input": text.strip()[:4000]
        }

        try:
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                vec = data["data"][0]["embedding"]
                return [float(x) for x in vec]
            else:
                logger.error("OpenRouter Embedding error %s: %s", res.status_code, res.text)
        except Exception as e:
            logger.error("Failed to generate embedding via OpenRouter: %s", e)

        # Fallback на детерминированный вектор размерности embedding_dimension
        import numpy as np
        dim = cfg.embedding_dimension
        vec = np.zeros(dim, dtype=np.float32)
        for i, word in enumerate(text.lower().split()):
            pos = (hash(word) % dim + dim) % dim
            vec[pos] += 1.0 / (i + 1)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    @staticmethod
    def analyze_message_with_context(
        content: str,
        sender_name: str,
        context_messages: List[Dict[str, Any]],
        known_deals_summary: str
    ) -> Dict[str, Any]:
        """
        Анализ сообщения рабочей группы через большую чат-модель (nvidia/nemotron-3-ultra-550b-a55b:free).
        Включает семантический контекст предыдущих сообщений и список известных сделок компании.
        """
        cfg = AISettings.get_active()

        formatted_context = ""
        if context_messages:
            formatted_context = "\n".join([
                f"- [{m.get('timestamp', '')}] {m.get('sender_name', 'Коллега')}: {m.get('content', '')}"
                for m in context_messages
            ])
        else:
            formatted_context = "(Предыдущий контекст отсутствует)"

        user_prompt = f"""КОНТЕКСТ СУЩЕСТВУЮЩИХ СДЕЛОК КОМПАНИИ:
{known_deals_summary}

СВЯЗАННЫЙ КОНТЕКСТ ИЗ ПРЕДЫДУЩИХ СООБЩЕНИЙ ЧАТА:
{formatted_context}

ТЕКУЩЕЕ ВХОДЯЩЕЕ СООБЩЕНИЕ:
Отправитель: {sender_name}
Текст сообщения:
\"\"\"{content}\"\"\"

Проанализируй текст с учетом контекста. Извлеки факты для создания/обновления сделки.
Ответь ТОЛЬКО валидным JSON-объектом по заданной в системном промпте схеме. Никакого текста вокруг JSON."""

        url = f"{cfg.chat_provider_url.rstrip('/')}/chat/completions"
        chat_key = cfg.chat_api_key or os.getenv('OPENROUTER_API_KEY', '')
        headers = {
            "Authorization": f"Bearer {chat_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": cfg.chat_model_name,
            "messages": [
                {"role": "system", "content": cfg.system_prompt_worker},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": cfg.chat_temperature,
            "response_format": {"type": "json_object"} if "free" not in cfg.chat_model_name else None
        }

        # Remove None keys
        payload = {k: v for k, v in payload.items() if v is not None}

        for attempt in range(2):
            try:
                res = requests.post(url, json=payload, headers=headers, timeout=30)
                if res.status_code == 200:
                    resp_json = res.json()
                    raw_content = resp_json["choices"][0]["message"]["content"]
                    
                    # Извлечение JSON блока
                    match = re.search(r'\{.*\}', raw_content, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group(0))
                        return parsed
                else:
                    logger.warning("OpenRouter Chat error attempt %d (%s): %s", attempt, res.status_code, res.text)
            except Exception as e:
                logger.error("Chat LLM request failed: %s", e)

        # Fallback эвристика, если модель временно недоступна
        return AIService._heuristic_extraction(content, sender_name)

    @staticmethod
    def _heuristic_extraction(content: str, sender_name: str) -> Dict[str, Any]:
        """Резервный детерминированный парсер фактов при сбое сети"""
        amount_match = re.search(r'(\d+[\d\s.,]*)\s*(млн|млрд|тыс)?\s*(тенге|тг|₸)?', content, re.IGNORECASE)
        amount = None
        if amount_match:
            try:
                v = float(amount_match.group(1).replace(' ', '').replace(',', '.'))
                unit = (amount_match.group(2) or '').lower()
                multiplier = 1_000_000 if 'млн' in unit else (1_000_000_000 if 'млрд' in unit else (1_000 if 'тыс' in unit else 1))
                amount = v * multiplier
            except ValueError:
                pass

        # Извлечение названия объекта
        object_name = None
        obj_match = re.search(r'(?:объекту|объект|жк)\s+([A-Za-zА-Яа-я0-9\s\-]+?)(?=\s+(?:утвердили|согласовали|отправил|заключили|на|\.|\,|$))', content, re.IGNORECASE)
        if obj_match:
            object_name = obj_match.group(1).strip()
            if 'жк' in content.lower() and not object_name.lower().startswith('жк'):
                object_name = f"ЖК {object_name}"
        elif 'жк' in content.lower():
            m = re.search(r'(жк\s+[A-Za-zА-Яа-я0-9\-]+)', content, re.IGNORECASE)
            if m:
                object_name = m.group(1).strip()

        # Следующее действие менеджера (обязательство)
        next_action = None
        action_match = re.search(r'(?:до\s+[A-Za-zА-Яа-я]+|завтра|сегодня)?\s*(?:отправлю|согласую|подготовлю|закрою|передам|выставлю|подпишу)[^.!?]*', content, re.IGNORECASE)
        if action_match:
            next_action = action_match.group(0).strip()

        can_create = bool(object_name and amount)
        confidence = 0.85 if can_create else (0.6 if (amount or object_name) else 0.3)

        return {
            "is_deal_fact": bool(amount or 'жк' in content.lower() or 'бмк' in content.lower() or 'бтп' in content.lower()),
            "confidence": confidence,
            "object_name": object_name,
            "company_name": None,
            "direction": "БТП" if 'бтп' in content.lower() else ("БМК" if 'бмк' in content.lower() else "БМК / БТП"),
            "contract_number": None,
            "deal_period": None,
            "stage": "in_execution" if "договор" in content.lower() else "qualification",
            "contract_amount": amount,
            "cost_amount": None,
            "paid_amount": amount if ('оплачен' in content.lower() or 'поступил' in content.lower()) else None,
            "responsible_name": sender_name,
            "current_action": content[:120],
            "next_action": next_action,
            "next_action_at": None,
            "can_create_deal": can_create
        }

    @staticmethod
    def chat_assistant(prompt: str, context: Dict[str, Any]) -> str:
        """
        Чат-ассистент для пользователей веб-интерфейса Mazory.
        """
        cfg = AISettings.get_active()
        guidance = (
            "ИНСТРУКЦИЯ ПО ДАННЫМ:\n"
            "В блоке АКТУАЛЬНЫЕ ДАННЫЕ ВИТРИНЫ ДАННЫХ переданы точные финансовые показатели из PostgreSQL "
            "(включая portfolio_summary с агрегатами и matched_projects со списком проектов).\n"
            "Если пользователь просит посчитать сумму, рассчитать итоги, предоставить статистику или сделать сравнение — "
            "используй эти точные цифры, выполняй математические вычисления и давай структурированный, уверенный ответ на русском языке с суммами в тенге (₸).\n"
            "Никогда не утверждай, что данных нет или что matched_projects пуст, если данные присутствуют в блоках portfolio_summary или matched_projects."
        )
        system_content = f"{cfg.system_prompt_assistant}\n\n{guidance}\n\nАКТУАЛЬНЫЕ ДАННЫЕ ВИТРИНЫ ДАННЫХ:\n{json.dumps(context, ensure_ascii=False)}"

        url = f"{cfg.chat_provider_url.rstrip('/')}/chat/completions"
        chat_key = cfg.chat_api_key or os.getenv('OPENROUTER_API_KEY', '')
        headers = {
            "Authorization": f"Bearer {chat_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": cfg.chat_model_name,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3
        }

        try:
            res = requests.post(url, json=payload, headers=headers, timeout=25)
            if res.status_code == 200:
                data = res.json()
                return data["choices"][0]["message"]["content"]
            else:
                return f"Аналитическая модель временно недоступна (Код {res.status_code}). Попробуйте позже."
        except Exception as e:
            return f"Ошибка обращения к AI ассистенту: {e}"
