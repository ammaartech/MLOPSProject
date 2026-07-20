import numpy as np


def naive_forecast(y_true_series):
    """
    Seasonal-naive baseline: predict the next value = the current value.
    Given a series, the prediction for step t+1 is simply value at t.
    Returns (y_true, y_pred) aligned, dropping the last point.
    """
    values = np.asarray(y_true_series)
    y_pred = values[:-1]   # today's value ...
    y_true = values[1:]    # ... predicts tomorrow's
    return y_true, y_pred


def mae(y_true, y_pred):
    return float(np.mean(np.abs(np.asarray(y_true) - np.asarray(y_pred))))