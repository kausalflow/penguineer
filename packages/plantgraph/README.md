# plantgraph

The plant context graph schema used by [penguineer](../../README.md) and homeostat. It depends only on Pydantic.

A graph has typed nodes (`unit`, `equipment`, `stream`, `tag`, `loop`, `actuator`, `sample_point`, `quality`) and typed edges (`flows_to`, `measures`, `controls`, `manipulates`, `samples`, `reported_as`, `affects`). Every node and edge carries a stable `id`, a `provenance` (declared, inferred or confirmed), a `source` (user, llm, data, dexpi or homeostat), a `confidence` and `valid_from` / `valid_to` dates.

```python
from plantgraph import PlantGraph

graph = PlantGraph.model_validate(data)  # data loaded from YAML or JSON
graph.valid_on(date(2026, 8, 3))  # the plant as it was on that day
graph.to_data()  # back to a plain dict
```

Licensed under Apache-2.0.
