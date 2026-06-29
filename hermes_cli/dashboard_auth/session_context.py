"""Task-local dashboard-auth session context.

The HTTP auth middleware stores the verified Session here so downstream
handlers can make ACL decisions without threading the session object through
every call signature. WebSocket handlers can set the context temporarily when
they validate a browser ticket or internal credential.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

from hermes_cli.dashboard_auth.base import Session

_CURRENT_SESSION: ContextVar[Optional[Session]] = ContextVar(
    "hermes_dashboard_session",
    default=None,
)


def get_current_session() -> Optional[Session]:
    """Return the currently authenticated dashboard session, if any."""
    return _CURRENT_SESSION.get()


def set_current_session(session: Optional[Session]) -> Token[Optional[Session]]:
    """Install *session* for the current task and return the reset token."""
    return _CURRENT_SESSION.set(session)


def reset_current_session(token: Token[Optional[Session]]) -> None:
    """Restore the previous session after a request finishes."""
    _CURRENT_SESSION.reset(token)
