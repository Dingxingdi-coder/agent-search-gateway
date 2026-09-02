#!/usr/bin/env python3
"""Reject credential-bearing HTTP URLs outside reserved example domains."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
URL_PATTERN = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
RESERVED_HOSTS = frozenset({"example.com", "example.net", "example.org"})
RESERVED_SUFFIXES = (
    ".example.com",
    ".example.net",
    ".example.org",
    ".example",
    ".invalid",
    ".test",
)
ZERO_SHA = "0" * 40


def run_git(*arguments: str, allow_no_matches: bool = False) -> str:
    """Run Git in the repository and return decoded stdout."""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return completed.stdout
    if allow_no_matches and completed.returncode == 1:
        return ""
    raise RuntimeError(completed.stderr.strip() or f"git {' '.join(arguments)} failed")


def is_reserved_example_host(host: str | None) -> bool:
    """Return whether a hostname is reserved for documentation or testing."""
    if host is None:
        return False
    normalized = host.casefold().rstrip(".")
    return normalized in RESERVED_HOSTS or normalized.endswith(RESERVED_SUFFIXES)


def has_sensitive_userinfo(text: str) -> bool:
    """Return whether text contains a credential-bearing URL that is not an example."""
    for match in URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(".,;!?)]}")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            authority = candidate.split("://", 1)[-1].split("/", 1)[0]
            if "@" in authority:
                return True
            continue
        if parsed.username is None and parsed.password is None:
            continue
        if not is_reserved_example_host(parsed.hostname):
            return True
    return False


def event_range(event_path: Path) -> tuple[str | None, str | None]:
    """Extract the before/after or pull-request base/head range from a GitHub event."""
    payload: dict[str, Any] = json.loads(event_path.read_text(encoding="utf-8"))
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        base = pull_request.get("base")
        head = pull_request.get("head")
        if isinstance(base, dict) and isinstance(head, dict):
            base_sha = base.get("sha")
            head_sha = head.get("sha")
            if isinstance(base_sha, str) and isinstance(head_sha, str):
                return base_sha, head_sha

    before = payload.get("before")
    after = payload.get("after")
    if isinstance(before, str) and isinstance(after, str):
        return before, after
    return None, None


def commit_snapshots(*, base: str | None, head: str | None, all_history: bool) -> tuple[str, ...]:
    """Return commit snapshots that must be checked."""
    if all_history:
        revisions = run_git("rev-list", "--all").splitlines()
    elif head is not None and base == ZERO_SHA:
        revisions = run_git("rev-list", head).splitlines()
    elif base is not None and head is not None:
        revisions = run_git("rev-list", "--reverse", f"{base}..{head}").splitlines()
    else:
        revisions = [head or "HEAD"]

    if not revisions:
        revisions = [head or "HEAD"]
    return tuple(dict.fromkeys(revisions))


def findings_for_revision(revision: str) -> Iterable[tuple[str, int]]:
    """Yield redacted locations containing a credential-bearing HTTP URL."""
    output = run_git(
        "grep",
        "-I",
        "-n",
        "-e",
        "http://",
        "-e",
        "https://",
        revision,
        "--",
        allow_no_matches=True,
    )
    for row in output.splitlines():
        try:
            _revision, path, line_number, text = row.split(":", 3)
        except ValueError:
            continue
        if has_sensitive_userinfo(text):
            yield path, int(line_number)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="first parent excluded from the scan")
    parser.add_argument("--head", help="last commit included in the scan")
    parser.add_argument(
        "--all-history",
        action="store_true",
        help="scan every commit reachable from local refs",
    )
    parser.add_argument(
        "--github-event",
        type=Path,
        help="derive the commit range from a GitHub Actions event payload",
    )
    return parser.parse_args()


def main() -> int:
    """Scan selected commit snapshots without printing credential values."""
    args = parse_args()
    base = args.base
    head = args.head
    all_history = bool(args.all_history)

    if args.github_event is not None:
        base, head = event_range(args.github_event)
        if os.environ.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"}:
            all_history = True

    revisions = commit_snapshots(base=base, head=head, all_history=all_history)
    findings: set[tuple[str, int]] = set()
    for revision in revisions:
        findings.update(findings_for_revision(revision))

    if findings:
        print(
            "Credential-bearing HTTP URLs outside reserved example domains were found. "
            "Values are intentionally redacted:"
        )
        for path, line_number in sorted(findings)[:20]:
            print(f"- {path}:{line_number}")
        if len(findings) > 20:
            print(f"- ... and {len(findings) - 20} more locations")
        return 1

    print(
        f"Scanned {len(revisions)} commit snapshot(s); "
        "no non-example credential-bearing HTTP URLs found."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
