"""Canonical environment-variable access with legacy compatibility.

New integrations must publish ``ANALOGSKILLS_*`` variables.  The historical
``SKILLS_Z_*`` spelling remains readable so existing run scripts keep working
during migration.  When both are present, the canonical value wins.
"""
from __future__ import annotations

import os
from typing import Mapping


CANONICAL_ENV_PREFIX = "ANALOGSKILLS_"
LEGACY_ENV_PREFIX = "SKILLS_Z_"


def get_env(
    suffix: str,
    default: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    values = os.environ if environ is None else environ
    normalized = str(suffix).strip().upper()
    canonical = f"{CANONICAL_ENV_PREFIX}{normalized}"
    legacy = f"{LEGACY_ENV_PREFIX}{normalized}"
    if canonical in values:
        return values[canonical]
    if legacy in values:
        return values[legacy]
    return default


def has_env(suffix: str, *, environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    normalized = str(suffix).strip().upper()
    return f"{CANONICAL_ENV_PREFIX}{normalized}" in values or f"{LEGACY_ENV_PREFIX}{normalized}" in values


def selected_env_name(suffix: str, *, environ: Mapping[str, str] | None = None) -> str:
    """Return the authoritative key, preferring the canonical spelling."""

    values = os.environ if environ is None else environ
    normalized = str(suffix).strip().upper()
    canonical = f"{CANONICAL_ENV_PREFIX}{normalized}"
    legacy = f"{LEGACY_ENV_PREFIX}{normalized}"
    if canonical in values or legacy not in values:
        return canonical
    return legacy


def canonical_env_view(values: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy where legacy variables are mirrored under canonical keys."""

    source = dict(os.environ if values is None else values)
    result = dict(source)
    for key, value in source.items():
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue
        canonical = CANONICAL_ENV_PREFIX + key[len(LEGACY_ENV_PREFIX) :]
        result.setdefault(canonical, value)
    return result
