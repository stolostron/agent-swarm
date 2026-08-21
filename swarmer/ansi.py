import re

from markupsafe import Markup, escape

_COLORS = {
    '30': '#555',     '31': '#c0392b', '32': '#27ae60', '33': '#e67e22',
    '34': '#2980b9',  '35': '#8e44ad', '36': '#16a085', '37': '#bdc3c7',
    '90': '#7f8c8d',  '91': '#e74c3c', '92': '#2ecc71', '93': '#f1c40f',
    '94': '#3498db',  '95': '#9b59b6', '96': '#1abc9c', '97': '#ecf0f1',
}
_ANSI_RE = re.compile(r'\x1b\[([\d;]*)m')


def ansi_to_html(text: str) -> Markup:
    """Convert ANSI SGR escape codes to HTML spans. HTML-escapes content before inserting tags."""
    safe = str(escape(text))
    out, stack, pos = [], [], 0
    for m in _ANSI_RE.finditer(safe):
        out.append(safe[pos:m.start()])
        pos = m.end()
        for code in (m.group(1).split(';') if m.group(1) else ['0']):
            if code in ('0', ''):
                out.extend('</span>' for _ in stack)
                stack.clear()
            elif code == '1':
                out.append('<span style="font-weight:bold">')
                stack.append('</span>')
            elif code in _COLORS:
                out.append(f'<span style="color:{_COLORS[code]}">')
                stack.append('</span>')
    out.append(safe[pos:])
    out.extend('</span>' for _ in stack)
    return Markup(''.join(out))
