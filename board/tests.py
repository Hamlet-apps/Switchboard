import datetime
import hashlib
import hmac
import json
import zoneinfo
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import buildings, engine, middleware, nodes, recipes, templating, worker
from .buildings import BuildingError
from .models import Connection, Event, Folder, Run, Secret, Settings, Workflow

TH_MANIFEST = {
    'protocol': '1', 'building': 'town-hall', 'name': 'Town Hall', 'instance': 'Cathedral',
    'authorize_url': 'https://th.test/switchboard/authorize',
    'token_url': 'https://th.test/switchboard/v1/token', 'api_base': 'https://th.test/switchboard/v1',
    'triggers': [
        {'type': 'volunteer.level_reached', 'title': 'Volunteer reached a level',
         'summary': '{person.name} reached level {level}',
         'fields': [{'key': 'person.name', 'type': 'string', 'example': 'Maria K.'},
                    {'key': 'person.email', 'type': 'string', 'example': 'maria@example.org'},
                    {'key': 'level', 'type': 'int', 'example': 3}]},
        {'type': 'shift.signed_up', 'title': 'Signed up for a shift',
         'fields': [{'key': 'person.name', 'example': 'Ana'}, {'key': 'person.email', 'example': 'ana@example.org'},
                    {'key': 'shift.starts_at', 'example': '2026-11-28T09:00:00-08:00'},
                    {'key': 'shift.ends_at', 'example': '2026-11-28T12:00:00-08:00'}]},
    ],
    'actions': [],
}
PV_MANIFEST = {
    'protocol': '1', 'building': 'pavilion', 'name': 'Pavilion', 'instance': 'Café',
    'authorize_url': 'https://pv.test/switchboard/authorize',
    'token_url': 'https://pv.test/switchboard/v1/token', 'api_base': 'https://pv.test/switchboard/v1',
    'triggers': [],
    'actions': [
        {'name': 'staff.find', 'kind': 'lookup', 'title': 'Find staff member', 'method': 'POST',
         'path': '/actions/staff.find',
         'params': [{'key': 'email', 'type': 'string', 'label': 'Email', 'required': True}],
         'output': [{'key': 'found', 'type': 'bool', 'example': True}, {'key': 'staff_id', 'example': '12'}]},
        {'name': 'staff.ensure', 'kind': 'action', 'title': 'Add staff member', 'path': '/actions/staff.ensure',
         'params': [{'key': 'name', 'type': 'string', 'label': 'Name', 'required': True},
                    {'key': 'email', 'type': 'string', 'label': 'Email', 'required': True}],
         'output': [{'key': 'staff_id', 'example': '12'}]},
        {'name': 'staff_card.issue', 'kind': 'action', 'title': 'Issue staff card', 'path': '/actions/staff_card.issue',
         'params': [{'key': 'staff_id', 'type': 'string', 'label': 'Staff member', 'required': True},
                    {'key': 'amount', 'type': 'money', 'label': 'Amount', 'required': True},
                    {'key': 'label', 'type': 'string', 'label': 'Card label'},
                    {'key': 'replace_previous', 'type': 'bool', 'label': 'Replace their previous card'}],
         'output': [{'key': 'card_id', 'example': '7'}, {'key': 'amount', 'example': 10.0}]},
        {'name': 'shift.add', 'kind': 'action', 'title': 'Add shift', 'path': '/actions/shift.add',
         'params': [{'key': 'staff_id', 'type': 'string', 'label': 'Staff member', 'required': True},
                    {'key': 'starts_at', 'type': 'datetime', 'label': 'Starts', 'required': True},
                    {'key': 'ends_at', 'type': 'datetime', 'label': 'Ends', 'required': True}],
         'output': [{'key': 'shift_id', 'example': '3'}]},
    ],
}


class FakeWorld:
    """Stands in for `buildings._http`: answers by (method, url) and remembers every call."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def on(self, method, url, *answers):
        """Answers are (status, json) pairs, or exceptions; the last one repeats."""
        self.routes[(method, url)] = list(answers)

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'json': parsed,
                           'raw': body.decode() if body else ''})
        answers = self.routes.get((method, url))
        if not answers:
            return 404, b'{"error": {"code": "not_found", "message": "No such thing."}}'
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, data = answer
        return status, json.dumps(data).encode() if not isinstance(data, bytes) else data

    def to(self, url):
        return [c for c in self.calls if c['url'] == url]


def connect(manifest, host):
    connection = Connection(base_url=f'https://{host}', status=Connection.CONNECTED,
                            api_key_enc=buildings.crypto.encrypt('sbk_test'),
                            webhook_secret_enc=buildings.crypto.encrypt('hush'))
    buildings.apply_manifest(connection, json.loads(json.dumps(manifest)))
    connection.save()
    return connection


class Base(TestCase):
    def setUp(self):
        middleware._has_users = False
        self.user = get_user_model().objects.create_superuser('andrew', password='correct horse')
        self.client.force_login(self.user)
        self.world = FakeWorld()
        patcher = mock.patch.object(buildings, '_http', self.world)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.th = connect(TH_MANIFEST, 'th.test')
        self.pv = connect(PV_MANIFEST, 'pv.test')

    def recipe(self, recipe_id='staff-cards', enabled=True):
        name, description, steps = recipes.build(recipe_id, [self.th, self.pv])
        return Workflow.objects.create(name=name, description=description, steps=steps, enabled=enabled)

    def deliver(self, connection, envelope, secret='hush'):
        raw = json.dumps(envelope).encode()
        signature = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(reverse('building_hook', args=[connection.hook_token]), raw,
                                content_type='application/json', headers={'X-Switchboard-Signature': signature})

    def level_event(self, level=3, event_id='evt_1'):
        return {'id': event_id, 'type': 'volunteer.level_reached',
                'source': {'building': 'town-hall', 'instance': 'https://th.test'},
                'occurred_at': '2026-11-19T16:20:04-08:00',
                'subject': {'type': 'person', 'id': '42', 'email': 'maria@example.org'},
                'data': {'person': {'id': '42', 'name': 'Maria K.', 'email': 'maria@example.org'}, 'level': level}}


class TemplatingTests(TestCase):
    ctx = {'a': {'person': {'name': 'Maria', 'email': 'm@x.org'}, 'level': 3, 'found': False, 'tags': ['x', 'y']}}

    def test_single_token_keeps_its_type(self):
        self.assertEqual(templating.resolve('{{a.level}}', self.ctx), 3)
        self.assertIs(templating.resolve(' {{ a.found }} ', self.ctx), False)

    def test_text_with_tokens(self):
        self.assertEqual(templating.resolve('Meal card for {{a.person.name}} (L{{a.level}})', self.ctx),
                         'Meal card for Maria (L3)')
        self.assertEqual(templating.resolve('x{{a.nope.deeper}}y', self.ctx), 'xy')

    def test_secrets(self):
        self.assertEqual(templating.resolve('Bearer {{secret.BOT}}', {}, {'BOT': 't0k'}), 'Bearer t0k')

    def test_lookup_table(self):
        table = {'map': {'on': '{{a.level}}', 'rules': [
            {'op': 'lte', 'value': '2', 'then': '5'}, {'op': 'eq', 'value': '3', 'then': '10'},
            {'op': 'between', 'value': '4', 'value2': '9', 'then': '15'}], 'else': '1'}}
        for level, amount in ((1, '5'), (2, '5'), (3, '10'), (4, '15'), (9, '15'), (10, '1')):
            self.assertEqual(templating.resolve(table, {'a': {'level': level}}), amount, level)

    def test_compare(self):
        self.assertTrue(templating.compare('3', 'gte', 3))
        self.assertTrue(templating.compare('Server', 'eq', ' server '))
        self.assertTrue(templating.compare(True, 'eq', 'true'))
        self.assertTrue(templating.compare(['Cook', 'Server'], 'contains', 'server'))
        self.assertTrue(templating.compare('', 'empty'))
        self.assertFalse(templating.compare('abc', 'gt', 2))
        self.assertTrue(templating.compare('false', 'false'))

    def test_coerce(self):
        self.assertEqual(templating.coerce('$10', 'money'), 10.0)
        self.assertEqual(templating.coerce('4', 'int'), 4)
        self.assertIs(templating.coerce('true', 'bool'), True)
        self.assertIsNone(templating.coerce('', 'string'))
        with self.assertRaises(ValueError):
            templating.coerce('ten', 'money')

    def test_flatten_and_back(self):
        pairs = templating.flatten({'person': {'name': 'M'}, 'level': 3})
        self.assertEqual(pairs, [('person.name', 'M'), ('level', 3)])
        self.assertEqual(templating.unflatten(pairs), {'person': {'name': 'M'}, 'level': 3})


class EngineTests(Base):
    def test_staff_card_recipe_end_to_end(self):
        workflow = self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff_card.issue',
                      (200, {'card_id': '7', 'amount': 10.0, 'label': 'Meal card for Maria K.', 'replaced': None}))
        response = self.deliver(self.th, self.level_event(level=3))
        self.assertEqual(response.status_code, 202)
        run = workflow.runs.get()
        self.assertEqual(run.status, Run.SUCCEEDED)
        self.assertEqual(run.trigger_label, 'Town Hall · Maria K. reached level 3')
        find, issue = self.world.to('https://pv.test/switchboard/v1/actions/staff.find')[0], \
            self.world.to('https://pv.test/switchboard/v1/actions/staff_card.issue')[0]
        self.assertEqual(find['json'], {'email': 'maria@example.org'})
        self.assertEqual(find['headers']['Authorization'], 'Bearer sbk_test')
        self.assertNotIn('Idempotency-Key', find['headers'])
        self.assertEqual(issue['json'], {'staff_id': '12', 'amount': 10.0, 'label': 'Volunteer meal card',
                                         'replace_previous': True})
        self.assertTrue(issue['headers']['Idempotency-Key'].startswith('sb_'))
        self.assertEqual([s.status for s in run.step_results.all()], ['ok', 'ok', 'ok', 'ok'])

    def test_amount_follows_the_level(self):
        self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff_card.issue', (200, {'card_id': '7'}))
        for level, amount in ((1, 5.0), (4, 15.0)):
            self.deliver(self.th, self.level_event(level=level, event_id=f'evt_l{level}'))
            self.assertEqual(self.world.to('https://pv.test/switchboard/v1/actions/staff_card.issue')[-1]['json']['amount'], amount)

    def test_not_staff_stops_and_is_logged(self):
        workflow = self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': False}))
        self.deliver(self.th, self.level_event())
        run = workflow.runs.get()
        self.assertEqual(run.status, Run.STOPPED)
        self.assertEqual(self.world.to('https://pv.test/switchboard/v1/actions/staff_card.issue'), [])
        self.assertEqual([s.status for s in run.step_results.all()], ['ok', 'ok', 'stopped'])

    def test_duplicate_event_runs_once(self):
        workflow = self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': False}))
        self.deliver(self.th, self.level_event())
        again = self.deliver(self.th, self.level_event())
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(workflow.runs.count(), 1)
        self.assertEqual(Event.objects.count(), 1)

    def test_5xx_is_retried_then_fails_and_4xx_is_not(self):
        workflow = self.recipe()
        find = 'https://pv.test/switchboard/v1/actions/staff.find'
        self.world.on('POST', find, (503, {'error': {'code': 'server_error', 'message': 'Down for a moment.'}}))
        self.deliver(self.th, self.level_event())
        run = workflow.runs.get()
        self.assertEqual(run.status, Run.FAILED)
        self.assertEqual(len(self.world.to(find)), 3)
        self.assertIn('503 · Down for a moment. (after 3 tries)', run.error)
        self.assertEqual(run.failed_uid, workflow.steps[1]['uid'])

        self.world.calls.clear()
        self.world.on('POST', find, (422, {'error': {'code': 'invalid_params', 'message': 'Email is missing.'}}))
        self.deliver(self.th, self.level_event(event_id='evt_2'))
        self.assertEqual(len(self.world.to(find)), 1)
        self.assertEqual(workflow.runs.first().error, '422 · Email is missing.')

    def test_network_error_then_success(self):
        workflow = self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find',
                      BuildingError("Couldn't reach pv.test", retry=True), (200, {'found': False}))
        self.deliver(self.th, self.level_event())
        self.assertEqual(workflow.runs.get().status, Run.STOPPED)

    def test_retry_from_failed_step_keeps_the_idempotency_key(self):
        workflow = self.recipe()
        issue = 'https://pv.test/switchboard/v1/actions/staff_card.issue'
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', issue, (500, {'error': {'code': 'server_error', 'message': 'Oops.'}}))
        self.deliver(self.th, self.level_event())
        run = workflow.runs.get()
        self.assertEqual((run.status, run.failed_uid), (Run.FAILED, workflow.steps[3]['uid']))
        first_key = self.world.to(issue)[0]['headers']['Idempotency-Key']
        finds = len(self.world.to('https://pv.test/switchboard/v1/actions/staff.find'))

        self.world.on('POST', issue, (200, {'card_id': '7'}))
        response = self.client.post(reverse('run_retry', args=[run.pk]))
        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertEqual((run.status, run.retries), (Run.SUCCEEDED, 1))
        self.assertEqual(self.world.to(issue)[-1]['headers']['Idempotency-Key'], first_key)
        self.assertEqual(len(self.world.to('https://pv.test/switchboard/v1/actions/staff.find')), finds)
        self.assertEqual(run.step_results.count(), 4)

    def test_revoked_key_marks_the_building_broken(self):
        workflow = self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find',
                      (401, {'error': {'code': 'unauthorized', 'message': 'Unknown key.'}}))
        self.deliver(self.th, self.level_event())
        self.pv.refresh_from_db()
        self.assertEqual(self.pv.status, Connection.BROKEN)
        self.assertIn('Reconnect', workflow.runs.get().error)

    def test_missing_required_setting_fails_plainly(self):
        workflow = self.recipe()
        event = self.level_event()
        del event['data']['person']['email']
        self.deliver(self.th, event)
        self.assertEqual(workflow.runs.get().error, 'Email is empty. Check where this step gets it from.')

    def test_http_step_and_secrets_stay_out_of_the_log(self):
        secret = Secret(name='BOT')
        secret.set_value('t0p-s3cret')
        secret.save()
        workflow = Workflow.objects.create(name='Alerts', enabled=True, steps=[
            {'uid': 't', 'node': 'manual', 'params': {}},
            {'uid': 'h', 'node': 'http', 'params': {
                'method': 'POST', 'url': 'https://chat.test/send', 'headers': 'Authorization: Bearer {{secret.BOT}}',
                'body': '{"text": "Started by {{t.started_by}}"}'}},
        ])
        self.world.on('POST', 'https://chat.test/send', (200, {'ok': True, 'id': 9}))
        response = self.client.post(reverse('workflow_run', args=[workflow.pk]))
        self.assertEqual(response.status_code, 302)
        run = workflow.runs.get()
        self.assertEqual(run.status, Run.SUCCEEDED)
        call = self.world.to('https://chat.test/send')[0]
        self.assertEqual(call['headers']['Authorization'], 'Bearer t0p-s3cret')
        self.assertEqual(call['json'], {'text': 'Started by andrew'})
        self.assertEqual(run.context['h']['body'], {'ok': True, 'id': 9})
        self.assertNotIn('t0p-s3cret', json.dumps(run.step_results.get(index=1).sent))

    def test_failure_workflow_hears_about_failures_but_not_its_own(self):
        failing = self.recipe()
        alert = Workflow.objects.create(name='Tell someone', enabled=True, steps=[
            {'uid': 't', 'node': 'failure', 'params': {}},
            {'uid': 'h', 'node': 'http', 'params': {'method': 'POST', 'url': 'https://chat.test/send',
                                                     'body': '{{t.workflow.name}}: {{t.error}}'}},
        ])
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find',
                      (400, {'error': {'code': 'invalid_request', 'message': 'Nope.'}}))
        self.world.on('POST', 'https://chat.test/send', (500, b'down'))
        self.deliver(self.th, self.level_event())
        self.assertEqual(failing.runs.get().status, Run.FAILED)
        alert_run = alert.runs.get()
        self.assertEqual(alert_run.status, Run.FAILED)   # and that failure started nothing else
        self.assertEqual(Run.objects.count(), 2)
        self.assertEqual(self.world.to('https://chat.test/send')[0]['raw'], 'Staff card by volunteer level: 400 · Nope.')

    def test_shift_sync_recipe(self):
        workflow = self.recipe('shift-sync')
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.ensure', (200, {'staff_id': '5', 'created': True}))
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/shift.add', (200, {'shift_id': '3', 'status': 'published'}))
        self.deliver(self.th, {'id': 'evt_s', 'type': 'shift.signed_up', 'data': {
            'person': {'name': 'Ana R.', 'email': 'ana@example.org'},
            'shift': {'starts_at': '2026-11-28T09:00:00-08:00', 'ends_at': '2026-11-28T12:00:00-08:00'}}})
        self.assertEqual(workflow.runs.get().status, Run.SUCCEEDED)
        self.assertEqual(self.world.to('https://pv.test/switchboard/v1/actions/shift.add')[0]['json'],
                         {'staff_id': '5', 'starts_at': '2026-11-28T09:00:00-08:00', 'ends_at': '2026-11-28T12:00:00-08:00'})


class BranchTests(Base):
    """If and Switch: one branch runs, then whatever comes after them."""

    find = 'https://pv.test/switchboard/v1/actions/staff.find'
    issue = 'https://pv.test/switchboard/v1/actions/staff_card.issue'
    ensure = 'https://pv.test/switchboard/v1/actions/staff.ensure'

    def card(self, uid, amount):
        return {'uid': uid, 'node': 'action', 'connection': self.pv.id, 'key': 'staff_card.issue',
                'params': {'staff_id': '{{f.staff_id}}', 'amount': amount}}

    def if_workflow(self):
        return Workflow.objects.create(name='Card or sign up', enabled=True, steps=[
            {'uid': 't', 'node': 'event', 'connection': self.th.id, 'key': 'volunteer.level_reached', 'params': {}},
            {'uid': 'f', 'node': 'action', 'connection': self.pv.id, 'key': 'staff.find',
             'params': {'email': '{{t.person.email}}'}},
            {'uid': 'i', 'node': 'if', 'params': {'left': '{{f.found}}', 'op': 'true'}, 'branches': [
                {'id': 'true', 'steps': [self.card('c', '10')]},
                {'id': 'false', 'steps': [
                    {'uid': 'e', 'node': 'action', 'connection': self.pv.id, 'key': 'staff.ensure',
                     'params': {'name': '{{t.person.name}}', 'email': '{{t.person.email}}'}}]},
            ]},
            {'uid': 'after', 'node': 'http', 'params': {'method': 'POST', 'url': 'https://chat.test/send',
                                                         'body': 'Went {{i.branch}} for {{t.person.name}}'}},
        ])

    def test_if_takes_the_true_branch_then_carries_on(self):
        workflow = self.if_workflow()
        self.world.on('POST', self.find, (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', self.issue, (200, {'card_id': '7'}))
        self.world.on('POST', 'https://chat.test/send', (200, {'ok': True}))
        self.deliver(self.th, self.level_event())
        run = workflow.runs.get()
        self.assertEqual(run.status, Run.SUCCEEDED)
        self.assertEqual([s.uid for s in run.step_results.all()], ['t', 'f', 'i', 'c', 'after'])
        self.assertEqual(run.context['i'], {'result': True, 'branch': 'true'})
        self.assertEqual(self.world.to(self.ensure), [])
        self.assertEqual(self.world.to('https://chat.test/send')[0]['raw'], 'Went true for Maria K.')

    def test_if_takes_the_false_branch(self):
        workflow = self.if_workflow()
        self.world.on('POST', self.find, (200, {'found': False}))
        self.world.on('POST', self.ensure, (200, {'staff_id': '30', 'created': True}))
        self.world.on('POST', 'https://chat.test/send', (200, {'ok': True}))
        self.deliver(self.th, self.level_event())
        run = workflow.runs.get()
        self.assertEqual([s.uid for s in run.step_results.all()], ['t', 'f', 'i', 'e', 'after'])
        self.assertEqual(self.world.to(self.issue), [])
        page = self.client.get(reverse('runs'))
        self.assertContains(page, 'Took the False branch')
        self.assertContains(page, 'Add staff member')
        self.assertNotContains(page, 'Issue staff card')   # the branch not taken isn't listed

    def test_retry_inside_a_branch_keeps_the_decision_and_repeats_nothing(self):
        workflow = self.if_workflow()
        self.world.on('POST', self.find, (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', self.issue, (500, {'error': {'code': 'server_error', 'message': 'Oops.'}}))
        self.world.on('POST', 'https://chat.test/send', (200, {'ok': True}))
        self.deliver(self.th, self.level_event())
        run = workflow.runs.get()
        self.assertEqual((run.status, run.failed_uid), (Run.FAILED, 'c'))
        self.assertContains(self.client.get(reverse('runs')), 'Retry from “Issue staff card”')
        key = self.world.to(self.issue)[0]['headers']['Idempotency-Key']
        # Even if Pavilion would now say "not found", the If already went True.
        self.world.on('POST', self.find, (200, {'found': False}))
        self.world.on('POST', self.issue, (200, {'card_id': '7'}))
        self.client.post(reverse('run_retry', args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, Run.SUCCEEDED)
        self.assertEqual(len(self.world.to(self.find)), 1)
        self.assertEqual(self.world.to(self.issue)[-1]['headers']['Idempotency-Key'], key)
        self.assertEqual([s.uid for s in run.step_results.all()], ['t', 'f', 'i', 'c', 'after'])
        self.assertEqual(self.world.to(self.ensure), [])

    def switch_workflow(self):
        case = lambda cid, op, value, amount, **more: {  # noqa: E731
            'id': cid, 'op': op, 'value': value, 'value2': more.get('value2', ''), 'label': more.get('label', ''),
            'steps': [self.card('k' + cid, amount)]}
        return Workflow.objects.create(name='Card by level', enabled=True, steps=[
            {'uid': 't', 'node': 'event', 'connection': self.th.id, 'key': 'volunteer.level_reached', 'params': {}},
            {'uid': 'f', 'node': 'action', 'connection': self.pv.id, 'key': 'staff.find',
             'params': {'email': '{{t.person.email}}'}},
            {'uid': 's', 'node': 'switch', 'params': {'on': '{{t.level}}'}, 'branches': [
                case('a', 'lte', '2', '5'), case('b', 'eq', '3', '10', label='Level three'),
                case('c', 'between', '4', '15', value2='6'),
                {'id': 'default', 'steps': []},
            ]},
        ])

    def test_switch_takes_the_first_matching_case_or_otherwise(self):
        workflow = self.switch_workflow()
        self.world.on('POST', self.find, (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', self.issue, (200, {'card_id': '7'}))
        for level, amount, case in ((1, 5.0, 'is at most 2'), (3, 10.0, 'Level three'), (5, 15.0, 'is between 4 and 6')):
            self.deliver(self.th, self.level_event(level=level, event_id=f'evt_{level}'))
            run = workflow.runs.first()
            self.assertEqual(self.world.to(self.issue)[-1]['json']['amount'], amount)
            self.assertEqual(run.context['s']['case'], case)
        issued = len(self.world.to(self.issue))
        self.deliver(self.th, self.level_event(level=9, event_id='evt_9'))
        run = workflow.runs.first()
        self.assertEqual(run.status, Run.SUCCEEDED)
        self.assertEqual(run.context['s'], {'matched': False, 'branch': 'default', 'case': 'Otherwise'})
        self.assertEqual(len(self.world.to(self.issue)), issued)

    def test_filter_inside_a_branch_stops_the_whole_run(self):
        workflow = self.if_workflow()
        steps = workflow.steps
        steps[2]['branches'][0]['steps'].insert(0, {'uid': 'g', 'node': 'condition',
                                                    'params': {'left': '{{t.level}}', 'op': 'gte', 'right': '4'}})
        workflow.steps = steps
        workflow.save()
        self.world.on('POST', self.find, (200, {'found': True, 'staff_id': '12'}))
        self.deliver(self.th, self.level_event(level=3))
        run = workflow.runs.get()
        self.assertEqual(run.status, Run.STOPPED)
        self.assertEqual(self.world.to('https://chat.test/send'), [])

    def test_helpers_know_the_tree(self):
        steps = self.if_workflow().steps
        self.assertEqual([s['uid'] for s in nodes.walk(steps)], ['t', 'f', 'i', 'c', 'e', 'after'])
        self.assertEqual([s['uid'] for s in nodes.before(steps, 'e')], ['t', 'f', 'i'])       # not its sibling branch
        self.assertEqual([s['uid'] for s in nodes.before(steps, 'after')], ['t', 'f', 'i', 'c', 'e'])
        connections = {self.th.id: self.th, self.pv.id: self.pv}
        self.assertEqual(nodes.problems(steps, connections), [])
        steps[2]['branches'][1]['steps'][0]['params']['email'] = ''
        self.assertEqual(nodes.problems(steps, connections), ['"Add staff member" needs email.'])
        self.assertEqual(nodes.problems(self.switch_workflow().steps[:2] + [
            {'uid': 's', 'node': 'switch', 'params': {'on': '{{t.level}}'}, 'branches': nodes.new_branches('switch')}],
            connections), ['A Switch case has nothing to compare with.'])


class BranchBuilderTests(Base):
    def post(self, name, workflow, body):
        return self.client.post(reverse(f'workflow_{name}', args=[workflow.pk]), json.dumps(body),
                                content_type='application/json')

    def test_save_keeps_branches_and_cleans_them(self):
        workflow = Workflow.objects.create(name='New')
        steps = [
            {'uid': 't', 'node': 'manual', 'params': {}},
            {'uid': 'i', 'node': 'if', 'params': {'left': '{{t.started_by}}', 'op': 'eq', 'right': 'andrew'},
             'branches': [{'id': 'false', 'steps': [{'uid': 'x', 'node': 'evil'}]},
                          {'id': 'bogus', 'steps': [{'uid': 'h9', 'node': 'http', 'params': {'url': 'https://a.test'}}]}]},
            {'uid': 's', 'node': 'switch', 'params': {'on': '{{t.started_by}}'}, 'branches': [
                {'id': 'c1', 'op': 'nonsense', 'value': 'andrew', 'steps': [
                    {'uid': 't', 'node': 'http', 'params': {'url': 'https://dup.test'}},   # a uid used twice
                    {'uid': 'h1', 'node': 'http', 'params': {'url': 'https://a.test'}}]},
                {'id': 'c1', 'op': 'eq', 'value': 'again'},
                {'id': 'default', 'steps': 'not a list'}]},
        ]
        answer = self.post('save', workflow, {'steps': steps}).json()
        self.assertEqual(answer['problems'], [])
        workflow.refresh_from_db()
        branch_ids = lambda step: [b['id'] for b in step['branches']]  # noqa: E731
        self.assertEqual(branch_ids(workflow.steps[1]), ['true', 'false'])
        self.assertEqual(workflow.steps[1]['branches'][1]['steps'], [])
        self.assertEqual(branch_ids(workflow.steps[2]), ['c1', 'default'])
        self.assertEqual(workflow.steps[2]['branches'][0]['op'], 'eq')
        self.assertEqual([s['uid'] for s in workflow.steps[2]['branches'][0]['steps']], ['h1'])
        page = self.client.get(reverse('builder', args=[workflow.pk]))
        self.assertEqual([d['title'] for d in page.context['data']['catalog']['steps']['basics']],
                         ['Filter', 'If', 'Switch', 'Send an HTTP request'])
        self.assertContains(self.client.get(reverse('workflows')), '4 steps')

    def test_test_step_on_an_if_and_inside_a_branch(self):
        workflow = Workflow.objects.create(name='New')
        steps = [
            {'uid': 't', 'node': 'manual', 'params': {}},
            {'uid': 'i', 'node': 'if', 'params': {'left': '{{t.started_by}}', 'op': 'eq', 'right': 'nobody'},
             'branches': [{'id': 'true', 'steps': []}, {'id': 'false', 'steps': [
                 {'uid': 'h', 'node': 'http', 'params': {'method': 'POST', 'url': 'https://chat.test/send',
                                                          'body': '{{i.branch}} {{t.started_by}}'}}]}]},
        ]
        answer = self.post('test_step', workflow, {'steps': steps, 'uid': 'i'}).json()
        self.assertEqual((answer['output'], answer['took']), ({'result': False, 'branch': 'false'}, 'False'))
        self.world.on('POST', 'https://chat.test/send', (200, {'ok': True}))
        self.post('test_step', workflow, {'steps': steps, 'uid': 'h'})
        self.assertEqual(self.world.calls[-1]['raw'], 'false Andrew')
        whole = self.post('test', workflow, {'steps': steps}).json()
        self.assertEqual([(s['uid'], s['status']) for s in whole['steps']], [('t', 'ok'), ('i', 'ok'), ('h', 'ok')])


class HookTests(Base):
    def test_bad_signature_and_unknown_connection(self):
        self.recipe()
        self.assertEqual(self.deliver(self.th, self.level_event(), secret='wrong').status_code, 401)
        gone = self.client.post(reverse('building_hook', args=['nope']), b'{}', content_type='application/json')
        self.assertEqual(gone.status_code, 410)
        self.assertEqual(Run.objects.count(), 0)

    def test_paused_workflow_keeps_missed_calls_for_replay(self):
        workflow = self.recipe(enabled=False)
        workflow.paused_at = timezone.now() - datetime.timedelta(minutes=5)
        workflow.save()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': False}))
        self.deliver(self.th, self.level_event())
        self.assertEqual(workflow.runs.count(), 0)
        self.assertEqual(engine.missed_events(workflow).count(), 1)
        page = self.client.get(reverse('builder', args=[workflow.pk]))
        self.assertContains(page, '1 call came in while this was paused')
        self.client.post(reverse('workflow_replay', args=[workflow.pk]))
        workflow.refresh_from_db()
        self.assertTrue(workflow.enabled)
        self.assertEqual(workflow.runs.get().status, Run.STOPPED)
        self.assertEqual(engine.missed_events(workflow).count(), 0)

    def test_webhook_trigger(self):
        workflow = Workflow.objects.create(name='Intake', enabled=True, steps=[
            {'uid': 't', 'node': 'webhook', 'params': {}},
            {'uid': 'c', 'node': 'condition', 'params': {'left': '{{t.body.email}}', 'op': 'not_empty'}},
        ])
        self.client.logout()
        url = reverse('workflow_hook', args=[workflow.hook_token])
        response = self.client.post(url, json.dumps({'email': 'sam@example.org'}), content_type='application/json')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(workflow.runs.get().status, Run.SUCCEEDED)
        self.client.post(url, '{}', content_type='application/json')
        self.assertEqual(workflow.runs.first().status, Run.STOPPED)
        Workflow.objects.filter(pk=workflow.pk).update(enabled=False)
        self.assertEqual(self.client.post(url, '{}', content_type='application/json').status_code, 409)
        self.assertEqual(self.client.post(reverse('workflow_hook', args=['nope'])).status_code, 404)


class ScheduleTests(Base):
    zone = zoneinfo.ZoneInfo('America/Vancouver')

    def at(self, *parts):
        return datetime.datetime(*parts, tzinfo=self.zone)

    def test_next_fire(self):
        monday_noon = self.at(2026, 11, 23, 12, 0)
        self.assertEqual(worker.next_fire({'repeat': 'week', 'weekday': 'mon', 'time': '08:00'}, monday_noon, self.zone),
                         self.at(2026, 11, 30, 8, 0))
        self.assertEqual(worker.next_fire({'repeat': 'week', 'weekday': 'wed', 'time': '08:00'}, monday_noon, self.zone),
                         self.at(2026, 11, 25, 8, 0))
        self.assertEqual(worker.next_fire({'repeat': 'day', 'time': '18:30'}, monday_noon, self.zone),
                         self.at(2026, 11, 23, 18, 30))
        self.assertEqual(worker.next_fire({'repeat': 'hour', 'time': '00:15'}, monday_noon, self.zone),
                         self.at(2026, 11, 23, 12, 15))

    def test_due_schedule_fires_once_and_moves_on(self):
        Settings.objects.update_or_create(pk=1, defaults={'time_zone': 'America/Vancouver'})
        workflow = Workflow.objects.create(name='Digest', enabled=True, steps=[
            {'uid': 't', 'node': 'schedule', 'params': {'repeat': 'day', 'time': '08:00'}},
            {'uid': 'c', 'node': 'condition', 'params': {'left': '{{t.fired_at}}', 'op': 'not_empty'}},
        ])
        worker.plan(workflow, now=self.at(2026, 11, 23, 7, 0))
        workflow.save()
        self.assertEqual(worker.fire_schedules(now=self.at(2026, 11, 23, 7, 59)), [])
        self.assertEqual(len(worker.fire_schedules(now=self.at(2026, 11, 23, 8, 0, 30))), 1)
        self.assertEqual(worker.fire_schedules(now=self.at(2026, 11, 23, 8, 1)), [])
        run = workflow.runs.get()
        self.assertEqual((run.status, run.trigger_label), (Run.SUCCEEDED, 'Schedule · Every day at 08:00'))
        workflow.refresh_from_db()
        self.assertEqual(workflow.next_fire_at, self.at(2026, 11, 24, 8, 0))

    def test_prune(self):
        workflow = self.recipe()
        old = timezone.now() - datetime.timedelta(days=40)
        Run.objects.create(workflow=workflow, status=Run.SUCCEEDED, created_at=old)
        Run.objects.create(workflow=workflow, status=Run.SUCCEEDED)
        Event.objects.create(connection=self.th, event_id='old', type='x', received_at=old)
        worker._last_prune = None
        worker.prune()
        self.assertEqual((Run.objects.count(), Event.objects.count()), (1, 0))


class ConnectTests(Base):
    def setUp(self):
        super().setUp()
        Connection.objects.all().delete()
        Settings.objects.update_or_create(pk=1, defaults={'org_name': 'Cathedral'})

    def test_handshake(self):
        self.world.on('GET', 'https://pv.test/.well-known/switchboard', (200, PV_MANIFEST))
        response = self.client.post(reverse('building_connect'), {'address': 'pv.test/admin/'})
        connection = Connection.objects.get()
        self.assertEqual(connection.status, Connection.PENDING)
        self.assertTrue(response['Location'].startswith('https://pv.test/switchboard/authorize?callback=http%3A%2F%2Ftestserver%2Fbuildings%2Fcallback%2F&state='))
        self.assertIn('name=Cathedral', response['Location'])

        self.world.on('POST', 'https://pv.test/switchboard/v1/token',
                      (200, {'api_key': 'sbk_live', 'webhook_secret': 'whs', 'connection_id': '7', 'instance': 'Café'}))
        back = self.client.get(reverse('building_callback'), {'state': connection.state, 'code': 'c0de'})
        self.assertRedirects(back, reverse('buildings'))
        connection.refresh_from_db()
        self.assertEqual((connection.status, connection.api_key, connection.webhook_secret), ('connected', 'sbk_live', 'whs'))
        self.assertNotIn('sbk_live', connection.api_key_enc)
        sent = self.world.to('https://pv.test/switchboard/v1/token')[0]['json']
        self.assertEqual(sent['code'], 'c0de')
        self.assertEqual(sent['webhook_url'], f'http://testserver/hooks/building/{connection.hook_token}')
        self.assertContains(self.client.get(reverse('buildings')), 'Pavilion')

    def test_cancel_and_bad_code_leave_nothing_behind(self):
        self.world.on('GET', 'https://pv.test/.well-known/switchboard', (200, PV_MANIFEST))
        self.client.post(reverse('building_connect'), {'address': 'https://pv.test'})
        state = Connection.objects.get().state
        self.client.get(reverse('building_callback'), {'state': state, 'error': 'access_denied'})
        self.assertEqual(Connection.objects.count(), 0)

        self.client.post(reverse('building_connect'), {'address': 'https://pv.test'})
        self.world.on('POST', 'https://pv.test/switchboard/v1/token',
                      (400, {'error': {'code': 'invalid_code', 'message': 'That code was already used.'}}))
        page = self.client.get(reverse('building_callback'), {'state': Connection.objects.get().state, 'code': 'x'}, follow=True)
        self.assertContains(page, 'That code was already used.')
        self.assertEqual(Connection.objects.count(), 0)
        self.assertEqual(self.client.get(reverse('building_callback'), {'state': 'made-up', 'code': 'x'}).status_code, 302)

    def test_not_a_building(self):
        self.world.on('GET', 'https://example.org/.well-known/switchboard', (200, b'<html>hello</html>'))
        page = self.client.post(reverse('building_connect'), {'address': 'example.org'}, follow=True)
        self.assertContains(page, 'answer like a Hamlet building')
        self.assertEqual(Connection.objects.count(), 0)

    def test_reconnecting_keeps_the_same_connection(self):
        pv = connect(PV_MANIFEST, 'pv.test')
        pv.status = Connection.BROKEN
        pv.save()
        self.world.on('GET', 'https://pv.test/.well-known/switchboard', (200, PV_MANIFEST))
        self.client.post(reverse('building_connect'), {'address': 'https://pv.test'})
        self.world.on('POST', 'https://pv.test/switchboard/v1/token', (200, {'api_key': 'sbk_new', 'webhook_secret': 'n'}))
        self.client.get(reverse('building_callback'), {'state': Connection.objects.get().state, 'code': 'c'})
        again = Connection.objects.get()
        self.assertEqual((again.pk, again.status, again.api_key), (pv.pk, 'connected', 'sbk_new'))

    def test_disconnect_tells_the_building_and_pauses_workflows(self):
        self.th, self.pv = connect(TH_MANIFEST, 'th.test'), connect(PV_MANIFEST, 'pv.test')
        workflow = self.recipe()
        self.world.on('DELETE', 'https://pv.test/switchboard/v1/connection', (200, {'ok': True}))
        self.client.post(reverse('building_disconnect', args=[self.pv.pk]))
        self.assertEqual(len(self.world.to('https://pv.test/switchboard/v1/connection')), 1)
        workflow.refresh_from_db()
        self.assertFalse(workflow.enabled)
        self.assertIn('disconnected', ' '.join(nodes.problems(workflow.steps, {self.th.id: self.th})))


class PageTests(Base):
    def test_first_run_goes_to_setup(self):
        get_user_model().objects.all().delete()
        middleware._has_users = False
        self.assertRedirects(self.client.get('/'), reverse('setup'))
        response = self.client.post(reverse('setup'), {
            'org_name': 'Holy Eucharist', 'username': 'andrew', 'password': 'long enough', 'time_zone': 'America/Vancouver'})
        self.assertRedirects(response, reverse('buildings'))
        self.assertEqual(Settings.load().org_name, 'Holy Eucharist')
        self.assertRedirects(self.client.get(reverse('setup')), reverse('login'), fetch_redirect_response=False)

    def test_pages_need_sign_in(self):
        self.client.logout()
        for name in ('workflows', 'runs', 'buildings', 'settings'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)
        bad = self.client.post(reverse('login'), {'username': 'andrew', 'password': 'nope'})
        self.assertContains(bad, 'form-error')
        good = self.client.post(reverse('login'), {'username': 'andrew', 'password': 'correct horse'})
        self.assertRedirects(good, reverse('workflows'))

    def test_workflows_page_folders_filters_and_recipes(self):
        folder = Folder.objects.create(name='Volunteers')
        workflow = self.recipe()
        workflow.folder = folder
        workflow.save()
        Workflow.objects.create(name='Website form intake')
        Run.objects.create(workflow=workflow, status=Run.FAILED)
        page = self.client.get(reverse('workflows'))
        self.assertContains(page, '2 workflows · 1 running')
        self.assertContains(page, 'Needs attention')
        self.assertContains(page, 'Sync shifts to time clock')
        card = reverse('builder', args=[workflow.pk])
        self.assertContains(page, card)
        self.assertNotContains(self.client.get(reverse('workflows'), {'status': 'paused'}), card)
        self.assertNotContains(self.client.get(reverse('workflows'), {'folder': folder.pk}), 'Website form intake')
        self.assertContains(self.client.get(reverse('workflows'), {'q': 'website'}), 'Website form intake')

    def test_new_from_recipe_duplicate_move_delete(self):
        response = self.client.post(reverse('workflow_new'), {'recipe': 'staff-cards'})
        workflow = Workflow.objects.get()
        self.assertRedirects(response, reverse('builder', args=[workflow.pk]))
        self.assertEqual(len(workflow.steps), 4)
        self.client.post(reverse('workflow_duplicate', args=[workflow.pk]))
        copy = Workflow.objects.exclude(pk=workflow.pk).get()
        self.assertNotEqual(copy.steps[0]['uid'], workflow.steps[0]['uid'])
        self.assertIn(copy.steps[0]['uid'], copy.steps[1]['params']['email'])
        self.assertEqual(nodes.problems(copy.steps, {self.th.id: self.th, self.pv.id: self.pv}), [])
        folder = Folder.objects.create(name='Restaurant')
        self.client.post(reverse('workflow_move', args=[copy.pk]), {'folder': folder.pk})
        self.assertEqual(Workflow.objects.get(pk=copy.pk).folder, folder)
        self.client.post(reverse('workflow_delete', args=[copy.pk]))
        self.assertEqual(Workflow.objects.count(), 1)

    def test_runs_page(self):
        workflow = self.recipe()
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': False}))
        self.deliver(self.th, self.level_event())
        page = self.client.get(reverse('runs'))
        self.assertContains(page, 'Town Hall · Maria K. reached level 3')
        self.assertContains(page, 'so the workflow stopped here')
        self.assertContains(page, 'Issue staff card')   # the step that never got a turn
        self.assertNotContains(self.client.get(reverse('runs'), {'status': 'failed'}), 'Maria K.')
        self.assertContains(self.client.get(reverse('runs'), {'workflow': workflow.pk, 'days': 1}), 'Maria K.')

    def test_settings(self):
        self.client.post(reverse('settings'), {'op': 'org', 'org_name': 'Cathedral', 'time_zone': 'America/Vancouver', 'keep_days': '14'})
        org = Settings.load()
        self.assertEqual((org.org_name, org.time_zone, org.keep_days), ('Cathedral', 'America/Vancouver', 14))
        self.client.post(reverse('settings'), {'op': 'secret_add', 'name': 'telegram bot', 'value': 'abc123'})
        secret = Secret.objects.get()
        self.assertEqual((secret.name, secret.value), ('TELEGRAM_BOT', 'abc123'))
        page = self.client.get(reverse('settings'))
        self.assertContains(page, 'TELEGRAM_BOT')
        self.assertNotContains(page, 'abc123')
        self.client.post(reverse('settings'), {'op': 'user_add', 'username': 'dana', 'password': 'long enough'})
        self.assertTrue(get_user_model().objects.filter(username='dana').exists())
        self.client.post(reverse('settings'), {'op': 'user_remove', 'id': self.user.pk})
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())


class BuilderTests(Base):
    def post(self, name, workflow, body):
        return self.client.post(reverse(f'workflow_{name}', args=[workflow.pk]), json.dumps(body),
                                content_type='application/json')

    def test_builder_page_carries_the_catalog(self):
        workflow = self.recipe()
        page = self.client.get(reverse('builder', args=[workflow.pk]))
        self.assertContains(page, 'Staff card by volunteer level')
        data = page.context['data']
        self.assertEqual([b['name'] for b in data['catalog']['triggers']['buildings']], ['Pavilion', 'Town Hall'])
        self.assertIn(f'action:{self.pv.pk}:staff_card.issue', data['defs'])
        self.assertEqual(data['problems'], [])

    def test_save_cleans_steps_and_pauses_a_broken_workflow(self):
        workflow = self.recipe()
        steps = workflow.steps + [{'uid': 'bad uid!', 'node': 'http'}, {'uid': 'x1', 'node': 'evil'},
                                  {'uid': 'h1', 'node': 'http', 'params': {'url': '', 'junk': {'a': 1}}}]
        response = self.post('save', workflow, {'name': '  Cards  ', 'steps': steps}).json()
        workflow.refresh_from_db()
        self.assertEqual(workflow.name, 'Cards')
        self.assertEqual(len(workflow.steps), 5)
        self.assertEqual(response['problems'], ['"Send an HTTP request" needs url.'])
        self.assertTrue(response['paused'])
        self.assertFalse(workflow.enabled)

    def test_toggle_refuses_until_it_can_run(self):
        workflow = Workflow.objects.create(name='New')
        self.assertFalse(self.post('toggle', workflow, {'on': True, 'steps': []}).json()['ok'])
        steps = [{'uid': 't', 'node': 'schedule', 'params': {'repeat': 'day', 'time': '08:00'}}]
        self.assertTrue(self.post('toggle', workflow, {'on': True, 'steps': steps}).json()['enabled'])
        workflow.refresh_from_db()
        self.assertIsNotNone(workflow.next_fire_at)
        self.post('toggle', workflow, {'on': False})
        workflow.refresh_from_db()
        self.assertEqual((workflow.enabled, workflow.next_fire_at), (False, None))
        self.assertIsNotNone(workflow.paused_at)

    def test_test_step_uses_sample_data_and_remembers_the_result(self):
        workflow = self.recipe(enabled=False)
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': True, 'staff_id': '12'}))
        trigger = self.post('test_step', workflow, {'steps': workflow.steps, 'uid': workflow.steps[0]['uid']}).json()
        self.assertEqual(trigger['output']['person']['name'], 'Maria K.')
        self.assertFalse(trigger['real'])
        result = self.post('test_step', workflow, {'steps': workflow.steps, 'uid': workflow.steps[1]['uid']}).json()
        self.assertEqual(result['output'], {'found': True, 'staff_id': '12'})
        self.assertEqual(self.world.calls[-1]['json'], {'email': 'maria@example.org'})
        workflow.refresh_from_db()
        self.assertEqual(workflow.samples[workflow.steps[1]['uid']], {'found': True, 'staff_id': '12'})
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find',
                      (422, {'error': {'code': 'invalid_params', 'message': 'Email is missing.'}}))
        failed = self.post('test_step', workflow, {'steps': workflow.steps, 'uid': workflow.steps[1]['uid']}).json()
        self.assertEqual((failed['ok'], failed['error']), (False, '422 · Email is missing.'))

    def test_test_workflow_runs_it_as_a_test(self):
        workflow = self.recipe(enabled=False)
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff.find', (200, {'found': True, 'staff_id': '12'}))
        self.world.on('POST', 'https://pv.test/switchboard/v1/actions/staff_card.issue', (200, {'card_id': '7'}))
        result = self.post('test', workflow, {'steps': workflow.steps}).json()
        self.assertEqual((result['ok'], result['status']), (True, 'succeeded'))
        self.assertEqual([s['status'] for s in result['steps']], ['ok'] * 4)
        run = workflow.runs.get()
        self.assertTrue(run.is_test)
        self.assertNotContains(self.client.get(reverse('runs')), 'Test · Volunteer reached a level')
        self.assertContains(self.client.get(reverse('runs'), {'tests': 1}), 'Test · Volunteer reached a level')

    def test_json_calls_need_sign_in(self):
        workflow = self.recipe()
        self.client.logout()
        self.assertEqual(self.post('save', workflow, {}).status_code, 401)
