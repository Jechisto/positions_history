from flask import Flask, render_template, request, jsonify, redirect, url_for
from pybit.unified_trading import HTTP
from decimal import Decimal
from typing import Dict, List, Optional
import os
from dotenv import load_dotenv
import logging
from datetime import datetime
import json
import time
from dataclasses import dataclass

# SQLAlchemy importy
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc

# APScheduler import
from flask_apscheduler import APScheduler

# Nastavení loggingu (beze změny)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_web.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Dataclass pro konfiguraci obchodování (beze změny)
@dataclass
class TradingConfig:
    testnet: bool = False
    default_leverage: int = 1
    default_category: str = "linear" 
    time_in_force: str = "GTC"
    # Nová konfigurace pro plánovač
    scheduler_interval_minutes: int = 15 # Výchozí interval 60 minut (1 hodina)

# Třída pro interakci s Bybit API (beze změny, jen pro úplnost)
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
                    logging.info(f"Leverage pro {symbol} již byl nastaven, pokračuji dál.")
                else:
                    logging.error(f"Chyba při nastavování páky pro {symbol}: {e}")
                    raise

            time.sleep(0.5)

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
                # positionIdx="1"  # Zakomentováno pro One-Way Mode
            )

            logging.info(f"Pozice úspěšně otevřena: {symbol} {side} {qty} za cenu {price}.")
            return response

        except Exception as e:
            logging.error(f"Chyba při otevírání pozice: {str(e)}")
            raise

    def close_position(self, symbol: str, qty: float) -> Dict:
        try:
            current_positions = self.get_open_positions()
            position_to_close_side = None
            for p in current_positions:
                if p['symbol'] == symbol:
                    position_to_close_side = p['side']
                    break
            
            if position_to_close_side == "Buy":
                close_side = "Sell"
            elif position_to_close_side == "Sell":
                close_side = "Buy"
            else:
                logging.warning(f"Nelze určit stranu pozice pro zavření {symbol}. Používám 'Sell' (výchozí pro long pozici).")
                close_side = "Sell" 

            response = self.session.place_order(
                category=self.config.default_category,
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=str(qty),
                timeInForce=self.config.time_in_force,
                reduceOnly=True
                # positionIdx="?" # Zakomentováno pro One-Way Mode
            )
            logging.info(f"Pozice úspěšně zavřena: {symbol} {qty}.")
            return response
        except Exception as e:
            logging.error(f"Chyba při zavírání pozice: {str(e)}")
            raise


# --- Flask Web Application ---
app = Flask(__name__)

# Konfigurace databáze SQLite
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trading_history.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Konfigurace APScheduler
app.config['SCHEDULER_API_ENABLED'] = True # Povolí API endpointy pro scheduler
app.config['SCHEDULER_TIMEZONE'] = 'Europe/Prague' # Nastav časovou zónu (nebo jakoukoliv jinou)

scheduler = APScheduler() # Inicializace scheduleru

# Databázový model pro ukládání historie pozic (beze změny)
class PositionRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    total_equity = db.Column(db.String(50))
    total_long_value = db.Column(db.String(50))
    total_short_value = db.Column(db.String(50))
    long_percentage = db.Column(db.String(10))
    short_percentage = db.Column(db.String(10))
    long_symbols = db.Column(db.String(500))
    short_symbols = db.Column(db.String(500))
    settlement_currency = db.Column(db.String(10))
    total_pnl = db.Column(db.String(50))

    positions_json = db.Column(db.Text)

    def __repr__(self):
        return f"<PositionRecord {self.timestamp} - Equity: {self.total_equity}>"

    def to_dict(self):
        return {
            'timestamp': self.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'summary': {
                'total_equity': self.total_equity,
                'total_long_value': self.total_long_value,
                'total_short_value': self.total_short_value,
                'long_percentage': self.long_percentage,
                'short_percentage': self.short_percentage,
                'long_symbols': self.long_symbols,
                'short_symbols': self.short_symbols,
                'settlement_currency': self.settlement_currency,
                'total_pnl': self.total_pnl
            },
            'positions': json.loads(self.positions_json) if self.positions_json else []
        }


# Inicializace BybitTrader
config = TradingConfig(testnet=False)
try:
    trader = BybitTrader(config)
except ValueError as e:
    logging.critical(f"Aplikace nemůže být spuštěna: {e}")
    trader = None

# --- Funkce pro plánovanou úlohu ---
def scheduled_position_save():
    """
    Tato funkce se bude spouštět plánovačem a ukládat aktuální pozice do DB.
    """
    if not trader:
        logging.error("Scheduler: Trading client není inicializován, přeskočeno ukládání pozic.")
        return

    with app.app_context(): # Důležité pro přístup k databázi ve Flask kontextu
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
            total_equity_str = balance_info.get('total_equity', '0')
            total_equity = Decimal(total_equity_str if total_equity_str != 'N/A' else '0')

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

            long_percentage = (total_long_value / total_equity) * 100 if total_equity > 0 else Decimal('0.00')
            short_percentage = (total_short_value / total_equity) * 100 if total_equity > 0 else Decimal('0.00')

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

            new_record = PositionRecord(
                total_equity=summary['total_equity'],
                total_long_value=summary['total_long_value'],
                total_short_value=summary['total_short_value'],
                long_percentage=summary['long_percentage'],
                short_percentage=summary['short_percentage'],
                long_symbols=summary['long_symbols'],
                short_symbols=summary['short_symbols'],
                settlement_currency=summary['settlement_currency'],
                total_pnl=summary['total_pnl'],
                positions_json=json.dumps(positions)
            )
            db.session.add(new_record)
            db.session.commit()
            logging.info(f"Scheduler: Historie pozic úspěšně uložena do databáze v {datetime.now()}.")
        except Exception as db_e:
            db.session.rollback()
            logging.error(f"Scheduler: Chyba při ukládání historie pozic do databáze: {db_e}")

# Routes (beze změny, jen pro úplnost)
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
        for key in ['total_equity', 'available_balance', 'totalMargin', 'totalWalletBalance']:
            if key in balance and isinstance(balance[key], Decimal):
                balance[key] = str(balance[key])
            elif key in balance and balance[key] == 'N/A':
                 balance[key] = '0.00'
        
        if 'coins' in balance:
            for coin in balance['coins']:
                if 'balance' in coin and isinstance(coin['balance'], Decimal):
                    coin['balance'] = str(coin['balance'])
                elif 'balance' in coin and coin['balance'] == 'N/A':
                    coin['balance'] = '0.000000'

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
        total_equity_str = balance_info.get('total_equity', '0')
        total_equity = Decimal(total_equity_str if total_equity_str != 'N/A' else '0')


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

        long_percentage = (total_long_value / total_equity) * 100 if total_equity > 0 else Decimal('0.00')
        short_percentage = (total_short_value / total_equity) * 100 if total_equity > 0 else Decimal('0.00')

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

        # Zde je volání scheduled_position_save() pro manuální uložení,
        # pokud klikneš na "Aktualizovat pozice".
        # Pokud chceš, aby se ukládalo POUZE automaticky, můžeš tento blok zakomentovat.
        # Já ho zde nechávám, aby se data uložila i při manuální aktualizaci z webu.
        try:
            # Abychom se vyhnuli duplicitnímu kódu, zkusíme volat přímo funkci plánovače.
            # Musíme ale předat data nebo nechat funkci načíst je sama.
            # Pro jednoduchost teď duplikuji logiku, ale ideálně by scheduled_position_save
            # měla přijímat 'positions' a 'summary' jako argumenty.
            # Prozatím ponecháme stávající logiku pro okamžité uložení.
            new_record = PositionRecord(
                total_equity=summary['total_equity'],
                total_long_value=summary['total_long_value'],
                total_short_value=summary['total_short_value'],
                long_percentage=summary['long_percentage'],
                short_percentage=summary['short_percentage'],
                long_symbols=summary['long_symbols'],
                short_symbols=summary['short_symbols'],
                settlement_currency=summary['settlement_currency'],
                positions_json=json.dumps(positions),
                total_pnl=summary['total_pnl'] # Přidáno pro konzistenci
            )
            db.session.add(new_record)
            db.session.commit()
            logging.info("Historie pozic manuálně uložena do databáze.")
        except Exception as db_e:
            db.session.rollback()
            logging.error(f"Chyba při manuálním ukládání historie pozic do databáze: {db_e}")

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
    price = float(data.get('price')) 

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
        history_records = PositionRecord.query.order_by(desc(PositionRecord.timestamp)).limit(5).all()
        history_data = [record.to_dict() for record in history_records]
        
        return jsonify(history_data)
    except Exception as e:
        return jsonify({"error": f"Chyba při načítání historie z databáze: {e}"}), 500

# Endpoint pro získání/nastavení intervalu plánovače
@app.route('/scheduler_interval', methods=['GET', 'POST'])
def scheduler_interval():
    if request.method == 'GET':
        return jsonify({"interval_minutes": config.scheduler_interval_minutes})
    elif request.method == 'POST':
        data = request.json
        new_interval = data.get('interval_minutes')
        if new_interval and isinstance(new_interval, int) and new_interval > 0:
            config.scheduler_interval_minutes = new_interval
            # Změnit úlohu v plánovači
            if scheduler.running:
                scheduler.remove_job('scheduled_position_save_job') # Odeber starou úlohu
                scheduler.add_job(
                    id='scheduled_position_save_job',
                    func=scheduled_position_save,
                    trigger='interval',
                    minutes=config.scheduler_interval_minutes,
                    replace_existing=True
                )
                logging.info(f"Interval plánovače změněn na {new_interval} minut.")
            return jsonify({"message": f"Interval aktualizován na {new_interval} minut."}), 200
        return jsonify({"error": "Neplatný interval. Musí být kladné celé číslo."}), 400


if __name__ == '__main__':
    with app.app_context():
        db.create_all()

    # Inicializace a spuštění plánovače
    scheduler.init_app(app)
    # Přidání úlohy
    scheduler.add_job(
        id='scheduled_position_save_job',
        func=scheduled_position_save,
        trigger='interval',
        minutes=config.scheduler_interval_minutes, # Použijeme konfigurovatelný interval
        max_instances=1 # Zabrání souběžnému spuštění více instancí úlohy
    )
    scheduler.start()
    logging.info(f"Plánovač spuštěn s intervalem {config.scheduler_interval_minutes} minut.")

    app.run(debug=True, use_reloader=False) # use_reloader=False je důležité pro APScheduler, aby se úloha nespustila dvakrát