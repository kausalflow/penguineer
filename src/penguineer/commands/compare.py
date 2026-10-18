from __future__ import annotations

from pathlib import Path

import polars as pl
from pydantic import Field, FilePath

from penguineer.core import CommandParams, CommandResult, RunContext, UsageError, command
from penguineer.engines.compare import welch
from penguineer.io.lims import read_table


class CompareParams(CommandParams):
    data: FilePath = Field(
        description="A table, CSV or Parquet: for example align's aligned.parquet",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    value: str = Field(description="Column to compare")
    group: str = Field(description="Column that splits the rows into groups")
    a: str = Field(description="Group A: rows whose --group equals this")
    b: str | None = Field(None, description="Group B (default: every row not in group A)")
    where: list[str] = Field(
        default_factory=list,
        description="Keep only rows where column=value; repeat to require several",
    )


class CompareResult(CommandResult):
    data: str
    value: str
    group: str
    a: str
    b: str | None
    where: list[str]
    n_a: int
    n_b: int
    mean_a: float
    mean_b: float
    sd_a: float | None
    sd_b: float | None
    diff: float = Field(description="mean_a - mean_b")
    t: float | None = Field(description="Welch's t")
    df: float | None = Field(description="Welch-Satterthwaite degrees of freedom")
    p_value: float | None = Field(description="Two-sided p-value of Welch's t-test")
    cohens_d: float | None = Field(description="diff over the root mean of the two variances")

    def summary(self) -> str:
        b = self.b if self.b is not None else f"not {self.a}"
        where = f" where {', '.join(self.where)}" if self.where else ""

        def num(x: float | None, fmt: str) -> str:
            return "n/a" if x is None else format(x, fmt)

        return "\n".join(
            [
                f"compare {self.value} by {self.group}{where}",
                f"  {self.a}: mean {self.mean_a:.4g} (n {self.n_a}, sd {num(self.sd_a, '.3g')})",
                f"  {b}: mean {self.mean_b:.4g} (n {self.n_b}, sd {num(self.sd_b, '.3g')})",
                f"  difference {self.diff:.4g}, Welch p {num(self.p_value, '.3g')}, "
                f"Cohen's d {num(self.cohens_d, '.3g')}",
            ]
        )


def _condition(table: pl.DataFrame, clause: str) -> pl.Expr:
    column, sep, value = clause.partition("=")
    if not sep or column not in table.columns:
        raise UsageError(f"--where {clause!r}: expected column=value with a column of the table")
    return pl.col(column).cast(pl.String) == value


@command("compare", params=CompareParams, result=CompareResult)
def compare(p: CompareParams, ctx: RunContext) -> CompareResult:
    """Compare a column between two groups: difference in means, Welch's t-test, Cohen's d."""
    table = read_table(Path(p.data))
    for col in (p.value, p.group):
        if col not in table.columns:
            raise UsageError(f"no column {col!r}; columns: {', '.join(table.columns)}")
    for clause in p.where:
        table = table.filter(_condition(table, clause))
    group = pl.col(p.group).cast(pl.String)
    values = pl.col(p.value).cast(pl.Float64, strict=False)
    a = table.filter(group == p.a).select(values).to_series().to_numpy()
    rest = group == p.b if p.b is not None else group != p.a
    b = table.filter(rest).select(values).to_series().to_numpy()
    try:
        c = welch(a, b)
    except ValueError as e:
        raise UsageError(
            f"compare {p.value}: {e} (group A {len(a)} rows, B {len(b)} rows)"
        ) from None
    return CompareResult(
        data=str(p.data), value=p.value, group=p.group, a=p.a, b=p.b, where=p.where, **vars(c)
    )
