"""Dependency-boundary errors shared by admission and the standard-library worker."""

class ForbiddenAPI(Exception):
    """Reward source attempted a capability outside the frozen contract."""
