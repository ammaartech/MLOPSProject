"""
Evidence: walk-forward replay of the allocation decision.

    backtest.py   re-decide at every step using only information that
                  existed then, accumulate cost and breaches, and compare
                  competing policies on identical data.
"""
