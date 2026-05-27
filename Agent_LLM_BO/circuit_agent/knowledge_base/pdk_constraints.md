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
| L (channel length) | 30nm | 1um | Min L=30nm for reliability; **analog circuits: recommend L ≥ 60nm** to reduce short-channel effects and improve output impedance |
| W (finger width) | 100nm | 3um | Per finger |
| nf (finger count) | 1 | 64 | Power of 2 preferred |
| M (multiplier) | 1 | 32 | Integer |

**Effective width** = W x nf x m

### `nf` vs `M` — When to Use Which
| Parameter | Purpose | Preferred Use Case |
|-----------|---------|-------------------|
| `nf` (finger count) | Splits one transistor into multiple gate fingers | Matching, reducing gate resistance, layout density |
| `M` (multiplier) | Replicates the entire unit cell | Current mirror ratios, large W scaling |

- For **matched pairs** (diff pair, mirror): fix `nf` and match it across devices; vary `M` for ratios.
- For **large W**: prefer increasing `nf` first (up to 64), then `M` (up to 32).
- Power of 2 preferred for both `nf` and `M` to simplify layout.


## Design Rules for Saturation
- **NMOS saturation**: Vds > Vdsat (we can use Vdsat = Vov = Vgs - Vth > 0 although it's not precise)
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
* if we write xxx.sp scripts
.lib '/path/to/your/pdk/hspice/toplevel.l' TOP_TT
* if we write xxx.scs scripts
include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt


## Corner Analysis (when using spice scripts)
| Corner | Description |
|--------|------------|
| TOP_TT | Typical-Typical |
| TOP_FF | Fast-Fast |
| TOP_SS | Slow-Slow |
| TOP_FS | Fast NMOS, Slow PMOS |
| TOP_SF | Slow NMOS, Fast PMOS |

> **Note**: Corner section names use UPPERCASE in HSPICE `.lib` calls.