"""Shared pytest configuration for agent-search-gateway."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Keep Darwin Unix-socket test paths below ``sockaddr_un.sun_path`` limits."""
    if sys.platform != "darwin" or config.option.basetemp is not None:
        return
    config.option.basetemp = Path("/tmp") / f"agw-{os.getpid()}"
