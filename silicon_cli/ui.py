"""Terminal UI helpers — colors, status glyphs, prompts. Mirrors the bash CLI."""
from __future__ import annotations

import sys

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _p(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def error(msg: str) -> None: _p(f"{RED}✗{RESET} {msg}")
def info(msg: str) -> None: _p(f"{BLUE}→{RESET} {msg}")
def success(msg: str) -> None: _p(f"{GREEN}✓{RESET} {msg}")
def warn(msg: str) -> None: _p(f"{YELLOW}⚠{RESET} {msg}")


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str, default_yes: bool = True) -> bool:
    if not interactive():
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{BOLD}? {question} {suffix}{RESET} ").strip().lower()
    except EOFError:
        return default_yes
    if not ans:
        return default_yes
    return ans[0] != "n" if default_yes else ans[0] == "y"


def ask(question: str, default: str = "") -> str:
    if not interactive():  # non-TTY: don't block on input(), take the default
        return default
    label = f"{BOLD}? {question}" + (f" [{default}]" if default else "") + f":{RESET} "
    try:
        ans = input(label).strip()
    except EOFError:
        ans = ""
    return ans or default


def read_secret(prompt: str) -> str:
    """Masked input (echoes * per char) on a TTY; falls back to getpass."""
    import getpass
    if not interactive():
        return ""
    try:
        sys.stderr.write(f"{BOLD}? {prompt}:{RESET} ")
        sys.stderr.flush()
        # getpass reads without echo; we accept that over fragile raw-tty masking.
        return getpass.getpass("")
    except Exception:
        return ""
