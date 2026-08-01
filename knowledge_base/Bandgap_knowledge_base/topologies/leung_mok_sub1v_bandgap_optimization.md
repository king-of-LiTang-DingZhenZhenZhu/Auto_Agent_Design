# Leung-Mok Sub-1-V Bandgap Optimization Guide

## Scope

`leung_mok_sub1v_bandgap` implements the complete circuit of Leung and Mok,
IEEE JSSC, April 2002, Fig. 3. It is a transistor-level parent topology and
does not use a frozen generic opamp child.

The implementation contains four paper blocks:

- divided-input bandgap core (`M1-M3`, `Q1/Q2`, `R1/R2/R3`, `Ccomp`);
- autonomous startup (`MS1-MS4`);
- PMOS forward-body-bias generator (`RSB`, `MSB`, `MA01-MA03`);
- PMOS-input low-voltage amplifier with BJT level shifting
  (`MA04-MA15`, `QA16/QA17`).

All PMOS bodies use `vb` except `MA08/MA09`, whose bodies remain at `vdd`, as
specified by the Fig. 3 caption. The topology uses regular MOS model roles,
not LVT roles.

## Paper Mapping and First-Order Relations

The amplifier forces `n1=n2`. Because the A/B dividers share `R2_HIGH` and
`R2_LOW`, this also forces `n3=n4`.

```text
R2 = R2_HIGH + R2_LOW
I = VEB2/R2 + (VT*ln(N))/R1
Vref = (R3/R2) * [VEB2 + (R2/R1)*VT*ln(N)]
Vin,CM = [R2_LOW/(R2_HIGH+R2_LOW)] * VEB2
```

Defaults use `N=64`, `R2/R1=5.5`, and `R3/R2=0.48`, giving a first-order
target near the paper's 603 mV result. The initial divider fraction is about
0.091, lowering the amplifier input common mode.

The absolute resistor defaults are initialization values because the paper
publishes ratios and trimming behavior rather than portable resistor values.
Final values must be recalibrated for the selected PNP and resistor models.

## Optimization Order

1. Verify startup escapes the zero-current state and `MS3/MS4` turn off.
2. At the cold/minimum-supply corner, check `vb` stays no more than roughly
   0.3 V below the PMOS sources; excessive junction forward current is unsafe.
3. Verify `MA08/MA09`, `MA12-MA15`, and `M1-M3` remain in their intended
   regions. Check `QA16/QA17` provide enough level shift.
4. Correct room-temperature `Vref` primarily with `R3`.
5. Correct first-order temperature slope with `R2/R1` while preserving the A/B
   divider matching. Adjust `BJT_AREA_RATIO` only when necessary.
6. Tune `R2_LOW/R2` for low-voltage input headroom, then recheck amplifier
   offset sensitivity and minimum supply.
7. Tune `Ccomp` only after the DC loop works; larger values improve stability
   but increase startup time.

## Dedicated Tests and Paper Conditions

- startup and PSRR use 1.0 V nominal supply by default;
- temperature sweep is 0-100 C, matching the reported 15 ppm/C interval;
- line sweep is 0.98-1.1 V for the current TSMC28 core-voltage domain;
- the paper also reports operation up to 1.5 V, but that voltage is outside the
  configured 1.1 V core-device limit and must not be applied without another
  verified PDK voltage domain.

The published `15 ppm/C`, `603 mV`, and `18 uA` are paper measurements, not
claims about this implementation before real Spectre/PVT calibration.

## Failure Clues

- Zero-current final state: inspect `vg`, `nstart`, and whether `MS3/MS4` inject
  current during the supply ramp.
- Cold low-supply collapse: inspect PMOS headroom, `vb`, and the divider common
  mode before changing PTAT ratios.
- Wrong Vref with reasonable slope: change `R3`, not the divider split.
- Wrong temperature slope: change `R2/R1`; keep A/B elements matched.
- Slow startup with stable DC: reduce `Ccomp` cautiously or strengthen startup.
- High current: increase absolute resistor scale while preserving ratios, then
  recheck amplifier bias and startup robustness.

