"""Shared state, loading, error display and formatting helpers for the Streamlit UI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.accounts import SubscriptionChoice, discover_subscriptions  # noqa: E402
from src.auth import build_credential  # noqa: E402
from src.config import load_config  # noqa: E402
from src.console import format_money  # noqa: E402
from src.context import AzureContext  # noqa: E402
from src.errors import AzureManagerError, ConfigError, OperationError  # noqa: E402
from src.logging_utils import redact, setup_logging  # noqa: E402
from src.models import ActionKind  # noqa: E402
from src.validation import is_guid  # noqa: E402

CONFIG_PATH = Path(os.environ.get("AZURE_MANAGER_CONFIG") or ROOT / "config.yaml")
EXAMPLE_PATH = ROOT / "config.example.yaml"
PAGES: dict[str, Any] = {}
ss = st.session_state

KIND_BADGE = {
    ActionKind.NOOP: ("OK", "green"),
    ActionKind.INVITE: ("INVITE", "violet"),
    ActionKind.CREATE: ("CREATE", "blue"),
    ActionKind.ASSIGN: ("ASSIGN", "blue"),
    ActionKind.UPDATE: ("UPDATE", "orange"),
    ActionKind.BLOCKED: ("BLOCKED", "red"),
}
SEVERITY_COLOR = {"HIGH": "red", "MEDIUM": "orange", "LOW": "blue", "INFO": "gray"}
HEALTH_COLOR = {"HEALTHY": "green", "WARNING": "orange", "CRITICAL": "red", "EXCEEDED": "red", "UNKNOWN": "gray"}


# --------------------------------------------------------------------------- one-time init


@st.cache_resource
def init_logging() -> Path | None:
    return setup_logging(os.environ.get("AZURE_MANAGER_LOG_DIR") or ROOT / "logs")


# --------------------------------------------------------------------------- text helpers


_MD_SPECIAL = re.compile(r"([\\`*_\[\]$<>#|~])")


def md(text: Any) -> str:
    """Escape user/Azure-provided text for st.markdown ('$' would otherwise start LaTeX)."""
    return _MD_SPECIAL.sub(r"\\\1", "" if text is None else str(text))


def money(amount: float | None, currency: str | None) -> str:
    return format_money(amount, currency)


def badge(label: str, color: str) -> str:
    """Inline markdown badge."""
    return f":{color}-badge[{md(label)}]"


# --------------------------------------------------------------------------- errors / loading


def show_error(exc: BaseException) -> None:
    if isinstance(exc, ConfigError):
        st.error("**Configuration problems**\n\n" + "\n".join(f"- {md(p)}" for p in exc.problems))
        return
    if isinstance(exc, OperationError):
        lines = [f"**{md(exc.operation)} failed**", md(exc.reason)]
        if exc.scope:
            lines.append(f"**Scope:** {md(exc.scope)}")
        if exc.required_permission:
            lines.append(f"**Required permission:** {md(exc.required_permission)}")
        if exc.hint:
            lines.append(f"**Suggestion:** {md(exc.hint)}")
        st.error("\n\n".join(lines))
        return
    if isinstance(exc, AzureManagerError):
        st.error(md(str(exc)))
        return
    st.error(f"Unexpected error: {md(exc.__class__.__name__)}: {md(redact(str(exc)))}")


def run(loader: Callable[[], Any], spinner: str | None = None) -> Any:
    """Run an Azure call, rendering any failure as a readable error. Returns None on failure."""
    try:
        if spinner:
            with st.spinner(spinner):
                return loader()
        return loader()
    except Exception as exc:  # noqa: BLE001 - every failure is shown, never a traceback
        show_error(exc)
        return None


def cached(key: Any, loader: Callable[[], Any], spinner: str | None = None) -> Any:
    """Load once per session (Streamlit re-runs the page on every click) until Refresh is pressed."""
    store = ss.setdefault("cache", {})
    if key in store:
        return store[key]
    data = run(loader, spinner)
    if data is not None:
        store[key] = data
    return data


def clear_cache() -> None:
    ss["cache"] = {}
    ctx = ss.get("ctx")
    if ctx is not None:
        ctx.reset_caches()


def refresh_button(key: str) -> None:
    if st.button("Refresh", icon=":material/refresh:", key=key, help="Reload data from Azure"):
        clear_cache()
        st.rerun()


# --------------------------------------------------------------------------- connection


def require_ctx() -> AzureContext:
    ctx = ss.get("ctx")
    if ctx is None:
        st.info("Sign in and choose a subscription first.")
        st.page_link(PAGES["account"], label="Go to Account", icon=":material/login:")
        st.stop()
    return ctx


def connect(choice: SubscriptionChoice) -> bool:
    mode = ss.get("auth_mode_effective", "cli")
    credential = build_credential(mode, choice.tenant_id)
    ss.ctx = AzureContext(credential, choice.subscription_id, ss.identity)
    ss.subscription = choice
    ss.pop("setup_plan", None)
    ss.pop("setup_result", None)
    clear_cache()
    return True


def disconnect() -> None:
    for key in ("ctx", "subscription", "discovery", "identity", "setup_plan", "setup_result"):
        ss.pop(key, None)
    ss["cache"] = {}


def load_subscriptions(mode: str) -> bool:
    from src.accounts import effective_mode

    result = run(lambda: discover_subscriptions(mode), "Signing in and loading your subscriptions…")
    if result is None:
        return False
    identity, choices, issues = result
    ss.identity = identity
    ss.discovery = (choices, issues)
    ss.auth_mode_effective = effective_mode(mode)
    return True


def default_subscription_id() -> str | None:
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        value = str(raw.get("subscription_id") or "").strip()
        return value if is_guid(value) else None
    except (OSError, yaml.YAMLError, AttributeError):
        return None


def auto_connect() -> None:
    """On first load, silently try the existing login so returning users land on their data."""
    if ss.get("auto_tried"):
        return
    ss.auto_tried = True
    try:
        from src.accounts import effective_mode

        identity, choices, issues = discover_subscriptions("auto")
    except Exception:  # noqa: BLE001 - not signed in yet; the Account page explains what to do
        return
    ss.identity = identity
    ss.discovery = (choices, issues)
    ss.auth_mode_effective = effective_mode("auto")
    preferred = default_subscription_id()
    match = next((c for c in choices if c.subscription_id.lower() == (preferred or "").lower()), None)
    if match is None and len(choices) == 1:
        match = choices[0]
    if match is not None:
        try:
            connect(match)
        except Exception:  # noqa: BLE001
            pass


def az_login(tenant: str | None) -> tuple[bool, str]:
    """Run 'az login' on this computer (opens the browser)."""
    az = shutil.which("az")
    if not az:
        return False, "The Azure CLI is not installed."
    command = [az, "login", "--only-show-errors", "--output", "none"]
    if tenant:
        command += ["--tenant", tenant]
    env = dict(os.environ)
    env["AZURE_CORE_LOGIN_EXPERIENCE_V2"] = "off"  # no interactive subscription prompt in the terminal
    env["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] = "false"  # use the browser instead of the Windows account popup
    try:
        proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "Sign-in timed out after 10 minutes."
    output = redact((proc.stderr or proc.stdout or "").strip())
    return proc.returncode == 0, output


# --------------------------------------------------------------------------- config file


def ensure_config() -> None:
    if not CONFIG_PATH.exists() and EXAMPLE_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")


def read_raw() -> dict:
    ensure_config()
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG_PATH.name} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH.name}: top level must be a mapping")
    return data


def _backup() -> None:
    if CONFIG_PATH.exists():
        CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".bak").write_text(
            CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )


def save_raw(raw: dict) -> bool:
    _backup()
    CONFIG_PATH.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False), encoding="utf-8"
    )
    return True


def save_text(text: str) -> bool:
    _backup()
    CONFIG_PATH.write_text(text, encoding="utf-8")
    return True


def config_fingerprint() -> tuple:
    sub = ss.get("subscription")
    mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0
    return (mtime, sub.subscription_id if sub else None)


def load_config_for_ui():
    """Config validated against the subscription chosen in the UI."""
    ensure_config()
    sub = ss.get("subscription")
    return load_config(
        CONFIG_PATH,
        subscription_override=sub.subscription_id if sub else None,
        tenant_override=sub.tenant_id if sub else None,
    )


def page_header(title: str, caption: str | None = None, refresh_key: str | None = None) -> None:
    left, right = st.columns([5, 1], vertical_alignment="bottom")
    with left:
        st.title(title)
        if caption:
            st.caption(caption)
    if refresh_key:
        with right:
            refresh_button(refresh_key)


def stamp() -> str:
    return time.strftime("%Y-%m-%d")
