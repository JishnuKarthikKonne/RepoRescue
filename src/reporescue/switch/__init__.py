"""Switch integration package for RepoRescue."""

from .client import (
    MockSwitchClient,
    SwitchAuthError,
    SwitchClient,
    SwitchClientProtocol,
    SwitchConfig,
    SwitchError,
)
from .dispatcher import RepoRescueDispatcher
from .models import SwitchMessageEvent, WorkflowFormatter

__all__ = [
    "MockSwitchClient",
    "RepoRescueDispatcher",
    "SwitchAuthError",
    "SwitchClient",
    "SwitchClientProtocol",
    "SwitchConfig",
    "SwitchError",
    "SwitchMessageEvent",
    "WorkflowFormatter",
]
