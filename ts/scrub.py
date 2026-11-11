"""Keep the debugging side looking at a network, not at how the network is hosted.

Everything that reaches a stored run, an email or a language model passes through here.
"""

from __future__ import annotations

import re

_RULES = [
    (re.compile(r"clab-lab1-"), ""),
    (re.compile(r"\bcEOS-?Lab\b", re.I), "EOS"),
    (re.compile(r"\bcEOS\b", re.I), "EOS"),
    (re.compile(r"\blab1\.clab\.ya?ml\b"), "the cabling inventory"),
    (re.compile(r"\bcontainer\s?lab\w*\b", re.I), "the network"),
    (re.compile(r"\bclab\b", re.I), "network"),
    (re.compile(r"\bdocker\s+(?:exec|inspect|ps|logs)\b[^\n]*", re.I), "(console session)"),
    (re.compile(r"\benvironment=\"?lab\d\"?"), ""),
    (re.compile(r"^#.*\bDocker Engine\b.*\n?", re.I | re.M), ""),
]


def text(value: str) -> str:
    for pattern, replacement in _RULES:
        value = pattern.sub(replacement, value)
    return value


def deep(value):
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        return {k: deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [deep(v) for v in value]
    return value
