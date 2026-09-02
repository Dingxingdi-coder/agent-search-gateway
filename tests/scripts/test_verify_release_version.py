from pathlib import Path

import pytest

from scripts.verify_release_version import (
    expected_package_version,
    package_version,
    verify,
)


@pytest.mark.parametrize(
    ("tag", "expected", "prerelease"),
    [
        ("v0.1.0", "0.1.0", False),
        ("v12.34.56", "12.34.56", False),
        ("v1.0.0-rc.1", "1.0.0rc1", True),
        ("v1.0.0-pre.2", "1.0.0a2", True),
    ],
)
def test_expected_package_version_maps_semver_to_pep440(
    tag: str, expected: str, prerelease: bool
) -> None:
    assert expected_package_version(tag) == (expected, prerelease)


@pytest.mark.parametrize(
    "tag",
    [
        "0.1.0",
        "v01.0.0",
        "v1.0",
        "v1.0.0-beta.1",
        "v1.0.0-rc",
        "v1.0.0-rc.01",
        "v1.0.0+build",
    ],
)
def test_expected_package_version_rejects_unsupported_tags(tag: str) -> None:
    with pytest.raises(ValueError):
        expected_package_version(tag)


def test_verify_accepts_matching_declarations() -> None:
    assert not verify("v0.1.0", declared_project="0.1.0", declared_package="0.1.0")


def test_verify_rejects_mismatched_declarations() -> None:
    with pytest.raises(ValueError, match="version mismatch"):
        verify("v0.1.0", declared_project="0.1.0", declared_package="0.1.1")


def test_package_version_reads_static_assignment(tmp_path: Path) -> None:
    module = tmp_path / "version.py"
    module.write_text('__version__ = "2.3.4"\n', encoding="utf-8")

    assert package_version(module) == "2.3.4"
