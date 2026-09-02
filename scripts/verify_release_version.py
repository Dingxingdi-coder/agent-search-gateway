#!/usr/bin/env python3
"""Verify that a release tag matches both Python package version declarations."""

from __future__ import annotations

import argparse
import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER_TAG = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<kind>rc|pre)\.(?P<number>0|[1-9][0-9]*))?$"
)


def expected_package_version(tag: str) -> tuple[str, bool]:
    """Map a supported SemVer tag to its PEP 440 package version."""
    match = SEMVER_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("release tag must be vX.Y.Z, vX.Y.Z-rc.N, or vX.Y.Z-pre.N")

    base = ".".join(match.group(name) for name in ("major", "minor", "patch"))
    kind = match.group("kind")
    number = match.group("number")
    if kind is None or number is None:
        return base, False
    if kind == "rc":
        return f"{base}rc{number}", True
    return f"{base}a{number}", True


def package_version(path: Path) -> str:
    """Read a static ``__version__`` assignment without importing the package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
        raise ValueError("__version__ must be a static string")
    raise ValueError(f"{path} has no static __version__ assignment")


def project_version(path: Path) -> str:
    """Read the canonical project version from pyproject.toml."""
    with path.open("rb") as handle:
        value = tomllib.load(handle)["project"]["version"]
    if not isinstance(value, str):
        raise ValueError("project.version must be a string")
    return value


def verify(tag: str, *, declared_project: str, declared_package: str) -> bool:
    """Validate declarations and return whether the release is a prerelease."""
    expected, prerelease = expected_package_version(tag)
    if declared_project != expected or declared_package != expected:
        raise ValueError(
            "version mismatch: "
            f"tag={tag!r}, expected_package={expected!r}, "
            f"pyproject={declared_project!r}, package={declared_package!r}"
        )
    return prerelease


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Git tag to validate")
    return parser.parse_args()


def main() -> int:
    """Validate the requested tag against the repository version declarations."""
    args = parse_args()
    declared_project = project_version(ROOT / "pyproject.toml")
    declared_package = package_version(ROOT / "src" / "agent_search_gateway" / "__init__.py")
    prerelease = verify(
        args.tag,
        declared_project=declared_project,
        declared_package=declared_package,
    )
    release_kind = "prerelease" if prerelease else "stable release"
    print(f"Validated {args.tag} as a {release_kind} for package version {declared_project}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
