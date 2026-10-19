"""Connectors: pull a window of plant data from where it lives into penguineer's inputs.

Each connector writes the same local files every command reads: a long historian table
(``timestamp, tag, value, quality``) as Parquet, a tag list ``graph draft`` reads, and lot windows
or lab results where the source has them. A fetched window is a snapshot, so a run is
reproducible after the source has moved on.

- :mod:`penguineer.connectors.pi`: AVEVA PI through the PI Web API.
- :mod:`penguineer.connectors.lake`: Parquet, CSV or Delta tables on S3, GCS, Azure or disk.
"""
