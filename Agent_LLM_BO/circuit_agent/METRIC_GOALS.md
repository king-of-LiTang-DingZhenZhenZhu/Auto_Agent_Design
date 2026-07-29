# Metric Goals

BO uses lexicographic, feasibility-first metric policies:

1. Reduce hard-constraint violations until every requested metric is feasible.
2. Only inside the feasible region, optimize metrics marked `minimize`,
   `maximize`, or `target`.
3. Critical DC operating-point violations remain hard blockers.

## Requirements Schema

Legacy fields remain supported and are automatically converted to policies.
For example, `gain_db` becomes a minimum constraint and `power_w` becomes a
maximum constraint plus a feasible-region minimize objective.

Explicit policies override the legacy policy for the same metric:

```json
{
  "targets": {
    "gain_db": 60,
    "bandwidth_hz": 100000000,
    "power_w": 0.001
  },
  "metric_goals": {
    "gain_db": {
      "constraint": "min",
      "target": 60,
      "objective": "none",
      "priority": 1.0
    },
    "phase_margin_deg": {
      "constraint": "range",
      "low": 60,
      "high": 75,
      "objective": "target",
      "objective_target": 67.5,
      "priority": 1.0
    },
    "power_w": {
      "constraint": "max",
      "target": 0.001,
      "objective": "minimize",
      "priority": 1.0
    }
  }
}
```

## Supported Policies

- `constraint=min`: actual value must be at least `target`.
- `constraint=max`: actual value must not exceed `target`.
- `constraint=range`: actual value must remain within `low` and `high`.
- `constraint=target`: absolute error from `target` must not exceed `tolerance`.
- `objective=none`: stop rewarding the metric after it becomes feasible.
- `objective=minimize`: prefer smaller feasible values.
- `objective=maximize`: prefer larger feasible values.
- `objective=target`: prefer values closer to `objective_target` inside the feasible range.

`priority` scales that metric's violation and feasible-region objective. Avoid
large arbitrary priority differences; feasibility always outranks objectives.

## Default Legacy Policies

- Gain, GBW, PM, SR, PSRR: minimum constraints.
- Power: maximum constraint and minimize objective.
- Settling time, tempco, temperature nonlinearity, line regulation, startup
  time: maximum constraints.
- Vref: target constraint using `vref_tolerance_v`.

The resolved policies are persisted in `requirements.json`, `results.json`,
`optimization_log.json`, hierarchy metadata, and are reused by PVT and Review.
