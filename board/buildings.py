"""Talking to Hamlet buildings (docs/PROTOCOL.md) and to outside URLs.

Every outgoing request goes through `_http`, which tests replace.
"""
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.utils import timezone

from . import crypto

USER_AGENT = 'Switchboard/1.0'

# Buildings whose logo ships with Switchboard, for when the manifest doesn't bring one.
BUNDLED = ('town-hall', 'pavilion')
ICON_TYPES = ('image/svg+xml', 'image/png', 'image/webp', 'image/jpeg')
ICON_LIMIT = 200_000


class BuildingError(Exception):
    """A call that didn't work. `retry` says whether trying again could help."""

    def __init__(self, message, status=None, retry=False, code=''):
        super().__init__(message)
        self.status = status
        self.retry = retry
        self.code = code


def look(building, name='', connection=None):
    """How a building's tile is drawn: its own logo when we have one, else its initials."""
    from django.templatetags.static import static
    from django.urls import reverse
    if connection is not None and connection.icon_data:
        stamp = int(connection.manifest_at.timestamp()) if connection.manifest_at else 0
        return {'img': f"{reverse('building_icon', args=[connection.pk])}?v={stamp}", 'label': name}
    if building in BUNDLED:
        return {'img': static(f'board/buildings/{building}.svg'), 'label': name}
    words = [w for w in (name or building or '?').replace('-', ' ').split() if w]
    code = (words[0][:1] + (words[1][:1] if len(words) > 1 else words[0][1:2])).upper()
    return {'code': code or '?', 'bg': '#4a4458', 'fg': '#ffffff'}


def _http(method, url, headers=None, body=None, timeout=None):
    """(status, body bytes). Network trouble raises BuildingError(retry=True)."""
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header('User-Agent', USER_AGENT)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout or settings.HTTP_TIMEOUT) as response:
            return response.status, response.read(2_000_000)
    except urllib.error.HTTPError as error:
        return error.code, error.read(2_000_000)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        reason = getattr(error, 'reason', error)
        raise BuildingError(f"Couldn't reach {urllib.parse.urlsplit(url).netloc or url}: {reason}", retry=True)


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8')) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def normalize_url(raw):
    """The server's origin from whatever was typed: scheme + host (+ port).

    "volunteers.example.org/en/console/" -> "https://volunteers.example.org". A missing
    scheme means https, except for localhost-style addresses.
    """
    raw = (raw or '').strip()
    if not raw:
        raise ValueError("Enter the building's address.")
    if '://' not in raw:
        host = (urllib.parse.urlsplit('//' + raw).hostname or '').lower()
        local = host in ('localhost', '127.0.0.1', '0.0.0.0', '::1') or host.endswith('.local') or '.' not in host
        raw = ('http://' if local else 'https://') + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError("That doesn't look like a web address.")
    try:
        port = parts.port
    except ValueError:
        raise ValueError('That address has an invalid port.')
    host = parts.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    return f'{parts.scheme}://{host}' + (f':{port}' if port else '')


def fetch_manifest(base_url):
    """Read and check a building's manifest."""
    status, raw = _http('GET', base_url + '/.well-known/switchboard', {'Accept': 'application/json'})
    manifest = _json(raw)
    if status == 404 or not isinstance(manifest, dict):
        raise BuildingError(
            f"{base_url.split('://', 1)[-1]} didn't answer like a Hamlet building. "
            'Check the address, and that the building is up to date.', status=status)
    if status != 200:
        raise BuildingError(f'The building answered {status} when asked what it can do.', status=status, retry=status >= 500)
    for key in ('building', 'name', 'authorize_url', 'token_url', 'api_base'):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise BuildingError(f"The building's manifest is missing \"{key}\".")
    if str(manifest.get('protocol')) != '1':
        raise BuildingError("This building speaks a newer protocol than this Switchboard. Update Switchboard.")
    manifest['triggers'] = [t for t in manifest.get('triggers') or [] if isinstance(t, dict) and t.get('type')]
    manifest['actions'] = [a for a in manifest.get('actions') or [] if isinstance(a, dict) and a.get('name')]
    from . import schema
    errors, _notes = schema.manifest_problems(manifest)
    if errors:
        more = f' (and {len(errors) - 1} more)' if len(errors) > 1 else ''
        raise BuildingError(f"{manifest['name']} describes itself in a way Switchboard can't use: {errors[0]}{more}. "
                            'The building needs updating.')
    return manifest


def apply_manifest(connection, manifest):
    from . import schema
    connection.manifest = manifest
    connection.contract_notes = schema.manifest_problems(manifest)[1][:schema.MAX_NOTES]
    connection.manifest_at = timezone.now()
    connection.building = manifest['building'][:60]
    connection.name = manifest['name'][:80]
    connection.instance = str(manifest.get('instance') or '')[:160]
    connection.api_base = manifest['api_base'].rstrip('/')


def _image_type(raw):
    head = raw[:300].lstrip()
    if head.startswith(b'\x89PNG'):
        return 'image/png'
    if head.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.startswith((b'<svg', b'<?xml')) and b'<svg' in raw[:2000]:
        return 'image/svg+xml'
    return ''


def fetch_icon(connection):
    """Keep a copy of the building's logo (the manifest's `icon`), so its steps carry it.

    Best effort: a building without one, or one that can't be fetched, keeps what it had
    (or falls back to the bundled logo or its initials).
    """
    import base64
    url = connection.manifest.get('icon')
    if not isinstance(url, str) or not url:
        return False
    url = urllib.parse.urljoin(connection.base_url + '/', url)
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        return False
    try:
        status, raw = _http('GET', url, {'Accept': 'image/svg+xml,image/*'})
    except BuildingError:
        return False
    kind = _image_type(raw) if status == 200 and raw else ''
    if not kind or len(raw) > ICON_LIMIT:
        return False
    connection.icon_type, connection.icon_data = kind, base64.b64encode(raw).decode()
    return True


def authorize_url(connection, callback, org_name):
    query = urllib.parse.urlencode({'callback': callback, 'state': connection.state, 'name': org_name})
    target = connection.manifest['authorize_url']
    return target + ('&' if '?' in target else '?') + query


def exchange(connection, code, webhook_url, org_name):
    """Trade the one-time code for the key and webhook secret."""
    body = json.dumps({'code': code, 'webhook_url': webhook_url, 'name': org_name}).encode()
    status, raw = _http('POST', connection.manifest['token_url'], {'Content-Type': 'application/json'}, body)
    data = _json(raw)
    if status != 200 or not isinstance(data, dict) or not data.get('api_key') or not data.get('webhook_secret'):
        raise BuildingError(_error_text(data) or f'The building answered {status} instead of a key.', status=status)
    connection.api_key_enc = crypto.encrypt(data['api_key'])
    connection.webhook_secret_enc = crypto.encrypt(data['webhook_secret'])
    connection.remote_id = str(data.get('connection_id') or '')[:80]
    if data.get('instance'):
        connection.instance = str(data['instance'])[:160]
    connection.status = connection.CONNECTED
    connection.state = ''
    connection.last_error = ''
    connection.connected_at = timezone.now()


def _error_text(data):
    if isinstance(data, dict) and isinstance(data.get('error'), dict):
        return str(data['error'].get('message') or data['error'].get('code') or '')
    return ''


def _auth(connection):
    return {'Authorization': f'Bearer {connection.api_key}', 'Accept': 'application/json'}


def call_action(connection, action, params, idempotency_key=''):
    """Run one of the manifest's actions. Returns the building's JSON object."""
    if connection.status != connection.CONNECTED:
        raise BuildingError(f'{connection.name} is not connected. Reconnect it on the Buildings page.')
    headers = {**_auth(connection), 'Content-Type': 'application/json'}
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    path = action.get('path') or f"/actions/{action['name']}"
    status, raw = _http(action.get('method') or 'POST', connection.api_base + path, headers, json.dumps(params).encode())
    data = _json(raw)
    if status == 401:
        connection.status = connection.BROKEN
        connection.last_error = f'{connection.name} no longer accepts our key.'
        connection.save(update_fields=['status', 'last_error'])
        raise BuildingError(f'{connection.name} no longer accepts our key. Reconnect it on the Buildings page.', status=401)
    if 200 <= status < 300 and isinstance(data, dict):
        return data
    message = _error_text(data) or (f"{connection.name} didn't answer properly." if status < 300 else f"{connection.name} answered {status}.")
    code = data['error'].get('code', '') if isinstance(data, dict) and isinstance(data.get('error'), dict) else ''
    raise BuildingError(f'{status} · {message}', status=status, retry=status >= 500, code=code)


OPTIONS_NAME = re.compile(r'^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$')
OPTIONS_LIMIT = 500


def fetch_options(connection, name):
    """A list a param's `options` names, read live from the building (PROTOCOL.md: "Options from
    the building"): [{"value", "label"}], at most OPTIONS_LIMIT, values kept as the building gave them."""
    if connection.status != connection.CONNECTED:
        raise BuildingError(f'{connection.name} is not connected. Reconnect it on the Buildings page.')
    if not isinstance(name, str) or not OPTIONS_NAME.match(name):
        raise BuildingError(f"{connection.name}'s manifest names a list Switchboard can't ask for.")
    status, raw = _http('GET', f'{connection.api_base}/options/{name}', _auth(connection))
    data = _json(raw)
    if status == 401:
        connection.status = connection.BROKEN
        connection.last_error = f'{connection.name} no longer accepts our key.'
        connection.save(update_fields=['status', 'last_error'])
        raise BuildingError(f'{connection.name} no longer accepts our key. Reconnect it on the Buildings page.', status=401)
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get('options'), list):
        message = _error_text(data) or f"{connection.name} didn't send the list ({status})."
        raise BuildingError(message, status=status)
    options, seen = [], set()
    for item in data['options']:
        if not isinstance(item, dict) or isinstance(item.get('value'), (dict, list, bool)) or item.get('value') in (None, ''):
            continue
        value = item['value']
        if str(value) in seen:
            continue
        seen.add(str(value))
        options.append({'value': value, 'label': str(item.get('label') or value)[:200]})
        if len(options) >= OPTIONS_LIMIT:
            break
    return options


def ping(connection):
    status, raw = _http('GET', connection.api_base + '/', _auth(connection))
    return status == 200 and isinstance(_json(raw), dict)


def disconnect(connection):
    """Tell the building to forget us. Best effort: we forget it either way."""
    if not connection.api_key:
        return
    try:
        _http('DELETE', connection.api_base + '/connection', _auth(connection))
    except BuildingError:
        pass


def verify_signature(connection, raw_body, header):
    secret = connection.webhook_secret
    if not secret or not header:
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def http_request(method, url, headers, body):
    """The HTTP request step. Returns {"status", "ok", "body"}; 5xx and network errors can be retried."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise BuildingError(f'"{url}" is not a web address. It should start with https://')
    data = None
    headers = dict(headers)
    if body not in (None, '') and method != 'GET':
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        else:
            data = str(body).encode()
            if _json(data) is not None:
                headers.setdefault('Content-Type', 'application/json')
            else:
                headers.setdefault('Content-Type', 'text/plain; charset=utf-8')
    status, raw = _http(method, url, headers, data)
    parsed = _json(raw)
    if parsed is None:
        parsed = raw[:20_000].decode('utf-8', 'replace')
    if status >= 400:
        detail = parsed if isinstance(parsed, str) else json.dumps(parsed)
        raise BuildingError(f'{status} · {parts.hostname} said: {detail[:200]}' if detail else f'{status} from {parts.hostname}',
                            status=status, retry=status >= 500)
    return {'status': status, 'ok': True, 'body': parsed}
