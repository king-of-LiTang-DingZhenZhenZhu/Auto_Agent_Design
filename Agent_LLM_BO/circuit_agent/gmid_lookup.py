"""Universal gm/Id lookup engine.

Loads process-specific gm/Id tables and provides fast interpolation-based
lookups for transistor sizing.

Key design:
- Loads the active PDK profile's gm/Id JSON once (singleton pattern).
- For each (L, Vds, Vbs, model) sweep, builds a 1D spline mapping
  gm_id → (Vgs, id_w, ft, gain, gds, cgg, vth).
- Cross-dimensional interpolation (L, Vds, Vbs) uses linear interpolation.
- Model aliasing: lvt models map to standard-VT table entries with a
  configurable scaling factor.

Usage:
    from gmid_lookup import get_lookup

    lu = get_lookup()
    r = lu.lookup("nch_mac", gm_id=12.0, L=80e-9, Vds=0.45)
    W = lu.get_W("pch_mac", Id_target=50e-6, gm_id=12.0, L=80e-9, Vds=0.3)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

try:
    from scipy.interpolate import interp1d
except ModuleNotFoundError:  # Allows GmidSizer tests with a fake lookup.
    interp1d = None

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


def _default_max_per_finger() -> float:
    try:
        from pdk_profiles import get_pdk_profile
        return get_pdk_profile().max_width_per_finger
    except Exception:
        return 2.6e-6

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GmidResult:
    """Output of a gm/Id lookup.

    All quantities are in SI units (A/m, Hz, V/V, S/m, F/m, V).
    """

    model: str
    gm_id: float  # 1/V — transconductance efficiency
    id_w: float  # A/m — current density per unit width
    ft: float  # Hz — transit frequency
    gain: float  # V/V — intrinsic gain (gm/gds)
    gds: float  # S/m — output conductance per unit width
    cgg: float  # F/m — gate capacitance per unit width
    vgs: float  # V — gate-source voltage
    vth: float  # V — threshold voltage
    L: float  # m — channel length
    Vds: float  # V — drain-source voltage
    Vbs: float  # V — bulk-source voltage


# ---------------------------------------------------------------------------
# Built-in model aliases. Extra model names present in a gm/Id table are
# loaded directly and become valid lookup model names.
# ---------------------------------------------------------------------------

MODEL_ALIASES: dict[str, str] = {
    "nch_lvt_mac": "nch_lvt_mac",
    "pch_lvt_mac": "pch_lvt_mac",
    "nch_mac": "nch_mac",
    "pch_mac": "pch_mac",
}


# ---------------------------------------------------------------------------
# Core lookup engine
# ---------------------------------------------------------------------------


class GmidLookup:
    """gm/Id lookup using pre-computed process-specific tables.

    Loads the JSON sweep data and builds interpolation structures for
    fast (gm_id, L, Vds, Vbs) → (id_w, ft, gain, ...) queries.
    """

    def __init__(self, json_path: str | Path):
        if interp1d is None:
            raise ModuleNotFoundError(
                "scipy is required to load gm/Id lookup tables. "
                "Install project requirements before using real lookup data."
            )
        self._json_path = Path(json_path)
        self._data: dict = {}
        # Per-model storage:
        #   _Ls[model]       : sorted unique L values (m)
        #   _Vdss[model]     : sorted unique Vds values (V)
        #   _Vbss[model]     : sorted unique Vbs values (V)
        #   _splines[model]  : dict[(L, Vds, Vbs), SplineData]
        self._Ls: dict[str, NDArray] = {}
        self._Vdss: dict[str, NDArray] = {}
        self._Vbss: dict[str, NDArray] = {}
        self._splines: dict[str, dict[tuple, dict]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(
        self,
        model: str,
        gm_id: float,
        L: float,
        Vds: float = 0.45,
        Vbs: float = 0.0,
    ) -> GmidResult:
        """Look up all device parameters at a given (gm_id, L, Vds, Vbs).

        Args:
            model: SPICE model name (e.g. "nch_mac", "pch_lvt_mac").
            gm_id: Target gm/Id ratio (1/V). Typical range: 5 (strong inv.)
                   to 25 (weak inv.).
            L: Channel length in meters.
            Vds: Drain-source voltage in V.
            Vbs: Bulk-source voltage in V.

        Returns:
            GmidResult with id_w, ft, gain, gds, cgg, Vgs, vth.
        """
        canonical = self._resolve_model(model)

        # Snap to available grid points
        L_q = self._snap(L, self._Ls[canonical], "L")
        Vds_q = self._snap(Vds, self._Vdss[canonical], "Vds")
        Vbs_q = self._snap(Vbs, self._Vbss[canonical], "Vbs")

        # Interpolate at the exact grid point(s)
        # For L: interpolate between 2 nearest lengths if not exact match
        result = self._interpolate_L(canonical, gm_id, L, L_q, Vds_q, Vbs_q)

        try:
            return GmidResult(
                model=model,
                gm_id=float(gm_id),
                id_w=float(result["id_w"]),
                ft=float(result["ft"]),
                gain=float(result["gain"]),
                gds=float(result["gds"]),
                cgg=float(result["cgg"]),
                vgs=float(result["vgs"]),
                vth=float(result["vth"]),
                L=L,
                Vds=Vds_q,
                Vbs=Vbs_q,
            )
        except Exception:
            logger.error(
                "Failed to construct GmidResult: model=%s gm_id=%.2f L=%.1fn Vds=%.2f",
                model,
                gm_id,
                L * 1e9,
                Vds,
            )
            raise

    def get_W(
        self,
        model: str,
        Id_target: float,
        gm_id: float,
        L: float,
        Vds: float = 0.45,
        Vbs: float = 0.0,
    ) -> float:
        """Compute required transistor width for a target drain current.

        Args:
            model: SPICE model name.
            Id_target: Target drain current in Amperes.
            gm_id: gm/Id operating point (1/V).
            L: Channel length in meters.
            Vds: Drain-source voltage in V.
            Vbs: Bulk-source voltage in V.

        Returns:
            Total transistor width in meters.
        """
        result = self.lookup(model, gm_id, L, Vds, Vbs)
        if result.id_w <= 0:
            raise ValueError(
                f"Invalid id_w={result.id_w:.2e} A/m for model={model} "
                f"gm_id={gm_id:.1f} L={L*1e9:.0f}nm — check gm_id range"
            )
        return Id_target / result.id_w

    def get_gm_id_range(
        self,
        model: str,
        L: float,
        Vds: float = 0.45,
        Vbs: float = 0.0,
    ) -> tuple[float, float]:
        """Return the available gm_id range for given conditions."""
        canonical = self._resolve_model(model)
        L_q = self._snap(L, self._Ls[canonical], "L")
        Vds_q = self._snap(Vds, self._Vdss[canonical], "Vds")
        Vbs_q = self._snap(Vbs, self._Vbss[canonical], "Vbs")

        key = (L_q, Vds_q, Vbs_q)
        if key not in self._splines[canonical]:
            key = self._find_nearest_key(canonical, key)
        sp = self._splines[canonical][key]
        return (float(sp["gm_id_min"]), float(sp["gm_id_max"]))

    def get_available_Ls(self, model: str) -> list[float]:
        """Return sorted list of available L values for this model."""
        canonical = self._resolve_model(model)
        return list(self._Ls[canonical])

    def get_available_Vdss(self, model: str) -> list[float]:
        """Return sorted list of available Vds values for this model."""
        canonical = self._resolve_model(model)
        return list(self._Vdss[canonical])

    def get_available_Vbss(self, model: str) -> list[float]:
        """Return sorted list of available Vbs values for this model."""
        canonical = self._resolve_model(model)
        return list(self._Vbss[canonical])

    # ------------------------------------------------------------------
    # Internal — data loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load JSON data and build interpolation structures."""
        logger.info("Loading gm/Id tables from %s", self._json_path)
        with open(self._json_path, encoding="utf-8") as f:
            raw = json.load(f)

        for model_key in sorted(raw):
            self._build_model(raw[model_key], model_key)
            MODEL_ALIASES.setdefault(model_key, model_key)

        logger.info(
            "Loaded gm/Id tables: %s",
            {
                model: len(splines)
                for model, splines in self._splines.items()
            },
        )

    def _build_model(self, model_data: dict, model_name: str) -> None:
        """Build interpolation structures for one model."""
        sweeps = model_data["sweeps"]

        # Collect unique grid values
        L_set = sorted(set(float(s["L"]) for s in sweeps))
        Vds_set = sorted(set(float(s["Vds"]) for s in sweeps))
        Vbs_set = sorted(set(float(s["Vbs"]) for s in sweeps))

        self._Ls[model_name] = np.array(L_set)
        self._Vdss[model_name] = np.array(Vds_set)
        self._Vbss[model_name] = np.array(Vbs_set)

        splines: dict[tuple, dict] = {}
        for s in sweeps:
            L_val = float(s["L"])
            Vds_val = float(s["Vds"])
            Vbs_val = float(s["Vbs"])
            key = (L_val, Vds_val, Vbs_val)

            # Arrays from sweep
            gm_id_arr = np.array(s["gm_id"], dtype=np.float64)
            id_w_arr = np.array(s["id_w"], dtype=np.float64)
            ft_arr = np.array(s["ft"], dtype=np.float64)
            gain_arr = np.array(s["gain"], dtype=np.float64)
            gds_arr = np.array(s["gds"], dtype=np.float64)
            cgg_arr = np.array(s["cgg"], dtype=np.float64)
            vgs_arr = np.array(s["Vgs"], dtype=np.float64)
            vth_arr = np.array(s["vth"], dtype=np.float64)

            # gm_id sweeps from weak (high gm_id) to strong (low gm_id).
            # We need a monotonically decreasing gm_id for interpolation.
            # Verify and reverse if needed.
            if gm_id_arr[0] < gm_id_arr[-1]:
                # Ascending — reverse all arrays
                gm_id_arr = gm_id_arr[::-1]
                id_w_arr = id_w_arr[::-1]
                ft_arr = ft_arr[::-1]
                gain_arr = gain_arr[::-1]
                gds_arr = gds_arr[::-1]
                cgg_arr = cgg_arr[::-1]
                vgs_arr = vgs_arr[::-1]
                vth_arr = vth_arr[::-1]

            # Align array lengths: some quantities (gds, cgg, vth, and
            # occasionally Vgs) may have one fewer point than gm_id
            # (they are computed at midpoints between Vgs steps).
            # Pad shorter arrays by repeating the last element.
            ref_len = len(gm_id_arr)
            if len(id_w_arr) == ref_len - 1:
                id_w_arr = np.append(id_w_arr, id_w_arr[-1])
            if len(ft_arr) == ref_len - 1:
                ft_arr = np.append(ft_arr, ft_arr[-1])
            if len(gain_arr) == ref_len - 1:
                gain_arr = np.append(gain_arr, gain_arr[-1])
            if len(gds_arr) == ref_len - 1:
                gds_arr = np.append(gds_arr, gds_arr[-1])
            if len(cgg_arr) == ref_len - 1:
                cgg_arr = np.append(cgg_arr, cgg_arr[-1])
            if len(vgs_arr) == ref_len - 1:
                vgs_arr = np.append(vgs_arr, vgs_arr[-1])
            if len(vth_arr) == ref_len - 1:
                vth_arr = np.append(vth_arr, vth_arr[-1])

            # Remove any non-monotonic trailing points
            mono_mask = self._make_monotonic_decreasing(gm_id_arr)
            gm_id_arr = gm_id_arr[mono_mask]
            id_w_arr = id_w_arr[mono_mask]
            ft_arr = ft_arr[mono_mask]
            gain_arr = gain_arr[mono_mask]
            gds_arr = gds_arr[mono_mask]
            cgg_arr = cgg_arr[mono_mask]
            vgs_arr = vgs_arr[mono_mask]
            vth_arr = vth_arr[mono_mask]

            if len(gm_id_arr) < 3:
                logger.warning(
                    "Sweep (%s, L=%.0fn, Vds=%.1f, Vbs=%.1f) has <3 valid points — skipping",
                    model_name,
                    L_val * 1e9,
                    Vds_val,
                    Vbs_val,
                )
                continue

            # Build 1D interpolators: gm_id → each quantity
            splines[key] = {
                "id_w": interp1d(gm_id_arr, id_w_arr, kind="linear",
                                 bounds_error=False,
                                 fill_value=(id_w_arr[-1], id_w_arr[0])),
                "ft": interp1d(gm_id_arr, ft_arr, kind="linear",
                               bounds_error=False,
                               fill_value=(ft_arr[-1], ft_arr[0])),
                "gain": interp1d(gm_id_arr, gain_arr, kind="linear",
                                 bounds_error=False,
                                 fill_value=(gain_arr[-1], gain_arr[0])),
                "gds": interp1d(gm_id_arr, gds_arr, kind="linear",
                                bounds_error=False,
                                fill_value=(gds_arr[-1], gds_arr[0])),
                "cgg": interp1d(gm_id_arr, cgg_arr, kind="linear",
                                bounds_error=False,
                                fill_value=(cgg_arr[-1], cgg_arr[0])),
                "vgs": interp1d(gm_id_arr, vgs_arr, kind="linear",
                                bounds_error=False,
                                fill_value=(vgs_arr[-1], vgs_arr[0])),
                "vth": interp1d(gm_id_arr, vth_arr, kind="linear",
                                bounds_error=False,
                                fill_value=(vth_arr[-1], vth_arr[0])),
                "gm_id_min": float(gm_id_arr[-1]),
                "gm_id_max": float(gm_id_arr[0]),
            }

        self._splines[model_name] = splines

    # ------------------------------------------------------------------
    # Internal — interpolation helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, model: str) -> str:
        """Map user-facing model name to canonical table key."""
        canonical = MODEL_ALIASES.get(model, model if model in self._splines else None)
        if canonical is None:
            raise ValueError(
                f"Unknown model '{model}'. Available: {sorted(self._splines)}"
            )
        if canonical not in self._splines:
            raise ValueError(
                f"No gm/Id data for model '{canonical}'. "
                f"Available: {list(self._splines.keys())}"
            )
        if model != canonical:
            logger.debug("Model alias: %s → %s", model, canonical)
        return canonical

    @staticmethod
    def _snap(value: float, grid: NDArray, name: str = "") -> float:
        """Snap a value to the nearest grid point with a warning if far."""
        idx = np.argmin(np.abs(grid - value))
        snapped = float(grid[idx])
        if name and abs(value - snapped) > 1e-12 and abs(value - snapped) / max(abs(value), 1e-30) > 0.1:
            logger.debug(
                "Snapping %s: requested %.4g → snapped to %.4g", name, value, snapped
            )
        return snapped

    @staticmethod
    def _make_monotonic_decreasing(arr: NDArray) -> NDArray:
        """Return boolean mask of monotonically decreasing (strict) prefix."""
        mask = np.ones(len(arr), dtype=bool)
        for i in range(1, len(arr)):
            if arr[i] >= arr[i - 1]:
                mask[i:] = False
                break
        return mask

    def _interpolate_at_key(
        self, canonical: str, gm_id: float, key: tuple
    ) -> dict[str, float]:
        """Evaluate all splines at a specific grid key."""
        sp = self._splines[canonical][key]
        gm_min = sp["gm_id_min"]
        gm_max = sp["gm_id_max"]
        gm_clamped = float(np.clip(gm_id, gm_min, gm_max))

        if gm_id < gm_min or gm_id > gm_max:
            logger.debug(
                "gm_id=%.1f outside range [%.1f, %.1f] for key=%s — clamped",
                gm_id, gm_min, gm_max, key,
            )

        return {
            "id_w": float(sp["id_w"](gm_clamped)),
            "ft": float(sp["ft"](gm_clamped)),
            "gain": float(sp["gain"](gm_clamped)),
            "gds": float(sp["gds"](gm_clamped)),
            "cgg": float(sp["cgg"](gm_clamped)),
            "vgs": float(sp["vgs"](gm_clamped)),
            "vth": float(sp["vth"](gm_clamped)),
        }

    def _find_nearest_key(
        self, canonical: str, key: tuple
    ) -> tuple:
        """Find the nearest available key by L distance."""
        available = list(self._splines[canonical].keys())
        L_target = key[0]
        best = min(available, key=lambda k: abs(k[0] - L_target))
        return best

    def _interpolate_L(
        self,
        canonical: str,
        gm_id: float,
        L_exact: float,
        L_snapped: float,
        Vds_q: float,
        Vbs_q: float,
    ) -> dict[str, float]:
        """Interpolate results across L dimension if exact L not available.

        Uses linear interpolation between the two nearest L grid values.
        Falls back to nearest-neighbour if only one L available or if the
        second L point has a different Vds/Vbs key missing.
        """
        exact_key = (L_snapped, Vds_q, Vbs_q)

        # Check for exact match
        if exact_key in self._splines[canonical] and abs(L_exact - L_snapped) < 1e-12:
            return self._interpolate_at_key(canonical, gm_id, exact_key)

        # Find two nearest L values
        L_vals = self._Ls[canonical]
        if len(L_vals) < 2:
            key = self._find_nearest_key(canonical, exact_key)
            logger.debug("Only 1 L available — using nearest: L=%.1fn", key[0] * 1e9)
            return self._interpolate_at_key(canonical, gm_id, key)

        idx = np.searchsorted(L_vals, L_exact)
        if idx == 0:
            L_lo = L_vals[0]
            L_hi = L_vals[1] if len(L_vals) > 1 else L_vals[0]
        elif idx >= len(L_vals):
            L_lo = L_vals[-2] if len(L_vals) > 1 else L_vals[-1]
            L_hi = L_vals[-1]
        else:
            L_lo = L_vals[idx - 1]
            L_hi = L_vals[idx]

        key_lo = (float(L_lo), Vds_q, Vbs_q)
        key_hi = (float(L_hi), Vds_q, Vbs_q)

        # Fall back to nearest if one key is missing
        if key_lo not in self._splines[canonical]:
            key_lo = self._find_nearest_key(canonical, key_lo)
        if key_hi not in self._splines[canonical]:
            key_hi = self._find_nearest_key(canonical, key_hi)

        if key_lo == key_hi or abs(L_hi - L_lo) < 1e-15:
            return self._interpolate_at_key(canonical, gm_id, key_lo)

        r_lo = self._interpolate_at_key(canonical, gm_id, key_lo)
        r_hi = self._interpolate_at_key(canonical, gm_id, key_hi)

        # Linear interpolation weight
        t = float((L_exact - L_lo) / (L_hi - L_lo))
        t = max(0.0, min(1.0, t))

        return {k: r_lo[k] + t * (r_hi[k] - r_lo[k]) for k in r_lo}


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_lookup_instance: GmidLookup | None = None


def get_lookup(json_path: str | Path | None = None) -> GmidLookup:
    """Return the singleton GmidLookup instance, creating it on first call.

    Args:
        json_path: Path to a gm/Id table JSON. If None, uses the value from
                   config.settings.gmid_table_path.
    """
    global _lookup_instance
    if _lookup_instance is not None:
        return _lookup_instance

    if json_path is None:
        try:
            from config import settings
            json_path = settings.gmid_table_path
        except Exception:
            pass

    if not json_path:
        # Auto-discover default path
        candidates = [
            "/home/userone/Desktop/analog_circuit_projects/gmid_tsmc28/output/gm_id_tables_tsmc28.json",
            "./gmid_tables/gm_id_tables_tsmc28.json",
        ]
        for p in candidates:
            if Path(p).exists():
                json_path = p
                break
        else:
            raise FileNotFoundError(
                "Cannot find gm/Id lookup table JSON. Set GMID_TABLE_PATH, "
                "configure the active PDK profile, or pass json_path explicitly."
            )

    _lookup_instance = GmidLookup(json_path)
    return _lookup_instance


# ---------------------------------------------------------------------------
# GmidSizer — converts gm/Id-space params to physical W/L
# ---------------------------------------------------------------------------


class GmidSizer:
    """Converts gm/Id-space parameters to physical W/L values for netlist rendering.

    This is the bridge between the BO search space (gm_id, L, Ibranch)
    and the netlist template (W, L physical values).

    Usage:
        spec = topology.get_gmid_spec()
        sizer = GmidSizer(spec, get_lookup())
        physical_params = sizer.size({"I_tail": 50e-6, "gm_id_diff": 12, ...})
        # → {"Wtail": 12.5e-6, "Ltail": 200e-9, "Wdp": 7.2e-6, "Ldp": 80e-9, ...}
    """

    def __init__(self, spec, lookup: GmidLookup | None = None):
        """Args:
            spec: GmidTopologySpec from the topology.
            lookup: GmidLookup instance. Uses singleton if None.
        """
        from models import GmidTopologySpec  # late import to avoid circular
        self._spec: GmidTopologySpec = spec
        self._lookup = lookup if lookup is not None else get_lookup()
        # Cache: (role, gm_id, L, Id) → W
        self._cache: dict[tuple, float] = {}

    def size(self, gmid_params: dict[str, float]) -> dict[str, float]:
        """Convert gm/Id-space parameters to physical W/L dict.

        Args:
            gmid_params: Dict with keys like "I_tail", "gm_id_diff_pair",
                         "L_diff_pair", "Cc", "Rz", etc.

        Returns:
            Dict with physical W/L keys like "Wtail", "Ltail", "Wdp", "Ldp", ...
            plus pass-through params like "Cc", "Rz".
        """
        result: dict[str, float] = {}
        fixed_width_scale = 1.0
        if self._spec.fixed_width_scale_param:
            scale_param = self._spec.fixed_width_scale_param
            reference = self._spec.fixed_width_scale_reference
            if reference <= 0:
                raise ValueError(
                    f"Invalid fixed_width_scale_reference={reference} for {scale_param}"
                )
            scale_value = gmid_params.get(
                scale_param,
                self._spec.fixed_params.get(scale_param, reference),
            )
            fixed_width_scale = scale_value / reference

        for name, value in self._spec.fixed_params.items():
            if name.startswith(("nf_", "m_")):
                continue
            if name.lower().startswith("w"):
                from models import split_width

                scaled_width = value * fixed_width_scale
                result[name] = scaled_width
                nf_name = f"nf_{name}"
                m_name = f"m_{name}"
                if nf_name in self._spec.fixed_params:
                    result[nf_name] = self._spec.fixed_params[nf_name]
                if m_name in self._spec.fixed_params:
                    result[m_name] = self._spec.fixed_params[m_name]
                if nf_name not in result or m_name not in result:
                    try:
                        from pdk_profiles import get_pdk_profile
                        max_per_finger = get_pdk_profile().max_width_per_finger
                    except Exception:
                        max_per_finger = 2.6e-6
                    _instance_w, nf, m = split_width(scaled_width, max_per_finger)
                    result.setdefault(nf_name, nf)
                    result.setdefault(m_name, m)
            else:
                result[name] = value

        # Step 1: Resolve branch currents
        branch_currents: dict[str, float] = {}
        for bc in self._spec.branch_currents:
            branch_currents[bc.name] = gmid_params.get(bc.name, bc.default)
        for dbc in self._spec.derived_branch_currents:
            branch_currents[dbc.name] = dbc.resolve(gmid_params)

        transistors_by_role = {ts.role: ts for ts in self._spec.transistors}
        mirror_output_roles = {
            mirror.output_role for mirror in self._spec.current_mirrors
        }

        # Step 1b: Derive mirror output branch currents before sizing other
        # devices that depend on those currents, e.g. a second gain stage.
        mirror_ratios: dict[str, int] = {}
        for mirror in self._spec.current_mirrors:
            ratio = int(round(gmid_params.get(
                mirror.ratio_param, mirror.ratio_default
            )))
            ratio = min(max(ratio, mirror.ratio_low), mirror.ratio_high)
            mirror_ratios[mirror.ratio_param] = ratio
            if mirror.derived_current_name:
                ref = transistors_by_role.get(mirror.reference_role)
                if ref is None:
                    raise ValueError(
                        f"Mirror reference role '{mirror.reference_role}' "
                        "is not present in topology spec"
                    )
                ref_current = (
                    branch_currents.get(ref.current_source, 0.0)
                    * ref.current_fraction
                )
                branch_currents[mirror.derived_current_name] = (
                    ref_current * ratio
                )

        # Step 2: Size each transistor from gm_id, L, Id
        total_width_by_role: dict[str, float] = {}
        length_by_role: dict[str, float] = {}
        for ts in self._spec.transistors:
            if ts.role in mirror_output_roles:
                continue

            # Determine drain current for this transistor
            Ibranch = branch_currents.get(ts.current_source, 0.0)
            Id_target = Ibranch * ts.current_fraction

            # Get gm_id and L from params (or defaults)
            gm_id_key = f"gm_id_{ts.role}"
            L_key = f"L_{ts.role}"
            gm_id_val = gmid_params.get(gm_id_key, ts.gm_id_default)
            if ts.l_param in result:
                L_val = result[ts.l_param]
            elif ts.l_param in gmid_params:
                L_val = gmid_params[ts.l_param]
            elif (
                ts.l_param in self._spec.derived_length_params
                and self._spec.derived_length_params[ts.l_param] in result
            ):
                L_val = result[self._spec.derived_length_params[ts.l_param]]
            else:
                L_val = gmid_params.get(L_key, ts.L_default)

            # Compute W via lookup
            cache_key = (ts.role, round(gm_id_val, 4), round(L_val, 15), round(Id_target, 15))
            if cache_key in self._cache:
                w = self._cache[cache_key]
            else:
                try:
                    w = self._lookup.get_W(
                        ts.model, Id_target, gm_id_val, L_val,
                        ts.Vds_estimate, ts.Vbs,
                    )
                except ValueError as e:
                    logger.warning(
                        "Sizing failed for %s (gm_id=%.1f, L=%.1fn, Id=%.2fuA): %s. "
                        "Using min feasible W.",
                        ts.role, gm_id_val, L_val * 1e9, Id_target * 1e6, e,
                    )
                    # Fallback: use minimum gm_id (max current density) to get smallest W
                    gm_range = self._lookup.get_gm_id_range(
                        ts.model, L_val, ts.Vds_estimate, ts.Vbs
                    )
                    w = self._lookup.get_W(
                        ts.model, Id_target, gm_range[0], L_val,
                        ts.Vds_estimate, ts.Vbs,
                    )
                self._cache[cache_key] = w

            # Apply guard-banded PDK constraints before finger/m splitting.
            w = max(w, 200e-9)   # Min W = 200nm (safe margin above 90nm)
            L_val = max(L_val, 120e-9)  # Min L = 120nm (above 108nm bin boundary)

            total_width_by_role[ts.role] = w
            length_by_role[ts.role] = L_val
            self._write_sized_device(result, ts, w, L_val)

        # Step 3: Pass-through params (Cc, Rz, etc.)
        for pp in self._spec.pass_through_params:
            if pp.name in gmid_params:
                result[pp.name] = gmid_params[pp.name]

        # Step 4: Size mirror outputs from total-width ratios.
        for mirror in self._spec.current_mirrors:
            ref = transistors_by_role.get(mirror.reference_role)
            out = transistors_by_role.get(mirror.output_role)
            if ref is None or out is None:
                raise ValueError(
                    f"Current mirror {mirror.ratio_param} references missing "
                    "transistor roles"
                )
            if mirror.reference_role not in total_width_by_role:
                raise ValueError(
                    f"Mirror reference role '{mirror.reference_role}' was not sized"
                )

            ratio = mirror_ratios[mirror.ratio_param]
            output_total_w = total_width_by_role[mirror.reference_role] * ratio
            if mirror.share_length:
                output_l = length_by_role[mirror.reference_role]
            else:
                output_l = gmid_params.get(f"L_{out.role}", out.L_default)
                output_l = min(max(output_l, out.L_low), out.L_high)

            total_width_by_role[out.role] = output_total_w
            length_by_role[out.role] = output_l
            self._write_sized_device(result, out, output_total_w, output_l)

        for derived_param, source_param in self._spec.derived_length_params.items():
            if source_param in result:
                result[derived_param] = result[source_param]

        # Step 5: Derive voltage biases from lookup operating points.
        for bias in self._spec.derived_gate_biases:
            transistor = next(
                (ts for ts in self._spec.transistors if ts.role == bias.role),
                None,
            )
            if transistor is None:
                raise ValueError(
                    f"Derived bias role '{bias.role}' is not present in topology spec"
                )

            gm_id_val = gmid_params.get(
                f"gm_id_{transistor.role}", transistor.gm_id_default
            )
            L_val = gmid_params.get(
                f"L_{transistor.role}", transistor.L_default
            )
            operating_point = self._lookup.lookup(
                transistor.model,
                gm_id_val,
                L_val,
                transistor.Vds_estimate,
                transistor.Vbs,
            )

            # Tables may store signed VGS or a positive magnitude.
            gate_voltage = bias.resolve_gate_voltage(operating_point.vgs)
            upper_bound = (
                bias.high if bias.high is not None else bias.supply_voltage
            )
            if not bias.low <= gate_voltage <= upper_bound:
                raise ValueError(
                    f"Derived {bias.param_name}={gate_voltage:.4g} V from "
                    f"{bias.role} is outside [{bias.low:.4g}, "
                    f"{upper_bound:.4g}] V"
                )
            result[bias.param_name] = gate_voltage

        return result

    def _write_sized_device(
        self,
        result: dict[str, float],
        transistor,
        total_width: float,
        length: float,
    ) -> None:
        """Write Spectre instance W, nf, m, and L for one sized transistor/group."""
        from models import split_width

        total_width = max(total_width, 200e-9)
        length = min(max(length, transistor.L_low), transistor.L_high)
        max_per_finger = (
            transistor.max_per_finger if transistor.max_per_finger else _default_max_per_finger()
        )
        instance_total_w, nf, m = split_width(total_width, max_per_finger)
        result[transistor.w_param] = instance_total_w
        result[f"nf_{transistor.w_param}"] = nf
        result[f"m_{transistor.w_param}"] = m
        result[transistor.l_param] = length

    def get_initial_gmid_params(self, netlist_content: str = "") -> dict[str, float]:
        """Return default gm/Id-space parameters for the initial simulation.

        Builds a param dict with branch currents, gm_id, and L defaults
        from the topology spec.  Pass-through params can optionally be
        initialised from a netlist's .param declarations.

        Args:
            netlist_content: Optional DUT netlist text.  If provided, any
                             pass-through param (Cc, Rz, …) whose name
                             appears in a .param line will be picked up.

        Returns:
            Dict suitable as the starting point for BO.
        """
        params: dict[str, float] = {}

        # Branch currents
        for bc in self._spec.branch_currents:
            params[bc.name] = bc.default

        # Transistor gm_id and L
        mirror_output_roles = {
            mirror.output_role for mirror in self._spec.current_mirrors
        }
        pass_through_names = {
            param.name for param in self._spec.pass_through_params
        }
        fixed_names = set(self._spec.fixed_params)
        for ts in self._spec.transistors:
            if ts.role in mirror_output_roles:
                continue
            params[f"gm_id_{ts.role}"] = ts.gm_id_default
            if (
                ts.l_param not in self._spec.derived_length_params
                and ts.l_param not in pass_through_names
                and ts.l_param not in fixed_names
            ):
                params[f"L_{ts.role}"] = ts.L_default

        for mirror in self._spec.current_mirrors:
            params[mirror.ratio_param] = mirror.ratio_default

        # Pass-through: use defaults, override from netlist if available
        for pp in self._spec.pass_through_params:
            val = (pp.low + pp.high) / 2 if pp.log_scale else (pp.low + pp.high) / 2
            params[pp.name] = val

        # Try to initialise pass-through params from .param/parameters lines
        if netlist_content:
            import re
            for match in re.finditer(
                r"^\s*(?:\.param|parameters)\s+(.+)$",
                netlist_content,
                re.IGNORECASE | re.MULTILINE,
            ):
                line = match.group(1)
                for kv in re.finditer(r"(\w+)\s*=\s*(\S+)", line):
                    name = kv.group(1)
                    if name.upper() in ("NF", "M"):
                        continue
                    if name in params:
                        try:
                            from models import _parse_spice_suffix
                            params[name] = _parse_spice_suffix(kv.group(2))
                        except (ValueError, ImportError):
                            continue

        return params

    def clear_cache(self) -> None:
        """Clear the internal sizing cache."""
        self._cache.clear()
