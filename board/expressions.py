"""Expressions: what goes between {{ and }} when it is more than one field.

    {{ s1.points.toNumber() * 2 }}           {{ t.body.first + " " + t.body.last }}
    {{ s1.level >= 3 ? "Gold" : "Silver" }}  {{ s2.starts_at.plus(2, "days").format("long") }}

A field is a step's uid followed by its path (`s1.person.name`, `s1.items.0`, `s1["first name"]`);
`secret.NAME` is a stored secret. On top of that: numbers, "text", true/false/null, + - * / %,
comparisons (== != > >= < <=), and/or/not, `cond ? a : b`, methods on values (METHODS) and a
few functions (now(), today()). There is no way to reach Python from here: it is parsed and
walked by this module alone.

`evaluate` raises ExpressionError with a message for the admin.
"""
import re
from decimal import Decimal, InvalidOperation

from . import transform
from .templating import compare, is_empty, number, text


class ExpressionError(ValueError):
    pass


_TOKEN = re.compile(r'''\s*(?:
    (?P<num>\d+(?:\.\d+)?(?![A-Za-z_]))
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*|\d+[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|>=|<=|&&|\|\||[-+*/%<>!?:().,\[\]])
)''', re.X)
_WORDS = {'and': '&&', 'or': '||', 'not': '!'}
_LITERALS = {'true': True, 'false': False, 'null': None}
_ESCAPES = {'n': '\n', 't': '\t'}


def _tokens(source):
    out, at = [], 0
    source = source.rstrip()
    while at < len(source):
        match = _TOKEN.match(source, at)
        if not match or match.end() == at:
            raise ExpressionError(f'Switchboard can\'t read "{source[at:].strip()[:20]}" here')
        at = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == 'str':
            value = re.sub(r'\\(.)', lambda m: _ESCAPES.get(m.group(1), m.group(1)), value[1:-1])
        elif kind == 'num':
            value = Decimal(value)
        elif kind == 'name' and value in _WORDS:
            kind, value = 'op', _WORDS[value]
        out.append((kind, value))
    out.append(('end', None))
    return out


# --- parsing: a small Pratt parser into tuples ---------------------------------------------------
#   ('lit', value)  ('ref', root, [key, ...])  ('get', node, key-node)  ('call', node, name, [args])
#   ('fn', name, [args])  ('bin', op, a, b)  ('not', a)  ('neg', a)  ('if', cond, a, b)

_BINARY = {'||': 2, '&&': 3, '==': 5, '!=': 5, '>': 5, '>=': 5, '<': 5, '<=': 5,
           '+': 6, '-': 6, '*': 7, '/': 7, '%': 7}


class _Parser:
    def __init__(self, source):
        self.items, self.at = _tokens(source), 0

    def peek(self):
        return self.items[self.at]

    def take(self, op=None):
        kind, value = self.items[self.at]
        if op is not None and not (kind == 'op' and value == op):
            shown = 'the end' if kind == 'end' else f'"{value}"'
            raise ExpressionError(f'Expected "{op}" but found {shown}')
        self.at += 1
        return kind, value

    def is_op(self, op):
        kind, value = self.peek()
        return kind == 'op' and value == op

    def parse(self):
        if self.peek()[0] == 'end':
            raise ExpressionError('There is nothing between {{ and }}')
        node = self.expression(0)
        if self.peek()[0] != 'end':
            raise ExpressionError(f'Switchboard didn\'t expect "{self.peek()[1]}" there')
        return node

    def expression(self, floor):
        node = self.prefix()
        while True:
            kind, value = self.peek()
            if kind == 'op' and value == '?' and floor < 1:
                self.take()
                yes = self.expression(0)
                self.take(':')
                node = ('if', node, yes, self.expression(0))
            elif kind == 'op' and value in _BINARY and _BINARY[value] > floor:
                self.take()
                node = ('bin', value, node, self.expression(_BINARY[value]))
            else:
                return node

    def prefix(self):
        kind, value = self.take()
        if kind == 'op' and value == '!':
            return ('not', self.expression(4))
        if kind == 'op' and value == '-':
            return ('neg', self.expression(7))
        if kind == 'op' and value == '(':
            node = self.expression(0)
            self.take(')')
            return self.postfix(node)
        if kind in ('num', 'str'):
            return self.postfix(('lit', value))
        if kind == 'name':
            if value in _LITERALS:
                return self.postfix(('lit', _LITERALS[value]))
            if self.is_op('('):
                return self.postfix(('fn', value, self.arguments()))
            return self.postfix(('ref', value, []))
        raise ExpressionError('the expression ends too soon' if kind == 'end' else f'Switchboard didn\'t expect "{value}" there')

    def arguments(self):
        self.take('(')
        args = []
        if not self.is_op(')'):
            args.append(self.expression(0))
            while self.is_op(','):
                self.take()
                args.append(self.expression(0))
        self.take(')')
        return args

    def postfix(self, node):
        while True:
            if self.is_op('.'):
                self.take()
                kind, value = self.take()
                if kind not in ('name', 'num'):
                    raise ExpressionError('A field or method name has to follow "."')
                key = str(value) if kind == 'name' else str(int(value)) if value == value.to_integral_value() else None
                if key is None:
                    raise ExpressionError(f'"{value}" isn\'t a field name')
                if self.is_op('('):
                    node = ('call', node, key, self.arguments())
                elif node[0] == 'ref':
                    node = ('ref', node[1], node[2] + [key])
                else:
                    node = ('get', node, ('lit', key))
            elif self.is_op('['):
                self.take()
                key = self.expression(0)
                self.take(']')
                if node[0] == 'ref' and key[0] == 'lit' and isinstance(key[1], str):
                    node = ('ref', node[1], node[2] + [key[1]])
                else:
                    node = ('get', node, key)
            else:
                return node


_CACHE = {}


def parse(source):
    if source not in _CACHE:
        if len(_CACHE) > 2000:
            _CACHE.clear()
        _CACHE[source] = _Parser(source).parse()
    return _CACHE[source]


def references(source):
    """Every (step uid, dotted path) an expression reads. [] when it can't be read."""
    try:
        tree = parse(source)
    except ExpressionError:
        return []
    found = []

    def walk(node):
        if not isinstance(node, tuple):
            return
        if node[0] == 'ref':
            found.append((node[1], '.'.join(node[2])))
            return
        for part in node[1:]:
            if isinstance(part, list):
                for item in part:
                    walk(item)
            else:
                walk(part)

    walk(tree)
    return found


# --- running -----------------------------------------------------------------------------------

def _zone():
    from .models import Settings
    return Settings.load().zone()


def _get(value, key):
    if isinstance(value, dict):
        return value.get(str(key))
    if isinstance(value, (list, str)):
        if key == 'length':
            return len(value)
        n = number(key)
        if n is not None and n == n.to_integral_value() and isinstance(value, list):
            try:
                return value[int(n)]
            except IndexError:
                return None
    return None


def _num(value, what):
    if value is None or value == '':
        raise ExpressionError(f'{what} is empty, so there is nothing to calculate with')
    if isinstance(value, bool):
        raise ExpressionError(f'{what} is yes or no, not a number')
    n = number(value)
    if n is None or not n.is_finite():
        raise ExpressionError(f'"{text(value)}" is not a number')
    return n


def _plain(n):
    if n == n.to_integral_value():
        return int(n)
    if -n.as_tuple().exponent > 12:
        n = n.quantize(Decimal('1e-12')).normalize()
    return float(n)


def truth(value):
    """Whether a value counts as yes in `and`, `or`, `not` and `? :`."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ('', 'false', 'no', '0', 'off')
    return bool(value)


_COMPARE = {'==': 'eq', '!=': 'ne', '>': 'gt', '>=': 'gte', '<': 'lt', '<=': 'lte'}


def _binary(op, a, b):
    if op in _COMPARE:
        return compare(a, _COMPARE[op], b)
    if op == '+':
        numeric = lambda v: isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)  # noqa: E731
        if numeric(a) and numeric(b):
            return _plain(Decimal(str(a)) + Decimal(str(b)))
        if isinstance(a, list) and isinstance(b, list):
            return a + b
        if (numeric(a) and b is None) or (numeric(b) and a is None):
            raise ExpressionError('One side of + is empty, so there is nothing to add')
        return text(a) + text(b)
    left, right = _num(a, 'The left side'), _num(b, 'The right side')
    try:
        if op == '-':
            return _plain(left - right)
        if op == '*':
            return _plain(left * right)
        if right == 0:
            raise ExpressionError("Can't divide by zero")
        return _plain(left / right if op == '/' else left % right)
    except InvalidOperation:
        raise ExpressionError('That calculation has no answer')


def _unit(value):
    word = text(value).strip().lower()
    word = word if word.endswith('s') else word + 's'
    if word not in dict(transform.UNITS):
        raise ExpressionError(f'"{text(value)}" isn\'t a unit. Use seconds, minutes, hours, days, weeks, months or years')
    return word


def _arity(name, args, low, high=None):
    high = low if high is None else high
    if not low <= len(args) <= high:
        wanted = f'{low}' if low == high else f'{low} to {high}'
        raise ExpressionError(f'.{name}() takes {wanted} value{"" if (low, high) == (1, 1) else "s"} in its brackets, not {len(args)}')


def _ifempty(v, fallback):
    return fallback if is_empty(v) else v


# Each method: (lowercase names, how many values it takes (low, high), what it does, help text).
_METHODS = [
    (('tonumber', 'tofloat'), (0, 0), lambda v, z: transform.to_number(v), '"12.50".toNumber() is 12.5'),
    (('toint', 'towholenumber', 'tointeger'), (0, 0), lambda v, z: transform.to_integer(v), '"2.6".toInt() is 3'),
    (('totext', 'tostring'), (0, 1), lambda v, z, d=None: transform.to_text(v, None if d is None else int(_num(d, 'The number of decimals')), False),
     '12.5.toText(2) is "12.50"'),
    (('toboolean', 'tobool', 'toyesno'), (0, 0), lambda v, z: transform.to_bool(v), '"yes".toBoolean() is true'),
    (('round',), (0, 1), lambda v, z, p=0: transform.calculate(v, 'round', places=int(_num(p, 'The number of decimals'))), '2.567.round(2) is 2.57'),
    (('floor', 'rounddown'), (0, 0), lambda v, z: transform.to_integer(v, 'down'), '2.9.floor() is 2'),
    (('ceil', 'roundup'), (0, 0), lambda v, z: transform.to_integer(v, 'up'), '2.1.ceil() is 3'),
    (('abs',), (0, 0), lambda v, z: _plain(abs(_num(v, 'The value'))), '(-4).abs() is 4'),
    (('upper', 'touppercase'), (0, 0), lambda v, z: text(v).upper(), '"ana".upper() is "ANA"'),
    (('lower', 'tolowercase'), (0, 0), lambda v, z: text(v).lower(), '"ANA".lower() is "ana"'),
    (('title', 'totitlecase', 'capitalize'), (0, 0), lambda v, z: transform.change_text(v, 'title'), '"maria k".title() is "Maria K"'),
    (('trim',), (0, 0), lambda v, z: transform.change_text(v, 'trim'), '" a  b ".trim() is "a b"'),
    (('replace', 'replaceall'), (2, 2), lambda v, z, a, b: text(v).replace(text(a), text(b)), '"a-b".replace("-", " ") is "a b"'),
    (('split',), (0, 1), lambda v, z, sep=',': [part.strip() for part in text(v).split(text(sep))] if text(v) else [],
     '"a, b".split(",") is a list of "a" and "b"'),
    (('join',), (0, 1), lambda v, z, sep=', ': text(sep).join(text(i) for i in (v if isinstance(v, list) else [v])),
     'a list .join(", ") is "a, b"'),
    (('slice', 'substring'), (1, 2), lambda v, z, a, b=None: (v if isinstance(v, list) else text(v))[int(_num(a, 'Where to start')):None if b is None else int(_num(b, 'Where to stop'))],
     '"Pavilion".slice(0, 3) is "Pav"'),
    (('first',), (0, 0), lambda v, z: (v[0] if v else None) if isinstance(v, list) else text(v)[:1], 'a list .first() is its first item'),
    (('last',), (0, 0), lambda v, z: (v[-1] if v else None) if isinstance(v, list) else text(v)[-1:], 'a list .last() is its last item'),
    (('length', 'count'), (0, 0), lambda v, z: len(v) if isinstance(v, (list, dict)) else len(text(v)), '"Ana".length() is 3'),
    (('includes', 'contains'), (1, 1), lambda v, z, x: compare(v, 'contains', x), '"Café".includes("af") is true'),
    (('startswith',), (1, 1), lambda v, z, x: text(v).lower().startswith(text(x).lower()), '"Ana".startsWith("an") is true'),
    (('endswith',), (1, 1), lambda v, z, x: text(v).lower().endswith(text(x).lower()), '"a.org".endsWith(".org") is true'),
    (('isempty',), (0, 0), lambda v, z: is_empty(v), 'true when there is nothing in it'),
    (('isnotempty',), (0, 0), lambda v, z: not is_empty(v), 'true when there is something in it'),
    (('ifempty', 'default'), (1, 1), lambda v, z, fallback: _ifempty(v, fallback), 'x.ifEmpty("n/a") is "n/a" when x is empty'),
    (('todate',), (0, 0), lambda v, z: transform.read_date(v, z()).iso(), '"Nov 28, 2026".toDate() is "2026-11-28"'),
    (('plus', 'add'), (2, 2), lambda v, z, n, unit: transform.change_date(v, z(), 'add', n, _unit(unit)),
     'date.plus(2, "days") is two days later'),
    (('minus', 'subtract'), (2, 2), lambda v, z, n, unit: transform.change_date(v, z(), 'subtract', n, _unit(unit)),
     'date.minus(1, "hour") is an hour earlier'),
    (('format',), (0, 1), lambda v, z, style='long': transform.format_date(v, z(), text(style)), 'date.format("short") is "Nov 23, 2026"'),
    (('until',), (2, 2), lambda v, z, other, unit: transform.between(v, other, z(), _unit(unit)),
     'today().until(date, "days") is how many days until it'),
]
METHODS = {name: (arity, run) for names, arity, run, _ in _METHODS for name in names}
FUNCTIONS = {
    'now': lambda z: transform.read_date('', z()).iso(),
    'today': lambda z: transform.read_date('', z()).when.date().isoformat(),
}


def help_rows():
    """[(example, what it does)] for the builder's cheat sheet."""
    return [('{{ s1.price * 2 }}', 'Arithmetic: + - * / and % (remainder)'),
            ('{{ s1.first + " " + s1.last }}', 'Join text with +'),
            ('{{ s1.level >= 3 ? "Gold" : "Silver" }}', 'Pick one of two: condition ? if yes : if no'),
            ('{{ s1.paid and not s1.refunded }}', 'and, or, not'),
            ('{{ now() }}  {{ today() }}', 'The time and date the step runs')] + [
        (_label(names[0], arity), example) for names, arity, _, example in _METHODS]


_NICE = {'tonumber': 'toNumber', 'toint': 'toInt', 'totext': 'toText', 'toboolean': 'toBoolean', 'startswith': 'startsWith',
         'endswith': 'endsWith', 'isempty': 'isEmpty', 'isnotempty': 'isNotEmpty', 'ifempty': 'ifEmpty', 'todate': 'toDate'}


def _label(name, arity):
    return f'.{_NICE.get(name, name)}({"…" if arity[1] else ""})'


def evaluate(source, ctx, secrets=None):
    """The value of what is between {{ and }}."""
    zone_cache = {}

    def zone():
        if 'zone' not in zone_cache:
            zone_cache['zone'] = _zone()
        return zone_cache['zone']

    def run(node):
        kind = node[0]
        if kind == 'lit':
            return node[1] if not isinstance(node[1], Decimal) else _plain(node[1])
        if kind == 'ref':
            value = (secrets or {}) if node[1] == 'secret' else ctx.get(node[1])
            for key in node[2]:
                value = _get(value, key)
            return value
        if kind == 'get':
            return _get(run(node[1]), text(run(node[2])))
        if kind == 'not':
            return not truth(run(node[1]))
        if kind == 'neg':
            return _plain(-_num(run(node[1]), 'The value after -'))
        if kind == 'if':
            return run(node[2]) if truth(run(node[1])) else run(node[3])
        if kind == 'bin':
            op = node[1]
            if op == '&&':
                return truth(run(node[2])) and truth(run(node[3]))
            if op == '||':
                return truth(run(node[2])) or truth(run(node[3]))
            return _binary(op, run(node[2]), run(node[3]))
        if kind == 'fn':
            name = node[1].lower()
            if name not in FUNCTIONS:
                raise ExpressionError(f'There is no function called {node[1]}()')
            if node[2]:
                raise ExpressionError(f'{node[1]}() takes nothing in its brackets')
            return FUNCTIONS[name](zone)
        if kind == 'call':
            name = node[2].lower()
            if name not in METHODS:
                raise ExpressionError(f'There is no method called .{node[2]}()')
            arity, method = METHODS[name]
            _arity(node[2], node[3], *arity)
            try:
                return method(run(node[1]), zone, *[run(arg) for arg in node[3]])
            except ExpressionError:
                raise
            except ValueError as error:
                raise ExpressionError(str(error))
            except (TypeError, IndexError, OverflowError, InvalidOperation):
                raise ExpressionError(f'.{node[2]}() can\'t work with that value')
        raise ExpressionError('Switchboard can\'t work this out')

    try:
        return run(parse(source))
    except RecursionError:
        raise ExpressionError('This expression is too deeply nested')
