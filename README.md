# SOL-Whales-Tracker 🐋⚡

A command-line Python script that monitors real-time whale activity (**accumulation** and **distribution**) for any trading pair on the Binance Spot market, using WebSocket streams.

By default, it is configured for **SOL/USDC**, but it can be easily adapted for any other pair.

---

## ✨ Features

* **Real-Time Monitoring**: Connects directly to Binance's WebSocket stream for minimal latency.
* **Whale Detection**: Identifies accumulation and distribution patterns based on trade flow, size repetition, and a dynamic threshold.
* **Large Trade Alerts**: Instantly prints alerts for individual trades exceeding a configurable USD threshold.
* **Telegram Notifications**: Optional alerts for significant trades and status changes can be sent to a Telegram bot.
* **Highly Configurable**: Easily change the trading pair, analysis window, thresholds, and more.

---

## 🚀 Getting Started

### Requirements
* Python 3.8+
* The following Python libraries: `requests`, `websockets`, `colorama`

### Installation

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/carminesanges/SOL-Whales-Tracker.git](https://github.com/carminesanges/SOL-Whales-Tracker.git)
    cd SOL-Whales-Tracker
    ```

2.  **Create a `requirements.txt` file** with the following content:
    ```
    requests
    websockets
    colorama
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

---

## ⚙️ Configuration

All settings can be adjusted by editing the constants at the top of the `whale_tracker_solusdc.py` file.

#### 1. Change Trading Pair

To monitor a different pair (e.g., BTC/USDC), simply change the `SYMBOL` variable:
```python
# From
SYMBOL = "SOLUSDC"
# To
SYMBOL = "BTCUSDC"
2. Enable Telegram Alerts (Optional)
Set TELEGRAM_ENABLED to True.

Add your bot token (from @BotFather) to TELEGRAM_BOT_TOKEN.

Add your numerical chat ID to TELEGRAM_CHAT_ID.

Python

TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE" 
TELEGRAM_CHAT_ID   = 123456789 
▶️ Usage
Once the configuration is set, run the script from your terminal:

Bash

python whale_tracker_solusdc.py
Press CTRL+C to stop the script gracefully.
