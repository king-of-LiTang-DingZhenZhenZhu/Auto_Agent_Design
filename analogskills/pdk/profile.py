"""Stable technology profiles above node-specific PDK JSON files.

The profile is deliberately a capability contract.  Loading a PCell template
does not imply that its geometry has been calibrated, and having a Calibre
adapter does not imply that a foundry deck was configured for a given node.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import PCellTemplate, PdkConfig


class ProcessNode(str, Enum):
    GENERIC = "generic"
    N28 = "28nm"
    N7 = "7nm"


class PdkCapability(str, Enum):
    LAYOUT = "layout"
    ROUTING = "routing"
    PCELL_TEMPLATES = "pcell_templates"
    PCELL_CALIBRATION = "pcell_calibration"
    PCELL_REALIZATIONS = "pcell_realizations"
    SHARED_DIFFUSION = "shared_diffusion"
    SPECTRE_MODELS = "spectre_models"
    CALIBRE_DRC = "calibre_drc"
    CALIBRE_LVS = "calibre_lvs"
    CALIBRE_PEX = "calibre_pex"


@dataclass(frozen=True)
class PdkToolBinding:
    """Node-specific collateral needed by one EDA stage."""

    stage: str
    configured: bool = False
    path: str = ""
    policy: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self, *, check_paths: bool = False) -> tuple[str, ...]:
        issues: list[str] = []
        if not self.configured:
            issues.append(f"{self.stage}: PDK collateral is not configured")
        if self.configured and self.stage in {"drc", "lvs", "pex", "spectre"} and not self.path:
            issues.append(f"{self.stage}: configured binding has no path")
        if check_paths and self.path and not Path(self.path).exists():
            issues.append(f"{self.stage}: configured path is unavailable: {self.path}")
        return tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "configured": self.configured,
            "path": self.path,
            "policy": self.policy,
            "environment": dict(self.environment),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PdkProfile:
    """One selectable process implementation of the technology interface."""

    key: str
    node: ProcessNode
    vendor: str
    config_path: Path
    config: PdkConfig
    aliases: tuple[str, ...] = ()
    capabilities: frozenset[PdkCapability] = frozenset()
    tool_bindings: Mapping[str, PdkToolBinding] = field(default_factory=dict)

    @property
    def node_nm(self) -> int:
        if self.node is ProcessNode.N28:
            return 28
        if self.node is ProcessNode.N7:
            return 7
        return 0

    def supports(self, capability: PdkCapability | str) -> bool:
        return PdkCapability(capability) in self.capabilities

    def require(self, capability: PdkCapability | str) -> None:
        required = PdkCapability(capability)
        if required not in self.capabilities:
            raise RuntimeError(f"PDK {self.key!r} does not provide {required.value}")

    def pcell_template(self, logical_name: str) -> PCellTemplate:
        return self.config.pcell_template_for(logical_name)

    def tool_binding(self, stage: str) -> PdkToolBinding:
        key = str(stage).lower()
        return self.tool_bindings.get(key, PdkToolBinding(stage=key))

    def validate_interface(self, *, check_paths: bool = False) -> tuple[str, ...]:
        issues = list(self.config.validate())
        required = {"nmos", "pmos", "bjt", "resistor", "capacitor"}
        missing = required.difference(self.config.pcell_templates)
        issues.extend(f"missing PCell template: {name}" for name in sorted(missing))
        for stage, binding in self.tool_bindings.items():
            if binding.configured:
                issues.extend(binding.validate(check_paths=check_paths))
        return tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "node": self.node.value,
            "node_nm": self.node_nm,
            "vendor": self.vendor,
            "config_path": str(self.config_path),
            "aliases": list(self.aliases),
            "capabilities": sorted(item.value for item in self.capabilities),
            "tool_bindings": {key: value.to_dict() for key, value in self.tool_bindings.items()},
        }


class PdkRegistry:
    def __init__(self, profiles: Iterable[PdkProfile] = ()) -> None:
        self._profiles: dict[str, PdkProfile] = {}
        self._aliases: dict[str, str] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: PdkProfile) -> None:
        key = _normalise(profile.key)
        if key in self._profiles:
            raise ValueError(f"duplicate PDK profile {profile.key!r}")
        self._profiles[key] = profile
        for alias in (profile.key, profile.node.value, *profile.aliases):
            normalised = _normalise(alias)
            owner = self._aliases.get(normalised)
            if owner is not None and owner != key:
                raise ValueError(f"PDK alias {alias!r} is already registered by {owner!r}")
            self._aliases[normalised] = key

    def get(self, name: str | ProcessNode) -> PdkProfile:
        lookup = _normalise(name.value if isinstance(name, ProcessNode) else name)
        key = self._aliases.get(lookup, lookup)
        try:
            return self._profiles[key]
        except KeyError as exc:
            raise KeyError(f"unknown PDK {name!r}; available: {', '.join(self.names())}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(profile.key for profile in self._profiles.values()))

    def profiles(self) -> tuple[PdkProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda item: item.node_nm, reverse=True))


def builtin_pdk_registry() -> PdkRegistry:
    root = Path(__file__).resolve().parents[1] / "pdk_data"
    return PdkRegistry(
        (
            _build_profile(
                key="crn28hpcp",
                node=ProcessNode.N28,
                vendor="TSMC-compatible research PDK",
                path=root / "crn28hpcp.json",
                aliases=("28", "n28", "t28", "crn28", "28nm"),
            ),
            _build_profile(
                key="tsmcn7",
                node=ProcessNode.N7,
                vendor="TSMC",
                path=root / "tsmcn7.json",
                aliases=("7", "n7", "tsmc7", "7nm"),
            ),
        )
    )


def load_pdk_profile(name: str | ProcessNode) -> PdkProfile:
    return builtin_pdk_registry().get(name)


def resolve_pdk_profile(
    value: PdkProfile | PdkConfig | ProcessNode | str | Path | None = None,
    *,
    default: str | ProcessNode = ProcessNode.N28,
) -> PdkProfile:
    """Resolve a built-in alias, JSON path, config object, or profile."""

    if value is None:
        return load_pdk_profile(default)
    if isinstance(value, PdkProfile):
        return value
    if isinstance(value, PdkConfig):
        return _profile_from_config(value, config_path=Path("<memory>"))
    if isinstance(value, ProcessNode):
        return load_pdk_profile(value)
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return _profile_from_config(PdkConfig.load_json(candidate), config_path=candidate.resolve())
    return load_pdk_profile(str(value))


def resolve_pdk_config(
    value: PdkProfile | PdkConfig | ProcessNode | str | Path | None = None,
    *,
    default: str | ProcessNode = ProcessNode.N28,
) -> PdkConfig:
    return resolve_pdk_profile(value, default=default).config


def _build_profile(*, key: str, node: ProcessNode, vendor: str, path: Path, aliases: tuple[str, ...]) -> PdkProfile:
    config = PdkConfig.load_json(path)
    return _profile_from_config(
        config,
        config_path=path,
        key=key,
        node=node,
        vendor=vendor,
        aliases=aliases,
    )


def _profile_from_config(
    config: PdkConfig,
    *,
    config_path: Path,
    key: str | None = None,
    node: ProcessNode | None = None,
    vendor: str | None = None,
    aliases: tuple[str, ...] = (),
) -> PdkProfile:
    metadata = config.metadata
    resolved_node = node or _infer_process_node(config)
    resolved_key = str(key or config.name)
    resolved_vendor = str(vendor or metadata.get("vendor") or metadata.get("source") or "custom")
    calibre = _mapping(metadata.get("calibre"))
    bindings = {
        "drc": _calibre_binding("drc", _mapping(calibre.get("generated_drc_run"))),
        "lvs": _calibre_binding("lvs", _mapping(calibre.get("generated_lvs_run"))),
        "pex": _calibre_binding("pex", _mapping(calibre.get("generated_pex_run"))),
        "spectre": _spectre_binding(config),
        "virtuoso": PdkToolBinding(
            stage="virtuoso",
            configured=bool(config.pcell_templates),
            policy="native_oa_pcell",
            metadata=_mapping(metadata.get("oa")),
        ),
    }
    capabilities = {
        PdkCapability.LAYOUT,
        PdkCapability.ROUTING,
        PdkCapability.PCELL_TEMPLATES,
        PdkCapability.PCELL_CALIBRATION,
    }
    if _mapping(metadata.get("pcell_realization")):
        capabilities.add(PdkCapability.PCELL_REALIZATIONS)
    if _mapping(metadata.get("shared_diffusion_realization")):
        capabilities.add(PdkCapability.SHARED_DIFFUSION)
    if bindings["spectre"].configured:
        capabilities.add(PdkCapability.SPECTRE_MODELS)
    if bindings["drc"].configured:
        capabilities.add(PdkCapability.CALIBRE_DRC)
    if bindings["lvs"].configured:
        capabilities.add(PdkCapability.CALIBRE_LVS)
    if bindings["pex"].configured:
        capabilities.add(PdkCapability.CALIBRE_PEX)
    return PdkProfile(
        resolved_key,
        resolved_node,
        resolved_vendor,
        config_path,
        config,
        aliases,
        frozenset(capabilities),
        bindings,
    )


def _infer_process_node(config: PdkConfig) -> ProcessNode:
    token = _normalise(str(config.metadata.get("node", config.name)))
    if token in {"7", "7nm", "n7", "tsmcn7"} or "7nm" in token:
        return ProcessNode.N7
    if token in {"28", "28nm", "n28", "crn28", "crn28hpcp"} or "28" in token:
        return ProcessNode.N28
    return ProcessNode.GENERIC


def _calibre_binding(stage: str, data: Mapping[str, Any]) -> PdkToolBinding:
    path = str(data.get("deck_template_path", data.get("rule_deck", "")))
    return PdkToolBinding(
        stage=stage,
        configured=bool(path),
        path=path,
        policy=str(data.get("policy", "")),
        metadata=dict(data),
    )


def _spectre_binding(config: PdkConfig) -> PdkToolBinding:
    paths = [lib.path for preset in config.signoff_presets.values() for lib in preset.model_libraries if lib.path]
    metadata = _mapping(config.metadata.get("spectre"))
    configured_path = str(metadata.get("model_library", metadata.get("model_path", paths[0] if paths else "")))
    return PdkToolBinding(
        stage="spectre",
        configured=bool(configured_path or config.signoff_presets),
        path=configured_path,
        policy="pdk_signoff_preset" if config.signoff_presets else "",
        metadata=dict(metadata),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalise(value: str) -> str:
    return str(value).strip().lower().replace("_", "").replace("-", "")
