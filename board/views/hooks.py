"""What other servers call: building events and workflows' private webhook URLs."""
import json

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .. import buildings, engine, worker
from ..models import Connection, Settings, Workflow


def _error(status, code, message):
    return JsonResponse({'error': {'code': code, 'message': message}}, status=status)


def healthz(request):
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
def building_hook(request, token):
    """A building delivering an event (docs/PROTOCOL.md, section 4)."""
    connection = Connection.objects.filter(hook_token=token).exclude(status=Connection.PENDING).first()
    if connection is None:
        return _error(410, 'gone', 'This Switchboard no longer knows this connection.')
    if not buildings.verify_signature(connection, request.body, request.headers.get('X-Switchboard-Signature', '')):
        return _error(401, 'bad_signature', "The signature doesn't match.")
    try:
        envelope = json.loads(request.body)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or not envelope.get('id') or not envelope.get('type'):
        return _error(400, 'invalid_request', 'Expected an event with an id and a type.')
    event, runs = engine.receive_event(connection, envelope)
    return JsonResponse({'ok': True, 'duplicate': event is None, 'runs': len(runs)}, status=202)


@csrf_exempt
@require_POST
def workflow_hook(request, token):
    """A workflow's "Incoming webhook" trigger: any JSON (or text) starts it."""
    workflow = Workflow.objects.filter(hook_token=token).first()
    trigger = workflow.trigger if workflow else None
    if not trigger or trigger.get('node') != 'webhook':
        return _error(404, 'not_found', 'No workflow listens at this address.')
    if not workflow.enabled:
        return _error(409, 'paused', 'This workflow is paused.')
    if len(request.body) > 1_000_000:
        return _error(413, 'too_large', 'Send less than 1 MB.')
    try:
        body = json.loads(request.body) if request.body else {}
    except ValueError:
        body = dict(request.POST.items()) if request.POST else request.body.decode('utf-8', 'replace')
    data = {
        'body': body, 'query': dict(request.GET.items()),
        'received_at': timezone.localtime(timezone.now(), Settings.load().zone()).isoformat(timespec='seconds'),
    }
    run = engine.start_run(workflow, data, 'Webhook · call received')
    worker.submit(run)
    return JsonResponse({'ok': True, 'run': run.pk}, status=202)
