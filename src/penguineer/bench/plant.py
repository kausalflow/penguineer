"""The public bench plant: a Homeostat scenario family and the faults injected into it.

The plant is a small continuous reactor train. Two feeds, A and B, are measured by flowmeters
and mixed in M-101, whose outlet composition an online analyzer (AT-101) reads. The mixture is
heated in the exchanger E-201 (loop TIC-201, inlet temperature TT-200) and fed to the reactor
R-301 (loop TIC-301). The lab samples the product every 4 hours and reports an hour later.

In Homeostat terms: the mixer is the ``mixer`` template (MX-101), the exchanger the
``heat_exchanger`` template (TIC-201) and the reactor the ``reactor`` template (TIC-301), whose
product property follows an Arrhenius law in the reactor temperature, a power of the feed ratio
(the mixer's outlet property) and the catalyst activity. The feeds are Ornstein-Uhlenbeck
sources with flowmeters. Each drawn plant differs in its volumes, its kinetics and its noise;
two grades (setpoints of the reactor) alternate in the first day.

Every scenario injects exactly one fault, of one of the types in :data:`FAULTS`, between hours 40
and 56 of a 3-day run, so that at least eight in-spec lots of the same grade come before it.
This module needs nothing but the standard library; :mod:`penguineer.bench.export` runs it.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any

# The lab: one sample every 4 h, reported 1 h later, as in Homeostat's reactor template.
LAB_PERIOD_S = 4 * 3600
LAB_DELAY_S = 3600
LAB_SPAN = (0.0, 10.0)
LAB_NOISE_PCT = 1.0
DURATION_S = 3 * 86400
START = "2026-08-01T00:00:00"
PROPERTY = "product_property"

# Equipment node IDs for each Homeostat unit, in the plant graph.
EQUIPMENT = {"MX-101": "M-101", "TIC-201": "E-201", "TIC-301": "R-301"}

BASE: dict[str, Any] = {
    "seed": 0,
    "duration": "3d",
    "dt": "20s",
    "library": "process@1",
    "units": [
        {"id": "MX-101", "template": "mixer", "volume": {"uniform": [1.5, 3.0]}},
        {"id": "TIC-201", "template": "heat_exchanger", "sp": 60, "F_nom": 20},
        {
            "id": "TIC-301",
            "template": "reactor",
            "volume": {"uniform": [4.0, 7.0]},
            "E_R": {"uniform": [4000, 6000]},
            "b": {"uniform": [0.6, 1.0]},
            "sp": 80,
            "F_nom": 20,
            "lab_noise": LAB_NOISE_PCT,
        },
    ],
    "exogenous": [
        {
            "target": "MX-101.flow_a",
            "unit": "m3/h",
            "source": {"kind": "ou", "mean": 10, "std": {"uniform": [0.2, 0.5]}, "tau": "2h"},
            "measured": {"tag": "FT-101", "sensor": "magnetic_flowmeter", "span": [0, 30]},
        },
        {
            "target": "MX-101.flow_b",
            "unit": "m3/h",
            "source": {"kind": "ou", "mean": 10, "std": {"uniform": [0.2, 0.5]}, "tau": "3h"},
            "measured": {"tag": "FT-102", "sensor": "magnetic_flowmeter", "span": [0, 30]},
        },
        {
            "target": "MX-101.prop_a",
            "source": {"kind": "ou", "mean": 1.0, "std": {"uniform": [0.02, 0.06]}, "tau": "8h"},
        },
        {
            "target": "TIC-201.inlet_temp",
            "unit": "degC",
            "source": {"kind": "ou", "mean": 25, "std": 2, "tau": "6h"},
            "measured": {"tag": "TT-200", "sensor": "thermocouple", "span": [0, 100]},
        },
        {"target": "TIC-201.flow", "from": "MX-101.flow"},
        {"target": "TIC-301.flow", "from": "MX-101.flow"},
        {"target": "TIC-301.ratio", "from": "MX-101.out"},
        {"target": "TIC-301.feed_temp", "from": "TIC-201.cv"},
    ],
    "regimes": {"grade_A": {"TIC-301.sp": 80}, "grade_B": {"TIC-301.sp": 82}},
    "plan": {
        "start": "grade_A",
        "production": [
            {"at": "12h", "to": "grade_B", "over": "1h"},
            {"at": "24h", "to": "grade_A", "over": "1h"},
        ],
        "maintenance": [],
    },
    "output": {"every": "1min", "start": START},
}

ONSET = {"uniform": ["40h", "56h"]}


@dataclass(frozen=True)
class FaultType:
    """One kind of injected fault and what counts as finding it."""

    name: str
    intervention: dict[str, Any]
    # Node IDs and tag names. A process hypothesis hits when its tag is listed; any other kind
    # (a loop, a setpoint, a measurement, an unmeasured change) when its node or tag is.
    root_cause: tuple[str, ...]
    kind: str  # the hypothesis kind that describes it
    data_fault: bool = False
    description: str = ""


FAULTS: dict[str, FaultType] = {
    f.name: f
    for f in (
        FaultType(
            "feed_property",
            {"target": "MX-101.prop_b", "action": "shift", "value": {"uniform": [0.4, 1.0]}},
            ("S-B", "M-101", "AT-101"),
            "process",
            description="Feed B's composition steps up; the mixer analyzer sees it.",
        ),
        FaultType(
            "feed_flow",
            {"target": "MX-101.flow_a.mean", "action": "shift", "value": {"uniform": [3.0, 6.0]}},
            ("S-A", "FT-101"),
            "process",
            description="Feed A's flow rises, which changes the feed ratio and the residence.",
        ),
        FaultType(
            "reactor_setpoint",
            {"target": "TIC-301.sp", "action": "shift", "value": {"uniform": [-6.0, -3.0]}},
            ("TIC-301", "TIC-301.sp", "R-301"),
            "setpoint_change",
            description="Someone lowers the reactor temperature setpoint.",
        ),
        FaultType(
            "reactor_fouling",
            {
                "target": "TIC-301",
                "fault": "fouling",
                "magnitude": {"uniform": [0.25, 0.4]},
                "profile": "ramp",
                "over": "2h",
            },
            ("R-301", "TIC-301", "TIC-301.out"),
            "lost_control",
            description="The reactor's heating fouls until its valve saturates.",
        ),
        FaultType(
            "reactor_sensor_bias",
            {"target": "TIC-301", "fault": "sensor_bias", "magnitude": {"uniform": [2.0, 5.0]}},
            ("TT-301",),
            "measurement_fault",
            data_fault=True,
            description="The reactor thermocouple reads high; the loop cools the true "
            "temperature to match.",
        ),
        FaultType(
            "reactor_sensor_drift",
            {"target": "TIC-301", "fault": "sensor_drift", "magnitude": {"uniform": [3.0, 6.0]}},
            ("TT-301",),
            "measurement_fault",
            data_fault=True,
            description="The reactor thermocouple drifts high.",
        ),
        FaultType(
            "catalyst_loss",
            {
                "target": "TIC-301.activity.value",
                "action": "scale",
                "value": {"uniform": [0.7, 0.85]},
            },
            ("R-301", "TIC-301"),
            "unmeasured_disturbance",
            description="The catalyst loses activity; nothing measured moves but the product.",
        ),
        FaultType(
            "feed_b_flow",
            {"target": "MX-101.flow_b.mean", "action": "shift", "value": {"uniform": [-6.0, -3.0]}},
            ("S-B", "FT-102"),
            "process",
            description="Feed B's flow drops, which changes the feed ratio and the residence.",
        ),
    )
}

SPLIT_SEEDS = {"train": 1000, "validation": 2000}


def family_seed(split_seed: int, fault: str) -> int:
    """The seed of one fault type's family in a split, stable across Python versions."""
    digest = hashlib.sha256(f"{split_seed}:{fault}".encode()).hexdigest()
    return int(digest[:8], 16)


def scenario_config(fault: FaultType, seed: int, plants: int) -> dict[str, Any]:
    """The Homeostat family config of ``plants`` drawn plants with ``fault`` injected."""
    cfg = copy.deepcopy(BASE)
    cfg["seed"] = seed
    cfg["interventions"] = [{"at": ONSET, **copy.deepcopy(fault.intervention)}]
    cfg["family"] = {"plants": plants}
    return cfg
