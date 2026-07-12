# NMCF Three-Stage OTA Optimization Guide

## Circuit Summary

Three-stage OTA with PMOS input first stage, NMOS intermediate gain stage, PMOS output stage, and nested Miller compensation (`Cc1/Rz1/Cc2`). This topology is intended only when simpler topologies cannot reach gain requirements.

## Tunable Parameters

- Stage 1: `Wtail1/Ltail1`, `Wdiff1/Ldiff1`, `Wload1/Lload1`.
- Stage 2: `Wgm2/Lgm2`, `Wload2/Lload2`.
- Stage 3: `Wgm3/Lgm3`, `Wload3/Lload3`.
- Bias: `Wbiasn/Lbiasn`, `Wbiasp/Lbiasp`.
- Compensation: `Cc1/Rz1/Cc2`.

## Metric-Guided Rules

- Gain low: increase gain-device/load lengths stage by stage; avoid only increasing final stage size.
- GBW low: identify dominant stage; increase earlier-stage gm before changing output stage aggressively.
- PM low or ringing: increase/tune `Cc1` and `Rz1`; adjust `Cc2` for output pole behavior.
- SR low: increase later-stage current capability or reduce excessive compensation.
- Power high: reduce stage currents from output stage backward, then verify GBW and settling.

## DC OP Rules

- `Mtail1` or `Mload1a/b` linear: first-stage bias/current mirror issue.
- `Mgm2/Mload2` linear: intermediate-stage bias or compensation loading issue.
- `Mgm3/Mload3` linear: output common-mode/headroom or load current issue.
- Bias devices linear: check generated `vbiasp`/`ibias` assumptions before changing signal-path devices.

## Avoid

- Do not use NMCF as the first choice for moderate-gain specs.
- Do not tune `Cc1`, `Rz1`, and `Cc2` randomly; use AC/phase data and settling waveform together.
