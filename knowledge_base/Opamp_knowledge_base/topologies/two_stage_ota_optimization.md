# Two-Stage OTA Optimization Guide

## Circuit Summary

NMOS input 5T first stage drives a PMOS common-source second stage. Miller compensation uses `Cc` and `Rz`. `Mtail` and `Mload` share the NMOS bias node `vb`; in gm/Id mode `Mload` is ratio-derived from `Mtail`.

## Tunable Parameters

- `Wdiff/Ldiff`: first-stage gm, input VOD, and GBW through `gm1/Cc`.
- `Wmirr/Lmirr`: first-stage PMOS mirror load resistance and current matching.
- `Wtail/Ltail`: first-stage tail current source.
- `Wcs`: second-stage PMOS gain/current capability; `Lcs` follows `Lload`.
- `Wload/Lload`: second-stage NMOS load in physical mode; gm/Id mode derives it by ratio.
- `ratio_load_tail`: gm/Id current mirror ratio from `Mtail` to `Mload`.
- `Cc/Rz`: phase margin, settling, GBW, and slew-rate tradeoff.

## Metric-Guided Rules

- Gain low: increase `Lmirr`, `Lload`, and sometimes `Wcs`; avoid shrinking `Cc` blindly.
- GBW low: increase input-pair gm (`Wdiff` or `I_tail`) and reduce `Cc` only if PM margin allows.
- PM low: increase `Cc`; tune `Rz` upward conservatively.
- SR low: increase second-stage current capability (`ratio_load_tail` or `Wcs`) or reduce excessive `Cc`.
- Power high: reduce current ratios/current-source widths only if GBW/SR have margin.

## DC OP Rules

- `Mtail` linear: NMOS bias is too weak/high headroom demand; inspect `VBIAS`, `Ltail`, and current.
- `Mload` linear: second-stage load headroom or current ratio is wrong; adjust `ratio_load_tail`/`Wload`.
- `Mcs` linear: output common-mode or second-stage PMOS VOD is wrong; increase `Wcs` or reduce overdrive.
- `Mmirr1` is diode-connected; treat its OP with caution and focus on `Mmirr2` for output resistance.

## Avoid

- Do not let `Cc` grow enough to make PM large but GBW/SR unusable.
- Do not reintroduce global hidden `VBIAS` bounds in `main.py`; topology must own bias ranges.
