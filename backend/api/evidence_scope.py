"""Select relevant literal clauses when an original contains several actions."""
import re


def statements(text):
    return re.split(r'(?<=[.!?])\s+|\n+|,\s+(?:а|но|и)\s+|,\s+(?=(?:сегодня|завтра|послезавтра)\b)', text, flags=re.I)


def delivery_action(text, action):
    """Keep whole evidence stored; use the promised artifact for validation only.

    An unlabelled deadline reply still applies to the existing promise. Other
    action types retain the conservative complete-message checks.
    """
    pattern = (r'\bкп\b|коммерческ\w*\s+предлож' if re.search(r'\bкп\b|коммерческ\w*\s+предлож', action, re.I)
               else r'таблиц' if re.search(r'таблиц', action, re.I) else None)
    if pattern is None:
        return text
    matches = [part for part in statements(text) if re.search(pattern, part, re.I)]
    verb = r'отправ|переда|высл|вышл|скин|направ' if re.search(r'отправ|переда|высл|вышл|скин|направ', action, re.I) else r'подготов|готов' if re.search(r'подготов', action, re.I) else None
    if verb:
        specific = [part for part in matches if re.search(verb, part, re.I)]
        if not specific:
            return text
        matches = specific
    if len(matches) > 1:
        # Recipient/object words distinguish two deliveries of the same artifact.
        ignored = {'отпра', 'перед', 'подго', 'комме', 'предл', 'табли', 'объек', 'проек', 'завтр', 'сегод', 'утром'}
        words = {word[:5] for word in re.findall(r'\w+', action.casefold()) if len(word)>3} - ignored
        scores = [len(words & {word[:5] for word in re.findall(r'\w+', part.casefold()) if len(word)>3}) for part in matches]
        if max(scores)>0:
            matches = [part for part,score in zip(matches,scores) if score==max(scores)]
    return '\n'.join(matches) if matches else text
