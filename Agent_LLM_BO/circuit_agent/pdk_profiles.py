"""Central PDK profile registry used by topology generators and exporters."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).parent / ".env")

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    process_sections: dict[str, str]
    vdd: float
    vdd_min: float
    vdd_max: float
    pvt_temperatures_c: tuple[float, ...]
    min_l: float
    max_width_per_finger: float
    min_width_per_finger: float
    gmid_table_path: str
    spectre_options: tuple[str, ...]
    virtuoso_tech_lib: str
    virtuoso_pdk_lib_path: str
    topology_presets: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        data = asdict(self)
        data["spectre_options"] = list(self.spectre_options)
        data["pvt_temperatures_c"] = list(self.pvt_temperatures_c)
        return data

    @property
    def model_names(self) -> dict[str, str]:
        """Return the standard model-role mapping for this profile."""
        return {
            "nmos": self.nmos_model,
            "pmos": self.pmos_model,
            "nmos_lvt": self.nmos_lvt_model,
            "pmos_lvt": self.pmos_lvt_model,
        }


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
        process_sections={
            "tt": "top_tt",
            "ss": "top_ss",
            "ff": "top_ff",
        },
        vdd=0.9,
        vdd_min=0.9,
        vdd_max=1.1,
        pvt_temperatures_c=(-40.0, 27.0, 125.0),
        min_l=120e-9,
        max_width_per_finger=2.6e-6,
        min_width_per_finger=0.2e-6,
        gmid_table_path=str(
            REPO_ROOT / "gmid_lookup_table" / "gm_id_tables_tsmc28.json"
        ),
        spectre_options=("rawfmt=psfascii", "soft_bin=allmodels"),
        virtuoso_tech_lib="tsmcN28",
        virtuoso_pdk_lib_path="/PDKS/TSMC28nm/tsmcN28",
        topology_presets={
            "5t_ota": {
                "default_params": {
                    "Wtail": 3e-6,
                    "Ltail": 200e-9,
                    "Wdp": 5e-6,
                    "Ldp": 130e-9,
                    "Wcm": 8e-6,
                    "Lcm": 130e-9,
                    "VBIAS": 0.35,
                },
                "testbench_defaults": {
                    "VCM": 0.15,
                    "CL": 500e-15,
                    "VBIAS": 0.35,
                },
            },
            "two_stage_ota": {
                "default_params": {
                    "Wtail": 5e-6,
                    "Ltail": 200e-9,
                    "Wdiff": 10e-6,
                    "Ldiff": 60e-9,
                    "Wmirr": 5e-6,
                    "Lmirr": 100e-9,
                    "Wcs": 20e-6,
                    "Wload": 10e-6,
                    "Lload": 200e-9,
                    "Cc": 500e-15,
                    "Rz": 1000.0,
                },
                "testbench_defaults": {
                    "VCM": 0.7,
                    "VBIAS": 0.55,
                    "CL": 2e-12,
                },
            },
            "folded_cascode_two_stage": {
                "default_params": {
                    "Wdiffp": 12e-6,
                    "Ldiffp": 80e-9,
                    "Wcs": 30e-6,
                    "m_half_unit": 2,
                    "m_load_ratio": 2,
                    "Lbias": 400e-9,
                    "Wbp_big": 4.8e-6,
                    "nf_Wbp_big": 4,
                    "m_Wbp_big": 1,
                    "Wbn_big": 4.8e-6,
                    "nf_Wbn_big": 4,
                    "m_Wbn_big": 1,
                    "Cc": 250e-15,
                    "Rz": 1000.0,
                },
                "testbench_defaults": {
                    "VCM": 0.4,
                    "IBIAS": 20e-6,
                    "CL": 1e-12,
                },
            },
            "nmcf_three_stage": {
                "default_params": {
                    "Wtail1": 18e-6,
                    "Ltail1": 200e-9,
                    "Wdiff1": 10e-6,
                    "Ldiff1": 80e-9,
                    "Wload1": 10e-6,
                    "Lload1": 100e-9,
                    "Wgm2": 14e-6,
                    "Lgm2": 80e-9,
                    "Wload2": 16e-6,
                    "Lload2": 120e-9,
                    "Wgm3": 24e-6,
                    "Lgm3": 100e-9,
                    "Wload3": 12e-6,
                    "Lload3": 180e-9,
                    "Wbiasn": 4e-6,
                    "Lbiasn": 200e-9,
                    "Wbiasp": 8e-6,
                    "Lbiasp": 200e-9,
                    "Cc1": 800e-15,
                    "Rz1": 1000.0,
                    "Cc2": 500e-15,
                },
                "testbench_defaults": {
                    "VCM": 0.3,
                    "IBIAS": 40e-6,
                    "CL": 10e-12,
                },
            },
        },
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

    external_file = os.getenv("PDK_PROFILE_FILE")
    if external_file and not name:
        return _apply_env_overrides(_load_external_profile(external_file))

    selected = name or os.getenv("CIRCUIT_AGENT_PDK") or os.getenv("PDK_PROFILE")
    selected = (selected or "tsmc28").strip()
    selected_path = Path(selected).expanduser()
    if selected_path.suffix.lower() in {".json"} and selected_path.exists():
        return _apply_env_overrides(_load_external_profile(selected_path))
    try:
        return _apply_env_overrides(PDK_PROFILES[selected])
    except KeyError as exc:
        known = ", ".join(sorted(PDK_PROFILES))
        raise ValueError(f"Unknown PDK profile '{selected}'. Known profiles: {known}") from exc


def spectre_include_line(profile: PDKProfile | None = None) -> str:
    """Render the Spectre model include line for a profile."""

    pdk = profile or get_pdk_profile()
    return (
        f'include "{_normalize_model_path(pdk.spectre_model_path)}" '
        f"section={pdk.spectre_section}"
    )


def get_topology_preset(
    topology_name: str,
    profile: PDKProfile | None = None,
) -> dict[str, object]:
    """Return the active PDK preset for one topology.

    The returned dict is a shallow copy with normalized section keys:
    ``default_params``, ``param_space_overrides``, and ``testbench_defaults``.
    Missing presets return empty sections.
    """
    pdk = profile or get_pdk_profile()
    raw = dict(pdk.topology_presets.get(topology_name, {}) or {})
    return {
        "default_params": dict(raw.get("default_params") or {}),
        "param_space_overrides": dict(raw.get("param_space_overrides") or {}),
        "testbench_defaults": dict(raw.get("testbench_defaults") or {}),
    }


def apply_topology_preset(
    topology_name: str,
    base_defaults: dict[str, float],
    profile: PDKProfile | None = None,
) -> dict[str, float]:
    """Overlay a topology's PDK-specific default params on base defaults."""
    merged = dict(base_defaults)
    merged.update(get_topology_preset(topology_name, profile)["default_params"])
    return merged


def get_param_override(
    topology_name: str,
    param_name: str,
    profile: PDKProfile | None = None,
) -> dict[str, object]:
    """Return a PDK-specific search-space override for one parameter."""
    overrides = get_topology_preset(topology_name, profile)["param_space_overrides"]
    return dict(overrides.get(param_name, {}) or {})


def get_testbench_defaults(
    topology_name: str,
    base_defaults: dict[str, float],
    profile: PDKProfile | None = None,
) -> dict[str, float]:
    """Overlay PDK-specific testbench defaults on topology defaults."""
    merged = dict(base_defaults)
    merged.update(get_topology_preset(topology_name, profile)["testbench_defaults"])
    return merged


def validate_pdk_profile(
    profile: PDKProfile | None = None,
    *,
    check_files: bool = False,
    require_gmid: bool = False,
    require_virtuoso: bool = False,
    required_model_roles: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Return validation errors for a PDK profile.

    ``check_files`` is intentionally optional because many development
    machines do not mount the real PDK.  Use it in the Cadence VM before
    launching real Spectre/Virtuoso jobs.
    """
    pdk = profile or get_pdk_profile()
    errors: list[str] = []

    if not pdk.name:
        errors.append("profile name is empty")
    if not pdk.spectre_model_path:
        errors.append("spectre_model_path is empty")
    if not pdk.spectre_section:
        errors.append("spectre_section is empty")
    if not pdk.process_sections:
        errors.append("process_sections is empty")
    for corner in ("tt", "ss", "ff"):
        if corner not in pdk.process_sections:
            errors.append(f"process_sections missing '{corner}'")
    if pdk.vdd <= 0 or pdk.vdd_min <= 0 or pdk.vdd_max <= 0:
        errors.append("VDD values must be positive")
    if not (pdk.vdd_min <= pdk.vdd <= pdk.vdd_max):
        errors.append(
            f"vdd={pdk.vdd:g} is outside [{pdk.vdd_min:g}, {pdk.vdd_max:g}]"
        )
    if pdk.min_l <= 0:
        errors.append("min_l must be positive")
    if not pdk.pvt_temperatures_c:
        errors.append("pvt_temperatures_c is empty")
    if pdk.min_width_per_finger <= 0 or pdk.max_width_per_finger <= 0:
        errors.append("finger width limits must be positive")
    if pdk.min_width_per_finger > pdk.max_width_per_finger:
        errors.append("min_width_per_finger exceeds max_width_per_finger")

    model_roles = pdk.model_names
    for role in required_model_roles or ():
        if role not in model_roles:
            errors.append(f"unknown required model role '{role}'")
        elif not model_roles[role]:
            errors.append(f"model role '{role}' is empty")

    if require_gmid or pdk.gmid_table_path:
        gmid_path = Path(pdk.gmid_table_path).expanduser()
        if not pdk.gmid_table_path:
            errors.append("gmid_table_path is empty")
        elif not gmid_path.exists():
            errors.append(f"gm/Id table not found: {gmid_path}")
        elif require_gmid:
            errors.extend(
                _validate_gmid_models(
                    gmid_path,
                    model_roles,
                    required_roles=required_model_roles,
                )
            )

    if require_virtuoso:
        if not pdk.virtuoso_tech_lib:
            errors.append("virtuoso_tech_lib is empty")
        if not pdk.virtuoso_pdk_lib_path:
            errors.append("virtuoso_pdk_lib_path is empty")

    errors.extend(_validate_topology_presets(pdk))

    if check_files:
        for label, path_value in (
            ("Spectre model", pdk.spectre_model_path),
            ("HSPICE model", pdk.hspice_model_path),
        ):
            if path_value and not Path(path_value).expanduser().exists():
                errors.append(f"{label} file not found: {path_value}")
        if require_virtuoso and pdk.virtuoso_pdk_lib_path:
            if not Path(pdk.virtuoso_pdk_lib_path).expanduser().exists():
                errors.append(
                    f"Virtuoso PDK library path not found: {pdk.virtuoso_pdk_lib_path}"
                )

    return errors


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
        "gmid_table_path": "GMID_TABLE_PATH",
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
    process_sections = os.getenv("PDK_PROCESS_SECTIONS")
    if process_sections:
        parsed_sections: dict[str, str] = {}
        for item in process_sections.split(","):
            if not item.strip():
                continue
            if ":" not in item:
                raise ValueError(
                    "PDK_PROCESS_SECTIONS entries must use name:section format"
                )
            name, section = item.split(":", 1)
            parsed_sections[name.strip()] = section.strip()
        if parsed_sections:
            updates["process_sections"] = parsed_sections
    spectre_options = os.getenv("PDK_SPECTRE_OPTIONS")
    if spectre_options:
        updates["spectre_options"] = tuple(
            item.strip()
            for item in spectre_options.replace(";", ",").split(",")
            if item.strip()
        )
    pvt_temperatures = os.getenv("PDK_PVT_TEMPERATURES")
    if pvt_temperatures:
        updates["pvt_temperatures_c"] = tuple(
            float(item.strip())
            for item in pvt_temperatures.replace(";", ",").split(",")
            if item.strip()
        )
    for field_name, env_name in float_overrides.items():
        value = os.getenv(env_name)
        if value:
            updates[field_name] = float(value)
    if not updates:
        return profile
    return replace(profile, **updates)


def _load_external_profile(path: str | Path) -> PDKProfile:
    """Load a profile from a JSON file.

    The file may contain either a single profile object or a mapping of profile
    names to profile objects.  YAML is intentionally not parsed here to avoid a
    new runtime dependency; convert YAML to JSON or register the profile in
    this module.
    """
    path = Path(path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    if "name" not in data:
        selected = os.getenv("CIRCUIT_AGENT_PDK") or os.getenv("PDK_PROFILE")
        if selected and selected in data:
            data = data[selected]
        elif len(data) == 1:
            data = next(iter(data.values()))
        else:
            raise ValueError(
                f"External PDK profile file {path} contains multiple profiles; "
                "set CIRCUIT_AGENT_PDK or PDK_PROFILE to choose one"
            )
    return _coerce_profile(data)


def _normalize_model_path(path_value: str) -> str:
    """Normalize common absolute PDK paths while preserving true relatives."""
    path_value = str(path_value).strip()
    if path_value.startswith("PDKS/"):
        return "/" + path_value
    if path_value.startswith("~"):
        return str(Path(path_value).expanduser())
    return path_value


def _coerce_profile(data: dict[str, object]) -> PDKProfile:
    values = dict(data)
    values["process_sections"] = dict(values.get("process_sections") or {})
    values["topology_presets"] = dict(values.get("topology_presets") or {})
    temps = values.get("pvt_temperatures_c", (-40.0, 27.0, 125.0))
    values["pvt_temperatures_c"] = tuple(float(temp) for temp in temps)
    options = values.get("spectre_options", ())
    if isinstance(options, str):
        options = tuple(
            item.strip()
            for item in options.replace(";", ",").split(",")
            if item.strip()
        )
    else:
        options = tuple(options)
    values["spectre_options"] = options
    return PDKProfile(**values)


def _validate_topology_presets(profile: PDKProfile) -> list[str]:
    """Validate topology preset names and parameter names when possible."""
    errors: list[str] = []
    if not profile.topology_presets:
        return errors
    try:
        from topologies import get_topology
    except Exception:
        return errors

    for topology_name, preset in profile.topology_presets.items():
        try:
            topology = get_topology(topology_name)
        except ValueError:
            errors.append(f"topology_presets contains unknown topology '{topology_name}'")
            continue

        if not isinstance(preset, dict):
            errors.append(f"topology preset '{topology_name}' must be an object")
            continue

        allowed_default = set(getattr(topology, "DEFAULT_PARAMS", {}))
        default_params = dict(preset.get("default_params") or {})
        for param_name in default_params:
            if param_name not in allowed_default:
                errors.append(
                    f"topology preset '{topology_name}' default_params has "
                    f"unknown parameter '{param_name}'"
                )

        try:
            allowed_space = {param.name for param in topology.get_param_space().params}
            gmid_spec = topology.get_gmid_spec()
            if gmid_spec is not None:
                allowed_space.update(param.name for param in gmid_spec.build_param_space().params)
        except Exception:
            allowed_space = allowed_default
        overrides = dict(preset.get("param_space_overrides") or {})
        for param_name in overrides:
            if param_name not in allowed_space:
                errors.append(
                    f"topology preset '{topology_name}' param_space_overrides "
                    f"has unknown parameter '{param_name}'"
                )

        allowed_tb = {"VCM", "CL", "IBIAS", "VBIAS", "VLOW", "VHIGH"}
        tb_defaults = dict(preset.get("testbench_defaults") or {})
        for param_name in tb_defaults:
            if param_name not in allowed_tb:
                errors.append(
                    f"topology preset '{topology_name}' testbench_defaults has "
                    f"unknown parameter '{param_name}'"
                )

    return errors


def _validate_gmid_models(
    gmid_path: Path,
    model_roles: dict[str, str],
    required_roles: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        raw = json.loads(gmid_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"cannot read gm/Id table {gmid_path}: {exc}"]
    available = set(raw)
    roles_to_check = required_roles or tuple(model_roles)
    for role in roles_to_check:
        model = model_roles.get(role, "")
        if model and model not in available:
            errors.append(
                f"gm/Id table missing model for role {role}: {model}"
            )
    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or validate PDK profiles")
    parser.add_argument("profile", nargs="?", help="Profile name or JSON path")
    parser.add_argument("--validate", action="store_true", help="Validate profile")
    parser.add_argument(
        "--check-files",
        action="store_true",
        help="Also require referenced local PDK/model files to exist",
    )
    parser.add_argument(
        "--require-gmid",
        action="store_true",
        help="Require gm/Id table existence and model coverage",
    )
    parser.add_argument(
        "--require-virtuoso",
        action="store_true",
        help="Require Virtuoso tech library settings",
    )
    parser.add_argument("--json", action="store_true", help="Print profile JSON")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    profile = get_pdk_profile(args.profile)
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
    if args.validate:
        errors = validate_pdk_profile(
            profile,
            check_files=args.check_files,
            require_gmid=args.require_gmid,
            require_virtuoso=args.require_virtuoso,
        )
        if errors:
            print(f"PDK profile '{profile.name}' is invalid:")
            for error in errors:
                print(f"  - {error}")
            raise SystemExit(1)
        print(f"PDK profile '{profile.name}' is valid")
    if not args.json and not args.validate:
        print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
