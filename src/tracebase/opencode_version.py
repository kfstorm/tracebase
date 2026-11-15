"""Strict OpenCode version parsing without a Minor/Patch support ceiling."""

import re

V1_MAJOR = 1
V2_MAJOR = 2
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def supported_major(value: str) -> int:
    if not VERSION.fullmatch(value):
        raise ValueError("Could not parse OpenCode version")
    major = int(value.partition(".")[0])
    if major not in {V1_MAJOR, V2_MAJOR}:
        raise ValueError("Unsupported OpenCode major version")
    return major
