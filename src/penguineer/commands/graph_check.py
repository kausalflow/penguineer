from __future__ import annotations

from pydantic import Field, FilePath

from penguineer.core import CommandParams, CommandResult, RunContext, command
from penguineer.graph.check import ConfirmItem, GraphIssue, IssueLevel, check_graph
from penguineer.io.graph import load_graph


class GraphCheckParams(CommandParams):
    graph: FilePath = Field(
        description="Plant graph in the plantgraph schema",
        json_schema_extra={"ui": "file", "accept": [".yaml", ".yml"]},
    )


class GraphCheckResult(CommandResult):
    graph: str
    n_nodes: int
    n_edges: int
    errors: int
    warnings: int
    issues: list[GraphIssue]
    confirm_queue: list[ConfirmItem]

    def exit_code(self) -> int:
        return 1 if self.errors else 0

    def summary(self) -> str:
        lines = [
            f"graph check: {self.n_nodes} nodes, {self.n_edges} edges, "
            f"{self.errors} errors, {self.warnings} warnings"
        ]
        lines += [f"  {i.level:<7} {i.code:<20} {i.message}" for i in self.issues]
        if self.confirm_queue:
            lines.append(f"confirm queue ({len(self.confirm_queue)} inferred edges):")
            lines += [
                f"  {c.edge_id}: {c.from_id} -{c.type}-> {c.to_id} "
                f"(confidence {c.confidence:.2f}, {c.dependent_qualities} qualities depend on it)"
                for c in self.confirm_queue[:20]
            ]
        return "\n".join(lines)


@command("graph check", params=GraphCheckParams, result=GraphCheckResult)
def graph_check(p: GraphCheckParams, ctx: RunContext) -> GraphCheckResult:
    """Validate the plant graph and list inferred edges to confirm, most important first."""
    graph = load_graph(p.graph)
    issues, queue = check_graph(graph)
    return GraphCheckResult(
        graph=str(p.graph),
        n_nodes=len(graph.nodes),
        n_edges=len(graph.edges),
        errors=sum(1 for i in issues if i.level == IssueLevel.error),
        warnings=sum(1 for i in issues if i.level == IssueLevel.warning),
        issues=issues,
        confirm_queue=queue,
    )
