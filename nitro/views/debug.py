import linecache
import sys
import traceback as tb
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)


def _extract_frames(exc: BaseException) -> list[dict]:
    frames = []
    trace = tb.TracebackException.from_exception(exc)

    for i, frame in enumerate(trace.stack):
        filename = frame.filename or "<unknown>"
        lineno = frame.lineno or 0
        name = frame.name or "<module>"

        pre_context: list[str] = []
        context_line: str | None = None
        post_context: list[str] = []
        pre_context_lineno = 0

        if filename != "<unknown>" and lineno:
            start = max(1, lineno - 4)
            pre_context_lineno = start
            for line_num in range(start, lineno + 5):
                raw = linecache.getline(filename, line_num)
                if not raw:
                    break
                raw = raw.rstrip("\n")
                if line_num < lineno:
                    pre_context.append(raw)
                elif line_num == lineno:
                    context_line = raw
                else:
                    post_context.append(raw)

        frames.append(
            {
                "id": i,
                "filename": filename,
                "lineno": lineno,
                "name": name,
                "pre_context": pre_context,
                "pre_context_lineno": pre_context_lineno,
                "context_line": context_line,
                "post_context": post_context,
            }
        )

    return frames


def render_404_page(
    method: str,
    path: str,
    url_patterns: list[str] | None = None,
) -> str:
    template = _env().get_template("error_404.html")
    return template.render(
        method=method,
        path=path,
        url_patterns=url_patterns or [],
    )


def render_500_page(method: str, path: str, exc: BaseException) -> str:
    template = _env().get_template("error_500.html")
    return template.render(
        method=method,
        path=path,
        exception_type=type(exc).__name__,
        exception_value=str(exc),
        frames=_extract_frames(exc),
        python_version=sys.version.split()[0],
    )
