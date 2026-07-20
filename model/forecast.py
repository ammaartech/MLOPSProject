import os
import time
import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

from config import DATA_DIR
from model.features import load_dataframe, build_features, TARGETS
from model.baseline import mae as baseline_mae

MODEL_DIR = os.path.join(DATA_DIR, "models")
os.makedirs(MODEL_DIR, exist_ok=True)

# Below this naive-MAE, the signal is effectively flat and the
# improvement percentage becomes misleading (division by ~0).
FLAT_THRESHOLD = 0.5


def chronological_split(X, y, test_frac=0.2):
    """
    Time-series split: train on the earlier portion, test on the later.
    NEVER shuffle time-series — that leaks the future into training.
    """
    split = int(len(X) * (1 - test_frac))
    return (
        X.iloc[:split], X.iloc[split:],
        y.iloc[:split], y.iloc[split:],
    )


def train_one(target, verbose=True):
    df = load_dataframe()
    X, y, feature_cols = build_features(df, target)

    if len(X) < 30:
        print(f"[{target}] Not enough data ({len(X)} rows). Collect more.")
        return None

    X_train, X_test, y_train, y_test = chronological_split(X, y)

    # --- Real model ---
    model = GradientBoostingRegressor(
        n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42
    )
    model.fit(X_train, y_train)

    # measure inference latency (cost/efficiency signal)
    t0 = time.perf_counter()
    y_pred = model.predict(X_test)
    infer_ms = (time.perf_counter() - t0) / len(X_test) * 1000

    model_mae = mean_absolute_error(y_test, y_pred)

    # --- Naive baseline on the same test window ---
    # baseline: predict next = current value (lag_1 feature IS the current value)
    naive_pred = X_test[f"{target}_lag_1"].values
    naive_mae = baseline_mae(y_test.values, naive_pred)

    if verbose:
        print(f"\n===== {target} =====")
        print(f"Train rows: {len(X_train)} | Test rows: {len(X_test)}")
        print(f"Model MAE   : {model_mae:6.3f}")
        print(f"Naive MAE   : {naive_mae:6.3f}")

        if naive_mae < FLAT_THRESHOLD:
            # Flat signal: baseline is already near-perfect, so the
            # percentage is noise. Report absolute error instead.
            print("Improvement : n/a (signal is flat; baseline near-optimal)")
            print(f"              abs MAE = {model_mae:.3f} "
                  f"(model matches naive to within a fraction of a %)")
        else:
            improvement = (naive_mae - model_mae) / naive_mae * 100
            verdict = "BEATS baseline" if model_mae < naive_mae else "loses to baseline"
            print(f"Improvement : {improvement:+5.1f}%  ({verdict})")

        print(f"Inference   : {infer_ms:.3f} ms/prediction")

    # save model + feature list
    path = os.path.join(MODEL_DIR, f"{target}.joblib")
    joblib.dump({"model": model, "features": feature_cols}, path)

    return {
        "target": target,
        "model_mae": model_mae,
        "naive_mae": naive_mae,
        "infer_ms": infer_ms,
        "flat": naive_mae < FLAT_THRESHOLD,
    }


def train_all():
    print("Training forecasters for all resources...")
    results = []
    for tgt in TARGETS:
        r = train_one(tgt)
        if r:
            results.append(r)
    print("\nAll models saved to:", MODEL_DIR)
    return results


def predict_next(target):
    """
    Load the saved model and predict the NEXT value of `target`
    using the most recent row of features. Used by Day 3's recommender.
    """
    path = os.path.join(MODEL_DIR, f"{target}.joblib")
    if not os.path.exists(path):
        print(f"No trained model for {target}. Run train_all() first.")
        return None

    bundle = joblib.load(path)
    model, feature_cols = bundle["model"], bundle["features"]

    df = load_dataframe()
    X, _, _ = build_features(df, target)
    if len(X) == 0:
        return None

    latest = X.iloc[[-1]][feature_cols]  # most recent feature row
    return float(model.predict(latest)[0])


if __name__ == "__main__":
    train_all()
    print("\n--- Next-step predictions ---")
    for tgt in TARGETS:
        pred = predict_next(tgt)
        print(f"{tgt}: predicted next = {pred:.2f}" if pred is not None
              else f"{tgt}: n/a")