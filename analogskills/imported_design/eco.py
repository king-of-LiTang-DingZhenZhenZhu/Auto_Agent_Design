from __future__ import annotations


def accept_eco_candidate(
    *,
    before_drc: int,
    before_lvs: int,
    after_drc: int,
    after_lvs: int,
    stages_ok: bool,
) -> bool:
    """Accept only a strict overall improvement with no DRC/LVS regression."""
    return (
        stages_ok
        and after_drc <= before_drc
        and after_lvs <= before_lvs
        and (after_drc < before_drc or after_lvs < before_lvs)
        and not (before_drc == 0 and after_drc > 0)
        and not (before_lvs == 0 and after_lvs > 0)
    )
