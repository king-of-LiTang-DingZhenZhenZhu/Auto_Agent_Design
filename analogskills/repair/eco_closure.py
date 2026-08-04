"""First-pass ECO closure policy for DRC/LVS feedback.
This module does not replace Calibre.  It turns marker/rule feedback into a
small set of repair scopes so the flow can decide whether to run a local ECO,
promote the issue back into SMT constraints, defer density/methodology rules,
or stop for manual inspection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class EcoClosurePolicy:
    local_rule_prefixes: tuple[str, ...] = ("M", "VIA", "CO", "PO")
    promote_to_smt_prefixes: tuple[str, ...] = ("OD", "NW", "DNW", "DOD", "DPO", "SR_DOD")
    defer_rule_tokens: tuple[str, ...] = ("density", ".DN.", "MOM.R.2")
    ignore_rule_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class EcoRuleAction:
    rule: str
    scope: str
    count: int = 1
    reason: str = ""


@dataclass(frozen=True)
class EcoClosureResult:
    actions: tuple[EcoRuleAction, ...]
    summary: Mapping[str, int] = field(default_factory=dict)

    @property
    def local_repairable_count(self) -> int:
        return int(self.summary.get("local_eco", 0))

    @property
    def promote_to_smt_count(self) -> int:
        return int(self.summary.get("promote_to_smt", 0))


def classify_eco_rule(rule: str, policy: EcoClosurePolicy | None = None) -> EcoRuleAction:
    policy = policy or EcoClosurePolicy()
    name = str(rule or "").strip()
    if not name:
        return EcoRuleAction(name, "manual", reason="empty rule name")
    lowered = name.lower()
    if any(token.lower() in lowered for token in policy.ignore_rule_tokens):
        return EcoRuleAction(name, "ignore", reason="matched ignore token")
    if any(token.lower() in lowered for token in policy.defer_rule_tokens):
        return EcoRuleAction(name, "defer", reason="density/methodology class")
    prefix = name.split(".", 1)[0].upper()
    if any(prefix == item.upper() or name.upper().startswith(item.upper() + ".") for item in policy.local_rule_prefixes):
        return EcoRuleAction(name, "local_eco", reason="local metal/via/contact geometry")
    if any(prefix == item.upper() or name.upper().startswith(item.upper() + ".") for item in policy.promote_to_smt_prefixes):
        return EcoRuleAction(name, "promote_to_smt", reason="device/well/blocking geometry affects placement/rule model")
    return EcoRuleAction(name, "manual", reason="unclassified rule class")


def plan_eco_closure(
    rules_or_markers: Sequence[object],
    policy: EcoClosurePolicy | None = None,
) -> EcoClosureResult:
    policy = policy or EcoClosurePolicy()
    counts_by_rule: dict[str, int] = {}
    for item in rules_or_markers:
        rule = _rule_name(item)
        if not rule:
            continue
        counts_by_rule[rule] = counts_by_rule.get(rule, 0) + 1
    actions: list[EcoRuleAction] = []
    summary: dict[str, int] = {}
    for rule, count in sorted(counts_by_rule.items()):
        action = classify_eco_rule(rule, policy)
        action = EcoRuleAction(action.rule, action.scope, count=count, reason=action.reason)
        actions.append(action)
        summary[action.scope] = summary.get(action.scope, 0) + count
    return EcoClosureResult(tuple(actions), summary)


def _rule_name(item: object) -> str:
    if isinstance(item, str):
        return item
    for attr in ("rule", "rule_name", "name", "check"):
        value = getattr(item, attr, None)
        if value:
            return str(value)
    if isinstance(item, Mapping):
        for key in ("rule", "rule_name", "name", "check"):
            value = item.get(key)
            if value:
                return str(value)
    return ""
