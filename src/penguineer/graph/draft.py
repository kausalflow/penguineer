"""Graph draft, rule-based pass: a tag list into Tag, Loop, Actuator and Equipment nodes.

Tag names are read with ISA-5.1 conventions. ``FIC-102.SP`` is the setpoint of flow controller
102; ``FI-102.PV`` and ``FT-102`` measure the same flow; ``FV-102`` is its valve. A first letter
gives the measured variable (F flow, T temperature, ...), the letters after it the function
(I indicate, C control, T transmit, V valve, ...), the number the loop. A suffix after a dot,
colon or slash names the signal: PV, SP, OP (or OUT, MV), MODE. A plant prefix (``11-``,
``U1.``) is kept as part of the loop.

From that the draft groups tags into loops (a controller letter C, or a setpoint or output
signal, makes a loop), adds the valve a loop's output drives, and reads equipment from the
``equipment`` column or from equipment IDs in the descriptions (``Reactor R-101 temperature``).
Units are checked against the variable the name gives.

Everything it adds is ``inferred`` with a confidence: the engineer confirms it with
``graph check``'s confirm queue. Piping (``flows_to``) is not in a tag list; it comes from the
engineer, a P&ID, or the LLM pass.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from plantgraph import (
    Actuator,
    Edge,
    EdgeType,
    Equipment,
    Loop,
    PlantGraph,
    Provenance,
    Source,
    Tag,
)

from penguineer.core.errors import UsageError

# ISA-5.1 first letters: the measured or initiating variable.
VARIABLES = {
    "A": "analysis",
    "B": "flame",
    "C": "conductivity",
    "D": "density",
    "E": "voltage",
    "F": "flow",
    "G": "gauging",
    "H": "hand",
    "I": "current",
    "J": "power",
    "K": "time",
    "L": "level",
    "M": "moisture",
    "P": "pressure",
    "Q": "quantity",
    "R": "radiation",
    "S": "speed",
    "T": "temperature",
    "U": "multivariable",
    "V": "vibration",
    "W": "weight",
    "X": "unclassified",
    "Y": "event",
    "Z": "position",
}
# Variable modifiers between the first letter and the function letters (PDIC, FQI, FFIC).
MODIFIERS = {"D": "differential", "F": "ratio", "Q": "totalized"}
# Signal suffixes after a dot, colon or slash.
SUFFIXES = {
    "PV": "measurement",
    "MEAS": "measurement",
    "CV": "measurement",
    "SP": "setpoint",
    "SV": "setpoint",
    "OP": "output",
    "OUT": "output",
    "MV": "output",
    "MODE": "mode",
    "MD": "mode",
}
# Equipment ID prefixes common on P&IDs.
EQUIPMENT_KINDS = {
    "B": "blower",
    "C": "column",
    "D": "drum",
    "E": "exchanger",
    "F": "filter",
    "H": "heater",
    "K": "compressor",
    "M": "mixer",
    "P": "pump",
    "R": "reactor",
    "T": "tank",
    "TK": "tank",
    "V": "vessel",
    "X": "package",
}
# Units that name a variable, to check a tag's letter against.
UNIT_VARIABLES = {
    "flow": {"m3/h", "m³/h", "nm3/h", "kg/h", "kg/s", "t/h", "l/min", "l/h", "gpm", "scfm"},
    "temperature": {"degc", "°c", "c", "degf", "°f", "k"},
    "pressure": {"bar", "barg", "bara", "mbar", "kpa", "mpa", "pa", "psi", "psig", "kg/cm2"},
    "level": {"mm"},
}

_SIGNAL = re.compile(r"^(?P<base>.+?)(?:[.:/](?P<suffix>[A-Za-z]+))?$")
_ISA = re.compile(r"(?P<letters>[A-Z]{1,6})[-_]?(?P<number>\d{1,6}[A-Z]?)$")
_EQUIPMENT_ID = re.compile(r"\b(?P<letters>[A-Z]{1,2})-?(?P<number>\d{2,5}[A-Z]?)\b")

TAG_COLUMNS = {
    "tag": ("tag", "tagname", "tag_name", "name"),
    "description": ("description", "desc", "descriptor"),
    "unit": ("unit", "units", "uom", "eng_unit", "engineering_unit"),
    "equipment": ("equipment", "equipment_id", "location"),
    "range_low": ("range_low", "low", "min"),
    "range_high": ("range_high", "high", "max"),
}


@dataclass
class ParsedTag:
    tag: str
    base: str  # without the signal suffix
    suffix: str | None
    prefix: str  # plant or area prefix before the ISA letters
    letters: str | None
    number: str | None

    def _split(self) -> tuple[str | None, str]:
        """(variable modifier letter, function letters). PDIC: D, IC; FT: None, T."""
        rest = (self.letters or "")[1:]
        if len(rest) >= 2 and rest[0] in MODIFIERS:
            return rest[0], rest[1:]
        return None, rest

    @property
    def variable(self) -> str | None:
        if not self.letters or self.letters[0] not in VARIABLES:
            return None
        modifier, _ = self._split()
        v = VARIABLES[self.letters[0]]
        return f"{MODIFIERS[modifier]} {v}" if modifier else v

    @property
    def functions(self) -> str:
        return self._split()[1]

    @property
    def loop_key(self) -> tuple[str, str, str] | None:
        if not self.letters or not self.number:
            return None
        return (self.prefix, self.letters[0], self.number)

    @property
    def is_valve(self) -> bool:
        return self.functions in ("V", "CV") and self.suffix is None

    @property
    def is_controller(self) -> bool:
        return "C" in self.functions and not self.is_valve

    @property
    def role(self) -> str | None:
        if self.suffix and self.suffix.upper() in SUFFIXES:
            return SUFFIXES[self.suffix.upper()]
        if not self.letters:
            return None
        if self.is_valve:
            return "output"
        if set(self.functions) & {"I", "T", "R", "E"} or self.is_controller:
            return "measurement"
        return None


def parse_tag(tag: str) -> ParsedTag:
    m = _SIGNAL.match(tag.strip())
    base, suffix = (m.group("base"), m.group("suffix")) if m else (tag, None)
    if suffix and suffix.upper() not in SUFFIXES:
        base, suffix = tag.strip(), None  # "TI-101.A" is part of the name, not a signal
    isa = _ISA.search(base)
    if isa is None:
        return ParsedTag(tag, base, suffix, "", None, None)
    prefix = base[: isa.start()].rstrip("-_. ")
    return ParsedTag(tag, base, suffix, prefix, isa.group("letters"), isa.group("number"))


@dataclass
class TagRow:
    tag: str
    description: str | None = None
    unit: str | None = None
    equipment: str | None = None
    range_low: float | None = None
    range_high: float | None = None


def read_tag_list(path: Path) -> list[TagRow]:
    """A CSV tag list: ``tag`` and optionally description, unit, equipment, range_low/high."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = {h.strip().lower(): h for h in reader.fieldnames or []}
        cols = {
            key: next((header[a] for a in aliases if a in header), None)
            for key, aliases in TAG_COLUMNS.items()
        }
        if cols["tag"] is None:
            raise UsageError(f"{path}: no tag column (one of {', '.join(TAG_COLUMNS['tag'])})")
        rows: list[TagRow] = []
        for i, raw in enumerate(reader, start=2):

            def get(key: str, raw: dict = raw) -> str | None:
                v = raw.get(cols[key]) if cols[key] else None
                return v.strip() or None if isinstance(v, str) else None

            tag = get("tag")
            if not tag:
                continue

            def num(key: str, i: int = i) -> float | None:
                v = get(key)
                try:
                    return float(v) if v is not None else None
                except ValueError as e:
                    raise UsageError(f"{path}, line {i}: {key} {v!r} is not a number") from e

            rows.append(
                TagRow(
                    tag=tag,
                    description=get("description"),
                    unit=get("unit"),
                    equipment=get("equipment"),
                    range_low=num("range_low"),
                    range_high=num("range_high"),
                )
            )
    if not rows:
        raise UsageError(f"{path}: no tags")
    return rows


@dataclass
class Draft:
    graph: PlantGraph
    warnings: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)  # tags with no ISA-5.1 name


def _unit_variable(unit: str | None) -> str | None:
    u = (unit or "").strip().lower().replace(" ", "")
    return next((v for v, units in UNIT_VARIABLES.items() if u in units), None)


def _node_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", text).strip("-")


def draft_from_tags(rows: list[TagRow], unit: str | None = None) -> Draft:
    """The rule-based draft: nodes for tags, loops, valves and equipment; inferred edges."""
    nodes: list = []
    edges: list[Edge] = []
    warnings: list[str] = []
    unparsed: list[str] = []
    seen_tags: set[str] = set()
    parsed: dict[str, ParsedTag] = {}
    groups: dict[tuple[str, str, str], list[ParsedTag]] = defaultdict(list)
    bases = set()
    for row in rows:
        if row.tag in seen_tags:
            warnings.append(f"{row.tag} is listed twice; the first row is used")
            continue
        seen_tags.add(row.tag)
        p = parse_tag(row.tag)
        parsed[row.tag] = p
        bases.add(p.base)
        if p.loop_key is None:
            unparsed.append(row.tag)
        else:
            groups[p.loop_key].append(p)

    def inferred(**kw):
        return {"provenance": Provenance.inferred, "source": Source.data, **kw}

    tag_ids: dict[str, str] = {}
    for row in rows:
        p = parsed.get(row.tag)
        if p is None or row.tag in tag_ids:
            continue
        tag_id = _node_id(row.tag)
        tag_ids[row.tag] = tag_id
        variable = p.variable
        by_unit = _unit_variable(row.unit)
        if variable and by_unit and by_unit not in variable and variable in UNIT_VARIABLES:
            warnings.append(
                f"{row.tag}: the name says {variable} but the unit {row.unit!r} is a {by_unit}"
            )
        nodes.append(
            Tag(
                id=tag_id,
                tag=row.tag,
                unit_of_measure=row.unit,
                range_low=row.range_low,
                range_high=row.range_high,
                role=p.role,
                description=row.description,
                **inferred(confidence=0.9 if p.role else 0.5),
            )
        )

    # Equipment: the equipment column, or equipment IDs in descriptions that are not tags.
    equipment: dict[str, Equipment] = {}

    def add_equipment(eq_id: str, confidence: float) -> str:
        node_id = _node_id(eq_id)
        if node_id not in equipment:
            letters = re.match(r"[A-Z]+", eq_id)
            kind = EQUIPMENT_KINDS.get(letters.group(0)) if letters else None
            equipment[node_id] = Equipment(id=node_id, kind=kind, **inferred(confidence=confidence))
        return node_id

    measures: list[tuple[str, str, str | None, float]] = []
    for row in rows:
        p = parsed[row.tag]
        if row.tag not in tag_ids:
            continue
        target, confidence = None, 0.0
        if row.equipment:
            target, confidence = add_equipment(row.equipment, 0.9), 0.9
        elif row.description:
            for m in _EQUIPMENT_ID.finditer(row.description):
                candidate = m.group(0)
                letters = m.group("letters")
                if candidate in bases or parse_tag(candidate).base in bases:
                    continue  # another tag's name, not equipment
                if letters not in EQUIPMENT_KINDS:
                    continue
                target, confidence = add_equipment(candidate, 0.6), 0.6
                break
        if target is not None and p.role in ("measurement", None) and not p.is_valve:
            measures.append((tag_ids[row.tag], target, p.variable, confidence))
    nodes.extend(equipment.values())
    for i, (tag_id, target, variable, confidence) in enumerate(measures, start=1):
        edges.append(
            Edge(
                id=f"draft-m{i}",
                type=EdgeType.measures,
                from_id=tag_id,
                to_id=target,
                measured_variable=variable,
                **inferred(confidence=confidence),
            )
        )

    # Loops: a controller (letter C) or a setpoint/output signal in a group makes one.
    n_controls = 0
    for key in sorted(groups):
        members = groups[key]
        controllers = [p for p in members if p.is_controller]
        signals = {p.role: p for p in members if p.suffix}
        if not controllers and not ({"setpoint", "output"} & set(signals)):
            continue
        prefix, letter, number = key
        name = controllers[0].base if controllers else f"{letter}C-{number}"
        loop_id = _node_id(f"loop:{name}")

        def pick(role: str, among: list[ParsedTag]) -> str | None:
            for p in among:
                if p.role == role:
                    return tag_ids[p.tag]
            return None

        # The PV: the controller's own measurement first, then a transmitter or indicator.
        ordered = sorted(
            members,
            key=lambda p: (not p.is_controller, "T" not in p.functions, p.suffix is None),
        )
        valves = [p for p in members if p.is_valve]
        pv = pick("measurement", [p for p in ordered if not p.is_valve])
        op = pick("output", [p for p in ordered if not p.is_valve])
        loop = Loop(
            id=loop_id,
            pv=pv,
            sp=pick("setpoint", ordered),
            op=op,
            mode=pick("mode", ordered),
            description=f"{VARIABLES.get(letter, letter)} loop {number}"
            + (f" ({prefix})" if prefix else ""),
            **inferred(confidence=0.9 if controllers else 0.7),
        )
        nodes.append(loop)
        if pv is None:
            warnings.append(f"{loop_id}: no process value found among {[p.tag for p in members]}")
        # The valve the loop drives: a valve tag in the group, else one named by convention.
        valve_base = valves[0].base if valves else None
        valve_id = (
            _node_id(valve_base)
            if valve_base
            else _node_id(f"{prefix + '-' if prefix else ''}{letter}V-{number}")
        )
        if valve_id in tag_ids.values():
            valve_id = f"{valve_id}-valve"
        if not any(n.id == valve_id for n in nodes):
            nodes.append(
                Actuator(
                    id=valve_id,
                    kind="valve",
                    description=f"valve of {name}",
                    **inferred(confidence=0.8 if valves else 0.5),
                )
            )
        n_controls += 1
        edges.append(
            Edge(
                id=f"draft-c{n_controls}",
                type=EdgeType.controls,
                from_id=loop_id,
                to_id=valve_id,
                **inferred(confidence=0.8 if valves else 0.5),
            )
        )
    return Draft(PlantGraph(unit=unit, nodes=nodes, edges=edges), warnings, unparsed)
