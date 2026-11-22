import datetime
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import engine, nodes, worker
from ..models import Connection, Run, RunStep, Settings, Workflow

STATUSES = [('all', 'All'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('stopped', 'Stopped')]
RANGES = [(1, 'Today'), (7, 'Last 7 days'), (30, 'Last 30 days'), (0, 'Everything kept')]
LOOK = {
    'succeeded': ('check_circle', 'Succeeded'), 'failed': ('error', 'Failed'),
    'stopped': ('do_not_disturb_on', 'Stopped'), 'queued': ('schedule', 'Waiting'),
    'running': ('progress_activity', 'Running'),
}


def took(ms):
    if ms < 1000:
        return f'{ms} ms'
    return f'{ms / 1000:.1f} s'


def _pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False) if value not in (None, {}, '') else ''


def _detail(run):
    """The selected run's steps: what ran, what stopped it, what never got a turn."""
    connections = {c.id: c for c in Connection.objects.all()}
    results = {r.index: r for r in run.step_results.all()}
    steps = []
    for index, step in enumerate(run.steps):
        result = results.get(index)
        definition = nodes.describe(step, connections)
        if result is None:
            waiting = run.status in (Run.QUEUED, Run.RUNNING)
            steps.append({'title': definition['title'], 'status': 'waiting' if waiting else 'skipped',
                          'note': 'Waiting' if waiting else 'Skipped', 'tile': definition['tile']})
            continue
        note = result.note
        if result.status == RunStep.OK and index > 0:
            note = f'{definition["kind"]} · {took(result.duration_ms)}'
            if result.tries > 1:
                note += f' · took {result.tries} tries'
        steps.append({
            'title': result.title, 'status': result.status, 'note': note, 'error': result.error,
            'tile': definition['tile'], 'sent': _pretty(result.sent), 'output': _pretty(result.output),
        })
    return {'run': run, 'steps': steps, 'icon': LOOK[run.status][0], 'label': LOOK[run.status][1],
            'took': took(run.duration_ms), 'retry_step': (run.failed_step or 0) + 1}


@login_required
def runs_page(request):
    zone = Settings.load().zone()
    now = timezone.localtime(timezone.now(), zone)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    runs = Run.objects.select_related('workflow')
    tests = request.GET.get('tests') == '1'
    if not tests:
        runs = runs.filter(is_test=False)
    workflow = Workflow.objects.filter(pk=request.GET.get('workflow') or None).first()
    if workflow:
        runs = runs.filter(workflow=workflow)
    status = request.GET.get('status') if request.GET.get('status') in dict(STATUSES) else 'all'
    if status != 'all':
        runs = runs.filter(status=status)
    try:
        days = int(request.GET.get('days', 7))
    except ValueError:
        days = 7
    days = days if days in dict(RANGES) else 7
    if days:
        runs = runs.filter(created_at__gte=today - datetime.timedelta(days=days - 1))
    rows = list(runs[:150])
    selected = next((r for r in rows if str(r.pk) == request.GET.get('run')), None)
    if selected is None and request.GET.get('run'):
        selected = Run.objects.select_related('workflow').filter(pk=request.GET.get('run')).first()
    if selected is None and rows:
        selected = rows[0]
    for run in rows:
        run.icon, run.label = LOOK[run.status]
        run.took = took(run.duration_ms) if run.status in Run.DONE else '…'
        run.when = timezone.localtime(run.created_at, zone)
    real = Run.objects.filter(is_test=False, created_at__gte=today)
    query = {k: v for k, v in (('workflow', workflow.pk if workflow else ''), ('status', '' if status == 'all' else status),
                               ('days', days if days != 7 else ''), ('tests', '1' if tests else '')) if v != ''}

    def link(**change):
        merged = {**query, **change}
        return '?' + '&'.join(f'{k}={v}' for k, v in merged.items() if v not in ('', None, 'all'))

    return render(request, 'board/runs.html', {
        'nav': 'runs', 'rows': rows, 'selected': _detail(selected) if selected else None,
        'workflows': Workflow.objects.all(), 'workflow': workflow, 'status': status, 'days': days, 'tests': tests,
        'chips': [{'label': label, 'on': key == status, 'url': link(status=key)} for key, label in STATUSES],
        'ranges': [{'label': label, 'value': value, 'on': value == days} for value, label in RANGES],
        'today': now, 'today_runs': real.count(), 'today_failed': real.filter(status=Run.FAILED).count(),
        'live': any(r.status in (Run.QUEUED, Run.RUNNING) for r in rows),
        'row_link': link(), 'tests_link': link(tests='' if tests else '1'), 'zone': zone,
    })


@login_required
@require_POST
def run_retry(request, pk):
    run = get_object_or_404(Run, pk=pk)
    if engine.retry(run):
        worker.submit(run)
        messages.success(request, 'Retrying from the step that failed.')
    else:
        messages.error(request, "Only failed runs can be retried.")
    back = request.POST.get('back', '')
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = f"{reverse('runs')}?run={run.pk}"
    return redirect(back)
