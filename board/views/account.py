import zoneinfo

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ..models import Settings


def _next(request):
    target = request.POST.get('next') or request.GET.get('next') or ''
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return 'workflows'


def remember_address(request):
    """Keep the address people reach Switchboard at, for links in failure alerts."""
    origin = f'{request.scheme}://{request.get_host()}'
    Settings.objects.filter(pk=1).exclude(public_url=origin).update(public_url=origin)


def setup(request):
    """First-time setup: the organization's name and the first account."""
    User = get_user_model()
    if User.objects.exists():
        return redirect('login')
    values = {'org_name': '', 'username': '', 'time_zone': 'UTC'}
    error = ''
    if request.method == 'POST':
        values = {k: request.POST.get(k, '').strip() for k in values}
        password = request.POST.get('password', '')
        try:
            zoneinfo.ZoneInfo(values['time_zone'] or 'UTC')
        except Exception:
            error = "That time zone isn't one Switchboard knows. Try something like America/Vancouver."
        if not error and (not values['org_name'] or not values['username']):
            error = "Fill in your organization's name and a username."
        elif not error and len(password) < 8:
            error = 'Choose a password of at least 8 characters.'
        if not error:
            user = User.objects.create_superuser(username=values['username'], password=password)
            org = Settings.load()
            org.org_name, org.time_zone = values['org_name'], values['time_zone'] or 'UTC'
            org.save()
            login(request, user)
            remember_address(request)
            messages.success(request, 'Switchboard is ready. Connect your first building to begin.')
            return redirect('buildings')
    return render(request, 'board/setup.html', {'values': values, 'error': error, 'host': request.get_host()})


def login_view(request):
    if request.user.is_authenticated:
        return redirect(_next(request))
    error, username = '', ''
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        user = authenticate(request, username=username, password=request.POST.get('password', ''))
        if user is not None:
            login(request, user)
            remember_address(request)
            return redirect(_next(request))
        error = "That username and password don't match."
    return render(request, 'board/login.html', {
        'error': error, 'username': username, 'next': request.GET.get('next', ''),
        'host': request.get_host(), 'org': Settings.load(),
    })


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')
