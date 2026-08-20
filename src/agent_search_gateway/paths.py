"""Filesystem paths owned by the gateway runtime."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    config_file: Path
    socket_file: Path
    results_dir: Path

    @property
    def logs_dir(self) -> Path:
        return self.socket_file.parent / "logs"

    @property
    def debug_log_file(self) -> Path:
        return self.logs_dir / "debug.log"

    @classmethod
    def from_home(cls, home: Path) -> "RuntimePaths":
        return cls(
            config_file=home / ".config/agent-search-gateway-cli/config.toml",
            socket_file=home / ".cache/agent-search-gateway-cli/daemon.sock",
            results_dir=home / ".cache/agent-search-gateway-cli/results",
        )

    @classmethod
    def default(cls) -> "RuntimePaths":
        return cls.from_home(Path.home())
