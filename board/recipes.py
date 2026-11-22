"""Ready-made workflows. A recipe is just a saved workflow people can open and edit later;
it is offered once every building it needs is connected and offers what it uses."""
import secrets

from . import buildings, nodes


def _uid():
    return 's' + secrets.token_hex(4)


def _tok(uid, key):
    return '{{' + f'{uid}.{key}' + '}}'


def _staff_cards(th, pv):
    trigger, find, check, card = _uid(), _uid(), _uid(), _uid()
    return [
        {'uid': trigger, 'node': 'event', 'connection': th.id, 'key': 'volunteer.level_reached', 'params': {}},
        {'uid': find, 'node': 'action', 'connection': pv.id, 'key': 'staff.find',
         'params': {'email': _tok(trigger, 'person.email')}},
        {'uid': check, 'node': 'condition', 'connection': None, 'key': '',
         'params': {'left': _tok(find, 'found'), 'op': 'true', 'right': ''}},
        {'uid': card, 'node': 'action', 'connection': pv.id, 'key': 'staff_card.issue', 'params': {
            'staff_id': _tok(find, 'staff_id'),
            'amount': {'map': {'on': _tok(trigger, 'level'), 'rules': [
                {'op': 'lte', 'value': '2', 'then': '5'},
                {'op': 'eq', 'value': '3', 'then': '10'},
                {'op': 'gte', 'value': '4', 'then': '15'},
            ], 'else': '5'}},
            # In Pavilion the label is the card type's name, so one label for everyone.
            'label': 'Volunteer meal card',
            'replace_previous': 'true',
        }},
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
        tiles = [{**buildings.look(slug), 'label': NAMES.get(slug, slug)} for slug in recipe['needs']]
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
