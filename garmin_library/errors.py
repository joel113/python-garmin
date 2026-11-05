"""Shared application errors with HTTP status information."""
from __future__ import annotations



class AppError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
