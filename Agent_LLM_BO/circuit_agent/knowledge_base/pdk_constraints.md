# PDK Constraints - TSMC N28

## Process Summary
- **Node**: TSMC 28nm (N28HPC+)
- **VDD Core**: 0.9V
- **VDD IO**: 1.8V / 2.5V

## Device Models
| Type | Model Name | Notes |
|------|-----------|-------|
| Core NMOS | `nch_mac` | Standard threshold |
| Core PMOS | `pch_mac` | Standard threshold |

## Device Parameter Ranges
| Parameter | Min | Max | Notes |
|-----------|-----|-----|-------|
| L (channel length) | 30nm | 1um | Min L=30nm for reliability |
| W (finger width) | 100nm | 3um | Per finger |
| nf (finger count) | 1 | 64 | Power of 2 preferred |
| M (multiplier) | 1 | 32 | Integer |

**Effective width** = W x nf x m

## Typical Device Parameters (TT corner)
| Parameter | NMOS (nch_mac) | PMOS (pch_mac) |
|-----------|---------------|---------------|
| Vth (typical) | ~0.4V | ~-0.4V |
| mu*Cox | ~300 uA/V^2 | ~100 uA/V^2 |
| Lambda (L=30n) | ~0.1 V^-1 | ~0.08 V^-1 |
| Lambda (L=100n) | ~0.03 V^-1 | ~0.025 V^-1 |

## Design Rules for Saturation
- **NMOS saturation**: Vds > Vgs - Vth (Vov = Vgs - Vth > 0)
- **PMOS saturation**: Vsd > Vsg - |Vth| 
- **Headroom**: With VDD=0.9V, maximum voltage stack ~2-3 devices
- **Typical Vov**: 50mV ~ 200mV for analog design

## Current Density Guidelines
- Recommended current density: 1~10 uA/um (per unit W)
- For matching: use L >= 60nm, larger W
- For speed: minimize L (30nm), optimize W/L for gm/Id

## Matching Constraints
- Differential pairs: identical W, L, nf, m; same orientation
- Current mirrors: identical L; W ratio determines current ratio
- Always use common-centroid layout for critical pairs

## PDK Library Include
```spice
.lib '/PDKS/TSMC28nm/models/spectre/toplevel.scs' top_tt
```

## Corner Analysis (for reference)
| Corner | Description |
|--------|------------|
| top_tt | Typical-Typical |
| top_ff | Fast-Fast |
| top_ss | Slow-Slow |
| top_fs | Fast NMOS, Slow PMOS |
| top_sf | Slow NMOS, Fast PMOS |
