"""
ml_sieve.py — Machine Learning Win-Probability Filter
======================================================
FEATURE 4: Logistic Regression model trained on trade_log.csv
Predicts P(win) for each incoming signal before execution.

Features: entry_rsi, vol_ratio, flow_score, put_call_ratio,
          gex_significance, spy_vs_sma20
Target:   1 = profitable close, 0 = scratch/stop-out

Requires: scikit-learn, pandas
Min rows: 30 historical trades before model activates
"""

import os
import logging
import pickle
from pathlib import Path
from typing import Optional

log = logging.getLogger("ml_sieve")

_MODEL_PATH     = Path("ml_sieve_model.pkl")
_SCALER_PATH    = Path("ml_sieve_scaler.pkl")
_MIN_ROWS       = 30   # minimum trades before ML activates
_WIN_THRESHOLD  = 0.45  # block if P(win) < 45%

# Module-level model cache — loaded once, retrained weekly
_model  = None
_scaler = None
_model_trained_on = 0   # row count when last trained

FEATURES = [
    "entry_rsi",
    "vol_ratio",
    "flow_score",
    "put_call_ratio",
    "gex_significance",
    "spy_vs_sma20",
]


def _load_trade_log() -> Optional["pd.DataFrame"]:
    """Load trade_log.csv and return cleaned DataFrame, or None."""
    try:
        import pandas as pd
        path = Path("trade_log.csv")
        if not path.exists():
            return None
        df = pd.read_csv(path)
        if df.empty:
            return None
        return df
    except Exception as e:
        log.debug("ML load_trade_log error: %s", e)
        return None


def _build_feature_matrix(df: "pd.DataFrame"):
    """
    Extract feature matrix X and target y from trade log.
    Missing columns are filled with neutral defaults.
    Returns (X, y) as numpy arrays, or (None, None) if insufficient data.
    """
    try:
        import pandas as pd
        import numpy as np

        # Target: 1 if profitable, 0 if scratch/loss
        if "profit_amt" not in df.columns:
            return None, None
        df = df.copy()
        df["profit_amt"] = pd.to_numeric(df["profit_amt"], errors="coerce").fillna(0)
        df["y"] = (df["profit_amt"] > 0).astype(int)

        # Fill missing feature columns with neutral values
        defaults = {
            "entry_rsi":       28.0,
            "vol_ratio":       1.0,
            "flow_score":      50.0,
            "put_call_ratio":  1.0,
            "gex_significance":5.0,
            "spy_vs_sma20":    0.0,
        }
        for col, default in defaults.items():
            if col not in df.columns:
                df[col] = default
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(default)

        X = df[FEATURES].values
        y = df["y"].values
        return X, y

    except Exception as e:
        log.debug("ML build_feature_matrix error: %s", e)
        return None, None


def train_model(force: bool = False) -> bool:
    """
    Train or retrain the logistic regression model on trade_log.csv.
    Saves model + scaler to disk for persistence across restarts.
    Returns True if training succeeded.
    """
    global _model, _scaler, _model_trained_on

    df = _load_trade_log()
    if df is None or len(df) < _MIN_ROWS:
        log.info("[ML] Insufficient data (%d rows, need %d) — model inactive",
                 len(df) if df is not None else 0, _MIN_ROWS)
        return False

    if not force and _model_trained_on == len(df):
        return True  # already trained on this exact dataset

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import cross_val_score
        import numpy as np

        X, y = _build_feature_matrix(df)
        if X is None or len(X) < _MIN_ROWS:
            return False

        # Fit scaler + model
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        model = LogisticRegression(
            max_iter=1000,
            class_weight="balanced",   # handles imbalanced win/loss ratio
            random_state=42,
            C=0.5,                     # light regularization — prevents overfitting small data
        )
        model.fit(X_scaled, y)

        # Cross-validate to check model quality
        if len(X) >= 50:
            scores = cross_val_score(model, X_scaled, y, cv=3, scoring="roc_auc")
            auc = float(scores.mean())
            log.info("[ML] Model trained: %d rows | AUC=%.3f | wins=%d losses=%d",
                     len(X), auc, int(y.sum()), int((1-y).sum()))
        else:
            log.info("[ML] Model trained: %d rows (too few for CV) | wins=%d losses=%d",
                     len(X), int(y.sum()), int((1-y).sum()))
            auc = 0.0

        # Save to disk
        pickle.dump(model, open(_MODEL_PATH, "wb"))
        pickle.dump(scaler, open(_SCALER_PATH, "wb"))

        _model  = model
        _scaler = scaler
        _model_trained_on = len(df)

        print(f"[ML Sieve] Model retrained on {len(X)} trades. "
              f"AUC={auc:.3f} | Wins: {int(y.sum())} / Losses: {int((1-y).sum())}")
        return True

    except ImportError:
        log.warning("[ML] scikit-learn not installed — pip install scikit-learn")
        return False
    except Exception as e:
        log.error("[ML] Training error: %s", e)
        return False


def load_model() -> bool:
    """
    Load persisted model from disk if available.
    Called at startup to avoid retraining each session.
    """
    global _model, _scaler
    try:
        if _MODEL_PATH.exists() and _SCALER_PATH.exists():
            _model  = pickle.load(open(_MODEL_PATH, "rb"))
            _scaler = pickle.load(open(_SCALER_PATH, "rb"))
            log.info("[ML] Model loaded from disk")
            return True
    except Exception as e:
        log.debug("ML load_model error: %s", e)
    return False


def predict_win_probability(
    entry_rsi: float,
    vol_ratio: float,
    flow_score: float,
    put_call_ratio: float,
    gex_significance: float,
    spy_vs_sma20: float,
) -> Optional[float]:
    """
    Predict P(win) for a trade setup using the trained logistic regression.
    Returns float 0.0–1.0, or None if model not ready.
    None = no block (model defers to other gates).
    """
    global _model, _scaler

    # Try loading from disk if not in memory
    if _model is None or _scaler is None:
        if not load_model():
            # Try training on the spot
            if not train_model():
                return None  # model not ready — don't block

    if _model is None or _scaler is None:
        return None

    try:
        import numpy as np
        X = np.array([[
            entry_rsi,
            vol_ratio,
            flow_score,
            put_call_ratio,
            gex_significance,
            spy_vs_sma20,
        ]])
        X_scaled = _scaler.transform(X)
        prob = float(_model.predict_proba(X_scaled)[0][1])  # P(class=1=win)
        return round(prob, 4)
    except Exception as e:
        log.debug("ML predict error: %s", e)
        return None


def should_block_trade(
    ticker: str,
    entry_rsi: float,
    vol_ratio: float,
    flow_score: float,
    put_call_ratio: float,
    gex_significance: float,
    spy_vs_sma20: float,
) -> tuple:
    """
    Main gate function called from main.py before routing to AI risk manager.

    Returns: (should_block: bool, p_win: float | None, reason: str)
      should_block=True  → print ML BLOCK and skip trade
      should_block=False → proceed normally
    """
    p_win = predict_win_probability(
        entry_rsi, vol_ratio, flow_score,
        put_call_ratio, gex_significance, spy_vs_sma20
    )

    if p_win is None:
        return False, None, "ML model not ready — deferring"

    if p_win < _WIN_THRESHOLD:
        reason = (
            f"P(win)={p_win:.1%} < {_WIN_THRESHOLD:.0%} threshold | "
            f"RSI={entry_rsi:.1f} flow={flow_score:.0f} PCR={put_call_ratio:.2f} "
            f"vol={vol_ratio:.1f}x spy={spy_vs_sma20:+.1f}%"
        )
        return True, p_win, reason

    return False, p_win, f"P(win)={p_win:.1%} ✅"


def maybe_retrain():
    """
    Check if the model needs retraining (new trades added since last train).
    Call this at the start of each trading session or after fills.
    """
    df = _load_trade_log()
    if df is None:
        return
    current_rows = len(df)
    if current_rows >= _MIN_ROWS and current_rows != _model_trained_on:
        log.info("[ML] %d new rows detected — retraining model...", 
                 current_rows - _model_trained_on)
        train_model(force=True)
