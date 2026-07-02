"""Central PDK profile registry used by topology generators and exporters."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).parent / ".env")


@dataclass(frozen=True)
class PDKProfile:
    """Process-specific paths, model names, and basic design limits."""

    name: str
    spectre_model_path: str
    spectre_section: str
    hspice_model_path: str
    hspice_section: str
    nmos_model: str
    pmos_model: str
    nmos_lvt_model: str
    pmos_lvt_model: str
    vdd: float
    vdd_min: float
    vdd_max: float
    min_l: float
    max_width_per_finger: float
    min_width_per_finger: float
    virtuoso_tech_lib: str
    virtuoso_pdk_lib_path: str


PDK_PROFILES: dict[str, PDKProfile] = {
    "tsmc28": PDKProfile(
        name="tsmc28",
        spectre_model_path="/PDKS/TSMC28nm/models/spectre/toplevel.scs",
        spectre_section="top_tt",
        hspice_model_path="/PDKS/TSMC28nm/models/hspice/toplevel.l",
        hspice_section="TOP_TT",
        nmos_model="nch_mac",
        pmos_model="pch_mac",
        nmos_lvt_model="nch_lvt_mac",
        pmos_lvt_model="pch_lvt_mac",
        vdd=0.9,
        vdd_min=0.9,
        vdd_max=1.1,
        min_l=120e-9,
        max_width_per_finger=2.6e-6,
        min_width_per_finger=0.2e-6,
        virtuoso_tech_lib="tsmcN28",
        virtuoso_pdk_lib_path="/PDKS/TSMC28nm/tsmcN28",
    ),
}


def get_pdk_profile(name: str | None = None) -> PDKProfile:
    """Return a configured PDK profile.

    Selection order:
    1. Explicit ``name`` argument.
    2. ``CIRCUIT_AGENT_PDK`` environment variable.
    3. ``PDK_PROFILE`` environment variable.
    4. Built-in ``tsmc28`` default.
    """

    selected = name or os.getenv("CIRCUIT_AGENT_PDK") or os.getenv("PDK_PROFILE")
    selected = (selected or "tsmc28").strip()
    try:
        return _apply_env_overrides(PDK_PROFILES[selected])
    except KeyError as exc:
        known = ", ".join(sorted(PDK_PROFILES))
        raise ValueError(f"Unknown PDK profile '{selected}'. Known profiles: {known}") from exc


def spectre_include_line(profile: PDKProfile | None = None) -> str:
    """Render the Spectre model include line for a profile."""

    pdk = profile or get_pdk_profile()
    return f'include "{pdk.spectre_model_path}" section={pdk.spectre_section}'


def _apply_env_overrides(profile: PDKProfile) -> PDKProfile:
    """Apply optional per-field environment overrides for local machines."""

    updates: dict[str, object] = {}
    string_overrides = {
        "spectre_model_path": "PDK_SPECTRE_PATH",
        "spectre_section": "PDK_SPECTRE_SECTION",
        "hspice_model_path": "PDK_HSPICE_PATH",
        "hspice_section": "PDK_HSPICE_SECTION",
        "nmos_model": "NMOS_MODEL",
        "pmos_model": "PMOS_MODEL",
        "nmos_lvt_model": "NMOS_LVT_MODEL",
        "pmos_lvt_model": "PMOS_LVT_MODEL",
        "virtuoso_tech_lib": "VIRTUOSO_TECH_LIB",
        "virtuoso_pdk_lib_path": "VIRTUOSO_PDK_LIB_PATH",
    }
    float_overrides = {
        "vdd": "VDD",
        "vdd_min": "VDD_MIN",
        "vdd_max": "VDD_MAX",
        "min_l": "PDK_MIN_L",
        "max_width_per_finger": "PDK_MAX_WIDTH_PER_FINGER",
        "min_width_per_finger": "PDK_MIN_WIDTH_PER_FINGER",
    }
    for field_name, env_name in string_overrides.items():
        value = os.getenv(env_name)
        if value:
            updates[field_name] = value
    for field_name, env_name in float_overrides.items():
        value = os.getenv(env_name)
        if value:
            updates[field_name] = float(value)
    if not updates:
        return profile
    return replace(profile, **updates)
