"""Terminal output helpers: symbols, colours, tables and currency formatting.

All user-facing output goes through :class:`Console` so that ``--quiet`` and
non-UTF-8 terminals are handled in one place.
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Sequence, TextIO

RULE_WIDTH = 60

_UNICODE_SYMBOLS = {
    "ok": "✓",
    "fail": "✗",
    "warn": "!",
    "open": "○",
    "hit": "●",
    "arrow": "→",
    "dot": "•",
}
_ASCII_SYMBOLS = {
    "ok": "[OK]",
    "fail": "[X]",
    "warn": "[!]",
    "open": "[ ]",
    "hit": "[*]",
    "arrow": "->",
    "dot": "-",
}

_CURRENCY_SYMBOLS = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "AUD": "A$",
    "CAD": "C$",
    "SGD": "S$",
    "CHF": "CHF ",
    "BRL": "R$",
    "KRW": "₩",
}

_COLORS = {
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "dim": "\033[2m",
}
_RESET = "\033[0m"


def configure_stdio() -> None:
    """Make stdout/stderr tolerant of characters the terminal cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _can_encode(stream: TextIO, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _enable_windows_ansi() -> bool:
    """Enable ANSI escape processing on Windows 10+ consoles."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # pragma: no cover - platform specific
        return False


def format_money(amount: float | None, currency: str | None = None, *, ascii_only: bool = False) -> str:
    """Format an amount with its currency symbol. INR uses Indian digit grouping."""
    if amount is None:
        return "N/A"
    code = (currency or "").upper()
    symbol = _CURRENCY_SYMBOLS.get(code, f"{code} " if code else "")
    if ascii_only and not symbol.isascii():
        symbol = f"{code} "
    negative = amount < 0
    amount = abs(amount)
    whole = int(amount)
    fraction = round(amount - whole, 2)
    if fraction >= 1:  # rounding carried over, e.g. 9.999
        whole += 1
        fraction = 0.0
    if code == "INR":
        digits = str(whole)
        if len(digits) > 3:
            head, tail = digits[:-3], digits[-3:]
            groups = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            if head:
                groups.insert(0, head)
            grouped = ",".join(groups) + "," + tail
        else:
            grouped = digits
    else:
        grouped = f"{whole:,}"
    text = grouped if fraction == 0 else f"{grouped}.{int(round(fraction * 100)):02d}"
    return f"{'-' if negative else ''}{symbol}{text}"


def format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    max_width: int = 48,
    indent: str = "",
) -> str:
    """Render rows as a fixed-width text table."""
    rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    cells = [list(headers)] + rows

    def clip(text: str) -> str:
        return text if len(text) <= max_width else text[: max_width - 1] + "…"

    cells = [[clip(c) for c in row] for row in cells]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = []
    for index, row in enumerate(cells):
        lines.append(indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            lines.append(indent + "  ".join("-" * w for w in widths))
    return "\n".join(lines)


class Console:
    """Writes human-readable output. Respects --quiet and terminal capabilities."""

    def __init__(self, *, quiet: bool = False, stream: TextIO | None = None, color: bool | None = None):
        self.quiet = quiet
        self.stream = stream or sys.stdout
        unicode_ok = _can_encode(self.stream, "✓✗○●→₹€") and not os.environ.get("AZURE_MANAGER_ASCII")
        self.symbols = _UNICODE_SYMBOLS if unicode_ok else _ASCII_SYMBOLS
        self.unicode = unicode_ok
        if color is None:
            color = (
                hasattr(self.stream, "isatty")
                and self.stream.isatty()
                and not os.environ.get("NO_COLOR")
                and _enable_windows_ansi()
            )
        self.color = bool(color)

    # -- primitives -----------------------------------------------------
    def style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(_COLORS[s] for s in styles) + text + _RESET

    def sym(self, name: str) -> str:
        return self.symbols[name]

    def money(self, amount: float | None, currency: str | None) -> str:
        return format_money(amount, currency, ascii_only=not self.unicode)

    def print(self, text: str = "", *, force: bool = False) -> None:
        if self.quiet and not force:
            return
        print(text, file=self.stream)

    # -- structure ------------------------------------------------------
    def rule(self, char: str = "=", *, force: bool = False) -> None:
        self.print(char * RULE_WIDTH, force=force)

    def banner(self, title: str, *, force: bool = False) -> None:
        self.rule(force=force)
        self.print(self.style(title, "bold"), force=force)
        self.rule(force=force)

    def section(self, title: str) -> None:
        self.print()
        self.print(self.style(title, "bold", "cyan"))
        self.print("-" * min(RULE_WIDTH, max(len(title), 20)))

    def field(self, label: str, value: object, width: int = 18) -> None:
        self.print(f"{(label + ':').ljust(width)}{value}")

    # -- status lines ---------------------------------------------------
    def ok(self, text: str, *, force: bool = False) -> None:
        self.print(f"{self.style(self.sym('ok'), 'green')} {text}", force=force)

    def fail(self, text: str) -> None:
        self.print(f"{self.style(self.sym('fail'), 'red')} {text}", force=True)

    def warn(self, text: str) -> None:
        self.print(f"{self.style('WARNING:', 'yellow', 'bold')} {text}", force=True)

    def note(self, text: str) -> None:
        self.print(self.style(text, "dim"))

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[object]], **kwargs) -> None:
        rows = list(rows)
        if not rows:
            self.print("  (none)")
            return
        self.print(render_table(headers, rows, **kwargs))

    def error_block(
        self,
        *,
        operation: str,
        reason: str,
        user: str | None = None,
        scope: str | None = None,
        required_permission: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.print(force=True)
        self.print(self.style("ERROR", "red", "bold"), force=True)
        for label, value in (
            ("Operation", operation),
            ("User", user),
            ("Scope", scope),
            ("Reason", reason),
            ("Required permission", required_permission),
            ("Suggestion", hint),
        ):
            if value:
                self.print(force=True)
                self.print(f"{label}:", force=True)
                self.print(f"  {value}", force=True)
        self.print(force=True)

    def confirm(self, prompt: str) -> bool:
        """Ask a yes/no question. Returns False when stdin is not interactive."""
        if not sys.stdin or not sys.stdin.isatty():
            return False
        try:
            answer = input(f"{prompt} [yes/no]: ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")
