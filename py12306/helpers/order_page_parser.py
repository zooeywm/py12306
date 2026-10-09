"""Safely parse 12306's JavaScript object-literal assignments.

The initDc page uses a JSON-like representation with single-quoted strings,
JavaScript null/true/false and sometimes bare property names. We do not execute
JavaScript or use eval; only Python literal structures are accepted.
"""
import ast
import re


class _JSScalars(ast.NodeTransformer):
    _values = {'null': None, 'true': True, 'false': False, 'undefined': None}

    def visit_Name(self, node):
        if node.id not in self._values:
            raise ValueError('unsupported JavaScript expression')
        return ast.copy_location(ast.Constant(self._values[node.id]), node)

    def visit_Dict(self, node):
        node.keys = [
            ast.copy_location(ast.Constant(key.id), key)
            if isinstance(key, ast.Name) else self.visit(key)
            for key in node.keys
        ]
        node.values = [self.visit(value) for value in node.values]
        return node


def parse_js_object_assignment(html, name):
    """Return a dict from a simple JS variable assignment, with bounded parsing.

    Errors intentionally contain no source HTML: it may include credentials,
    booking tokens, passenger IDs or other private data.
    """
    if not isinstance(html, str) or len(html) > 4_000_000:
        raise ValueError('invalid or oversized document')
    if name not in ('ticketInfoForPassengerForm', 'orderRequestDTO'):
        raise ValueError('unsupported assignment name')

    assignment = re.search(r'\b(?:var|let|const)\s+' + re.escape(name) + r'\s*=\s*', html)
    if not assignment:
        raise ValueError('assignment missing')
    start = assignment.end()
    if start >= len(html) or html[start] != '{':
        raise ValueError('assignment is not an object')

    depth = 0
    quote = None
    escaped = False
    literal = None
    for position in range(start, min(len(html), start + 400_000)):
        char = html[position]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == '{':
            depth += 1
            if depth > 64:
                raise ValueError('object nesting is too deep')
        elif char == '}':
            depth -= 1
            if depth == 0:
                literal = html[start:position + 1]
                break
    if literal is None:
        raise ValueError('incomplete or oversized object')

    try:
        parsed = ast.parse(literal, mode='eval')
        result = ast.literal_eval(_JSScalars().visit(parsed))
    except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError) as error:
        raise ValueError('unsupported object literal') from error
    if not isinstance(result, dict):
        raise ValueError('object is not a dictionary')
    return result
