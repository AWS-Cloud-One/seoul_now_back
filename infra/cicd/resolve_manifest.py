"""Resolve exactly one manifest commit from an ECR image's whitespace-separated tags.

A digest can have several git-<sha> tags. Never pick a configuration from their
listing order: refuse an ambiguous image before uploading a bundle or deploying.
"""

import re
import sys


def manifest_commit(tags):
    commits = {
        match.group(1) for tag in tags if (match := re.fullmatch(r"git-([0-9a-f]{40})", tag))
    }
    if not commits:
        raise ValueError(
            "image has no git-<40-character sha> tag; use the manual recovery procedure"
        )
    if len(commits) != 1:
        raise ValueError(
            "image has multiple git-<sha> tags; its manifest commit is ambiguous. "
            "Use a uniquely tagged image or explicitly verified manual recovery"
        )
    return commits.pop()


def main():
    try:
        commit = manifest_commit(sys.stdin.read().split())
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
