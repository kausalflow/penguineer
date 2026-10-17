# The plant graph

The plant context graph lives in its own small package, `plantgraph`, which depends only on Pydantic. penguineer and homeostat both import it, so a real plant and a simulated one are described by the same types.

Every node and edge carries a stable `id`, a `provenance` (`declared`, `inferred` or `confirmed`), a `source` (`user`, `llm`, `data`, `dexpi` or `homeostat`), a `confidence` between 0 and 1, and `valid_from` and `valid_to` dates. `graph.valid_on(day)` gives the plant as it was on that day.

## Nodes

| Type | What it represents | Key fields |
|---|---|---|
| `unit` | The scope of one project | `name` |
| `equipment` | Vessels, exchangers, columns, pumps | `kind`, `holdup_m3` |
| `stream` | A material flow between equipment | `phase` |
| `tag` | One historian signal | `tag`, `unit_of_measure`, `range_low`, `range_high`, `role` |
| `loop` | A control loop | `pv`, `sp`, `op`, `mode` (IDs of tag nodes) |
| `actuator` | A valve or drive a loop moves | `kind` |
| `sample_point` | Where a lab sample is taken | |
| `quality` | A LIMS property with a spec | `property`, `spec_low`, `spec_high` |

A tag's `role` is `measurement`, `setpoint`, `output`, `mode` or `setting`. Setpoints, modes and settings may sit still for days, so `data health` never reports them as frozen.

## Edges

| Type | From → to | Key fields |
|---|---|---|
| `flows_to` | equipment or stream → equipment or stream | `dead_time_min` with an interval, `recycle` |
| `measures` | tag → equipment or stream | `measured_variable` |
| `controls` | loop → actuator | `acting` |
| `manipulates` | actuator → equipment or stream | |
| `samples` | sample point → equipment or stream | |
| `reported_as` | sample point → quality | |
| `affects` | any → any | a cause link an engineer asserts |

## In YAML

```yaml
schema_version: 1
unit: demo-reactor
nodes:
  - {id: R-101, type: equipment, kind: vessel, holdup_m3: 12.0}
  - {id: TI-101, type: tag, tag: TI-101.PV, unit_of_measure: degC, range_low: 0, range_high: 300}
edges:
  - {id: m1, type: measures, from: TI-101, to: R-101, measured_variable: temperature}
```

## Checking a graph

`penguineer graph check --graph graph.yaml` validates the schema and these plant rules: unique IDs, no edges to unknown nodes, edge endpoints of the right types, every loop with a PV and an OP, `flows_to` cycles only through an edge flagged as `recycle`, and every quality reachable from a sample point on the plant. It then lists the confirm queue: inferred edges, sorted by how many qualities depend on them. It exits with 1 when there are errors.
