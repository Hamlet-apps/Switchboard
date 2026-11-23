"""What every kind of step is: its name, settings and the fields it gives.

Built-in nodes are described here. Building nodes are generated from each connection's
manifest, so Switchboard has no code for any particular building.

A definition (also what the builder's JavaScript gets):

    {"id", "node", "connection", "key", "trigger", "title", "desc", "kind",
     "tile": {"icon"} or {"code", "bg", "fg"}, "params": [...], "out": [{"key", "example"}]}
"""
from . import buildings
from .templating import MAP_OPS, OPS, UNARY, flatten, is_empty, unflatten

TRIGGERS = ('manual', 'schedule', 'webhook', 'failure', 'event')

WEEKDAYS = [('mon', 'Monday'), ('tue', 'Tuesday'), ('wed', 'Wednesday'), ('thu', 'Thursday'),
            ('fri', 'Friday'), ('sat', 'Saturday'), ('sun', 'Sunday')]


def _choices(pairs):
    return [{'value': value, 'label': label} for value, label in pairs]


BUILTIN = {
    'manual': {
        'trigger': True, 'icon': 'play_circle', 'kind': 'Basic trigger', 'title': 'Run it manually',
        'desc': 'Starts when someone presses Run now.',
        'params': [],
        'out': [('started_by', 'Andrew'), ('started_at', '2026-11-20T13:02:00-08:00')],
    },
    'schedule': {
        'trigger': True, 'icon': 'schedule', 'kind': 'Basic trigger', 'title': 'On a schedule',
        'desc': 'Starts on a timetable you set, like every Monday morning.',
        'params': [
            {'key': 'repeat', 'label': 'Repeat', 'type': 'choice', 'default': 'week', 'plain': True,
             'choices': _choices([('hour', 'Every hour'), ('day', 'Every day'), ('week', 'Every week')])},
            {'key': 'weekday', 'label': 'On', 'type': 'choice', 'default': 'mon', 'plain': True,
             'choices': _choices(WEEKDAYS), 'show_if': {'repeat': ['week']}},
            {'key': 'time', 'label': 'At', 'type': 'time', 'default': '08:00', 'plain': True,
             'help': 'In your organization\'s time zone. "Every hour" uses the minutes.'},
        ],
        'out': [('fired_at', '2026-11-23T08:00:00-08:00')],
    },
    'webhook': {
        'trigger': True, 'icon': 'webhook', 'kind': 'Basic trigger', 'title': 'Incoming webhook',
        'desc': 'Starts when anything sends JSON to its private URL.',
        'params': [],
        'out': [('body.name', 'Sam R.'), ('body.email', 'sam@example.org'), ('received_at', '2026-11-20T12:04:00-08:00')],
    },
    'failure': {
        'trigger': True, 'icon': 'notification_important', 'kind': 'Basic trigger', 'title': 'A workflow failed',
        'desc': 'Starts when any other workflow fails, so you can tell someone.',
        'params': [],
        'out': [('workflow.name', 'Staff card by volunteer level'), ('workflow.id', 1), ('run.id', 142),
                ('run.url', 'https://switchboard.example.org/runs/?run=142'),
                ('step', 'Issue staff card'), ('error', "503 · Pavilion didn't answer after 3 tries")],
    },
    'condition': {
        'icon': 'filter_alt', 'kind': 'Logic', 'title': 'Filter',
        'desc': 'Only continues if the condition is true. Otherwise the workflow stops here, and the run is logged as stopped.',
        'params': [
            {'key': 'left', 'label': 'Check this field', 'type': 'string', 'required': True},
            {'key': 'op', 'label': 'It', 'type': 'choice', 'default': 'true', 'plain': True, 'choices': _choices(OPS)},
            {'key': 'right', 'label': 'This', 'type': 'string',
             'show_if': {'op': [op for op, _ in OPS if op not in UNARY]}},
        ],
        'out': [('passed', True)],
    },
    'if': {
        'icon': 'call_split', 'kind': 'Logic', 'title': 'If',
        'desc': 'Checks a condition and sends the run out of one of two outputs: True or False.',
        'params': [
            {'key': 'left', 'label': 'Check this field', 'type': 'string', 'required': True},
            {'key': 'op', 'label': 'It', 'type': 'choice', 'default': 'true', 'plain': True, 'choices': _choices(OPS)},
            {'key': 'right', 'label': 'This', 'type': 'string',
             'show_if': {'op': [op for op, _ in OPS if op not in UNARY]}},
        ],
        'out': [('result', True), ('branch', 'true')],
    },
    'switch': {
        'icon': 'alt_route', 'kind': 'Logic', 'title': 'Switch',
        'desc': 'Looks at one field and sends the run out of the first case that matches, or Otherwise when none does.',
        'params': [
            {'key': 'on', 'label': 'Look at this field', 'type': 'string', 'required': True},
        ],
        'out': [('matched', True), ('branch', 'case'), ('case', 'is 3')],
    },
    'loop': {
        'icon': 'repeat', 'kind': 'Logic', 'title': 'For each',
        'desc': 'Goes through a list. The steps wired to "Each item" run once for every item, then the workflow '
                'carries on from "When done". Inside the loop, a Filter skips to the next item.',
        'params': [
            {'key': 'list', 'label': 'List to go through', 'type': 'list', 'required': True,
             'help': 'Drop a list field here, like the people a lookup found. Text with commas in it works too.'},
        ],
        'out': [('item', ''), ('index', 1), ('count', 3)],
    },
    'http': {
        'icon': 'language', 'kind': 'HTTP request', 'title': 'Send an HTTP request',
        'desc': 'Calls any URL, like a Telegram bot or another service.',
        'params': [
            {'key': 'method', 'label': 'Method', 'type': 'choice', 'default': 'POST', 'plain': True,
             'choices': _choices([(m, m) for m in ('POST', 'GET', 'PUT', 'PATCH', 'DELETE')])},
            {'key': 'url', 'label': 'URL', 'type': 'string', 'required': True,
             'placeholder': 'https://api.telegram.org/bot…/sendMessage'},
            {'key': 'headers', 'label': 'Headers', 'type': 'text',
             'help': 'One per line, like "Authorization: Bearer …". Keep keys in Settings › Secrets and add them as {{secret.NAME}}.'},
            {'key': 'body', 'label': 'Body', 'type': 'text', 'help': 'JSON or plain text.',
             'show_if': {'method': ['POST', 'PUT', 'PATCH', 'DELETE']}},
        ],
        'out': [('status', 200), ('ok', True), ('body', '')],
    },
}

TRIGGER_BASICS = ['manual', 'schedule', 'webhook', 'failure']
STEP_BASICS = ['condition', 'if', 'switch', 'loop', 'http']
BRANCHING = ('if', 'switch')


def _builtin(node):
    spec = BUILTIN[node]
    return {
        'id': node, 'node': node, 'connection': None, 'key': '', 'trigger': bool(spec.get('trigger')),
        'title': spec['title'], 'desc': spec['desc'], 'kind': spec['kind'], 'tile': {'icon': spec['icon']},
        'params': spec['params'], 'out': [{'key': k, 'example': v} for k, v in spec['out']],
    }


def _fields(items):
    out = []
    for f in items or []:
        if not isinstance(f, dict) or not f.get('key'):
            continue
        field = {'key': str(f['key']), 'type': str(f.get('type') or ''), 'example': f.get('example')}
        if field['type'] == 'list':
            field['items'] = _fields(f.get('items'))       # what a "For each" over it offers
        out.append(field)
    return out


def _limits(spec):
    """A param's limits in words, added to its help so people see them before they hit them."""
    words = []
    if isinstance(spec.get('max_length'), (int, float)):
        words.append(f"At most {int(spec['max_length'])} characters.")
    low, high = spec.get('min'), spec.get('max')
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        words.append(f'Between {low:g} and {high:g}.')
    elif isinstance(low, (int, float)):
        words.append(f'At least {low:g}.')
    elif isinstance(high, (int, float)):
        words.append(f'At most {high:g}.')
    return ' '.join(words)


def _param(spec):
    choices = [c for c in spec.get('choices') or [] if isinstance(c, dict) and 'value' in c]
    out = {
        'key': str(spec['key']), 'label': str(spec.get('label') or spec['key']),
        'type': 'choice' if choices else str(spec.get('type') or 'string'),
        'required': bool(spec.get('required')),
        'help': ' '.join(part for part in (str(spec.get('help') or ''), _limits(spec)) if part),
    }
    for limit in ('min', 'max', 'max_length'):
        if isinstance(spec.get(limit), (int, float)) and not isinstance(spec.get(limit), bool):
            out[limit] = spec[limit]
    if choices:
        out['choices'] = [{'value': c['value'], 'label': str(c.get('label', c['value']))} for c in choices]
        out['value_type'] = str(spec.get('type') or 'string')
    if spec.get('default') is not None:
        out['default'] = spec['default']
    return out


def _group(spec, name):
    """The heading a trigger or action sits under in the picker: the manifest's `group`, else
    the first word of its name ("staff_card.issue" -> "Staff card")."""
    given = spec.get('group')
    if isinstance(given, str) and given.strip():
        return given.strip()[:40]
    return name.split('.')[0].replace('_', ' ').capitalize()


def _event(connection, trigger):
    return {
        'group': _group(trigger, trigger['type']),
        'id': f"event:{connection.id}:{trigger['type']}", 'node': 'event', 'connection': connection.id,
        'key': trigger['type'], 'trigger': True,
        'title': str(trigger.get('title') or trigger['type']), 'desc': str(trigger.get('description') or ''),
        'kind': connection.name, 'tile': buildings.look(connection.building, connection.name, connection),
        'params': [], 'out': _fields(trigger.get('fields')), 'summary': str(trigger.get('summary') or ''),
    }


def _action(connection, action):
    lookup = action.get('kind') == 'lookup'
    return {
        'id': f"action:{connection.id}:{action['name']}", 'node': 'action', 'connection': connection.id,
        'key': action['name'], 'trigger': False, 'lookup': lookup, 'group': _group(action, action['name']),
        'title': str(action.get('title') or action['name']), 'desc': str(action.get('description') or ''),
        'kind': f"{connection.name} · {'Look up' if lookup else 'Action'}",
        'tile': buildings.look(connection.building, connection.name, connection),
        'params': [_param(p) for p in action.get('params') or [] if isinstance(p, dict) and p.get('key')],
        'out': _fields(action.get('output')),
    }


def _missing(step, why):
    return {
        'id': f"missing:{step.get('uid')}", 'node': step.get('node'), 'connection': step.get('connection'),
        'key': step.get('key', ''), 'trigger': step.get('node') in TRIGGERS, 'missing': True,
        'title': step.get('key') or 'Unknown step', 'desc': why, 'kind': 'Not available',
        'tile': {'icon': 'link_off'}, 'params': [], 'out': [],
    }


def describe(step, connections):
    """The definition behind one stored step. `connections` is {id: Connection}."""
    node = step.get('node')
    if node in BUILTIN:
        return _builtin(node)
    connection = connections.get(step.get('connection'))
    if node in ('event', 'action'):
        if connection is None:
            return _missing(step, 'The building this step used was disconnected.')
        found = connection.trigger(step.get('key')) if node == 'event' else connection.action(step.get('key'))
        if not found:
            return _missing(step, f'{connection.name} no longer offers this.')
        return _event(connection, found) if node == 'event' else _action(connection, found)
    return _missing(step, "Switchboard doesn't know this kind of step.")


def catalog(connections):
    """Everything the picker offers: {"triggers": [...sections], "steps": [...sections]}."""
    live = [c for c in connections if c.status != c.PENDING]

    def building(connection, items, word):
        return {
            'connection': connection.id, 'name': connection.name, 'instance': connection.instance,
            'tile': buildings.look(connection.building, connection.name, connection),
            'broken': connection.status == connection.BROKEN,
            'sub': f"{len(items)} {word}{'' if len(items) == 1 else 's'} · "
                   + ('needs reconnecting' if connection.status == connection.BROKEN else 'connected'),
            'items': items, 'groups': list(dict.fromkeys(item['group'] for item in items)),
        }

    return {
        'triggers': {
            'basics': [_builtin(n) for n in TRIGGER_BASICS],
            'buildings': [building(c, [_event(c, t) for t in c.triggers()], 'event') for c in live],
        },
        'steps': {
            'basics': [_builtin(n) for n in STEP_BASICS],
            'buildings': [building(c, [_action(c, a) for a in c.actions()], 'action') for c in live],
        },
    }


def new_params(definition):
    return {p['key']: p['default'] for p in definition['params'] if p.get('default') is not None}


def example_output(definition):
    """Sample data from the manifest's examples, for a step nobody has tested yet."""
    return unflatten((f['key'], f['example']) for f in definition['out'])


def sample_fields(definition, sample):
    """[(key, value)] the editor lists for a step: real test data when there is some."""
    if isinstance(sample, dict) and sample:
        return flatten(sample)
    return [(f['key'], f['example']) for f in definition['out']]


def visible(param, values):
    """Whether a setting applies given the other settings (`show_if`)."""
    for key, allowed in (param.get('show_if') or {}).items():
        if values.get(key, '') not in allowed:
            return False
    return True


# --- the step graph ---------------------------------------------------------------------------
# Steps are a flat list wired together like a patch panel. Every step has `next`, a map from
# each of its outputs to the uid of the step that output leads to:
#   most steps      {"main": uid}
#   If              {"true": uid, "false": uid}
#   Switch          {<case id>: uid, …, "default": uid}   with its cases in step["cases"]
# An output can be left unconnected (the run ends there), and several outputs may lead to the
# same step. `x`/`y` are where the step sits on the canvas. The trigger is always first.

def ports(step):
    """The names of a step's outputs, in the order they are drawn."""
    if step.get('node') == 'if':
        return ['true', 'false']
    if step.get('node') == 'switch':
        return [case['id'] for case in step.get('cases') or []] + ['default']
    if step.get('node') == 'loop':
        return ['each', 'done']
    return ['main']


def new_cases():
    return [{'id': 'c1', 'op': 'eq', 'value': '', 'value2': '', 'label': ''}]


def port_label(step, port):
    """What an output is called on the canvas and in the run log."""
    if step.get('node') == 'if':
        return 'True' if port == 'true' else 'False'
    if step.get('node') == 'loop':
        return 'Each item' if port == 'each' else 'When done'
    if step.get('node') != 'switch':
        return ''
    if port == 'default':
        return 'Otherwise'
    case = next((c for c in step.get('cases') or [] if c['id'] == port), None)
    if case is None:
        return ''
    if case.get('label'):
        return case['label']
    words = dict(MAP_OPS).get(case.get('op'), 'is')
    if case.get('op') == 'between':
        return f"{words} {case.get('value', '')} and {case.get('value2', '')}"
    return f"{words} {case.get('value', '')}".strip()


def upgrade(steps):
    """Steps in today's shape. Older workflows kept a plain list, or a tree with `branches`
    inside If and Switch steps; both are rewired into the graph here (without positions: the
    builder lays those out)."""
    steps = steps or []
    if all('next' in step for step in steps):
        return steps
    if any('next' in step for step in steps):       # wired, with a step or two missing its map
        return [step if 'next' in step else {**step, 'next': {}} for step in steps]
    out = []

    def build(items, dangling):
        """Add `items` in order, joined to the open outputs in `dangling`; returns the open
        outputs they leave behind."""
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or 'uid' not in item:
                continue
            step = {k: v for k, v in item.items() if k != 'branches'}
            step['next'] = {}
            out.append(step)
            for source, port in dangling:
                source['next'][port] = step['uid']
            if item.get('node') in BRANCHING:
                branches = [b for b in item.get('branches') or [] if isinstance(b, dict)] if isinstance(item.get('branches'), list) else []
                if item['node'] == 'switch':
                    step['cases'] = [{k: b.get(k, '') for k in ('id', 'op', 'value', 'value2', 'label')}
                                     for b in branches if b.get('id') != 'default']
                dangling = []
                for port in ports(step):
                    branch = next((b for b in branches if b.get('id') == port), {})
                    dangling += build(branch.get('steps'), [(step, port)])
            else:
                dangling = [(step, 'main')]
        return dangling

    build(steps, [])
    return out


def walk(steps):
    return iter(upgrade(steps))


def find(steps, uid):
    return next((step for step in upgrade(steps) if step.get('uid') == uid), None)


def reachable(steps):
    """The steps a run can get to from the trigger, nearest first."""
    steps = upgrade(steps)
    by_uid = {step['uid']: step for step in steps}
    seen, queue = [], [steps[0]['uid']] if steps else []
    while queue:
        uid = queue.pop(0)
        if uid in seen or uid not in by_uid:
            continue
        seen.append(uid)
        queue += [target for target in (by_uid[uid].get('next') or {}).values() if target]
    return [by_uid[uid] for uid in seen]


def before(steps, uid):
    """The steps whose output `uid` can use: every step a run may have passed through on its
    way there, nearest to the trigger first."""
    steps = upgrade(steps)
    parents = {}
    for step in steps:
        for target in (step.get('next') or {}).values():
            parents.setdefault(target, []).append(step['uid'])
    found, queue = set(), list(parents.get(uid, []))
    while queue:
        current = queue.pop()
        if current not in found and current != uid:
            found.add(current)
            queue += parents.get(current, [])
    return [step for step in reachable(steps) if step['uid'] in found]


def _from(by_uid, start):
    """Every step a run can reach from `start` (inclusive)."""
    seen, queue = [], [start] if start else []
    while queue:
        uid = queue.pop(0)
        if uid in seen or uid not in by_uid:
            continue
        seen.append(uid)
        queue += [t for t in (by_uid[uid].get('next') or {}).values() if t]
    return [by_uid[uid] for uid in seen]


def would_loop(steps, source, target):
    """Whether wiring source -> target would let a run come back round to `source`."""
    by_uid = {step['uid']: step for step in steps}
    seen, queue = set(), [target]
    while queue:
        uid = queue.pop()
        if uid == source:
            return True
        if uid in seen or uid not in by_uid:
            continue
        seen.add(uid)
        queue += [t for t in (by_uid[uid].get('next') or {}).values() if t]
    return False


def problems(steps, connections):
    """Why a workflow can't run yet, in plain words. Empty when it can.

    Only steps wired to the trigger count: a step left unconnected on the canvas never runs.
    """
    steps = upgrade(steps)
    if not steps:
        return ['Add a trigger first.']
    found = []
    first = describe(steps[0], connections)
    if not first['trigger']:
        found.append('The first step has to be a trigger.')
    for index, step in enumerate(reachable(steps)):
        definition = describe(step, connections)
        if definition.get('missing'):
            found.append(f'"{definition["title"]}": {definition["desc"]}')
            continue
        if index > 0 and definition['trigger']:
            found.append(f'"{definition["title"]}" is a trigger. Only the first step can be one.')
        values = {**new_params(definition), **(step.get('params') or {})}
        for param in definition['params']:
            if param.get('required') and visible(param, values) and is_empty(values.get(param['key'])):
                found.append(f'"{definition["title"]}" needs {param["label"].lower()}.')
            elif step.get('node') == 'action' and visible(param, values):
                from . import schema
                wrong = schema.literal_problem(param, values.get(param['key']))
                if wrong:
                    found.append(f'"{definition["title"]}": {param["label"].lower()} {wrong}.')
        if step.get('node') == 'loop':
            by_uid = {s['uid']: s for s in steps}
            inside = {s['uid'] for s in _from(by_uid, (step.get('next') or {}).get('each'))}
            after = {s['uid'] for s in _from(by_uid, (step.get('next') or {}).get('done'))}
            if inside & after:
                shared = describe(by_uid[sorted(inside & after)[0]], connections)['title']
                found.append(f'"{shared}" is wired both inside a For each and after it. '
                             'Give the loop\'s two outputs their own steps.')
        if step.get('node') == 'switch':
            for case in step.get('cases') or []:
                if is_empty(case.get('value')):
                    found.append('A Switch case has nothing to compare with.')
        connection = connections.get(step.get('connection'))
        if connection is not None and connection.status == connection.BROKEN:
            found.append(f'{connection.name} needs reconnecting.')
    return list(dict.fromkeys(found))
