"""Typed, provenance-aware evidence and reproducible run manifests."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from time import time
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4


class EvidenceLevel(str, Enum):
    ANALYTICAL = "analytical"
    DRY_RUN = "dry_run"
    INLINE_DRC = "inline_drc"
    INLINE_LVS = "inline_lvs"
    VIRTUOSO = "virtuoso"
    SPECTRE_PRE_LAYOUT = "spectre_pre_layout"
    CALIBRE_DRC = "calibre_drc"
    CALIBRE_LVS = "calibre_lvs"
    CALIBRE_PEX = "calibre_pex"
    SPECTRE_POST_LAYOUT = "spectre_post_layout"


class ArtifactStatus(str, Enum):
    PLANNED = "planned"
    PRODUCED = "produced"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    name: str
    provenance: EvidenceLevel
    status: ArtifactStatus = ArtifactStatus.PRODUCED
    path: str = ""
    sha256: str = ""
    pdk_key: str = ""
    tool: str = ""
    tool_version: str = ""
    deck_path: str = ""
    lib_name: str = ""
    cell_name: str = ""
    view_name: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)
    parent_artifact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    artifact_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time)

    @property
    def ok(self) -> bool:
        return self.status in {ArtifactStatus.PRODUCED, ArtifactStatus.PASSED, ArtifactStatus.WAIVED}

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        kind: str,
        provenance: EvidenceLevel,
        name: str | None = None,
        status: ArtifactStatus = ArtifactStatus.PRODUCED,
        **kwargs: Any,
    ) -> "ArtifactRef":
        target = Path(path)
        digest = _file_sha256(target) if target.is_file() else ""
        return cls(
            kind=kind,
            name=str(name or target.name),
            provenance=provenance,
            status=status,
            path=str(target),
            sha256=digest,
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["provenance"] = self.provenance.value
        result["status"] = self.status.value
        result["ok"] = self.ok
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            kind=str(data.get("kind", "")),
            name=str(data.get("name", "")),
            provenance=EvidenceLevel(str(data.get("provenance", EvidenceLevel.DRY_RUN.value))),
            status=ArtifactStatus(str(data.get("status", ArtifactStatus.PRODUCED.value))),
            path=str(data.get("path", "")),
            sha256=str(data.get("sha256", "")),
            pdk_key=str(data.get("pdk_key", "")),
            tool=str(data.get("tool", "")),
            tool_version=str(data.get("tool_version", "")),
            deck_path=str(data.get("deck_path", "")),
            lib_name=str(data.get("lib_name", "")),
            cell_name=str(data.get("cell_name", "")),
            view_name=str(data.get("view_name", "")),
            metrics={str(key): float(value) for key, value in dict(data.get("metrics", {})).items()},
            parent_artifact_ids=tuple(str(item) for item in data.get("parent_artifact_ids", ())),
            metadata=dict(data.get("metadata", {})),
            artifact_id=str(data.get("artifact_id", uuid4().hex)),
            created_at=float(data.get("created_at", time())),
        )


@dataclass(frozen=True)
class RunManifest:
    stage: str
    pdk_key: str
    status: ArtifactStatus
    inputs: tuple[ArtifactRef, ...] = ()
    outputs: tuple[ArtifactRef, ...] = ()
    command: tuple[str, ...] = ()
    tool: str = ""
    tool_version: str = ""
    deck_path: str = ""
    parent_run_id: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""
    run_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "analogskills.run_manifest/v1",
            "run_id": self.run_id,
            "stage": self.stage,
            "pdk_key": self.pdk_key,
            "status": self.status.value,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
            "command": list(self.command),
            "tool": self.tool,
            "tool_version": self.tool_version,
            "deck_path": self.deck_path,
            "parent_run_id": self.parent_run_id,
            "parameters": dict(self.parameters),
            "metrics": dict(self.metrics),
            "environment": dict(self.environment),
            "notes": self.notes,
            "created_at": self.created_at,
        }

    def save_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load_json(cls, path: str | Path) -> "RunManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            stage=str(data.get("stage", "")),
            pdk_key=str(data.get("pdk_key", "")),
            status=ArtifactStatus(str(data.get("status", ArtifactStatus.PLANNED.value))),
            inputs=tuple(ArtifactRef.from_dict(item) for item in data.get("inputs", ())),
            outputs=tuple(ArtifactRef.from_dict(item) for item in data.get("outputs", ())),
            command=tuple(str(item) for item in data.get("command", ())),
            tool=str(data.get("tool", "")),
            tool_version=str(data.get("tool_version", "")),
            deck_path=str(data.get("deck_path", "")),
            parent_run_id=str(data.get("parent_run_id", "")),
            parameters=dict(data.get("parameters", {})),
            metrics={str(key): float(value) for key, value in dict(data.get("metrics", {})).items()},
            environment={str(key): str(value) for key, value in dict(data.get("environment", {})).items()},
            notes=str(data.get("notes", "")),
            run_id=str(data.get("run_id", uuid4().hex)),
            created_at=float(data.get("created_at", time())),
        )


@dataclass(frozen=True)
class Checkpoint:
    """Immutable design-state delta used by agent and ECO iterations."""

    name: str
    stage: str
    parent_checkpoint_id: str = ""
    baseline_artifact_id: str = ""
    dsl_patch: Mapping[str, Any] = field(default_factory=dict)
    layout_patch: Mapping[str, Any] = field(default_factory=dict)
    repair_actions: tuple[Mapping[str, Any], ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    accepted: bool = False
    notes: str = ""
    checkpoint_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "analogskills.checkpoint/v1",
            "checkpoint_id": self.checkpoint_id,
            "name": self.name,
            "stage": self.stage,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "baseline_artifact_id": self.baseline_artifact_id,
            "dsl_patch": dict(self.dsl_patch),
            "layout_patch": dict(self.layout_patch),
            "repair_actions": [dict(item) for item in self.repair_actions],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "metrics": dict(self.metrics),
            "accepted": self.accepted,
            "notes": self.notes,
            "created_at": self.created_at,
        }

    def save_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load_json(cls, path: str | Path) -> "Checkpoint":
        return _checkpoint_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class CheckpointJournal:
    """Append-only checkpoint index with explicit parent validation."""

    def __init__(self, checkpoints: Iterable[Checkpoint] = ()) -> None:
        self._items: dict[str, Checkpoint] = {}
        for checkpoint in checkpoints:
            self.append(checkpoint)

    def append(self, checkpoint: Checkpoint) -> None:
        if checkpoint.checkpoint_id in self._items:
            raise ValueError(f"duplicate checkpoint: {checkpoint.checkpoint_id}")
        if checkpoint.parent_checkpoint_id and checkpoint.parent_checkpoint_id not in self._items:
            raise ValueError(f"unknown parent checkpoint: {checkpoint.parent_checkpoint_id}")
        self._items[checkpoint.checkpoint_id] = checkpoint

    def get(self, checkpoint_id: str) -> Checkpoint:
        try:
            return self._items[str(checkpoint_id)]
        except KeyError as exc:
            raise KeyError(f"unknown checkpoint: {checkpoint_id}") from exc

    def ancestry(self, checkpoint_id: str) -> tuple[Checkpoint, ...]:
        rows: list[Checkpoint] = []
        current = self.get(checkpoint_id)
        seen: set[str] = set()
        while True:
            if current.checkpoint_id in seen:
                raise ValueError("checkpoint ancestry contains a cycle")
            seen.add(current.checkpoint_id)
            rows.append(current)
            if not current.parent_checkpoint_id:
                break
            current = self.get(current.parent_checkpoint_id)
        return tuple(rows)

    def rollback_target(self, checkpoint_id: str) -> Checkpoint | None:
        current = self.get(checkpoint_id)
        return self.get(current.parent_checkpoint_id) if current.parent_checkpoint_id else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "analogskills.checkpoint_journal/v1",
            "checkpoints": [item.to_dict() for item in self._items.values()],
        }

    def save_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load_json(cls, path: str | Path) -> "CheckpointJournal":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        checkpoints = tuple(_checkpoint_from_dict(item) for item in data.get("checkpoints", ()))
        return cls(checkpoints)


def _checkpoint_from_dict(data: Mapping[str, Any]) -> Checkpoint:
    return Checkpoint(
        name=str(data.get("name", "")),
        stage=str(data.get("stage", "")),
        parent_checkpoint_id=str(data.get("parent_checkpoint_id", "")),
        baseline_artifact_id=str(data.get("baseline_artifact_id", "")),
        dsl_patch=dict(data.get("dsl_patch", {})),
        layout_patch=dict(data.get("layout_patch", {})),
        repair_actions=tuple(dict(item) for item in data.get("repair_actions", ())),
        artifacts=tuple(ArtifactRef.from_dict(item) for item in data.get("artifacts", ())),
        metrics={str(key): float(value) for key, value in dict(data.get("metrics", {})).items()},
        accepted=bool(data.get("accepted", False)),
        notes=str(data.get("notes", "")),
        checkpoint_id=str(data.get("checkpoint_id", uuid4().hex)),
        created_at=float(data.get("created_at", time())),
    )


@dataclass(frozen=True)
class ClosureRequirement:
    name: str
    kinds: tuple[str, ...]
    provenances: tuple[EvidenceLevel, ...]
    statuses: tuple[ArtifactStatus, ...] = (ArtifactStatus.PASSED,)
    min_metric: tuple[str, float] | None = None


@dataclass(frozen=True)
class ClosureReport:
    passed: bool
    satisfied: tuple[str, ...]
    issues: tuple[str, ...]
    evidence_ids: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ClosureGate:
    def __init__(self, requirements: Sequence[ClosureRequirement]) -> None:
        self.requirements = tuple(requirements)

    def evaluate(self, artifacts: Iterable[ArtifactRef]) -> ClosureReport:
        records = tuple(artifacts)
        satisfied: list[str] = []
        issues: list[str] = []
        evidence: dict[str, str] = {}
        for requirement in self.requirements:
            match = next((item for item in records if _matches(item, requirement)), None)
            if match is None:
                expected = ", ".join(item.value for item in requirement.provenances)
                issues.append(
                    f"missing {requirement.name}: kinds={requirement.kinds}, provenance={expected}, "
                    f"statuses={tuple(item.value for item in requirement.statuses)}"
                )
                continue
            satisfied.append(requirement.name)
            evidence[requirement.name] = match.artifact_id
        return ClosureReport(not issues, tuple(satisfied), tuple(issues), evidence)


FINAL_ANALOG_CLOSURE_REQUIREMENTS: tuple[ClosureRequirement, ...] = (
    ClosureRequirement("calibre_drc_clean", ("drc",), (EvidenceLevel.CALIBRE_DRC,)),
    ClosureRequirement("calibre_lvs_clean", ("lvs",), (EvidenceLevel.CALIBRE_LVS,)),
    ClosureRequirement("calibre_pex_complete", ("pex",), (EvidenceLevel.CALIBRE_PEX,)),
    ClosureRequirement("post_layout_sim_pass", ("simulation",), (EvidenceLevel.SPECTRE_POST_LAYOUT,)),
)


def _matches(artifact: ArtifactRef, requirement: ClosureRequirement) -> bool:
    if artifact.kind not in requirement.kinds:
        return False
    if artifact.provenance not in requirement.provenances or artifact.status not in requirement.statuses:
        return False
    if requirement.min_metric is not None:
        name, threshold = requirement.min_metric
        return float(artifact.metrics.get(name, float("-inf"))) >= threshold
    return True


def _file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
