"""Typed error contract shared across the scraping pipeline and MCP tools.

Every failure that reaches an MCP tool boundary should be traceable to one of
these stable types rather than an arbitrary exception message, so a caller
(or another agent) can tell a genuine anti-bot block apart from a transient
network hiccup, and never mistake either for "no results found."
"""

from enum import Enum


class ErrorType(str, Enum):
    BLOCKED = "blocked"
    TRANSPORT_ERROR = "transport_error"
    ROBOTS_DISALLOWED = "robots_disallowed"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    INTERNAL = "internal"


class LeadOrbytError(Exception):
    def __init__(self, type: ErrorType, message: str, retryable: bool = False):
        super().__init__(message)
        self.type = type
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict:
        return {"type": self.type.value, "message": self.message, "retryable": self.retryable}

    def __str__(self) -> str:
        return f"error: {self.type.value}: {self.message}"
