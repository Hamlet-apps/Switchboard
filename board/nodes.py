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
        'desc': 'Checks a condition and goes one of two ways: the True branch or the False branch. '
                'Steps after it run whichever way it went.',
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
        'desc': 'Looks at one field and takes the first case that matches, or Otherwise when none does. '
                'Steps after it run whichever case was taken.',
        'params': [
            {'key': 'on', 'label': 'Look at this field', 'type': 'string', 'required': True},
        ],
        'out': [('matched', True), ('branch', 'case'), ('case', 'is 3')],
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
STEP_BASICS = ['condition', 'if', 'switch', 'http']
BRANCHING = ('if', 'switch')
MAX_DEPTH = 4


def _builtin(node):
    spec = BUILTIN[node]
    return {
        'id': node, 'node': node, 'connection': None, 'key': '', 'trigger': bool(spec.get('trigger')),
        'title': spec['title'], 'desc': spec['desc'], 'kind': spec['kind'], 'tile': {'icon': spec['icon']},
        'params': spec['params'], 'out': [{'key': k, 'example': v} for k, v in spec['out']],
    }


def _fields(items):
    return [{'key': str(f['key']), 'example': f.get('example')} for f in items or []
            if isinstance(f, dict) and f.get('key')]


def _param(spec):
    choices = [c for c in spec.get('choices') or [] if isinstance(c, dict) and 'value' in c]
    out = {
        'key': str(spec['key']), 'label': str(spec.get('label') or spec['key']),
        'type': 'choice' if choices else str(spec.get('type') or 'string'),
        'required': bool(spec.get('required')), 'help': str(spec.get('help') or ''),
    }
    if choices:
        out['choices'] = [{'value': c['value'], 'label': str(c.get('label', c['value']))} for c in choices]
        out['value_type'] = str(spec.get('type') or 'string')
    if spec.get('default') is not None:
        out['default'] = spec['default']
    return out


def _event(connection, trigger):
    return {
        'id': f"event:{connection.id}:{trigger['type']}", 'node': 'event', 'connection': connection.id,
        'key': trigger['type'], 'trigger': True,
        'title': str(trigger.get('title') or trigger['type']), 'desc': str(trigger.get('description') or ''),
        'kind': connection.name, 'tile': buildings.look(connection.building, connection.name),
        'params': [], 'out': _fields(trigger.get('fields')), 'summary': str(trigger.get('summary') or ''),
    }


def _action(connection, action):
    lookup = action.get('kind') == 'lookup'
    return {
        'id': f"action:{connection.id}:{action['name']}", 'node': 'action', 'connection': connection.id,
        'key': action['name'], 'trigger': False, 'lookup': lookup,
        'title': str(action.get('title') or action['name']), 'desc': str(action.get('description') or ''),
        'kind': f"{connection.name} · {'Look up' if lookup else 'Action'}",
        'tile': buildings.look(connection.building, connection.name),
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
            'tile': buildings.look(connection.building, connection.name),
            'broken': connection.status == connection.BROKEN,
            'sub': f"{len(items)} {word}{'' if len(items) == 1 else 's'} · "
                   + ('needs reconnecting' if connection.status == connection.BROKEN else 'connected'),
            'items': items,
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


# --- the step tree ----------------------------------------------------------------------------
# An If or Switch step holds its branches: [{"id", "steps": [...]}, …]. If has "true" and
# "false"; Switch has one per case ({"id", "op", "value", "value2", "label"}) and "default".

def new_branches(node):
    if node == 'if':
        return [{'id': 'true', 'steps': []}, {'id': 'false', 'steps': []}]
    if node == 'switch':
        return [{'id': 'c1', 'op': 'eq', 'value': '', 'value2': '', 'label': '', 'steps': []},
                {'id': 'default', 'steps': []}]
    return None


def branch_label(step, branch):
    """What a branch is called on the canvas and in the run log."""
    if step.get('node') == 'if':
        return 'True' if branch['id'] == 'true' else 'False'
    if branch['id'] == 'default':
        return 'Otherwise'
    if branch.get('label'):
        return branch['label']
    words = dict(MAP_OPS).get(branch.get('op'), 'is')
    if branch.get('op') == 'between':
        return f"{words} {branch.get('value', '')} and {branch.get('value2', '')}"
    return f"{words} {branch.get('value', '')}".strip()


def walk(steps):
    """Every step, parents before their branches, in the order the canvas shows them."""
    for step in steps or []:
        yield step
        for branch in step.get('branches') or []:
            yield from walk(branch.get('steps'))


def find(steps, uid):
    return next((step for step in walk(steps) if step.get('uid') == uid), None)


def before(steps, uid):
    """The steps whose output `uid` can use: everything that can have run before it.

    Steps in a sibling branch never run in the same run, so they are left out; steps inside
    an earlier If or Switch are kept (the step may run after either branch).
    """
    def search(items, seen):
        for step in items:
            if step.get('uid') == uid:
                return seen
            mine = seen + [step]
            for branch in step.get('branches') or []:
                found = search(branch.get('steps') or [], mine)
                if found is not None:
                    return found
            seen = seen + list(walk([step]))
        return None
    return search(steps or [], []) or []


def problems(steps, connections):
    """Why a workflow can't run yet, in plain words. Empty when it can."""
    if not steps:
        return ['Add a trigger first.']
    found = []
    first = describe(steps[0], connections)
    if not first['trigger']:
        found.append('The first step has to be a trigger.')
    for index, step in enumerate(walk(steps)):
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
        if step.get('node') == 'switch':
            for branch in step.get('branches') or []:
                if branch['id'] != 'default' and is_empty(branch.get('value')):
                    found.append('A Switch case has nothing to compare with.')
        connection = connections.get(step.get('connection'))
        if connection is not None and connection.status == connection.BROKEN:
            found.append(f'{connection.name} needs reconnecting.')
    return list(dict.fromkeys(found))
