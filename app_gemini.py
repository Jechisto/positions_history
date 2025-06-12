from flask import Flask, render_template, request, jsonify, redirect, url_for
from pybit.unified_trading import HTTP
from decimal import Decimal
from typing import Dict, List, Optional
import os
from dotenv import load_dotenv
import logging
from datetime import datetime
import json
import time  # Pro krátké zpoždění
from dataclasses import dataclass

# Nastavení loggingu
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_web.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Dataclass pro konfiguraci obchodování


# Dataclass pro konfiguraci obchodování
@dataclass  # <-- Důležité je mít tu dekorátor
class TradingConfig:
    testnet: bool = False
    default_leverage: int = 1
    default_category: str = "linear"
    time_in_force: str = "GTC"

# Třída pro interakci s Bybit API


class BybitTrader:
    def __init__(self, config: TradingConfig):
        load_dotenv()
        self.api_key = os.getenv('BYBIT_API_KEY')
        self.api_secret = os.getenv('BYBIT_API_SECRET')

        if not self.api_key or not self.api_secret:
            logging.error("API klíče nejsou nastaveny v .env souboru.")
            raise ValueError("API klíče nejsou nastaveny v .env souboru")

        self.config = config
        self.session = self._initialize_session()

    def _initialize_session(self) -> HTTP:
        return HTTP(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.config.testnet
        )

    def get_account_balance(self) -> Dict:
        try:
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            balances = response.get("result", {}).get("list", [])
            if not balances:
                logging.warning("Žádné zůstatky účtu nalezeny.")
                return {}

            balance_info = {
                'total_equity': balances[0].get('totalEquity', 'N/A'),
                'available_balance': balances[0].get('availableBalance', 'N/A'),
                'totalMargin': balances[0].get('totalMargin', 'N/A'),
                'totalWalletBalance': balances[0].get('totalWalletBalance', 'N/A'),
                'coins': []
            }
            for coin in balances[0].get('coin', []):
                balance_info['coins'].append({
                    'name': coin.get('coin', 'Unknown'),
                    'balance': coin.get('walletBalance', 'N/A')
                })
            return balance_info
        except Exception as e:
            logging.error(f"Chyba při získávání zůstatku: {e}")
            raise

    def get_open_positions(self, settlement_currency: str = "USDT") -> List[Dict]:
        try:
            response = self.session.get_positions(
                category=self.config.default_category,
                settleCoin=settlement_currency
            )
            positions = response.get("result", {}).get("list", [])
            if not positions:
                logging.info("Žádné otevřené pozice.")
                return []
            return [{
                'symbol': pos['symbol'],
                'size': pos['size'],
                'positionValue': pos['positionValue'],
                'side': pos['side'],
                'unrealized_pnl': pos['unrealisedPnl'],
                'avgPrice': pos['avgPrice'],
                'createdTime': datetime.fromtimestamp(int(pos['createdTime']) / 1000).strftime('%Y-%m-%d %H:%M:%S')
            } for pos in positions]
        except Exception as e:
            logging.error(f"Chyba při získávání pozic: {e}")
            raise

    def open_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        leverage: Optional[int] = None,
        price: Optional[float] = None
    ) -> Dict:
        try:
            leverage = leverage or self.config.default_leverage
            try:
                self.session.set_leverage(
                    category=self.config.default_category,
                    symbol=symbol,
                    buyLeverage=str(leverage),
                    sellLeverage=str(leverage)
                )
            except Exception as e:
                if "leverage not modified" in str(e):
                    logging.info("Leverage již byl nastaven, pokračuji dál.")
                else:
                    raise

            time.sleep(0.5)  # Krátké zpoždění

            if price is None:
                raise ValueError("Pro limit order musíte zadat cenu.")

            response = self.session.place_order(
                category=self.config.default_category,
                symbol=symbol,
                side=side,
                orderType="Limit",
                qty=str(qty),
                price=str(price),
                timeInForce=self.config.time_in_force,
                positionIdx="1"
            )
            logging.info(
                f"Pozice úspěšně otevřena: {symbol} {side} {qty} za cenu {price}.")
            return response
        except Exception as e:
            logging.error(f"Chyba při otevírání pozice: {e}")
            raise

    def close_position(self, symbol: str, qty: float) -> Dict:
        try:
            response = self.session.place_order(
                category=self.config.default_category,
                symbol=symbol,
                side="Sell" if self._get_position_side(
                    symbol) == "Buy" else "Buy",  # Dynamicky určí stranu pro zavření
                orderType="Market",
                qty=str(qty),
                timeInForce=self.config.time_in_force,
                reduceOnly=True
            )
            logging.info(f"Pozice úspěšně zavřena: {symbol} {qty}.")
            return response
        except Exception as e:
            logging.error(f"Chyba při zavírání pozice: {e}")
            raise

    def _get_position_side(self, symbol: str) -> Optional[str]:
        """Pomocná metoda pro zjištění strany otevřené pozice."""
        positions = self.get_open_positions()
        for pos in positions:
            if pos['symbol'] == symbol:
                return pos['side']
        return None


# --- Flask Web Application ---
app = Flask(__name__)
# Není potřeba předávat argumenty, dataclass si to zařídí automaticky
# Použije se výchozí hodnota pro 'testnet' (False)
config = TradingConfig()
try:
    trader = BybitTrader(config)
except ValueError as e:
    logging.critical(f"Aplikace nemůže být spuštěna: {e}")
    trader = None  # Aby aplikace nespadla hned

# Trvalé ukládání historie pozic
POSITIONS_HISTORY_FILE = 'positions_history.json'


def load_positions_history():
    if os.path.exists(POSITIONS_HISTORY_FILE):
        with open(POSITIONS_HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_positions_history(data):
    with open(POSITIONS_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@app.route('/')
def index():
    if not trader:
        return render_template('error.html', message="Chyba inicializace API. Zkontrolujte prosím .env soubor.")
    return render_template('index.html')


@app.route('/balance', methods=['GET'])
def get_balance():
    if not trader:
        return jsonify({"error": "Trading client not initialized."}), 500
    try:
        balance = trader.get_account_balance()
        return jsonify(balance)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/positions', methods=['GET'])
def get_positions():
    if not trader:
        return jsonify({"error": "Trading client not initialized."}), 500
    try:
        positions = trader.get_open_positions()
        settlement_currency = "USDT"

        total_long_value = Decimal('0')
        total_short_value = Decimal('0')
        total_pnl = Decimal('0')
        long_symbols = []
        short_symbols = []
        long_positions_detailed = []
        short_positions_detailed = []

        balance_info = trader.get_account_balance()
        total_equity = Decimal(balance_info.get('total_equity', '0'))

        for pos in positions:
            position_value = Decimal(str(abs(float(pos['positionValue']))))
            unrealized_pnl = Decimal(str(pos['unrealized_pnl']))
            total_pnl += unrealized_pnl

            if pos['side'] == 'Buy':
                total_long_value += position_value
                long_symbols.append(pos['symbol'].replace('USDT', ''))
                long_positions_detailed.append(pos)
            else:
                total_short_value += position_value
                short_symbols.append(pos['symbol'].replace('USDT', ''))
                short_positions_detailed.append(pos)

        long_percentage = (total_long_value / total_equity) * \
            100 if total_equity > 0 else 0
        short_percentage = (total_short_value / total_equity) * \
            100 if total_equity > 0 else 0

        summary = {
            'total_long_value': f"{total_long_value:.2f}",
            'total_short_value': f"{total_short_value:.2f}",
            'long_percentage': f"{long_percentage:.2f}",
            'short_percentage': f"{short_percentage:.2f}",
            'long_symbols': ','.join(long_symbols),
            'short_symbols': ','.join(short_symbols),
            'settlement_currency': settlement_currency,
            'total_pnl': f"{total_pnl:.2f}",
            'total_equity': f"{total_equity:.2f}"
        }

        # Save to history
        current_date_str = datetime.now().strftime("%Y-%m-%d")
        current_time_str = datetime.now().strftime("%H:%M:%S")

        export_data = {
            current_date_str: {
                current_time_str: {
                    'long_positions': long_positions_detailed,
                    'short_positions': short_positions_detailed,
                    'summary': summary
                }
            }
        }
        history = load_positions_history()
        history.append(export_data)
        save_positions_history(history)

        return jsonify({
            "positions": positions,
            "summary": summary
        })
    except Exception as e:
        logging.error(f"Chyba při získávání pozic pro web: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/open_position', methods=['POST'])
def open_position():
    if not trader:
        return jsonify({"error": "Trading client not initialized."}), 500
    data = request.json
    symbol = data.get('symbol')
    side = data.get('side')
    qty = float(data.get('qty'))
    leverage = int(data.get('leverage', config.default_leverage))
    price = float(data.get('price'))  # Cena je pro limit order povinná

    if not all([symbol, side, qty, price]):
        return jsonify({"error": "Všechna pole (symbol, side, qty, price) jsou povinná."}), 400

    try:
        response = trader.open_position(symbol, side, qty, leverage, price)
        return jsonify({"message": "Pozice úspěšně otevřena.", "response": response})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/close_position', methods=['POST'])
def close_position():
    if not trader:
        return jsonify({"error": "Trading client not initialized."}), 500
    data = request.json
    symbol = data.get('symbol')
    qty = float(data.get('qty'))

    if not all([symbol, qty]):
        return jsonify({"error": "Všechna pole (symbol, qty) jsou povinná."}), 400

    try:
        response = trader.close_position(symbol, qty)
        return jsonify({"message": "Pozice úspěšně zavřena.", "response": response})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/history', methods=['GET'])
def get_history():
    try:
        history = load_positions_history()
        return jsonify(history)
    except Exception as e:
        return jsonify({"error": f"Chyba při načítání historie: {e}"}), 500


if __name__ == '__main__':
    app.run(debug=True)  # debug=True pro vývoj, v produkci nastavit na False
