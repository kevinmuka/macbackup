"""Internal macbackup i18n module."""

from __future__ import annotations

import builtins
import json
import os

def macbackup_language() -> str:
    return 'it' if os.environ.get('MACBACKUP_LANG', 'en').lower() == 'it' else 'en'



_ITALIAN_TRANSLATOR = None



def localized(value: str) -> str:
    if macbackup_language() != 'it':
        return value
    global _ITALIAN_TRANSLATOR
    if _ITALIAN_TRANSLATOR is None:
        from .locales.it import translate
        _ITALIAN_TRANSLATOR = translate
    return _ITALIAN_TRANSLATOR(value)



def message(template: str, **values) -> str:
    """Translate a whole template before inserting paths or other user data."""
    return localized(template).format(**values)



_TRANSLATED_JSON_FIELDS = {'reason', 'note', 'error', 'errors', 'warnings'}



def localized_json(value, key: str = ''):
    if isinstance(value, dict):
        return {name: localized_json(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [localized_json(item, key) for item in value]
    if isinstance(value, str) and key in _TRANSLATED_JSON_FIELDS:
        return localized(value)
    return value



def print(*values, **kwargs):
    """Translate presentation text and explanatory JSON values, never JSON keys."""
    if macbackup_language() != 'it':
        return builtins.print(*values, **kwargs)
    rendered = []
    for value in values:
        if isinstance(value, str):
            try:
                data = json.loads(value) if value.lstrip().startswith(('{', '[')) else None
            except json.JSONDecodeError:
                data = None
            if isinstance(data, (dict, list)):
                value = json.dumps(localized_json(data), ensure_ascii=False,
                                   indent=2 if '\n' in value else None)
            else:
                value = localized(value)
        rendered.append(value)
    return builtins.print(*rendered, **kwargs)
