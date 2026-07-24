"""
STAGE 6b — Categorical encoding.

Everything the collector measures is a number except one column: `regime`,
the idle / ramp / saturated label that `transform.label_regimes` attaches
to each row. A gradient booster cannot consume the string "saturated", so
before this module existed the label was carried alongside the data for
error reporting and then dropped. The model never saw it.

That is a real loss. The regime label says which part of the operating
range a sample sits in, and error is not uniform across those parts — a
forecaster can be excellent at idle and poor during a ramp. Encoding puts
that information where the model can use it.

Two methods, both declared in config
------------------------------------
    onehot    one column per category, value 0 or 1.
              Makes no claim about ordering. The safe default.

    ordinal   one column holding an integer rank.
              Compact, and it asserts idle < ramp < saturated. That
              ordering happens to be true here, which is why the option
              exists — but it IS an assertion, so it lives in config
              where it can be read, rather than in code where it cannot.

The vocabulary is fixed, and that is the important part
------------------------------------------------------
The list of categories comes from configuration, never from the data in
front of the encoder.

`pandas.get_dummies` builds its columns from whatever values it finds. At
training time the frame holds every regime, so it produces four columns.
At serving time the frame is ONE ROW holding one regime, so it produces
one column — and the model is handed a feature vector with three columns
missing and one in the wrong position. The prediction still comes back as
a plausible-looking number, which is what makes this class of bug
expensive: nothing raises, the output is simply wrong.

Deriving the vocabulary from config removes the failure. `encode()`
produces the identical column set, in the identical order, for a
900-row training frame and for a single serving row.

Why the result is not stored in the database
--------------------------------------------
Encoded columns are a deterministic function of `regime`, which
`metrics_clean` already holds. Persisting them would duplicate state and
force a migration every time the vocabulary changes. Both the training
path and the serving path call this module on the way past, which is the
same reasoning `transform.add_derived` follows and the same reason both
paths stay in step.
"""

import pandas as pd

import config

# Separates a column from the category it encodes: `regime` + `idle`
# becomes `regime_idle`. Kept as a constant because the drift monitor
# needs to recognise encoded columns without re-deriving the vocabulary.
SEPARATOR = "_"
ORDINAL_SUFFIX = "_ordinal"


# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------
def vocabulary(column):
    """The fixed, ordered category list for `column`.

    Resolution order:

    1. An explicit entry in `encoding.categories`. Use this when the
       categories are not derivable from anything else.
    2. For `regime`, the keys of `regime.bounds` — the same config value
       that decides the labels in the first place. Adding a band there
       gives the encoder its column automatically, so the two cannot
       disagree.

    The unknown label is always appended. A null reading, or a value that
    falls outside every band, has to land somewhere; without a column of
    its own it would either raise or be silently folded into a real
    category.
    """
    explicit = config.get_json("encoding.categories") or {}
    unknown = config.get_str("encoding.unknown_label")

    if column in explicit and explicit[column]:
        categories = list(explicit[column])
    elif column == "regime":
        categories = list(config.get_json("regime.bounds").keys())
    else:
        # No vocabulary can be derived, and guessing one from the data is
        # exactly the failure this module exists to prevent.
        return []

    if unknown not in categories:
        categories.append(unknown)
    return categories


def encoded_columns(column):
    """The column names `encode()` will produce for `column`.

    Available without touching data, so a caller can check train/serve
    agreement before building anything.
    """
    categories = vocabulary(column)
    if not categories:
        return []

    if config.get_str("encoding.method") == "ordinal":
        return [f"{column}{ORDINAL_SUFFIX}"]

    if config.get_bool("encoding.drop_first"):
        categories = categories[1:]
    return [f"{column}{SEPARATOR}{c}" for c in categories]


def all_encoded_columns():
    """Every column the encoder adds, across every configured column."""
    names = []
    for column in config.get_json("encoding.columns"):
        names += encoded_columns(column)
    return names


# ----------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------
def _normalise(series, categories, unknown):
    """Map raw values onto the vocabulary.

    Nulls and unrecognised values become the unknown label rather than
    NaN. A NaN here would propagate into every encoded column and then be
    dropped by the feature builder's `dropna`, quietly discarding rows for
    a reason that has nothing to do with their quality.
    """
    values = series.astype("object").where(pd.notna(series), unknown)
    values = values.astype(str).str.strip()
    return values.where(values.isin(categories), unknown)


def encode_column(df, column):
    """Encode one categorical column. Returns (frame, added_column_names)."""
    categories = vocabulary(column)
    if not categories:
        return df, []

    out = df.copy()
    unknown = config.get_str("encoding.unknown_label")
    method = config.get_str("encoding.method")

    # A configured column absent from the frame still contributes its
    # columns, filled with the unknown category. Silently skipping it
    # would give training and serving different feature counts whenever
    # one of them happened to lack the source column.
    if column not in out.columns:
        source = pd.Series([unknown] * len(out), index=out.index)
    else:
        source = _normalise(out[column], categories, unknown)

    if method == "ordinal":
        rank = {c: i for i, c in enumerate(categories)}
        name = f"{column}{ORDINAL_SUFFIX}"
        out[name] = source.map(rank).astype("int16")
        return out, [name]

    # One-hot, built by reindexing onto the fixed vocabulary. This is the
    # line that makes a one-row serving frame produce the full column set:
    # `get_dummies` alone would emit only the categories present.
    dummies = pd.get_dummies(source, prefix=column, prefix_sep=SEPARATOR)
    wanted = [f"{column}{SEPARATOR}{c}" for c in categories]
    dummies = dummies.reindex(columns=wanted, fill_value=0).astype("int8")

    if config.get_bool("encoding.drop_first"):
        dummies = dummies.iloc[:, 1:]

    for name in dummies.columns:
        out[name] = dummies[name].to_numpy()
    return out, list(dummies.columns)


def encode(df, verbose=False):
    """Encode every column named in `encoding.columns`.

    Returns (frame, report). Disabled in config, this is a no-op that
    still reports itself, so a run where encoding was off is legible from
    its report alone.
    """
    if df is None or df.empty:
        return df, {"encoded_columns_added": 0, "encoded_columns": [],
                    "encoding_method": None, "encoding_enabled": False}

    if not config.get_bool("encoding.enabled"):
        return df, {"encoded_columns_added": 0, "encoded_columns": [],
                    "encoding_method": config.get_str("encoding.method"),
                    "encoding_enabled": False,
                    "note": "encoding.enabled is false"}

    out = df.copy()
    method = config.get_str("encoding.method")
    added, vocabularies, occupancy = [], {}, {}

    for column in config.get_json("encoding.columns"):
        categories = vocabulary(column)
        if not categories:
            continue

        out, names = encode_column(out, column)
        added += names
        vocabularies[column] = categories

        # Share of rows per category, from the encoded columns rather than
        # the source text — so the report describes what the model will
        # actually receive.
        if names and method != "ordinal":
            total = len(out)
            occupancy[column] = {
                name.split(SEPARATOR, 1)[1]: round(
                    100.0 * float(out[name].sum()) / total, 1
                )
                for name in names
            }

    report = {
        "encoding_enabled": True,
        "encoding_method": method,
        "encoded_columns_added": len(added),
        "encoded_columns": added,
        "vocabularies": vocabularies,
        "category_occupancy_pct": occupancy,
    }

    if verbose:
        print(format_report(report))
    return out, report


# ----------------------------------------------------------------------
# Category shares — what the drift monitor compares
# ----------------------------------------------------------------------
def category_shares(df, column):
    """Fraction of rows in each category, as a vector over the vocabulary.

    Ordered by the vocabulary, not by what the frame contains, so two
    windows always yield comparable vectors even when one of them holds
    no examples of a category. `serving.drift` subtracts these.
    """
    categories = vocabulary(column)
    if not categories or df is None or df.empty:
        return {}

    unknown = config.get_str("encoding.unknown_label")

    if column in df.columns:
        source = _normalise(df[column], categories, unknown)
    else:
        # The source text is gone, so recover the label from the one-hot
        # block: the column with the 1 in it names the category.
        encoded = [c for c in encoded_columns(column) if c in df.columns]
        if not encoded or config.get_str("encoding.method") == "ordinal":
            return {}
        source = (df[encoded].idxmax(axis=1)
                  .str.split(SEPARATOR, n=1).str[1])

    counts = source.value_counts()
    total = float(len(source)) or 1.0
    return {c: float(counts.get(c, 0)) / total for c in categories}


# ----------------------------------------------------------------------
def format_report(report):
    lines = [
        "=" * 78,
        "CATEGORICAL ENCODING",
        "=" * 78,
    ]
    if not report.get("encoding_enabled"):
        lines.append(f"  DISABLED — {report.get('note', 'encoding.enabled is false')}")
        lines.append("=" * 78)
        return "\n".join(lines)

    lines.append(f"  method               : {report['encoding_method']}")
    lines.append(f"  columns added        : {report['encoded_columns_added']}")
    for column, categories in report.get("vocabularies", {}).items():
        lines.append(f"  {column:20s} : {categories}")
    for column, shares in report.get("category_occupancy_pct", {}).items():
        lines.append(f"  {column} occupancy % : {shares}")
    lines.append("-" * 78)
    lines.append("  vocabulary comes from config, never from the data — so a "
                 "one-row\n  serving frame produces the same columns as the "
                 "training frame.")
    lines.append("=" * 78)
    return "\n".join(lines)


if __name__ == "__main__":
    from pipeline.clean import clean
    from pipeline.sources import get_source
    from pipeline.transform import label_regimes

    frame, _ = clean(get_source("sqlite://").read())
    frame, _ = label_regimes(frame)
    encoded, rep = encode(frame)
    print(format_report(rep))

    names = rep["encoded_columns"]
    if names:
        print("\nFirst 8 rows of the encoded block:")
        print(encoded[["regime"] + names].head(8).to_string(index=False))

        # The point of the fixed vocabulary, demonstrated: one row in,
        # the full column set out.
        single, _ = encode(frame.tail(1))
        produced = [c for c in rep["encoded_columns"] if c in single.columns]
        print(f"\nSingle-row serving frame produced {len(produced)} of "
              f"{len(names)} encoded columns "
              f"({'consistent' if len(produced) == len(names) else 'MISMATCH'})")
