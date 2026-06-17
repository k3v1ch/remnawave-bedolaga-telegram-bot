"""Task-local "are we serving a clone bot right now?" flag.

Set by :class:`app.middlewares.tenant_context.TenantContextMiddleware` in the cloner
host for the duration of each update, and read by UI builders (keyboards) that need to
hide main-brand-only actions on white-label clones (e.g. the Gift / Profile buttons on
the subscription screen).

It is a ``contextvars.ContextVar`` so it is scoped to the asyncio task handling the
current update — no cross-request leakage, no signature churn through the handler chain.
Always ``None`` in the main bot process (the middleware that sets it runs only in the
cloner), so the main bot's UI is unchanged.
"""

from __future__ import annotations

import contextvars
from typing import Any


_current_clone: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    'current_clone_bot', default=None
)


def set_current_clone(clone: Any | None) -> contextvars.Token:
    """Mark the current task as serving ``clone`` (a CloneSnapshot or None). Returns a
    token to pass back to :func:`reset_current_clone` in a ``finally`` block."""
    return _current_clone.set(clone)


def reset_current_clone(token: contextvars.Token) -> None:
    _current_clone.reset(token)


def get_current_clone() -> Any | None:
    return _current_clone.get()


def is_clone_context() -> bool:
    """True when the current update is being handled for a white-label clone bot."""
    return _current_clone.get() is not None
