"""
The data plane: stages 1-6 of the end-to-end pipeline.

    sources.py    [1] Data Sources        CSV, API, database, streaming
    schema.py     [2] Data Engineering    connectors, schema management
    etl.py        [3] ETL Pipeline        extract, transform, load automation
    validate.py   [4] Data Validation     nulls, duplicates, business rules
    clean.py      [5] Data Cleaning       imputation, deduplication
    transform.py  [6] Data Transformation standardisation, normalisation

Each stage is a module with a declared input and output, so it can be
run and tested on its own. `etl.py` is the only thing that knows the
order they go in.
"""
