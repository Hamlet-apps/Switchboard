import zoneinfo

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.text import slugify

from .. import worker
from ..models import Secret, Settings, Workflow


@login_required
def settings_page(request):
    """Organization, secrets and people. Each form posts an `op`."""
    User = get_user_model()
    org = Settings.load()
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'org':
            zone = request.POST.get('time_zone', '').strip() or 'UTC'
            try:
                zoneinfo.ZoneInfo(zone)
            except Exception:
                messages.error(request, "That time zone isn't one Switchboard knows. Try something like America/Vancouver.")
                return redirect('settings')
            org.org_name = request.POST.get('org_name', '').strip()[:120]
            org.time_zone = zone
            try:
                org.keep_days = min(max(int(request.POST.get('keep_days', 30)), 1), 3650)
            except ValueError:
                pass
            org.save()
            for workflow in Workflow.objects.filter(enabled=True, next_fire_at__isnull=False):
                worker.plan(workflow)
                workflow.save(update_fields=['next_fire_at'])
            messages.success(request, 'Saved.')
        elif op == 'secret_add':
            name = slugify(request.POST.get('name', '')).replace('-', '_').upper()[:60]
            value = request.POST.get('value', '')
            if not name or not value:
                messages.error(request, 'A secret needs a name and a value.')
            else:
                secret = Secret.objects.filter(name=name).first() or Secret(name=name)
                secret.set_value(value)
                secret.save()
                messages.success(request, f'Saved. Use it in an HTTP step as {{{{secret.{name}}}}}.')
        elif op == 'secret_delete':
            Secret.objects.filter(pk=request.POST.get('id') or None).delete()
            messages.success(request, 'Secret removed. Steps that used it will send an empty value.')
        elif op == 'user_add':
            username = request.POST.get('username', '').strip()
            password = request.POST.get('password', '')
            if not username or len(password) < 8:
                messages.error(request, 'Give them a username and a password of at least 8 characters.')
            elif User.objects.filter(username__iexact=username).exists():
                messages.error(request, 'Someone already has that username.')
            else:
                User.objects.create_superuser(username=username, password=password)
                messages.success(request, f'{username} can sign in now. Tell them their password and ask them to change it.')
        elif op == 'user_remove':
            target = User.objects.filter(pk=request.POST.get('id') or None).first()
            if target and target.pk != request.user.pk:
                target.delete()
                messages.success(request, 'Removed.')
        elif op == 'password':
            password = request.POST.get('password', '')
            if not request.user.check_password(request.POST.get('current', '')):
                messages.error(request, "Your current password isn't right.")
            elif len(password) < 8:
                messages.error(request, 'Choose a password of at least 8 characters.')
            else:
                request.user.set_password(password)
                request.user.save()
                update_session_auth_hash(request, request.user)
                messages.success(request, 'Password changed.')
        return redirect('settings')
    return render(request, 'board/settings.html', {
        'nav': 'settings', 'org': org, 'secrets': Secret.objects.all(), 'people': User.objects.order_by('username'),
        'zones': sorted(zoneinfo.available_timezones()),
    })
