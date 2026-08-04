"""Physical implementation backend embedded in Auto_Agent_Design.

The upstream project exposes a much wider analog-design API.  This embedded
package intentionally publishes only the contracts used by the imported
netlist-to-layout flow so importing it cannot select or resize a topology.
"""

from .contracts import (
    Device,
    DeviceRole,
    LayoutConstraintSet,
    MatchGroup,
    NetRole,
    RoutingConstraint,
    TerminalRef,
    TopologyGraph,
)

__all__ = [
    "Device",
    "DeviceRole",
    "LayoutConstraintSet",
    "MatchGroup",
    "NetRole",
    "RoutingConstraint",
    "TerminalRef",
    "TopologyGraph",
]
