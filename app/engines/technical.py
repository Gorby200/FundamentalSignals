"""
app/engines/technical.py — Pure technical analysis engine.

This module contains ONLY pure functions — no I/O, no side effects, no state.
Functions receive price arrays and parameters, return indicator values.

Why pure functions?
  - Testable: feed in known arrays, assert expected outputs.
  - Composable: signal engine calls these freely without coupling.
  - No hidden state: every input is explicit, every output is deterministic.

Indicators implemented:
  - SMA (Simple Moving Average) — trend direction, crossover signals
  - EMA (Exponential Moving Average) — faster trend response
  - RSI (Relative Strength Index) — overbought / oversold
  - MACD (Moving Average Convergence Divergence) — momentum + direction
  - Bollinger Bands — volatility envelope
  - ATR (Average True Range) — volatility measure for stop-loss sizing
  - Price Velocity — rate of change (momentum derivative)
"""

import numpy as np
from typing import Dict, Any, Optional, List


def sma(prices: np.ndarray, period: int) -> np.ndarray:
    """
    Simple Moving Average.
    SMA gives equal weight to all prices in the window.
    We use it for crossover detection: when short SMA > long SMA, trend is bullish.
    """
    if len(prices) < period:
        return np.array([])
    return np.convolve(prices, np.ones(period) / period, mode='valid')


def ema(prices: np.ndarray, period: int) -> np.ndarray:
    """
    Exponential Moving Average.
    EMA reacts faster to recent prices than SMA, making it better for
    detecting trend changes early. The multiplier is 2/(period+1).
    """
    if len(prices) < period:
        return np.array([])
    multiplier = 2.0 / (period + 1)
    result = np.zeros_like(prices, dtype=float)
    result[:period - 1] = np.nan
    result[period - 1] = np.mean(prices[:period])
    for i in range(period, len(prices)):
        result[i] = (prices[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def rsi(prices: np.ndarray, period: int = 14) -> float:
    """
    Relative Strength Index (0-100 scale).

    Interpretation (from 30+ years of market experience):
      - RSI > 70: overbought territory — potential reversal DOWN or strong trend continuation
      - RSI < 30: oversold territory — potential reversal UP or strong downtrend continuation
      - RSI 40-60: neutral zone — no clear momentum signal

    We don't blindly trade RSI extremes. We COMBINE RSI with trend direction
    from SMA crossover and MACD to filter false signals.
    """
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
    return 100.0 - (100.0 / (1.0 + rs))


def macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
    """
    MACD (Moving Average Convergence Divergence).

    Returns:
      - macd_line: fast EMA minus slow EMA (momentum)
      - signal_line: EMA of the MACD line (trigger)
      - histogram: MACD minus signal (momentum strength)

    Why MACD matters:
      - Histogram > 0 and rising = bullish momentum accelerating
      - Histogram < 0 and falling = bearish momentum accelerating
      - Crossover of MACD above signal = BUY trigger
      - Crossover of MACD below signal = SELL trigger

    We use MACD as our PRIMARY momentum indicator because it combines
    trend (EMA difference) with a trigger (signal line crossover).
    """
    if len(prices) < slow + signal:
        return {"macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0}
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    valid = ~np.isnan(ema_fast) & ~np.isnan(ema_slow)
    if not np.any(valid):
        return {"macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0}
    macd_line = ema_fast[valid] - ema_slow[valid]
    if len(macd_line) < signal:
        return {"macd_line": float(macd_line[-1]) if len(macd_line) > 0 else 0.0,
                "signal_line": 0.0, "histogram": 0.0}
    signal_line = ema(macd_line, signal)
    valid_sig = ~np.isnan(signal_line)
    if not np.any(valid_sig):
        return {"macd_line": float(macd_line[-1]), "signal_line": 0.0, "histogram": 0.0}
    m = macd_line[valid_sig]
    s = signal_line[valid_sig]
    min_len = min(len(m), len(s))
    return {
        "macd_line": float(m[-min_len:][-1]),
        "signal_line": float(s[-min_len:][-1]),
        "histogram": float((m[-min_len:] - s[-min_len:])[-1]),
    }


def bollinger_bands(prices: np.ndarray, period: int = 20, num_std: float = 2.0) -> Dict[str, float]:
    """
    Bollinger Bands.

    Bands widen when volatility increases (good for breakout detection).
    Price touching upper band = potentially overbought.
    Price touching lower band = potentially oversold.

    We use Bollinger %B (where 0 = lower band, 1 = upper band) to normalize
    position within the bands. This gives us a 0-1 signal that's easy to combine
    with other indicators.
    """
    if len(prices) < period:
        return {"upper": 0.0, "middle": 0.0, "lower": 0.0, "percent_b": 0.5}
    slice_prices = prices[-period:]
    middle = float(np.mean(slice_prices))
    std = float(np.std(slice_prices))
    upper = middle + num_std * std
    lower = middle - num_std * std
    current = float(prices[-1])
    percent_b = (current - lower) / (upper - lower) if (upper - lower) != 0 else 0.5
    return {"upper": upper, "middle": middle, "lower": lower, "percent_b": percent_b}


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """
    Average True Range — measures volatility.

    ATR is critical for stop-loss placement:
      - Conservative SL = entry - 1.5 * ATR
      - Aggressive SL = entry - 1.0 * ATR
      - Wide SL = entry - 2.0 * ATR

    We use ATR-based stops because they adapt to current volatility.
    A fixed 2% stop makes no sense when ATR is 5% (crypto) vs 0.5% (forex).
    """
    if len(highs) < period + 1:
        return 0.0
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    atr_val = np.mean(tr[-period:])
    return float(atr_val)


def price_velocity(prices: np.ndarray, period: int = 10) -> float:
    """
    Price velocity — rate of change as percentage.

    Positive = prices accelerating upward.
    Negative = prices accelerating downward.
    Near zero = consolidation (breakout may be imminent).

    We use this as a confirming signal: a BUY signal with negative velocity
    is suspicious (possible false breakout), while positive velocity confirms
    the trend is intact.
    """
    if len(prices) < period + 1:
        return 0.0
    recent = prices[-period:]
    returns = np.diff(recent) / recent[:-1]
    return float(np.mean(returns)) * 100


def generate_technical_signal(prices: np.ndarray) -> Dict[str, Any]:
    """
    Master technical analysis function.

    Combines all indicators into a unified technical score and direction.

    Scoring methodology (designed for robustness, not curve-fitting):
      1. SMA Crossover (+/- 0.3): Trend direction from fast vs slow SMA
      2. RSI Extreme (+/- 0.2): Mean reversion signal at extremes
      3. MACD Histogram (+/- 0.25): Momentum confirmation
      4. Bollinger %B (+/- 0.15): Overbought/oversold within bands
      5. Price Velocity (+/- 0.1): Trend acceleration

    Total score ranges roughly from -1.0 to +1.0.
    Score > 0.3 = BUY bias, Score < -0.3 = SELL bias.

    This weighting was chosen because:
      - Trend (SMA) is the king indicator — it gets the most weight
      - Momentum (MACD) is the queen — confirms trend changes
      - RSI and Bollinger are princes — they filter extremes
      - Velocity is a scout — early warning of changes
    """
    if len(prices) < 30:
        return {"score": 0.0, "direction": "neutral", "confidence": 0.0,
                "indicators": {}, "error": "insufficient_data"}

    score = 0.0
    indicators = {}

    # --- 1. SMA Crossover (trend direction) ---
    sma_short = sma(prices, 7)
    sma_long = sma(prices, 20)
    if len(sma_short) > 0 and len(sma_long) > 0:
        short_val = sma_short[-1]
        long_val = sma_long[-1]
        crossover = (short_val - long_val) / long_val if long_val != 0 else 0
        score += np.clip(crossover * 10, -0.3, 0.3)
        indicators["sma_crossover"] = float(crossover)

    # --- 2. RSI ---
    rsi_val = rsi(prices, 14)
    indicators["rsi"] = rsi_val
    if rsi_val > 70:
        score -= 0.2
    elif rsi_val < 30:
        score += 0.2
    elif rsi_val < 45:
        score += 0.05
    elif rsi_val > 55:
        score -= 0.05

    # --- 3. MACD ---
    macd_data = macd(prices)
    indicators["macd_histogram"] = macd_data["histogram"]
    if macd_data["histogram"] > 0:
        score += min(0.25, macd_data["histogram"] * 5)
    else:
        score += max(-0.25, macd_data["histogram"] * 5)

    # --- 4. Bollinger Bands ---
    bb = bollinger_bands(prices)
    indicators["bollinger_percent_b"] = bb["percent_b"]
    if bb["percent_b"] > 0.85:
        score -= 0.15
    elif bb["percent_b"] < 0.15:
        score += 0.15

    # --- 5. Price Velocity ---
    vel = price_velocity(prices, 10)
    indicators["price_velocity"] = vel
    score += np.clip(vel * 2, -0.1, 0.1)

    # --- Final determination ---
    if score > 0.15:
        direction = "buy"
    elif score < -0.15:
        direction = "sell"
    else:
        direction = "neutral"

    confidence = min(abs(score) * 2.5, 1.0)

    return {
        "score": float(score),
        "direction": direction,
        "confidence": float(confidence),
        "indicators": indicators,
    }
