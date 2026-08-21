from pathlib import Path

from agent_search_gateway.paths import RuntimePaths


def test_runtime_paths_are_derived_from_home_without_global_mutation(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)

    assert paths.config_file == tmp_path / ".config/agent-search-gateway-cli/config.toml"
    assert paths.socket_file == tmp_path / ".cache/agent-search-gateway-cli/daemon.sock"
    assert paths.results_dir == tmp_path / ".cache/agent-search-gateway-cli/results"
    assert paths.logs_dir == tmp_path / ".cache/agent-search-gateway-cli/logs"
    assert paths.debug_log_file == paths.logs_dir / "debug.log"
    assert RuntimePaths.from_home(tmp_path) == paths

    manual = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "runtime/daemon.sock",
        results_dir=tmp_path / "results",
    )
    assert manual.logs_dir == tmp_path / "runtime/logs"
    assert manual.debug_log_file == tmp_path / "runtime/logs/debug.log"
