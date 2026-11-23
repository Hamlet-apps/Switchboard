"""The workflow builder: one page, plus the JSON calls its script makes."""
import datetime
import json
import re
import time
import uuid
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import buildings, engine, expressions, nodes, templating, worker
from ..buildings import BuildingError
from ..models import Connection, Event, Folder, Run, Secret, Settings, Workflow
from ..templating import MAP_OPS

UID = re.compile(r'^[A-Za-z0-9_]{1,24}$')
KNOWN = set(nodes.BUILTIN) | {'event', 'action'}


def json_call(view):
    """POST with a JSON object body, signed in; errors answer as {"error": "…"}."""
    @wraps(view)
    def wrapped(request, pk):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'You were signed out. Sign in again in another tab, then retry.'}, status=401)
        if request.method != 'POST':
            return JsonResponse({'error': 'POST only.'}, status=405)
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JsonResponse({'error': "That request wasn't JSON."}, status=400)
        return view(request, get_object_or_404(Workflow, pk=pk), body)
    return wrapped


def _clean_params(raw):
    params = {}
    for key, value in raw.items() if isinstance(raw, dict) else []:
        if isinstance(value, dict) and isinstance(value.get('map'), dict):
            spec = value['map']
            value = {'map': {
                'on': str(spec.get('on', ''))[:500],
                'rules': [{k: str(rule.get(k, ''))[:500] for k in ('op', 'value', 'value2', 'then')}
                          for rule in spec.get('rules') or [] if isinstance(rule, dict)][:30],
                'else': str(spec.get('else', ''))[:500],
            }}
        elif isinstance(value, bool):
            value = 'true' if value else 'false'
        else:
            value = str(value if value is not None else '')[:20_000]
        params[str(key)[:80]] = value
    return params


def _clean_cases(raw):
    cases, ids = [], set()
    for case in raw if isinstance(raw, list) else []:
        key = str(case.get('id', '')) if isinstance(case, dict) else ''
        if key in ('default', 'main', 'true', 'false', 'each', 'done', 'error') or not UID.match(key) or key in ids or len(cases) >= 12:
            continue
        ids.add(key)
        op = case.get('op') if case.get('op') in dict(MAP_OPS) else 'eq'
        cases.append({'id': key, 'op': op, 'value': str(case.get('value', ''))[:500],
                      'value2': str(case.get('value2', ''))[:500], 'label': str(case.get('label', ''))[:60]})
    return cases or nodes.new_cases()


FIELD_NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,79}$')


def _clean_fields(raw):
    """Set fields rows: a name (a dotted name makes a nested field) and a value."""
    fields = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict) or len(fields) >= 50:
            continue
        name = '.'.join(part.strip() for part in str(row.get('name') or '').split('.')).strip()
        fields.append({'name': name if FIELD_NAME.match(name) else '', 'value': str(row.get('value') or '')[:20_000]})
    return fields


def _number(value):
    try:
        return max(-20000, min(20000, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return None


def clean_steps(raw):
    """Steps from the browser, keeping only what a step may hold.

    Wires that lead nowhere, into the trigger, or back round in a loop are dropped, and the
    trigger is moved to the front.
    """
    raw = nodes.upgrade([item for item in raw if isinstance(item, dict)]) if isinstance(raw, list) else []
    steps, seen = [], set()
    for item in raw:
        uid, node = str(item.get('uid', '')), item.get('node')
        if not UID.match(uid) or uid in seen or uid == 'secret' or node not in KNOWN or len(steps) >= 80:
            continue
        seen.add(uid)
        connection = item.get('connection') if node in ('event', 'action') else None
        step = {'uid': uid, 'node': node, 'connection': connection if isinstance(connection, int) else None,
                'key': str(item.get('key') or '')[:120] if node in ('event', 'action') else '',
                'params': _clean_params(item.get('params')),
                'x': _number(item.get('x')), 'y': _number(item.get('y')), 'next': {}}
        if node == 'switch':
            step['cases'] = _clean_cases(item.get('cases'))
        if node == 'set':
            step['fields'] = _clean_fields(item.get('fields'))
        if node in nodes.CAN_FAIL_ON and item.get('on_error') == 'branch':
            step['on_error'] = 'branch'
        step['_wires'] = item.get('next') if isinstance(item.get('next'), dict) else {}
        steps.append(step)
    steps.sort(key=lambda step: step['node'] not in nodes.TRIGGERS)     # stable: the trigger first
    trigger = steps[0]['uid'] if steps and steps[0]['node'] in nodes.TRIGGERS else None
    for step in steps:
        wires = step.pop('_wires')
        for port in nodes.ports(step):
            target = wires.get(port)
            if (isinstance(target, str) and target in seen and target not in (step['uid'], trigger)
                    and not nodes.would_loop(steps, step['uid'], target)):
                step['next'][port] = target
    return steps


def _connections():
    return {c.id: c for c in Connection.objects.all()}


def _apply(workflow, body, connections):
    """Save what the builder sent. Returns the problems that keep it from running."""
    if 'name' in body:
        workflow.name = str(body['name']).strip()[:120] or 'Untitled workflow'
    if 'description' in body:
        workflow.description = str(body['description']).strip()[:300]
    if 'steps' in body:
        workflow.steps = clean_steps(body['steps'])
        workflow.samples = {uid: sample for uid, sample in workflow.samples.items()
                            if nodes.find(workflow.steps, uid)}
    found = nodes.problems(workflow.steps, connections)
    paused = False
    if workflow.enabled and found:
        workflow.enabled, workflow.paused_at, paused = False, timezone.now(), True
    worker.plan(workflow)
    workflow.save()
    return found, paused


@login_required
def builder(request, pk):
    workflow = get_object_or_404(Workflow.objects.select_related('folder'), pk=pk)
    connections = list(Connection.objects.all())
    by_id = {c.id: c for c in connections}
    catalog = nodes.catalog(connections)
    defs = {}
    for side in ('triggers', 'steps'):
        for definition in catalog[side]['basics']:
            defs[definition['id']] = definition
        for building in catalog[side]['buildings']:
            for definition in building['items']:
                defs[definition['id']] = definition
    missing = {step['uid']: d for step in nodes.walk(workflow.steps)
               for d in [nodes.describe(step, by_id)] if d.get('missing')}
    hook = request.build_absolute_uri(reverse('workflow_hook', args=[workflow.hook_token]))
    data = {
        'workflow': {'id': workflow.pk, 'name': workflow.name, 'description': workflow.description,
                     'enabled': workflow.enabled, 'steps': nodes.upgrade(workflow.steps)},
        'catalog': catalog, 'defs': defs, 'missing': missing, 'samples': workflow.samples,
        'hookUrl': hook,
        'testHookUrl': request.build_absolute_uri(reverse('workflow_hook_test', args=[workflow.hook_token])),
        'secrets': [s.name for s in Secret.objects.all()],
        'mapOps': [{'value': v, 'label': l} for v, l in MAP_OPS],
        'builtins': list(nodes.BUILTIN),
        'problems': nodes.problems(workflow.steps, by_id) if workflow.steps else [],
        'urls': {name: reverse(f'workflow_{name}', args=[workflow.pk])
                 for name in ('save', 'toggle', 'test', 'test_step', 'listen', 'run', 'preview', 'options')},
        'expressionHelp': expressions.help_rows(),
        'buildingsUrl': reverse('buildings'),
        'openPicker': not workflow.steps,
    }
    return render(request, 'board/builder.html', {
        'workflow': workflow, 'data': data, 'folders': Folder.objects.all(),
        'run_count': workflow.runs.filter(is_test=False).count(),
        'missed': engine.missed_events(workflow).count(),
        'back': f"{reverse('workflows')}?folder={workflow.folder_id}" if workflow.folder_id else reverse('workflows'),
    })


@json_call
def workflow_save(request, workflow, body):
    found, paused = _apply(workflow, body, _connections())
    return JsonResponse({'ok': True, 'problems': found, 'enabled': workflow.enabled, 'paused': paused})


@json_call
def workflow_toggle(request, workflow, body):
    connections = _connections()
    found, _ = _apply(workflow, body, connections)
    want = bool(body.get('on'))
    if want and found:
        return JsonResponse({'ok': False, 'enabled': False, 'problems': found, 'error': found[0]})
    workflow.enabled = want
    workflow.paused_at = None if want else timezone.now()
    worker.plan(workflow)
    workflow.save()
    return JsonResponse({'ok': True, 'enabled': want, 'problems': []})


def _sample_context(workflow, earlier, connections):
    """What the steps before this one would give: their last test, else sample data."""
    ctx = {}
    for index, step in enumerate(earlier):
        sample = workflow.samples.get(step['uid'])
        if not isinstance(sample, dict):
            definition = nodes.describe(step, connections)
            if index == 0:
                sample = engine.trigger_sample(workflow, step, definition)[0]
            elif step['node'] in nodes.TRANSFORMS:
                # These only work values out, so the real answer costs nothing.
                try:
                    sample = engine.perform(step, definition, ctx, connections, {}, '')[0]
                except BuildingError:
                    sample = nodes.example_output(definition)
            elif step['node'] == 'loop':
                # Inside a loop, steps see its first item: go through the list as it stands in the samples.
                try:
                    sample = engine.loop_sample(engine.perform(step, definition, ctx, connections, {}, '')[0])
                except BuildingError:
                    sample = {'item': None, 'index': 1, 'count': 0}
            else:
                sample = nodes.example_output(definition)
        ctx[step['uid']] = sample
    return ctx


@json_call
def workflow_test_step(request, workflow, body):
    """Run one step for real, feeding it the earlier steps' sample data."""
    connections = _connections()
    steps = clean_steps(body.get('steps'))
    step = nodes.find(steps, body.get('uid'))
    if step is None:
        return JsonResponse({'error': "That step isn't in the workflow any more."}, status=400)
    definition = nodes.describe(step, connections)
    began = time.monotonic()
    if steps and steps[0] is step:
        output, real = engine.trigger_sample(workflow, step, definition)
        result = {'ok': True, 'output': output, 'real': real}
    else:
        ctx = _sample_context(workflow, nodes.before(steps, step['uid']), connections)
        secrets_ = {s.name: s.value for s in Secret.objects.all()}
        try:
            output, _sent, note, _tries = engine.perform_with_retries(
                step, definition, ctx, connections, secrets_, uuid.uuid4().hex, waits=[])
            if step['node'] == 'loop':
                note, output = f'{note} to go through', engine.loop_sample(output)
            result = {'ok': True, 'output': output, 'real': True}
            if step['node'] == 'loop':
                result['note'] = note
            if step['node'] in nodes.BRANCHING:
                result['took'] = note
        except engine.Stop:
            output = {'passed': False}
            result = {'ok': True, 'output': output, 'real': True, 'stopped': True}
        except BuildingError as error:
            message = engine._redact(str(error), secrets_)
            if not nodes.on_error_output(step):
                return JsonResponse({'ok': False, 'error': message})
            # Not kept as its sample: the steps on its main output would get the error instead.
            return JsonResponse({'ok': True, 'output': nodes.error_output(message), 'real': True, 'took': 'Error',
                                 'error': message, 'ms': int((time.monotonic() - began) * 1000)})
    Workflow.objects.filter(pk=workflow.pk).update(samples={**workflow.samples, step['uid']: output})
    result['ms'] = int((time.monotonic() - began) * 1000)
    return JsonResponse(result)


@json_call
def workflow_preview(request, workflow, body):
    """What a setting with expressions in it comes to, on the sample data of the steps before."""
    connections = _connections()
    steps = clean_steps(body.get('steps'))
    step = nodes.find(steps, body.get('uid'))
    if step is None:
        return JsonResponse({'error': "That step isn't in the workflow any more."}, status=400)
    ctx = _sample_context(workflow, nodes.before(steps, step['uid']), connections)
    hidden = {s.name: '••••' for s in Secret.objects.all()}
    try:
        value = templating.resolve(str(body.get('value') or '')[:20_000], ctx, hidden)
    except expressions.ExpressionError as error:
        return JsonResponse({'ok': False, 'error': str(error)})
    return JsonResponse({'ok': True, 'value': value, 'shown': templating.text(value)[:500]})


# How long the webhook's test URL stays open after the builder last asked. The builder asks
# every 2 s, so this only matters once it stops (closed tab, lost connection).
TEST_URL_GRACE = datetime.timedelta(seconds=30)


@json_call
def workflow_options(request, workflow, body):
    """The list a building param offers (its manifest `options`), read from the building now."""
    connection = Connection.objects.filter(pk=body.get('connection')).first()
    if connection is None:
        return JsonResponse({'error': 'That building is no longer connected.'})
    try:
        options = buildings.fetch_options(connection, body.get('name'))
    except BuildingError as error:
        return JsonResponse({'error': str(error)})
    return JsonResponse({'ok': True, 'options': options})


@json_call
def workflow_listen(request, workflow, body):
    """Wait for the trigger to really fire ("Test this step" on a building or webhook trigger).

    The builder calls this every few seconds until something comes in, and that becomes the
    trigger's sample. Nothing runs.

    - A building's event: buildings send every event and each one is stored, so this looks for
      one of the trigger's type from its building that is newer than `after`. The first call
      (no `after`) answers with where to look from.
    - A webhook: the test URL answers only while this keeps being called, and leaves the call it
      caught in `Workflow.test_call`. `{"stop": true}` closes the test URL.
    """
    steps = clean_steps(body.get('steps'))
    step = nodes.find(steps, body.get('uid'))
    if step is None or not steps or steps[0] is not step or step['node'] not in ('event', 'webhook'):
        return JsonResponse({'ok': False, 'error': 'Only a trigger from a building or a webhook can listen.'}, status=400)
    if step['node'] == 'webhook':
        return _listen_webhook(workflow, step, body)
    connection = _connections().get(step['connection'])
    if connection is None or connection.status == Connection.PENDING:
        return JsonResponse({'ok': False, 'error': 'This building is no longer connected. Reconnect it in Buildings, then try again.'})
    if connection.status == Connection.BROKEN:
        return JsonResponse({'ok': False, 'error': f'{connection.name} refused our key, so its events can’t reach Switchboard. Reconnect it in Buildings, then try again.'})
    after = body.get('after')
    if not isinstance(after, int):
        latest = Event.objects.order_by('-pk').values_list('pk', flat=True).first()
        return JsonResponse({'ok': True, 'listening': True, 'after': latest or 0})
    event = (Event.objects.filter(connection=connection, type=step['key'], pk__gt=after)
             .order_by('pk').first())
    if event is None:
        return JsonResponse({'ok': True, 'listening': True, 'after': after})
    output = event.payload.get('data') if isinstance(event.payload.get('data'), dict) else {}
    return _heard(workflow, step, output, event.summary, event.received_at)


def _listen_webhook(workflow, step, body):
    mine = Workflow.objects.filter(pk=workflow.pk)
    if body.get('stop'):
        mine.update(test_until=None, test_call=None)
        return JsonResponse({'ok': True, 'listening': False})
    if not isinstance(body.get('after'), int):          # starting: forget any call from before
        mine.update(test_until=timezone.now() + TEST_URL_GRACE, test_call=None)
        return JsonResponse({'ok': True, 'listening': True, 'after': 0})
    call = workflow.test_call
    if isinstance(call, dict) and mine.filter(test_call__isnull=False).update(test_call=None):
        return _heard(workflow, step, call, 'Call to the test URL', timezone.now())
    mine.update(test_until=timezone.now() + TEST_URL_GRACE)
    return JsonResponse({'ok': True, 'listening': True, 'after': 0})


def _heard(workflow, step, output, what, when):
    """The trigger really fired while the builder listened: keep it as the sample."""
    Workflow.objects.filter(pk=workflow.pk).update(samples={**workflow.samples, step['uid']: output})
    at = timezone.localtime(when).strftime('%H:%M:%S')
    return JsonResponse({'ok': True, 'output': output, 'real': True, 'heard': what, 'at': at})


@json_call
def workflow_test(request, workflow, body):
    """Save, then run the whole workflow once with sample trigger data. Actions really happen."""
    connections = _connections()
    found, _ = _apply(workflow, body, connections)
    if found:
        return JsonResponse({'ok': False, 'error': found[0], 'problems': found})
    trigger = workflow.steps[0]
    definition = nodes.describe(trigger, connections)
    sample = workflow.samples.get(trigger['uid'])
    if not isinstance(sample, dict):
        sample = engine.trigger_sample(workflow, trigger, definition)[0]
    run = engine.start_run(workflow, sample, f"Test · {definition['title']}", is_test=True)
    engine.execute(run)
    run.refresh_from_db()
    # A step inside a loop has a result per item: show the worst, and keep the first item's
    # output as its sample.
    rank = {'failed': 0, 'stopped': 1, 'ok': 2}
    results, samples = {}, {}
    for result in run.step_results.all():
        if result.uid not in results or rank.get(result.status, 3) < rank.get(results[result.uid].status, 3):
            results[result.uid] = result
    for key, value in run.context.items():
        uid, _, iteration = key.partition('@')
        step = nodes.find(run.steps, uid)
        if step and isinstance(value, dict) and (not iteration or set(iteration.split('.')) == {'1'}):
            samples[uid] = engine.loop_sample(value) if step['node'] == 'loop' else value
    Workflow.objects.filter(pk=workflow.pk).update(samples={**workflow.samples, **samples})
    return JsonResponse({
        'ok': run.status != Run.FAILED, 'status': run.status, 'run': run.pk, 'error': run.error,
        'runUrl': f"{reverse('runs')}?run={run.pk}&tests=1",
        'steps': [{'uid': step['uid'], 'status': results[step['uid']].status if step['uid'] in results else 'skipped',
                   'error': results[step['uid']].error if step['uid'] in results else '',
                   'output': samples.get(step['uid']) if step['uid'] in results else None}
                  for step in nodes.walk(run.steps)],
    })


@login_required
@require_POST
def workflow_run(request, pk):
    """Run now: for workflows that start by hand."""
    workflow = get_object_or_404(Workflow, pk=pk)
    trigger = workflow.trigger
    found = nodes.problems(workflow.steps, _connections())
    if not trigger or trigger.get('node') != 'manual' or found:
        messages.error(request, found[0] if found else 'Only workflows that start manually have Run now.')
        return redirect('builder', workflow.pk)
    name = request.user.get_full_name() or request.user.get_username()
    now = timezone.localtime(timezone.now(), Settings.load().zone()).isoformat(timespec='seconds')
    run = engine.start_run(workflow, {'started_by': name, 'started_at': now}, f'Manual · started by {name}')
    worker.submit(run)
    messages.success(request, 'Started. Follow it in Runs.')
    return redirect(f"{reverse('runs')}?workflow={workflow.pk}&run={run.pk}")


@login_required
@require_POST
def workflow_replay(request, pk):
    """Put through the calls that came in while the workflow was paused."""
    workflow = get_object_or_404(Workflow, pk=pk)
    events = list(engine.missed_events(workflow))
    if request.POST.get('drop'):
        workflow.paused_at = timezone.now()
        workflow.save(update_fields=['paused_at', 'updated_at'])
        messages.success(request, 'Dropped. Those calls will not run.')
        return redirect('builder', workflow.pk)
    found = nodes.problems(workflow.steps, _connections())
    if found:
        messages.error(request, found[0])
        return redirect('builder', workflow.pk)
    workflow.enabled, workflow.paused_at = True, None
    worker.plan(workflow)
    workflow.save()
    count = engine.replay(workflow, events)
    messages.success(request, f"Switched on and put {count} missed call{'' if count == 1 else 's'} through.")
    return redirect(f"{reverse('runs')}?workflow={workflow.pk}")
