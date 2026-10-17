"""The ranking's tunable numbers: test weights, rule scores and confidence thresholds.

They are fitted on the public homeostat splits (``scripts/fit_calibration.py``): the weights and
rule scores on the training split, the confidence thresholds on the validation split. The fitted
values ship in ``calibration.json`` next to this module, with where they came from, and every
report records them.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, Field


class FittedOn(BaseModel):
    weights: str = Field(description="Split and size the weights and rule scores were fitted on")
    thresholds: str = Field(description="Split and size the thresholds were chosen on")
    homeostat: str | None = None
    plant: str | None = Field(None, description="Hash of the bench plant and its faults")
    date: str | None = None
    hit_at_1: float | None = Field(None, description="On the split the weights were fitted on")
    wrong_confident_rate: float | None = Field(
        None, description="On the split the thresholds were chosen on"
    )


class Calibration(BaseModel):
    # Weights of the three tests in a candidate's score.
    effect: float = 0.4
    onset: float = 0.3
    lag: float = 0.3
    # A loop held its PV while its output moved and nothing measured upstream moved: factors on
    # the output move's score for "the PV reads wrong" and "something unmeasured disturbed it".
    pv_bias: float = 0.8
    loop_disturbance: float = 0.4
    # Nothing measured moved clearly (no effect strength reaches hidden_below): the score of
    # "something unmeasured changed just upstream of the sample".
    hidden: float = 0.35
    hidden_below: float = 0.5
    # Health findings in the lot window, by severity.
    fault_critical: float = 0.6
    fault_warning: float = 0.45
    # Confidence: high needs the score and a margin over the next hypothesis; medium the score.
    high: float = 0.65
    medium: float = 0.4
    margin: float = 0.15
    fitted_on: FittedOn | None = None

    def describe(self) -> str:
        text = (
            f"Confidence: high when the score is at least {self.high:g} and {self.margin:g} "
            f"ahead of the next hypothesis, medium from {self.medium:g}. Test weights: effect "
            f"size {self.effect:g}, onset {self.onset:g}, lag correlation {self.lag:g}."
        )
        if self.fitted_on is None:
            return text + " Provisional: not fitted on the homeostat bench."
        f = self.fitted_on
        return (
            text + f" Weights fitted on {f.weights}, thresholds chosen on {f.thresholds}"
            f" (homeostat {f.homeostat}, bench plant {f.plant})."
        )


@lru_cache(maxsize=1)
def default_calibration() -> Calibration:
    """The shipped calibration (the provisional defaults if ``calibration.json`` is absent)."""
    path = resources.files("penguineer.engines").joinpath("calibration.json")
    if not path.is_file():
        return Calibration()
    return Calibration.model_validate(json.loads(path.read_text()))
