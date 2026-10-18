# Lots in material time, and comparing groups

A lab result belongs to a lot, but the process values that made the lot were measured earlier
and further upstream. `penguineer align` lines them up: for each lot it averages every tag over
the window that produced it, shifted back by that tag's dead time to the point where the lot is
measured. `penguineer compare` then tests a difference between two groups of lots.

## align

```bash
penguineer align --data historian.csv --lots lots.csv \
  --graph graph.yaml --sample-point SP-1 \
  --tags TI-101.PV --tags TI-104.PV --tags FI-102.PV
```

```text
align: 9 lots, 3 tags
  TI-101.PV: dead time 50 min (graph)
  TI-104.PV: dead time 45 min (graph)
  FI-102.PV: dead time 52 min (graph)
table: runs/<run_id>/aligned.parquet
```

Material leaving between a lot's start and end passed a tag with dead time *D* between
start − *D* and end − *D*, so each tag is averaged over its own shifted window, `[start − D, end − D)`.

Where the dead time comes from:

- **From the graph** (`--graph` with `--sample-point`): the sum of the `dead_time_min` of the
  `flows_to` edges from the tag's node to the sampled node, the same paths
  [investigate](investigate.md) uses. A sample point stands for the stream or vessel it samples.
  Tags that are not upstream are left out and listed in the notes.
- **Fixed** (`--dead-time-min`): one residence time for every tag, or, with a graph, for the tags
  the graph cannot place.

The lots file needs `lot`, `start` and `end` columns (also spelled `lot_id`, `batch`,
`start_ts`, `end_ts` and similar), as CSV or Parquet. Its other columns, such as a resin lot or a
crew, are kept, so the table can be grouped by them.

The output, `aligned.parquet` and `aligned.csv` in the run folder, has one row per lot: the lot's
columns, then `<tag>` (the mean) and `<tag>_n` (the samples in the window). A window with no
samples gives an empty mean and `_n` 0, and the summary counts them per tag. The `--json` result
also carries the rows.

When the dead time itself is uncertain, fit it first with [lag](lag.md) or
[graph lags](dead-times.md).

## compare

```bash
penguineer compare --data aligned.parquet --value TI-101.PV --group day --a "3 Aug"
```

```text
compare TI-101.PV by day
  3 Aug: mean 175.7 (n 3, sd 2.91)
  not 3 Aug: mean 180.7 (n 6, sd 1.56)
  difference -4.996, Welch p 0.0814, Cohen's d -2.14
```

Group A is the rows whose `--group` column equals `--a`; group B is `--b`, or every other row.
`--where column=value` (repeatable) keeps only matching rows first, for example the lots of one
week. Missing values are dropped.

The result has the means, standard deviations and sizes of both groups, the difference
(A − B), Welch's t with its degrees of freedom and two-sided p-value, and Cohen's *d* (the
difference over the root mean of the two variances). With fewer than two values in a group there
is no spread, so the p-value and *d* are empty. Welch's test does not assume equal variances,
which suits lots from different periods or raw materials.

A small p-value says the groups differ more than chance would make them; it does not say why.
To rank causes along the plant graph, use [investigate](investigate.md).
