"""DEXPI P&ID reader: equipment, piping connectivity, instrument functions and their valves.

A DEXPI file (Proteus XML, DEXPI 1.x) holds the P&ID as objects. This reader keeps what the
plant graph needs and ignores the drawing:

- Equipment: each top-level ``Equipment`` by its tag name, with its nozzles and sub-equipment.
- Piping: each ``Connection`` in a ``PipingNetworkSegment`` is a directed link from its
  ``FromID`` to its ``ToID`` (DEXPI draws connections in the flow direction). Following the links
  from one equipment's nozzles through pipes, tees and valves to the next equipment gives a flow.
  Off-page connectors are the boundaries: a ``FlowIn`` connector is a feed, ``FlowOut`` a product.
- Instruments: each ``ProcessInstrumentationFunction`` (category, functions and number: T, IC,
  201 is TIC-201), the sensors on its measuring lines and where they sit (``is located in``), and
  the valve its signal line drives (actuating function, actuating system, operated valve).

Everything here is as drawn. Where a sensor or valve sits on a pipe, not on equipment, the reader
names the equipment the pipe comes from (a sensor) or goes to (a valve), with a lower confidence.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from penguineer.core.errors import UsageError

# DEXPI equipment classes to plant graph equipment kinds; the first match wins.
EQUIPMENT_CLASSES = (
    ("HeatExchanger", "exchanger"),
    ("Cooler", "exchanger"),
    ("Heater", "heater"),
    ("Furnace", "furnace"),
    ("Pump", "pump"),
    ("Compressor", "compressor"),
    ("Blower", "blower"),
    ("Fan", "blower"),
    ("Column", "column"),
    ("Reactor", "reactor"),
    ("Mixer", "mixer"),
    ("Agitator", "mixer"),
    ("Tank", "tank"),
    ("Silo", "tank"),
    ("Vessel", "vessel"),
    ("Separator", "separator"),
    ("Filter", "filter"),
    ("Dryer", "dryer"),
    ("Centrifuge", "centrifuge"),
)

# Confidence of each kind of finding, as drawn: a nozzle on equipment is explicit; a pipe
# location is the reader's reading of where the pipe comes from or goes to.
FLOW_CONFIDENCE = 0.85
ON_EQUIPMENT = 0.85
ON_PIPE = 0.6
VALVE_TARGET = 0.7


@dataclass
class PidEquipment:
    id: str  # the DEXPI object ID
    name: str  # the tag name (R-301), or the ID when it has none
    component_class: str
    kind: str | None  # from the DEXPI class; None when the class does not say


@dataclass
class PidBoundary:
    id: str
    name: str
    direction: str  # "in" (a feed) or "out" (a product)


@dataclass
class PidFlow:
    from_name: str
    to_name: str
    valves: list[str] = field(default_factory=list)  # valves on the way, by name or ID


@dataclass
class PidSensor:
    name: str | None  # TT-201
    located_at: str | None  # equipment or boundary name
    confidence: float


@dataclass
class PidInstrument:
    category: str  # T
    functions: str  # IC
    number: str  # 201
    sensors: list[PidSensor] = field(default_factory=list)
    valve: str | None = None  # TV-201
    valve_target: str | None = None  # the equipment the valve's pipe goes to
    valve_confidence: float = 0.0

    @property
    def name(self) -> str:
        return f"{self.category}{self.functions}-{self.number}"


@dataclass
class Pid:
    equipment: list[PidEquipment]
    boundaries: list[PidBoundary]
    flows: list[PidFlow]
    instruments: list[PidInstrument]
    warnings: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(e: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in e if _local(c.tag) == name]


def _attributes(e: ET.Element) -> dict[str, str]:
    """The element's own generic attributes (not its children's)."""
    out = {}
    for group in _children(e, "GenericAttributes"):
        for a in _children(group, "GenericAttribute"):
            if a.get("Name") and a.get("Value") is not None:
                out[a.get("Name")] = a.get("Value")
    return out


def _attribute(e: ET.Element, *stems: str) -> str | None:
    attrs = _attributes(e)
    for stem in stems:
        for name, value in attrs.items():
            if name.startswith(stem) and value.strip():
                return value.strip()
    return None


def _associations(e: ET.Element, kind: str) -> list[str]:
    return [a.get("ItemID") for a in _children(e, "Association") if a.get("Type") == kind]


def _kind(component_class: str) -> str | None:
    for stem, kind in EQUIPMENT_CLASSES:
        if stem.lower() in component_class.lower():
            return kind
    return None


def _objects(root: ET.Element, name: str | None = None):
    """Plant objects, depth first; the shape catalogue's templates are not objects."""
    for child in root:
        if _local(child.tag) == "ShapeCatalogue":
            continue
        if name is None or _local(child.tag) == name:
            yield child
        yield from _objects(child, name)


def read_dexpi(path: Path) -> Pid:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise UsageError(f"{path}: not readable as XML ({exc})") from exc
    if _local(root.tag) != "PlantModel":
        found = _local(root.tag)
        raise UsageError(f"{path}: not a DEXPI file (the root is {found}, not PlantModel)")
    warnings: list[str] = []
    by_id = {e.get("ID"): e for e in _objects(root) if e.get("ID")}

    # Equipment, and which equipment each nozzle and sub-equipment belongs to.
    equipment: list[PidEquipment] = []
    owner: dict[str, str] = {}  # DEXPI ID -> equipment name
    for e in _children(root, "Equipment"):
        cls = e.get("ComponentClass") or "Equipment"
        name = e.get("TagName") or _attribute(e, "TagNameAssignmentClass", "TagName") or e.get("ID")
        if not name:
            continue
        equipment.append(PidEquipment(e.get("ID") or name, name, cls, _kind(cls)))
        for part in e.iter():
            if part.get("ID"):
                owner[part.get("ID")] = name
    names = [q.name for q in equipment]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        warnings.append(f"equipment {dup} is drawn more than once; its pieces are merged")

    boundaries: list[PidBoundary] = []
    for e in _objects(root, "PipeOffPageConnector"):
        cls = e.get("ComponentClass") or ""
        direction = "in" if "In" in cls else "out" if "Out" in cls else None
        if direction is None:
            continue
        name = _attribute(e, "PipeOffPageConnectorName", "ConnectorName", "Name") or e.get("ID")
        boundaries.append(PidBoundary(e.get("ID"), name, direction))
        owner[e.get("ID")] = name
    boundary_dir = {b.name: b.direction for b in boundaries}

    # Piping: directed links between DEXPI IDs, and the valves each segment carries.
    links: dict[str, list[tuple[str, str]]] = defaultdict(list)
    back: dict[str, list[tuple[str, str]]] = defaultdict(list)
    segment_of: dict[str, str] = {}
    segment_valves: dict[str, list[str]] = defaultdict(list)
    segment_ends: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for seg in _objects(root, "PipingNetworkSegment"):
        seg_id = seg.get("ID") or f"segment-{len(segment_of)}"
        for comp in seg:
            if comp.get("ID"):
                segment_of[comp.get("ID")] = seg_id
            if "Valve" in (comp.get("ComponentClass") or ""):
                label = _attribute(comp, "PipingComponentName", "TagName") or comp.get("ID")
                segment_valves[seg_id].append(label)
        for conn in _children(seg, "Connection"):
            a, b = conn.get("FromID"), conn.get("ToID")
            if a or b:
                segment_ends[seg_id].append((a, b))
            if a and b:
                links[a].append((b, seg_id))
                back[b].append((a, seg_id))

    def walk(starts: list[str], graph, stop_at_start: str | None) -> dict[str, set[str]]:
        """Names of the equipment or boundaries first reached from ``starts``, with segments."""
        found: dict[str, set[str]] = {}
        queue = deque((s, frozenset()) for s in starts)
        seen = set(starts)
        while queue:
            point, segs = queue.popleft()
            for nxt, seg in graph.get(point, ()):
                path = segs | {seg}
                hit = owner.get(nxt)
                if hit is not None:
                    if hit != stop_at_start:
                        found.setdefault(hit, set()).update(path)
                    continue
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, path))
        return found

    points_of: dict[str, list[str]] = defaultdict(list)
    for point, name in owner.items():
        points_of[name].append(point)

    flows: list[PidFlow] = []
    sources = names + [b.name for b in boundaries if b.direction == "in"]
    for src in dict.fromkeys(sources):
        for dst, segs in sorted(walk(points_of[src], links, src).items()):
            if boundary_dir.get(dst) == "in":
                warnings.append(f"the pipe from {src} runs into the feed {dst}: drawn backwards?")
                continue
            valves = [v for s in sorted(segs) for v in segment_valves[s]]
            flows.append(PidFlow(src, dst, valves))

    def place(item: str | None, downstream: bool) -> tuple[str | None, float]:
        """Equipment an object sits on, else the equipment its pipe comes from (or goes to)."""
        if item is None:
            return None, 0.0
        if item in owner:
            return owner[item], ON_EQUIPMENT
        segment = item if item in segment_ends else segment_of.get(item)
        # The segment's end on that side, then the pipe beyond it; then the other side (a pipe
        # drawn open at one end).
        for side in (downstream, not downstream):
            ends = [e for a, b in segment_ends.get(segment, []) if (e := b if side else a)]
            for end in ends:
                if end in owner:
                    return owner[end], ON_PIPE
            found = walk([item, *ends], links if side else back, None)
            if len(found) == 1:
                return next(iter(found)), ON_PIPE
        return None, 0.0

    # Instrumentation: lines between functions, and the functions themselves.
    line_ends: dict[str, tuple[str | None, str | None]] = {}
    for e in _objects(root, "InformationFlow"):
        if e.get("ID"):
            start = (_associations(e, "has logical start") or [None])[0]
            end = (_associations(e, "has logical end") or [None])[0]
            line_ends[e.get("ID")] = (start, end)

    instruments: list[PidInstrument] = []
    for pif in _objects(root, "ProcessInstrumentationFunction"):
        category = _attribute(pif, "ProcessInstrumentationFunctionCategory")
        functions = _attribute(pif, "ProcessInstrumentationFunctions") or ""
        number = _attribute(pif, "ProcessInstrumentationFunctionNumber")
        if not category or not number:
            warnings.append(f"instrument function {pif.get('ID')} has no category or number")
            continue
        inst = PidInstrument(category, functions, number)
        pif_id = pif.get("ID")
        for start, end in line_ends.values():
            other = start if end == pif_id else end if start == pif_id else None
            target = by_id.get(other) if other else None
            if target is None:
                continue
            kind = (
                _local(target.tag)
                if target.get("ComponentClass") is None
                else (target.get("ComponentClass"))
            )
            if kind == "ProcessSignalGeneratingFunction" and end == pif_id:
                located = (_associations(target, "is located in") or [None])[0]
                at, confidence = place(located, downstream=False)
                sensor = _attribute(target, "ProcessSignalGeneratingFunctionNumber")
                inst.sensors.append(PidSensor(sensor, at, confidence))
            elif kind == "ActuatingFunction" and start == pif_id and inst.valve is None:
                inst.valve = _attribute(target, "ActuatingFunctionNumber")
                for system_id in _associations(target, "is fulfilled by"):
                    system = by_id.get(system_id)
                    if system is None:
                        continue
                    inst.valve = _attribute(system, "ActuatingSystemNumber") or inst.valve
                    for comp in system.iter():
                        for valve_id in _associations(comp, "refers to"):
                            at, confidence = place(valve_id, downstream=True)
                            if at is not None:
                                inst.valve_target = at
                                inst.valve_confidence = min(confidence, VALVE_TARGET)
        instruments.append(inst)
    return Pid(equipment, boundaries, flows, instruments, warnings)
