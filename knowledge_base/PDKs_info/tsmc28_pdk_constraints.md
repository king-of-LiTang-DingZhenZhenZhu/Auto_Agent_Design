# PDK Constraints - TSMC N28

## Process Summary
- **Node**: TSMC 28nm (N28HPC+)
- **VDD Core**: 0.9 - 1.1V
- **VDD IO**: 1.8V / 2.5V

## Device Models
| Type | Model Name | Notes |
|------|-----------|-------|
| Core NMOS | `nch_mac` | Standard threshold |
| Core PMOS | `pch_mac` | Standard threshold |

## Device Parameter Ranges
| Parameter | Min | Max | Notes |
|-----------|-----|-----|-------|
| L (channel length) | 30nm | 1um | Min L=30nm for reliability; **analog circuits: recommend L ≥ 60nm** to reduce short-channel effects and improve output impedance |
| W (finger width) | 100nm | 2.7um | Per finger |
| nf (finger count) | 1 | 64 | Power of 2 preferred |
| M (multiplier) | 1 | 32 | Integer |

**Effective width** = W x nf x m


## Current Density Guidelines
- Recommended current density: 1~10 uA/um (per unit W)
- For matching: use L >= 60nm, larger W
- For speed: minimize L (30nm), optimize W/L for gm/Id


## PDK Library Include
```spice
* if we write xxx.sp or xxx.cir scripts
.lib '/path/to/your/pdk/hspice/toplevel.l' TOP_TT
* if we write xxx.scs scripts
include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt
