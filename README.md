# Open Interest & PCR Directional Options Trading Engine

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform: Delta Exchange](https://img.shields.io/badge/Exchange-Delta%20Exchange-orange.svg)](https://www.delta.exchange)
[![Strategy: Order Flow Skew](https://img.shields.io/badge/Strategy-Put--Call%20Ratio%20Directional-green.svg)]()
[![Alerts: Telegram API](https://img.shields.io/badge/Alerts-Telegram%20Bot%20API-0088cc.svg)](https://core.telegram.org/bots)
[![Security: Zero Secrets](https://img.shields.io/badge/Security-Environment--Driven-brightgreen.svg)]()

A quantitative options order flow engine on **Delta Exchange** analyzing real-time **Put-Call Ratio (PCR)** and Open Interest distribution across multiple expiries to execute directional credit positions aligned with institutional writing pressure.

---

## 🔍 Directional Decision Matrix

The engine scans live orderbooks across Near, Next, and All expiries, calculating dynamic Put-Call Ratios:

$$\\text{PCR} = \\frac{\\sum \\text{Put Open Interest}}{\\sum \\text{Call Open Interest}}$$

| Condition | Market Interpretation | Systematic Action |
| :--- | :--- | :--- |
| **$\\text{PCR} > 1.0$** | **Put Writing Dominance (Bullish Floor)** | **SELL OTM PUT** (Bullish credit position) |
| **$\\text{PCR} < 1.0$** | **Call Writing Dominance (Bearish Ceiling)** | **SELL OTM CALL** (Bearish credit position) |
| **$\\text{PCR} == 1.0$** | **Indecision / Balanced Orderbook** | **NO TRADE** (Preserve capital, zero exposure) |

---

## ⚡ Key Features

- **Multi-Expiry Scanning**: Operational modes including `ALL_PCR`, `NEAR_PCR`, `NEXT_PCR`, `MAX_OPEN_INTEREST`, and `FIRST_PCR_CHANGE`.
- **Automated Telegram Telemetry**: Real-time notifications dispatched on every state change and trade fill.
- **Process Guard**: Linux flock single-instance lock ensuring zero duplicate process execution.

---

## 🚀 Quickstart Guide

```bash
git clone https://github.com/RaviMahor/open-interest-pcr-strategy.git
cd open-interest-pcr-strategy

# Setup virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure environment credentials
cp .env.example .env

# Run strategy
python3 Open_Interest_Crypto.py
```

---

## 👤 Author
**Ravi Mahor** · *Options & Derivatives Trader at Jainam Broking Limited*  
- **LinkedIn**: [linkedin.com/in/ravimahor](https://linkedin.com/in/ravimahor)  
- **Certifications**: NISM-Series-XVI (Commodity Derivatives) · NISM-Series-VIII (Equity Derivatives)
