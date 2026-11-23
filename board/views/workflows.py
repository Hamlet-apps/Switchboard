import copy
import json
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import nodes, recipes
from ..models import Connection, Folder, Run, Workflow

STRIP = 16


def state_of(workflow, last_run):
    """running, paused, or attention (running but its last real run failed)."""
    if not workflow.enabled:
        return 'paused'
    if last_run is not None and last_run.status == Run.FAILED:
        return 'attention'
    return 'running'


STATE_LABELS = {'running': 'Running', 'paused': 'Paused', 'attention': 'Needs attention'}


def _cards(workflows, connections):
    """What each workflow card shows. One query for the recent runs of all of them."""
    recent = {}
    for run in Run.objects.filter(workflow__in=workflows, is_test=False).order_by('-created_at', '-id')[:STRIP * max(len(workflows), 1) * 4]:
        bucket = recent.setdefault(run.workflow_id, [])
        if len(bucket) < STRIP:
            bucket.append(run)
    totals = {row['workflow']: row['n'] for row in
              Run.objects.filter(workflow__in=workflows, is_test=False).values('workflow').annotate(n=Count('id'))}
    cards = []
    for workflow in workflows:
        runs = recent.get(workflow.id, [])
        tiles, seen = [], set()
        for step in nodes.walk(workflow.steps):
            tile = nodes.describe(step, connections)
            key = tile['tile'].get('img') or tile['tile'].get('code') or tile['tile'].get('icon')
            if key not in seen and len(tiles) < 4:
                seen.add(key)
                tiles.append({**tile['tile'], 'label': tile['tile'].get('label') or tile['title']})
        strip = [{'succeeded': 'ok', 'failed': 'bad'}.get(r.status, 'idle') for r in reversed(runs)]
        strip = ['none'] * (STRIP - len(strip)) + strip
        state = state_of(workflow, runs[0] if runs else None)
        cards.append({
            'workflow': workflow, 'tiles': tiles, 'state': state, 'state_label': STATE_LABELS[state],
            'strip': strip, 'steps': len(workflow.steps), 'total': totals.get(workflow.id, 0),
            'last': runs[0].created_at if runs else None,
        })
    return cards


@login_required
def workflows_page(request):
    folders = list(Folder.objects.all())
    folder = next((f for f in folders if str(f.id) == request.GET.get('folder')), None)
    connections = list(Connection.objects.all())
    everything = list(Workflow.objects.select_related('folder'))
    cards = _cards(everything, {c.id: c for c in connections})
    scoped = [c for c in cards if folder is None or c['workflow'].folder_id == folder.id]
    status = request.GET.get('status') if request.GET.get('status') in STATE_LABELS else 'all'
    query = request.GET.get('q', '').strip()
    shown = [c for c in scoped if (status == 'all' or c['state'] == status)
             and (not query or query.lower() in f"{c['workflow'].name} {c['workflow'].description}".lower())]
    counts = {f.id: 0 for f in folders}
    for card in cards:
        if card['workflow'].folder_id in counts:
            counts[card['workflow'].folder_id] += 1
    chips = [{'key': key, 'label': label, 'count': sum(1 for c in scoped if key == 'all' or c['state'] == key),
              'on': key == status} for key, label in [('all', 'All'), *STATE_LABELS.items()]]
    return render(request, 'board/workflows.html', {
        'nav': 'workflows', 'folder': folder, 'folders': [{'folder': f, 'count': counts[f.id]} for f in folders],
        'cards': shown, 'chips': chips, 'status': status, 'query': query,
        'total': len(everything), 'running': sum(1 for c in cards if c['workflow'].enabled),
        'in_folder': len(scoped), 'recipes': recipes.available(connections),
        'has_buildings': any(c.status == c.CONNECTED for c in connections),
    })


@login_required
@require_POST
def workflow_new(request):
    folder = Folder.objects.filter(pk=request.POST.get('folder') or None).first()
    workflow = Workflow(folder=folder, paused_at=timezone.now())
    recipe = request.POST.get('recipe')
    if recipe:
        built = recipes.build(recipe, list(Connection.objects.all()))
        if built is None:
            messages.error(request, 'Connect the buildings this recipe uses first.')
            return redirect('buildings')
        workflow.name, workflow.description, workflow.steps = built
    workflow.save()
    if recipe:
        messages.success(request, 'Recipe added. Check the steps, then switch it on.')
    return redirect('builder', workflow.pk)


@login_required
@require_POST
def workflow_delete(request, pk):
    workflow = get_object_or_404(Workflow, pk=pk)
    name = workflow.name
    workflow.delete()
    messages.success(request, f'Deleted "{name}" and its runs.')
    return redirect('workflows')


@login_required
@require_POST
def workflow_duplicate(request, pk):
    source = get_object_or_404(Workflow, pk=pk)
    steps = copy.deepcopy(nodes.upgrade(source.steps))
    renamed = {step['uid']: 's' + secrets.token_hex(4) for step in steps}
    for step in steps:
        step['uid'] = renamed[step['uid']]
        step['next'] = {port: renamed[target] for port, target in (step.get('next') or {}).items() if target in renamed}
    text = json.dumps(steps)
    for old, new in renamed.items():
        text = text.replace('{{' + old + '.', '{{' + new + '.')
    copy_ = Workflow.objects.create(
        name=f'{source.name} (copy)'[:120], description=source.description, folder=source.folder,
        steps=json.loads(text), paused_at=timezone.now(),
    )
    messages.success(request, 'Copied. The copy is paused.')
    return redirect('builder', copy_.pk)


@login_required
@require_POST
def workflow_move(request, pk):
    workflow = get_object_or_404(Workflow, pk=pk)
    workflow.folder = Folder.objects.filter(pk=request.POST.get('folder') or None).first()
    workflow.save(update_fields=['folder', 'updated_at'])
    messages.success(request, f'Moved to {workflow.folder.name}.' if workflow.folder else 'Taken out of its folder.')
    back = request.POST.get('back', '')
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = 'workflows'
    return redirect(back)


@login_required
@require_POST
def folder_new(request):
    name = request.POST.get('name', '').strip()[:80]
    if not name:
        messages.error(request, 'Give the folder a name.')
        return redirect('workflows')
    tint = Folder.TINTS[Folder.objects.count() % len(Folder.TINTS)]
    Folder.objects.create(name=name, tint=tint)
    messages.success(request, 'Folder added.')
    return redirect('workflows')


@login_required
@require_POST
def folder_rename(request, pk):
    folder = get_object_or_404(Folder, pk=pk)
    name = request.POST.get('name', '').strip()[:80]
    if name:
        folder.name = name
        folder.save(update_fields=['name'])
    return redirect(f"{reverse('workflows')}?folder={folder.pk}")


@login_required
@require_POST
def folder_delete(request, pk):
    folder = get_object_or_404(Folder, pk=pk)
    folder.delete()
    messages.success(request, 'Folder removed. Its workflows are still under All workflows.')
    return redirect('workflows')
