"""Velocity (VTL) mapping templates for API Gateway integrations.

A non-proxy integration reshapes the request through ``requestTemplates`` and
the response through ``responseTemplates``. This module renders those templates
and exposes the variables AWS binds into them: ``$input``, ``$util``,
``$context`` and ``$stageVariables``.

The Velocity language itself comes from airspeed; what lives here is the AWS
binding layer on top of it.
"""

import base64
import json
import urllib.parse

import airspeed
from airspeed.operators import __additional_methods__ as _AIRSPEED_METHODS

__all__ = ["render", "build_namespace", "RequestOverrides", "TemplateError"]

TemplateError = airspeed.TemplateError

_UNPARSED = object()


# airspeed maps part of java.lang.String onto str. These are the ones it leaves
# out: without them the reference resolves to nothing and the template silently
# renders an empty value instead of failing.
_AIRSPEED_METHODS[str].update(
    {
        "trim": lambda self: self.strip(),
        "isEmpty": lambda self: len(self) == 0,
        "toLowerCase": lambda self: self.lower(),
        "toUpperCase": lambda self: self.upper(),
        "endsWith": lambda self, suffix: self.endswith(suffix),
        "equals": lambda self, other: self == other,
        "equalsIgnoreCase": lambda self, other: self.lower() == str(other).lower(),
        "concat": lambda self, other: self + str(other),
        "charAt": lambda self, index: self[index],
        "lastIndexOf": lambda self, value: self.rfind(value),
        "toString": lambda self: self,
        "replace": lambda self, old, new: self.replace(old, new),
    }
)
_AIRSPEED_METHODS[list].update(
    {
        "indexOf": lambda self, value: self.index(value),
        "toString": lambda self: str(self),
    }
)


class _Node(dict):
    """Dict whose missing levels come into being on access.

    ``#set($context.requestOverride.header.X = ...)`` walks the path with
    ``__getitem__``, so the intermediate levels have to exist by the time the
    assignment reaches the leaf.
    """

    def __missing__(self, name):
        child = _Node()
        self[name] = child
        return child

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


class _Headers(dict):
    """Header map whose lookups ignore case: templates spell header names the
    way the API documents them, clients send them however they like."""

    def __init__(self, data=None):
        super().__init__(data or {})
        self._lower = {k.lower(): k for k in self}

    def get(self, name, default=""):
        if name in self:
            return self[name]
        real = self._lower.get(str(name).lower())
        return self[real] if real is not None else default

    def containsKey(self, name):
        return name in self or str(name).lower() in self._lower

    def keySet(self):
        return list(self.keys())


class _Params:
    """What ``$input.params()`` returns: the three maps, plus ``get`` for the
    single-argument form."""

    def __init__(self, path=None, querystring=None, header=None):
        self.path = dict(path or {})
        self.querystring = dict(querystring or {})
        self.header = _Headers(header or {})

    # $input.params('name') resolves against path, then querystring, then header.
    def get(self, name, default=""):
        if name in self.path:
            return self.path[name]
        if name in self.querystring:
            return self.querystring[name]
        return self.header.get(name, default)

    def __str__(self):
        return "{path=%s, querystring=%s, header=%s}" % (
            self.path,
            self.querystring,
            dict(self.header),
        )


def _json_path(data, expression):
    """Resolve the JSONPath subset mapping templates use: ``$`` and dotted
    paths, with a numeric subscript indexing a list."""
    if data is None:
        return None

    expression = (expression or "$").strip()
    if expression in ("$", ""):
        return data

    if expression.startswith("$."):
        expression = expression[2:]
    elif expression.startswith("$"):
        expression = expression[1:]

    current = data
    for part in expression.split("."):
        if not part:
            continue
        name, _, index = part.partition("[")
        if name:
            if not isinstance(current, dict):
                return None
            current = current.get(name)
        if index:
            try:
                current = current[int(index.rstrip("]"))]
            except (ValueError, IndexError, TypeError, KeyError):
                return None
        if current is None:
            return None
    return current


class _Input:
    """``$input``: the request as the template sees it."""

    def __init__(self, body, params):
        if isinstance(body, bytes):
            text = body.decode("utf-8", "replace")
        else:
            text = body or ""
        self._body = text
        self._params = params
        self._parsed = _UNPARSED

    @property
    def body(self):
        return self._body

    def _data(self):
        if self._parsed is _UNPARSED:
            try:
                self._parsed = json.loads(self._body) if self._body else None
            except (ValueError, TypeError):
                self._parsed = None
        return self._parsed

    def params(self, name=None):
        if name is None:
            return self._params
        return self._params.get(name, "")

    # path() hands back the value itself; json() hands back its JSON text.
    def path(self, expression="$"):
        return _json_path(self._data(), expression)

    def json(self, expression="$"):
        value = _json_path(self._data(), expression)
        if value is None:
            return "{}" if expression in ("$", "") else ""
        return json.dumps(value, ensure_ascii=False)


class _Util:
    """``$util``: the helpers AWS exposes to mapping templates."""

    def escapeJavaScript(self, value):
        if value is None:
            return ""
        text = str(value)
        # AWS escapes the apostrophe as well, which is why a template that wants
        # a raw one follows this call with .replaceAll("\\'", "'").
        replacements = (
            (chr(92), chr(92) * 2),
            ('"', chr(92) + '"'),
            ("'", chr(92) + "'"),
            ("\r", chr(92) + "r"),
            ("\n", chr(92) + "n"),
            ("\t", chr(92) + "t"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        return text

    def urlEncode(self, value):
        return urllib.parse.quote(str(value or ""), safe="")

    def urlDecode(self, value):
        return urllib.parse.unquote(str(value or ""))

    def base64Encode(self, value):
        return base64.b64encode(str(value or "").encode("utf-8")).decode("ascii")

    def base64Decode(self, value):
        try:
            return base64.b64decode(str(value or "")).decode("utf-8", "replace")
        except Exception:
            return ""

    def parseJson(self, value):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None

    def toJson(self, value):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return "null"


class RequestOverrides:
    """Header, querystring and path overrides a request template asked for
    through ``$context.requestOverride``."""

    def __init__(self, node):
        override = (node or {}).get("requestOverride") or {}
        self.header = {k: str(v) for k, v in (override.get("header") or {}).items()}
        self.querystring = {k: str(v) for k, v in (override.get("querystring") or {}).items()}
        self.path = {k: str(v) for k, v in (override.get("path") or {}).items()}

    def __bool__(self):
        return bool(self.header or self.querystring or self.path)


def build_namespace(
    body=None,
    path_params=None,
    query_params=None,
    headers=None,
    stage_variables=None,
    context=None,
):
    """Assemble the variables a mapping template can reference.

    Returns the namespace and the mutable ``$context`` node, which carries back
    whatever the template assigned to ``requestOverride``.
    """
    flat_query = {}
    for key, value in (query_params or {}).items():
        if isinstance(value, (list, tuple)):
            flat_query[key] = value[0] if value else ""
        else:
            flat_query[key] = value

    params = _Params(path=path_params, querystring=flat_query, header=headers)

    context_node = _Node()
    for key, value in (context or {}).items():
        context_node[key] = value

    namespace = {
        "input": _Input(body, params),
        "util": _Util(),
        "context": context_node,
        "stageVariables": dict(stage_variables or {}),
    }
    return namespace, context_node


def render(template, namespace):
    """Render a mapping template. An empty template renders to an empty string."""
    if not template:
        return ""
    return airspeed.Template(template).merge(namespace)
