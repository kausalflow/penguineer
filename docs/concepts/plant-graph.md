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

## Drafting a graph from a tag list

`penguineer graph draft` writes a first `graph.yaml` from the historian's tag list, so that an
engineer starts by confirming a draft rather than typing every node.

```bash
penguineer graph draft --tags tags.csv
penguineer graph check --graph graph.yaml
```

The tag list is a CSV with a `tag` column and, if the export has them, `description`, `unit`,
`equipment`, `range_low` and `range_high` (common alternatives such as `TagName`, `UOM` or `Max`
are recognized).

```text
graph draft: 14 tags, 2 loops, 2 valves, 2 equipment; 5 edges, all inferred
wrote graph.yaml
1 tags without an ISA-5.1 name: GRADE
warning: TT-305: the name says temperature but the unit 'bar' is a pressure
note: no piping (flows_to) in a tag list: add it, or confirm what a P&ID or the LLM proposes
next: penguineer graph check --graph graph.yaml
```

The draft reads each name with ISA-5.1 conventions:

| Part | Example | Read as |
|---|---|---|
| First letter | **F**IC-102, **T**T-301, **P**DI-5 | The measured variable: flow, temperature, pressure, level (L), analysis (A), ... |
| Modifier | P**D**I-5, F**Q**I-7 | Differential, totalized or ratio |
| Function letters | FI**C**-102, F**T**-102, F**V**-102 | Controller, transmitter, indicator, valve, ... |
| Number | FIC-**102** | The loop: FIC-102, FT-102 and FV-102 belong together |
| Prefix | **11**FIC1001, **U1**.TI101 | A plant or area code, kept as part of the loop |
| Suffix | FIC-102.**SP**, TIC-301.**out** | The signal: `PV`, `SP`, `OP` (or `OUT`, `MV`), `MODE` |

From that it drafts:

- a `tag` node for every tag, with its role (measurement, setpoint, output, mode);
- a `loop` for every group with a controller (letter C) or a setpoint or output signal: its PV
  is the controller's own measurement, else the group's transmitter or indicator;
- the `actuator` each loop drives, from the group's valve tag (`FV-102`) or named by
  convention, with a `controls` edge;
- `equipment` from the `equipment` column, or from equipment IDs in the descriptions
  ("Reactor R-101 temperature" gives R-101, a reactor), with a `measures` edge from the tag.

Every drafted node and edge is `inferred`, with a confidence (lower when it comes from a
description or a naming convention than from a column or a tag in the list), so all of them
appear in `graph check`'s confirm queue. A unit that contradicts the name is reported. Tags
without an ISA-5.1 name, such as `GRADE`, become tag nodes without a role.

A tag list does not say how material flows, so the draft has no `flows_to` edges, no streams,
sample points or qualities: those come from the engineer. Reading the piping from a DEXPI P&ID
(`--pid`) and LLM-proposed relations (`--llm`) are not built yet.
