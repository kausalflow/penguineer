"""The investigation agent: a tool loop, the method it follows, and the tools it can call.

penguineer owns how an investigation is run; a host such as Alchemist supplies the data tools
(a catalog, SQL, a notebook), decides which models may be used, and stores and seals the run.

- :mod:`~penguineer.agent.loop`: the tool loop, budgets, scripted models for tests
- :mod:`~penguineer.agent.models`: any provider through Pydantic AI (``penguineer[agent]``)
- :mod:`~penguineer.agent.protocol`: prompt blocks, the protocol's tools, the independent review
- :mod:`~penguineer.agent.findings`: findings and ``{{Fn}}`` placeholders in a conclusion
- :mod:`~penguineer.agent.knowledge`: search over plant documents
- :mod:`~penguineer.agent.tools`: penguineer's commands as tools
"""

from penguineer.agent.findings import ReportError, fill, finding, fmt
from penguineer.agent.loop import (
    Budget,
    Model,
    ScriptedModel,
    ScriptedText,
    ToolOutput,
    run_loop,
    strip_images,
)
from penguineer.agent.protocol import TOOLS, review

__all__ = [
    "TOOLS",
    "Budget",
    "Model",
    "ReportError",
    "ScriptedModel",
    "ScriptedText",
    "ToolOutput",
    "fill",
    "finding",
    "fmt",
    "review",
    "run_loop",
    "strip_images",
]
