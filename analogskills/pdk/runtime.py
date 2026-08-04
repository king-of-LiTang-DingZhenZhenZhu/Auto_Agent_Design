"""Runtime resolution of process collateral and EDA executables."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from analogskills.env import get_env

from .config import PdkConfig
from .profile import PdkProfile, ProcessNode, resolve_pdk_profile


def resolve_tool_binary(tool: str, *, default: str | None = None) -> str:
    name = str(tool).strip().upper()
    return str(get_env(f"{name}_BINARY", default or tool.lower()) or default or tool.lower())


def resolve_spectre_model_path(
    pdk: PdkProfile | PdkConfig | ProcessNode | str | Path = ProcessNode.N28,
    *,
    project_roots: Iterable[str | Path] = (),
) -> Path:
    """Resolve a Spectre model include without embedding a machine path."""

    profile = resolve_pdk_profile(pdk)
    key = profile.key.upper().replace("-", "_")
    explicit = get_env(f"{key}_SPECTRE_MODEL") or get_env("SPECTRE_MODEL")
    if explicit:
        return Path(explicit).expanduser()

    binding = profile.tool_binding("spectre")
    candidates: list[Path] = []
    if binding.path:
        candidates.append(Path(binding.path).expanduser())
    roots = [Path(item).expanduser() for item in project_roots]
    roots.extend((Path.cwd(), Path.cwd().parent))
    remote_root = get_env("REMOTE_PROJECT_ROOT")
    if remote_root:
        roots.append(Path(remote_root).expanduser())
    if profile.node is ProcessNode.N28:
        relative = Path("iPDK_t28") / "CRN28HPCp" / "models" / "spectre" / "toplevel_1d8.scs"
        candidates.extend(root / relative for root in roots)
    existing = next((item for item in candidates if item.exists()), None)
    if existing is not None:
        return existing
    if candidates:
        return candidates[0]
    raise RuntimeError(
        f"no Spectre model path configured for {profile.key}; set "
        f"ANALOGSKILLS_{key}_SPECTRE_MODEL or ANALOGSKILLS_SPECTRE_MODEL"
    )
