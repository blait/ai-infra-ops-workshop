"""Shared exception types (kept dependency-free to avoid import cycles)."""


class ApprovalError(Exception):
    """A proposal/approval/execution request was refused. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
