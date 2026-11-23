"""The Data and Date and time steps: small, pure changes to values.

`run(node, values, step, zone)` does one; it raises ValueError with a message for the admin
("\"abc\" is not a number") when the input can't be used. Nothing here calls out, so the
builder can work these steps out on sample data at no cost.
"""
import calendar
import datetime
import re
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone

from .templating import number, resolve, text, unflatten

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
          'October', 'November', 'December']
WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
UNITS = [('seconds', 'Seconds'), ('minutes', 'Minutes'), ('hours', 'Hours'), ('days', 'Days'),
         ('weeks', 'Weeks'), ('months', 'Months'), ('years', 'Years')]
CLOCK = {'seconds': 1, 'minutes': 60, 'hours': 3600}

YES = {'true', 'yes', 'y', '1', 'on', 'checked'}
NO = {'false', 'no', 'n', '0', 'off', 'unchecked', 'none'}


def _plain(n):
    """A Decimal as JSON likes it: 3 rather than 3.0, 2.5 as 2.5."""
    if n == n.to_integral_value():
        return int(n)
    return float(n)


def _number(value, what='Number'):
    n = number(value)
    if n is None or not n.is_finite():
        raise ValueError(f'"{text(value)}" is not a number')
    return n


# --- Data ---------------------------------------------------------------------------------

def to_number(value):
    return _plain(_number(value))


def to_integer(value, rounding='round'):
    n = _number(value)
    if rounding == 'exact':
        if n != n.to_integral_value():
            raise ValueError(f'"{text(value)}" is not a whole number')
        return int(n)
    mode = {'down': ROUND_FLOOR, 'up': ROUND_CEILING}.get(rounding, ROUND_HALF_UP)
    return int(n.to_integral_value(rounding=mode))


def to_text(value, decimals=None, group=False):
    n = number(value) if not isinstance(value, str) or decimals is not None or group else None
    if n is None or isinstance(value, bool):
        return text(value)
    if decimals is not None:
        n = n.quantize(Decimal(1).scaleb(-int(decimals)), rounding=ROUND_HALF_UP)
        return f'{n:,f}' if group else f'{n:f}'
    shown = f'{n.normalize():f}' if n != n.to_integral_value() else str(int(n))
    if group:
        whole, _, rest = shown.partition('.')
        shown = f'{int(whole):,}' + (f'.{rest}' if rest else '')
    return shown


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    word = text(value).strip().lower()
    if word in YES:
        return True
    if word in NO:
        return False
    raise ValueError(f'"{text(value)}" isn\'t yes or no. Switchboard understands yes/no, true/false, on/off and 1/0')


def calculate(value, op, by=None, places=None):
    n = _number(value)
    if op == 'round':
        digits = int(places or 0)
        return _plain(n.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))
    if by is None:
        raise ValueError('There is no number to calculate with')
    other = _number(by)
    try:
        if op == 'add':
            result = n + other
        elif op == 'subtract':
            result = n - other
        elif op == 'multiply':
            result = n * other
        elif op in ('divide', 'remainder'):
            if other == 0:
                raise ValueError("Can't divide by zero")
            result = n / other if op == 'divide' else n % other
        else:
            raise ValueError('Pick what to do with the number')
    except InvalidOperation:
        raise ValueError('That calculation has no answer')
    # Division can run on forever (10 / 3); twelve places is plenty for anyone.
    if result != result.to_integral_value() and -result.as_tuple().exponent > 12:
        result = result.quantize(Decimal('1e-12'), rounding=ROUND_HALF_UP).normalize()
    return _plain(result)


def change_text(value, op, find=None, replace=None, length=None):
    words = text(value)
    if op == 'upper':
        return words.upper()
    if op == 'lower':
        return words.lower()
    if op == 'title':
        return re.sub(r"[^\W_]+('[^\W_]+)?", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), words)
    if op == 'trim':
        return ' '.join(words.split())
    if op == 'replace':
        if not find:
            raise ValueError('Say what text to replace')
        return words.replace(find, replace or '')
    if op == 'first':
        return words[:max(0, int(length or 0))]
    raise ValueError('Pick how to change the text')


def set_fields(fields, ctx, secrets):
    """Set fields: [{name, value}] -> one object; dotted names make nested fields."""
    return unflatten((row['name'], resolve(row.get('value', ''), ctx, secrets))
                     for row in fields if str(row.get('name') or '').strip())


# --- Date and time ------------------------------------------------------------------------

READ_FORMATS = [
    '%b %d, %Y', '%B %d, %Y', '%d %b %Y', '%d %B %Y', '%A, %B %d, %Y', '%a, %b %d, %Y',
    '%b %d, %Y, %I:%M %p', '%b %d, %Y %I:%M %p', '%B %d, %Y, %I:%M %p', '%B %d, %Y %I:%M %p',
    '%Y-%m-%d %I:%M %p', '%Y/%m/%d', '%Y/%m/%d %H:%M',
]
SLASHED = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ ,T]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?)?$')


class Moment:
    """A point in time in the organization's zone, remembering whether it was only a date."""

    def __init__(self, when, date_only=False):
        self.when, self.date_only = when, date_only

    def iso(self):
        return self.when.date().isoformat() if self.date_only else self.when.isoformat(timespec='seconds')


def read_date(value, zone):
    """A Moment from what buildings and people write; an empty value is now."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return Moment(timezone.localtime(timezone.now(), zone).replace(microsecond=0))
    if isinstance(value, (int, float)) and not isinstance(value, bool) or (
            isinstance(value, str) and re.fullmatch(r'\d{9,13}(\.\d+)?', value.strip())):
        seconds = float(value)
        if seconds > 1e11:          # milliseconds, as JavaScript gives them
            seconds /= 1000
        return Moment(datetime.datetime.fromtimestamp(seconds, zone).replace(microsecond=0))
    raw = text(value).strip()
    when, date_only = None, False
    try:
        when = datetime.datetime.fromisoformat(raw.replace('z', 'Z'))
        date_only = len(raw) == 10
    except ValueError:
        pass
    if when is None:
        slashed = SLASHED.match(raw)
        if slashed:
            a, b, year, hour, minute, second, half = slashed.groups()
            month, day = (int(a), int(b)) if int(a) <= 12 else (int(b), int(a))   # 11/23/2026, or 23/11/2026
            hour = int(hour or 0)
            if half:
                hour = hour % 12 + (12 if half.lower() == 'pm' else 0)
            try:
                when = datetime.datetime(int(year), month, day, hour, int(minute or 0), int(second or 0))
            except ValueError:
                when = None
            date_only = when is not None and slashed.group(4) is None
    if when is None:
        for pattern in READ_FORMATS:
            try:
                when = datetime.datetime.strptime(raw, pattern)
                date_only = '%I' not in pattern and '%H' not in pattern
                break
            except ValueError:
                continue
    if when is None:
        raise ValueError(f'"{raw[:60]}" isn\'t a date Switchboard can read. Write it like 2026-11-23 or 2026-11-23T08:00')
    when = when.replace(tzinfo=zone) if when.tzinfo is None else when.astimezone(zone)
    return Moment(when.replace(microsecond=0), date_only)


def _add_months(when, months):
    month = when.month - 1 + months
    year, month = when.year + month // 12, month % 12 + 1
    if not 1 <= year <= 9999:
        raise ValueError('That date is out of range')
    return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))


def change_date(value, zone, direction, amount, unit):
    moment = read_date(value, zone)
    amount = _number(amount, 'Amount')
    if direction == 'subtract':
        amount = -amount
    when = moment.when
    try:
        if unit in CLOCK:
            # Hours and minutes are real time passing, so a jump across a clock change is right.
            utc = when.astimezone(datetime.timezone.utc) + datetime.timedelta(seconds=float(amount) * CLOCK[unit])
            return Moment(utc.astimezone(zone).replace(microsecond=0)).iso()
        if unit in ('months', 'years'):
            if amount != amount.to_integral_value():
                raise ValueError(f'Use a whole number of {unit}')
            when = _add_months(when, int(amount) * (12 if unit == 'years' else 1))
        else:
            # Days keep the time on the clock: 9:00 tomorrow is 9:00, even after a clock change.
            days = float(amount) * (7 if unit == 'weeks' else 1)
            naive = when.replace(tzinfo=None) + datetime.timedelta(days=days)
            when = naive.replace(tzinfo=zone)
    except OverflowError:
        raise ValueError('That date is out of range')
    return Moment(when.replace(microsecond=0), moment.date_only and unit not in CLOCK
                  and when.time() == datetime.time()).iso()


def _clock(when):
    return f'{when.hour % 12 or 12}:{when.minute:02d} {"AM" if when.hour < 12 else "PM"}'


FORMATS = [
    ('iso', lambda w: w.isoformat(timespec='seconds')),
    ('date', lambda w: w.date().isoformat()),
    ('short', lambda w: f'{MONTHS[w.month - 1][:3]} {w.day}, {w.year}'),
    ('long', lambda w: f'{WEEKDAYS[w.weekday()]}, {MONTHS[w.month - 1]} {w.day}, {w.year}'),
    ('datetime', lambda w: f'{MONTHS[w.month - 1][:3]} {w.day}, {w.year}, {_clock(w)}'),
    ('mdy', lambda w: f'{w.month:02d}/{w.day:02d}/{w.year}'),
    ('dmy', lambda w: f'{w.day:02d}/{w.month:02d}/{w.year}'),
    ('time', _clock),
    ('time24', lambda w: f'{w.hour:02d}:{w.minute:02d}'),
    ('weekday', lambda w: WEEKDAYS[w.weekday()]),
    ('month', lambda w: MONTHS[w.month - 1]),
    ('year', lambda w: w.year),
    ('unix', lambda w: int(w.timestamp())),
]


def format_choices():
    """Each format, labelled with what a sample date looks like in it."""
    sample = datetime.datetime(2026, 11, 23, 8, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-8)))
    labels = {'iso': ' (for other systems)', 'unix': ' (seconds, for other systems)'}
    return [{'value': key, 'label': f'{show(sample)}{labels.get(key, "")}'} for key, show in FORMATS]


def format_date(value, zone, style):
    when = read_date(value, zone).when
    return dict(FORMATS).get(style, FORMATS[0][1])(when)


def between(start, end, zone, unit):
    """Whole units from `start` to `end`, negative when `end` comes first."""
    first, last = read_date(start, zone), read_date(end, zone)
    a, b = first.when, last.when
    if unit in ('months', 'years'):
        sign = 1 if b >= a else -1
        a, b = (a, b) if sign > 0 else (b, a)
        months = (b.year - a.year) * 12 + b.month - a.month
        if months and _add_months(a, months) > b:
            months -= 1
        return sign * (months // 12 if unit == 'years' else months)
    if unit in ('days', 'weeks') and first.date_only and last.date_only:
        days = (b.date() - a.date()).days
    else:
        seconds = (b - a).total_seconds()
        if unit in CLOCK:
            return int(seconds / CLOCK[unit])
        days = seconds / 86400
    return int(days / 7) if unit == 'weeks' else int(days)


# --- running one ----------------------------------------------------------------------------

def run(node, values, step, zone, ctx=None, secrets=None):
    """Do a Data or Date step. Returns (output, short note)."""
    value = values.get('value')
    if node == 'to_number':
        result = to_number(value)
    elif node == 'to_integer':
        result = to_integer(value, values.get('rounding', 'round'))
    elif node == 'to_text':
        result = to_text(value, values.get('decimals'), bool(values.get('group')))
    elif node == 'to_bool':
        result = to_bool(value)
    elif node == 'calculate':
        result = calculate(value, values.get('op'), values.get('by'), values.get('places'))
    elif node == 'change_text':
        result = change_text(value, values.get('op'), values.get('find'), values.get('replace'), values.get('length'))
    elif node == 'set':
        output = set_fields(step.get('fields') or [], ctx or {}, secrets)
        return output, f"{len(output)} field{'' if len(output) == 1 else 's'}"
    elif node == 'date_change':
        result = change_date(values.get('date'), zone, values.get('direction', 'add'), values.get('amount'),
                             values.get('unit', 'days'))
    elif node == 'date_format':
        result = format_date(values.get('date'), zone, values.get('format', 'iso'))
    elif node == 'date_between':
        result = between(values.get('start'), values.get('end'), zone, values.get('unit', 'days'))
    else:
        raise ValueError("This step can't run here")
    return {'value': result}, text(result)[:120]
