"""Filling a step's settings from what earlier steps gave.

A setting is text with `{{uid.path}}` tokens (uid = the step it comes from, path = a dotted
key into that step's output; `{{secret.NAME}}` reads a stored secret), or a lookup table:

    {"map": {"on": "{{s1.level}}", "rules": [{"op": "lte", "value": "2", "then": "5"}], "else": ""}}

A setting that is exactly one token keeps the value's type (a number stays a number).
"""
import re
from decimal import Decimal, InvalidOperation

TOKEN = re.compile(r'\{\{\s*([A-Za-z0-9_]+)\.([^{}]+?)\s*\}\}')

OPS = [
    ('eq', 'is'),
    ('ne', 'is not'),
    ('gt', 'is more than'),
    ('gte', 'is at least'),
    ('lt', 'is less than'),
    ('lte', 'is at most'),
    ('contains', 'contains'),
    ('true', 'is true'),
    ('false', 'is false'),
    ('empty', 'is empty'),
    ('not_empty', 'is not empty'),
]
UNARY = {'true', 'false', 'empty', 'not_empty'}
MAP_OPS = [('eq', 'is'), ('gte', 'is at least'), ('lte', 'is at most'), ('between', 'is between')]


class Missing(Exception):
    pass


def dig(obj, path):
    """`a.b.0.c` into nested dicts and lists; None when it isn't there."""
    for part in path.split('.'):
        if isinstance(obj, dict):
            if part not in obj:
                return None
            obj = obj[part]
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


def flatten(obj, prefix='', limit=60):
    """[(dotted key, value)] for every leaf, for the editor's field list."""
    out = []

    def walk(value, key):
        if len(out) >= limit:
            return
        if isinstance(value, dict) and value:
            for k, v in value.items():
                walk(v, f'{key}.{k}' if key else str(k))
        elif key:
            out.append((key, value))        # a list stays one field: a "For each" step goes through it

    walk(obj, prefix)
    return out


def unflatten(pairs):
    """{'person.name': 'Maria'} -> {'person': {'name': 'Maria'}}."""
    out = {}
    for key, value in pairs:
        node = out
        parts = key.split('.')
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        node[parts[-1]] = value
    return out


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _lookup(src, path, ctx, secrets):
    if src == 'secret':
        return (secrets or {}).get(path, '')
    return dig(ctx.get(src), path)


def resolve(value, ctx, secrets=None):
    """A setting's stored value -> the real one."""
    if isinstance(value, dict) and 'map' in value:
        return _resolve_map(value['map'] or {}, ctx, secrets)
    if not isinstance(value, str):
        return value
    whole = TOKEN.fullmatch(value.strip())
    if whole:
        return _lookup(whole.group(1), whole.group(2), ctx, secrets)
    return TOKEN.sub(lambda m: text(_lookup(m.group(1), m.group(2), ctx, secrets)), value)


def number(value):
    """A Decimal from 3, 3.5, "3", "$10", "1,200.50"; None when it isn't a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    cleaned = str(value).strip().replace(',', '').lstrip('$€£').strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ('true', 'yes', 'y', '1', 'on')
    return bool(value)


def is_empty(value):
    return value is None or value == '' or value == [] or value == {}


def compare(left, op, right=None):
    """The check behind "Only continue if…" and the lookup-table rules."""
    if op == 'true':
        return truthy(left)
    if op == 'false':
        return not truthy(left)
    if op == 'empty':
        return is_empty(left)
    if op == 'not_empty':
        return not is_empty(left)
    ln, rn = number(left), number(right)
    if op in ('gt', 'gte', 'lt', 'lte'):
        if ln is None or rn is None:
            return False
        return {'gt': ln > rn, 'gte': ln >= rn, 'lt': ln < rn, 'lte': ln <= rn}[op]
    if op in ('eq', 'ne'):
        if ln is not None and rn is not None:
            same = ln == rn
        elif isinstance(left, bool) or isinstance(right, bool):
            same = truthy(left) == truthy(right)
        else:
            same = text(left).strip().lower() == text(right).strip().lower()
        return same if op == 'eq' else not same
    if op == 'contains':
        if isinstance(left, list):
            return any(text(item).strip().lower() == text(right).strip().lower() for item in left)
        return text(right).strip().lower() in text(left).lower()
    return False


def _resolve_map(spec, ctx, secrets):
    on = resolve(spec.get('on', ''), ctx, secrets)
    for rule in spec.get('rules') or []:
        op = rule.get('op', 'eq')
        first = resolve(rule.get('value', ''), ctx, secrets)
        if op == 'between':
            hit = compare(on, 'gte', first) and compare(on, 'lte', resolve(rule.get('value2', ''), ctx, secrets))
        else:
            hit = compare(on, op, first)
        if hit:
            return resolve(rule.get('then', ''), ctx, secrets)
    return resolve(spec.get('else', ''), ctx, secrets)


def coerce(value, type_):
    """Shape a resolved value for a building: raises ValueError with a plain message."""
    if type_ == 'list' and isinstance(value, list):
        return value                # an empty list is still a list: the loop just has nothing to do
    if is_empty(value):
        return None
    if type_ == 'int':
        n = number(value)
        if n is None or n != n.to_integral_value():
            raise ValueError(f'"{text(value)}" is not a whole number')
        return int(n)
    if type_ in ('number', 'money'):
        n = number(value)
        if n is None:
            raise ValueError(f'"{text(value)}" is not a number')
        return float(n)
    if type_ == 'bool':
        return truthy(value)
    if type_ in ('string', 'datetime'):
        return text(value)
    if type_ == 'list':
        return as_list(value)
    return value


def as_list(value):
    """What a "For each" goes through: a list as it is, or text split at commas and line breaks."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[,\n]', value) if part.strip()]
    raise ValueError(f'"{text(value)}" isn\'t a list')


def tokens_in(value):
    """Every (uid, path) a stored setting refers to."""
    found = []
    if isinstance(value, dict) and 'map' in value:
        spec = value['map'] or {}
        found += tokens_in(spec.get('on', '')) + tokens_in(spec.get('else', ''))
        for rule in spec.get('rules') or []:
            for key in ('value', 'value2', 'then'):
                found += tokens_in(rule.get(key, ''))
    elif isinstance(value, str):
        found += [(m.group(1), m.group(2)) for m in TOKEN.finditer(value)]
    return found
