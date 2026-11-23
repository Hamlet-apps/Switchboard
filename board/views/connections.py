"""The Buildings page: connecting, refreshing and disconnecting buildings."""
import datetime
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import buildings, nodes
from ..buildings import BuildingError
from ..models import Connection, Event, Settings, Workflow
from .account import remember_address


def _uses(connection, workflows):
    return [w for w in workflows if any(step.get('connection') == connection.id for step in nodes.walk(w.steps))]


@login_required
def buildings_page(request):
    stale = timezone.now() - datetime.timedelta(hours=1)
    Connection.objects.filter(status=Connection.PENDING, created_at__lt=stale).delete()
    workflows = list(Workflow.objects.all())
    week = timezone.now() - datetime.timedelta(days=7)
    cards = []
    for connection in Connection.objects.exclude(status=Connection.PENDING):
        cards.append({
            'connection': connection, 'tile': buildings.look(connection.building, connection.name, connection),
            'triggers': connection.triggers(), 'actions': connection.actions(),
            'workflows': _uses(connection, workflows),
            'events_week': Event.objects.filter(connection=connection, received_at__gte=week).count(),
        })
    return render(request, 'board/buildings.html', {
        'nav': 'buildings', 'cards': cards, 'address': request.GET.get('address', ''),
    })


@login_required
@require_POST
def building_connect(request):
    """Step 1 and 2 of the handshake: read the manifest, send the browser to the building."""
    typed = request.POST.get('address', '')
    try:
        base = buildings.normalize_url(typed)
        manifest = buildings.fetch_manifest(base)
    except (ValueError, BuildingError) as error:
        messages.error(request, str(error))
        return redirect(f"{reverse('buildings')}?address={typed}")
    connection = Connection.objects.filter(base_url=base).first() or Connection(base_url=base, created_by=request.user)
    buildings.apply_manifest(connection, manifest)
    buildings.fetch_icon(connection)
    connection.state = secrets.token_urlsafe(24)
    connection.save()
    remember_address(request)
    callback = request.build_absolute_uri(reverse('building_callback'))
    return redirect(buildings.authorize_url(connection, callback, Settings.load().org_name or 'Switchboard'))


@login_required
def building_callback(request):
    """Step 3 and 4: the building sent the browser back; trade the code for a key."""
    state = request.GET.get('state', '')
    connection = Connection.objects.filter(state=state).first() if state else None
    if connection is None:
        messages.error(request, "That connection attempt expired. Start again from here.")
        return redirect('buildings')
    if request.GET.get('error') or not request.GET.get('code'):
        if connection.status == Connection.PENDING:
            connection.delete()
        else:
            Connection.objects.filter(pk=connection.pk).update(state='')
        messages.error(request, f'{connection.name} was not connected: the request was cancelled there.')
        return redirect('buildings')
    webhook = request.build_absolute_uri(reverse('building_hook', args=[connection.hook_token]))
    try:
        buildings.exchange(connection, request.GET['code'], webhook, Settings.load().org_name or 'Switchboard')
    except BuildingError as error:
        if connection.status == Connection.PENDING:
            connection.delete()
        messages.error(request, f"{connection.name} didn't finish connecting: {error}")
        return redirect('buildings')
    connection.save()
    messages.success(request, f'{connection.name} is connected. Its events and actions are in the builder now.')
    return redirect('buildings')


@login_required
@require_POST
def building_refresh(request, pk):
    """Read the manifest again, after the building was updated."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        buildings.apply_manifest(connection, buildings.fetch_manifest(connection.base_url))
        buildings.fetch_icon(connection)
        alive = buildings.ping(connection)
    except BuildingError as error:
        messages.error(request, str(error))
        return redirect('buildings')
    connection.status = Connection.CONNECTED if alive else Connection.BROKEN
    connection.last_error = '' if alive else f'{connection.name} no longer accepts our key.'
    connection.save()
    if alive:
        messages.success(request, f'{connection.name} checked: {len(connection.triggers())} events, {len(connection.actions())} actions.')
    else:
        messages.error(request, f'{connection.name} no longer accepts our key. Connect it again.')
    return redirect('buildings')


@login_required
@require_POST
def building_disconnect(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    using = _uses(connection, Workflow.objects.filter(enabled=True))
    buildings.disconnect(connection)
    name = connection.name
    for workflow in using:
        workflow.enabled, workflow.paused_at, workflow.next_fire_at = False, timezone.now(), None
        workflow.save()
    connection.delete()
    note = f" Paused {len(using)} workflow{'' if len(using) == 1 else 's'} that used it." if using else ''
    messages.success(request, f'{name} is disconnected.{note}')
    return redirect('buildings')


@login_required
def building_icon(request, pk):
    """A connected building's logo, from the copy fetched when it was connected or checked.

    It came from another server, so it is only ever shown as an image, and is served with a
    policy that lets it do nothing else even if opened by itself.
    """
    import base64
    connection = get_object_or_404(Connection, pk=pk)
    if not connection.icon_data or connection.icon_type not in buildings.ICON_TYPES:
        raise Http404
    response = HttpResponse(base64.b64decode(connection.icon_data), content_type=connection.icon_type)
    response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, max-age=86400'
    return response
