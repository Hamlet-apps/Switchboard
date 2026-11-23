"""Holding buildings to what their manifest says (docs/PROTOCOL.md, section 1).

Three checks, from strict to gentle:

- `manifest_problems`: when a building is connected or checked again. A manifest Switchboard
  can't build steps from is refused; smaller slips (an example of the wrong type) are kept as
  notes on the connection for whoever maintains the building.
- `check_params` / `literal_problem`: a step's settings against the action's params, before
  anything is sent, so a wrong value is caught here with a plain message instead of as a 422.
- `mismatches`: what a building answers, and the events it sends, against the fields it
  declared. These never fail a run; they are noted on the connection (`remember`).
"""
import datetime
import re

from .buildings import BuildingError
from .templating import EXPR, coerce, is_empty

TYPES = ('string', 'int', 'number', 'bool', 'datetime', 'money')
FIELD_TYPES = TYPES + ('list',)        # fields (outputs, events) may also be lists; params may not
LIST_LIMIT = 200
NAME = re.compile(r'^[a-z0-9_]+(\.[a-z0-9_]+)*$')
KEY = re.compile(r'^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$')
MAX_NOTES = 20


def type_error(value, type_):
    """'' when `value` is of a manifest type, else what it should have been."""
    if type_ == 'string':
        return '' if isinstance(value, str) else 'should be text'
    if type_ == 'bool':
        return '' if isinstance(value, bool) else 'should be true or false'
    if type_ == 'int':
        return '' if isinstance(value, int) and not isinstance(value, bool) else 'should be a whole number'
    if type_ in ('number', 'money'):
        return '' if isinstance(value, (int, float)) and not isinstance(value, bool) else 'should be a number'
    if type_ == 'datetime':
        if isinstance(value, str):
            try:
                datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
                return ''
            except ValueError:
                pass
        return 'should be a date and time like 2026-11-19T16:20:04-08:00'
    if type_ == 'list':
        return '' if isinstance(value, list) and all(isinstance(item, dict) for item in value) else 'should be a list of objects'
    return ''


def _fields(items, where, errors, notes, nested=False):
    if not isinstance(items, list):
        errors.append(f'{where}: the fields should be a list')
        return
    seen = set()
    for field in items:
        if not isinstance(field, dict) or not isinstance(field.get('key'), str) or not KEY.match(field['key']):
            errors.append(f'{where}: every field needs a key like "person.name"')
            continue
        key = field['key']
        if key in seen:
            notes.append(f'{where}: the field "{key}" is listed twice')
        seen.add(key)
        if field.get('type') not in FIELD_TYPES:
            errors.append(f'{where}: the field "{key}" has no type Switchboard knows (one of {", ".join(FIELD_TYPES)})')
        elif field['type'] == 'list':
            items = field.get('items')
            if nested:
                errors.append(f'{where}: the list "{key}" is inside another list')
            elif not isinstance(items, list) or not items:
                errors.append(f'{where}: the list "{key}" does not say what its items hold')
            else:
                _fields(items, f'{where}, list "{key}"', errors, notes, nested=True)
                if not isinstance(field.get('example'), list) or not field['example']:
                    notes.append(f'{where}: the list "{key}" has no example item')
        elif 'example' not in field:
            notes.append(f'{where}: the field "{key}" has no example')
        elif type_error(field['example'], field['type']):
            notes.append(f'{where}: the example for "{key}" {type_error(field["example"], field["type"])}')


def _params(items, where, errors, notes):
    if not isinstance(items, list):
        errors.append(f'{where}: the params should be a list')
        return
    for param in items:
        if not isinstance(param, dict) or not isinstance(param.get('key'), str) or not KEY.match(param['key']):
            errors.append(f'{where}: every param needs a key')
            continue
        key = param['key']
        if param.get('type') not in TYPES:
            errors.append(f'{where}: the param "{key}" has no type Switchboard knows (one of {", ".join(TYPES)})')
            continue
        if not isinstance(param.get('label'), str) or not param['label']:
            notes.append(f'{where}: the param "{key}" has no label')
        for limit in ('min', 'max', 'max_length'):
            if limit in param and (isinstance(param[limit], bool) or not isinstance(param[limit], (int, float))):
                notes.append(f'{where}: "{limit}" on "{key}" should be a number')
        if 'choices' in param and not (isinstance(param['choices'], list) and all(
                isinstance(c, dict) and 'value' in c for c in param['choices'])):
            notes.append(f'{where}: the choices for "{key}" should be a list of {{"value", "label"}}')
        if 'default' in param and param['default'] is not None and type_error(param['default'], param['type']):
            notes.append(f'{where}: the default for "{key}" {type_error(param["default"], param["type"])}')


def manifest_problems(manifest):
    """(errors, notes) for a manifest. Errors mean Switchboard can't use it."""
    errors, notes = [], []
    names = set()
    for trigger in manifest.get('triggers') or []:
        name = trigger.get('type')
        where = f'event "{name}"'
        if not isinstance(name, str) or not NAME.match(name):
            errors.append(f'{where}: the type should be lowercase words joined by dots, like "shift.signed_up"')
            continue
        if name in names:
            errors.append(f'{where} is listed twice')
        names.add(name)
        if not trigger.get('title'):
            notes.append(f'{where} has no title')
        _fields(trigger.get('fields') or [], where, errors, notes)
    names = set()
    for action in manifest.get('actions') or []:
        name = action.get('name')
        where = f'action "{name}"'
        if not isinstance(name, str) or not NAME.match(name):
            errors.append(f'{where}: the name should be lowercase words joined by dots, like "person.find"')
            continue
        if name in names:
            errors.append(f'{where} is listed twice')
        names.add(name)
        if action.get('kind') not in ('lookup', 'action'):
            errors.append(f'{where}: "kind" should be "lookup" or "action"')
        if str(action.get('method') or 'POST').upper() != 'POST':
            errors.append(f'{where}: the method should be POST')
        if not action.get('title'):
            notes.append(f'{where} has no title')
        _params(action.get('params') or [], where, errors, notes)
        _fields(action.get('output') or [], where, errors, notes)
    return errors, notes


def limit_error(param, value):
    """'' when `value` (already of the param's type) is within the param's limits."""
    choices = param.get('choices')
    if choices and value not in [c.get('value') for c in choices]:
        return 'should be one of: ' + ', '.join(str(c.get('label', c.get('value'))) for c in choices)
    if isinstance(value, str) and isinstance(param.get('max_length'), (int, float)) and len(value) > param['max_length']:
        return f"can be at most {int(param['max_length'])} characters (this is {len(value)})"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(param.get('min'), (int, float)) and value < param['min']:
            return f"should be at least {param['min']:g}"
        if isinstance(param.get('max'), (int, float)) and value > param['max']:
            return f"should be at most {param['max']:g}"
    return ''


def check_params(definition, values):
    """Refuse settings the action's params rule out, before anything is sent."""
    for param in definition.get('params') or []:
        if param['key'] not in values:
            continue
        value = values[param['key']]
        wrong = type_error(value, param.get('value_type') or param['type']) if param['type'] in TYPES else ''
        wrong = wrong or limit_error(param, value)
        if wrong:
            raise BuildingError(f'{param["label"]} {wrong}.')


def literal_problem(param, stored):
    """What is wrong with a setting that is typed in as it is (no fields dropped into it)."""
    if not isinstance(stored, str) or is_empty(stored) or EXPR.search(stored):
        return ''
    kind = param.get('value_type') or param['type']
    if kind not in TYPES:
        return ''
    try:
        value = coerce(stored, kind)
    except ValueError as error:
        return f'{error}'
    return type_error(value, kind) or limit_error(param, value)


def mismatches(fields, data):
    """Where `data` disagrees with the declared fields. A field that is missing or null is
    fine (a lookup that finds nothing answers only `found`); one of the wrong type is not."""
    from .templating import dig
    found = []
    if not isinstance(data, dict):
        return ['answered something that is not an object']
    for field in fields or []:
        value = dig(data, field['key'])
        if value is None or field.get('type') not in FIELD_TYPES:
            continue
        wrong = type_error(value, field['type'])
        if wrong:
            found.append(f'"{field["key"]}" {wrong}, but was {value!r}'[:200])
        elif field['type'] == 'list':
            if len(value) > LIST_LIMIT:
                found.append(f'"{field["key"]}" has {len(value)} items; a list carries at most {LIST_LIMIT}')
            for index, item in enumerate(value[:20]):
                found += [f'"{field["key"]}" item {index + 1}: {wrong}' for wrong in mismatches(field.get('items'), item)][:3]
    return found[:10]


def remember(connection, notes):
    """Keep notes for whoever maintains the building (shown on the Buildings page)."""
    if not notes or connection is None:
        return
    from .models import Connection
    current = list(Connection.objects.filter(pk=connection.pk).values_list('contract_notes', flat=True).first() or [])
    added = [note for note in dict.fromkeys(notes) if note not in current]
    if added:
        current = (current + added)[-MAX_NOTES:]
        Connection.objects.filter(pk=connection.pk).update(contract_notes=current)
        connection.contract_notes = current
