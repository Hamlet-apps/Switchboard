"""Ready-made workflows. A recipe is just a saved workflow people can open and edit later;
it is offered once every building it needs is connected and offers what it uses."""
import secrets

from . import buildings, nodes


def _uid():
    return 's' + secrets.token_hex(4)


def _tok(uid, key):
    return '{{' + f'{uid}.{key}' + '}}'


def _staff_cards(th, pv):
    trigger, find, check, level = _uid(), _uid(), _uid(), _uid()

    def card(amount):
        return {'uid': _uid(), 'node': 'action', 'connection': pv.id, 'key': 'staff_card.issue', 'next': {}, 'params': {
            'staff_id': _tok(find, 'staff_id'), 'amount': amount,
            # In Pavilion the label is the card type's name, so one label for everyone.
            'label': 'Volunteer meal card', 'replace_previous': 'true'}}

    five, ten, fifteen = card('5'), card('10'), card('15')
    return [
        {'uid': trigger, 'node': 'event', 'connection': th.id, 'key': 'volunteer.level_reached', 'params': {},
         'next': {'main': find}},
        {'uid': find, 'node': 'action', 'connection': pv.id, 'key': 'staff.find',
         'params': {'email': _tok(trigger, 'person.email')}, 'next': {'main': check}},
        {'uid': check, 'node': 'condition', 'connection': None, 'key': '',
         'params': {'left': _tok(find, 'found'), 'op': 'true', 'right': ''}, 'next': {'main': level}},
        {'uid': level, 'node': 'switch', 'connection': None, 'key': '', 'params': {'on': _tok(trigger, 'level')},
         'cases': [{'id': 'c1', 'op': 'lte', 'value': '2', 'value2': '', 'label': 'Level 1 to 2'},
                   {'id': 'c2', 'op': 'eq', 'value': '3', 'value2': '', 'label': 'Level 3'},
                   {'id': 'c3', 'op': 'gte', 'value': '4', 'value2': '', 'label': 'Level 4 and up'}],
         'next': {'c1': five['uid'], 'c2': ten['uid'], 'c3': fifteen['uid']}},
        five, ten, fifteen,
    ]


def _shift_sync(th, pv):
    trigger, staff, shift = _uid(), _uid(), _uid()
    return [
        {'uid': trigger, 'node': 'event', 'connection': th.id, 'key': 'shift.signed_up', 'params': {}},
        {'uid': staff, 'node': 'action', 'connection': pv.id, 'key': 'staff.ensure',
         'params': {'name': _tok(trigger, 'person.name'), 'email': _tok(trigger, 'person.email')}},
        {'uid': shift, 'node': 'action', 'connection': pv.id, 'key': 'shift.add', 'params': {
            'staff_id': _tok(staff, 'staff_id'),
            'starts_at': _tok(trigger, 'shift.starts_at'),
            'ends_at': _tok(trigger, 'shift.ends_at'),
        }},
    ]


RECIPES = [
    {
        'id': 'staff-cards', 'name': 'Staff card by volunteer level',
        'description': 'When a volunteer levels up in Town Hall, give them a better meal card in Pavilion.',
        'fill_in': 'Card amount per level',
        'needs': {'town-hall': {'triggers': ['volunteer.level_reached']},
                  'pavilion': {'actions': ['staff.find', 'staff_card.issue']}},
        'build': _staff_cards,
    },
    {
        'id': 'shift-sync', 'name': 'Sync shifts to time clock',
        'description': 'New Town Hall shift sign-ups show up on Pavilion’s schedule so volunteers can clock in.',
        'fill_in': 'Nothing, it works as it is',
        'needs': {'town-hall': {'triggers': ['shift.signed_up']},
                  'pavilion': {'actions': ['staff.ensure', 'shift.add']}},
        'build': _shift_sync,
    },
]

NAMES = {'town-hall': 'Town Hall', 'pavilion': 'Pavilion'}


def _match(recipe, connections):
    """({slug: Connection}, [names of buildings still missing])."""
    chosen, missing = {}, []
    for slug, needs in recipe['needs'].items():
        fit = next((
            c for c in connections if c.building == slug and c.status == c.CONNECTED
            and all(c.trigger(t) for t in needs.get('triggers', []))
            and all(c.action(a) for a in needs.get('actions', []))
        ), None)
        if fit:
            chosen[slug] = fit
        else:
            missing.append(NAMES.get(slug, slug))
    return chosen, missing


def available(connections):
    """Every recipe with what it still needs, for the workflows page."""
    out = []
    for recipe in RECIPES:
        chosen, missing = _match(recipe, connections)
        tiles = [buildings.look(slug, NAMES.get(slug, slug)) for slug in recipe['needs']]
        out.append({**{k: recipe[k] for k in ('id', 'name', 'description', 'fill_in')},
                    'ready': not missing, 'missing': missing, 'tiles': tiles})
    return out


def build(recipe_id, connections):
    """(name, description, steps) for a recipe, or None when it can't be made yet."""
    recipe = next((r for r in RECIPES if r['id'] == recipe_id), None)
    if recipe is None:
        return None
    chosen, missing = _match(recipe, connections)
    if missing:
        return None
    steps = recipe['build'](chosen['town-hall'], chosen['pavilion'])
    assert not nodes.problems(steps, {c.id: c for c in connections})
    return recipe['name'], recipe['description'], steps
