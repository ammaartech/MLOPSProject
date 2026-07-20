import pandas as pd
from crud.query import execute_query

# Which columns we forecast
TARGETS = ["cpu_percent", "mem_percent", "disk_percent"]

# Lag depths (in samples). At 3s spacing: 1=3s, 20=60s back.
LAGS = [1, 3, 5, 10, 20]
ROLL_WINDOW = 10  # ~30s rolling stats


def load_dataframe():
    """Pull the metrics table into a time-sorted DataFrame."""
    rows = execute_query("SELECT * FROM metrics ORDER BY ts ASC", fetch=True)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "id", "ts", "cpu_percent", "mem_percent",
        "mem_used_mb", "disk_percent", "disk_used_gb"
    ])
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def build_features(df, target):
    """
    Turn the series into a supervised table for one target column.
    X = lag/rolling/time features, y = the NEXT value of `target`.
    """
    data = df.copy()

    # --- Lag features: value N steps ago ---
    for lag in LAGS:
        data[f"{target}_lag_{lag}"] = data[target].shift(lag)

    # --- Rolling stats: recent trend & volatility ---
    data[f"{target}_roll_mean"] = data[target].rolling(ROLL_WINDOW).mean()
    data[f"{target}_roll_std"] = data[target].rolling(ROLL_WINDOW).std()
    data[f"{target}_roll_max"] = data[target].rolling(ROLL_WINDOW).max()

    # --- Time features: seasonality (shine on longer datasets) ---
    data["hour"] = data["ts"].dt.hour
    data["minute"] = data["ts"].dt.minute
    data["dayofweek"] = data["ts"].dt.dayofweek

    # --- Target: the NEXT value (what we predict) ---
    data["target"] = data[target].shift(-1)

    # Drop rows with NaNs created by shifting
    data = data.dropna().reset_index(drop=True)

    feature_cols = (
        [f"{target}_lag_{lag}" for lag in LAGS]
        + [f"{target}_roll_mean", f"{target}_roll_std", f"{target}_roll_max"]
        + ["hour", "minute", "dayofweek"]
    )

    X = data[feature_cols]
    y = data["target"]
    return X, y, feature_cols


if __name__ == "__main__":
    df = load_dataframe()
    print("Loaded rows:", len(df))
    for tgt in TARGETS:
        X, y, cols = build_features(df, tgt)
        print(f"\n{tgt}: {len(X)} training rows, {len(cols)} features")
        print("Features:", cols)