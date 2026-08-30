import asyncio
import logging
import json
import os
import time
from datetime import datetime, timezone
import aiohttp
import ccxt.async_support as ccxt
import ccxt.pro as ccxtpro
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==========================================
# 0. .env 환경변수 파일 자동 로드
# ==========================================
load_dotenv()

# ==========================================
# 1. 설정 및 상수 정의 (.env 자동 연동)
# ==========================================
OKX_CONFIG = {
    'apiKey': os.getenv('OKX_API_KEY'),
    'secret': os.getenv('OKX_SECRET_KEY'),
    'password': os.getenv('OKX_PASSPHRASE'),
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    }
}

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_ADMIN_ID = int(os.getenv('TELEGRAM_ADMIN_ID', 0))

# 트레이딩 파라미터
LEVERAGE = 3                     # 레버리지 3배
MIN_FUNDING_RATE = 0.0001        # 최소 펀딩비 (0.01%)
MIN_VOLUME_24H_USD = 10_000_000  # 최소 24시간 거래량 ($10,000,000)
MIN_ORDER_USDT = 10.0            # 최소 주문 금액 세이프가드 ($10)
BALANCE_USAGE_RATIO = 0.90       # 증거금 안전을 위해 가용 USDT의 90%만 투입
SPOT_FEE_RATE = 0.0015           # 현물 매수 수수료 버퍼 안전하게 0.15% 설정

# 💡 [수수료 절감 최적화 파라미터]
ROTATION_THRESHOLD_SCORE = 1.5   # 신규 종목 펀딩비가 기존 대비 50% 이상 높을 때만 스위칭 (수수료 방어)
MIN_HOLD_TIME_SECONDS = 28800    # 최소 포지션 유지 시간: 8시간 (펀딩비 최소 1회 수령 후 스위칭 허용)
CHECK_INTERVAL_SECONDS = 60      # 1분 간격 체크

# 고도화 실행 옵션 (Pure Maker Chasing & Slippage)
MAX_SLIPPAGE_TOLERANCE_PCT = 0.002 # 0.2% 이상 예상 슬리피지 발생 시 진입 취소
CHASE_RETRY_LIMIT = 5              # 100% 지정가 Chasing 재시도 횟수
CHASE_TIMEOUT_SECONDS = 2.0        # 회당 체결 대기 시간(초)

# 리스크 관리 파라미터
MAX_BASIS_DIVERGENCE_PCT = 0.02   # 현선 괴리율(Basis) 2% 이상 확대로 델타 붕괴 시 손절
TOTAL_EQUITY_STOP_LOSS_PCT = 0.03 # 진입 시점 대비 계정 총 자산 -3% 손실 시 전체 손절
LIQUIDATION_BUFFER_PCT = 0.05     # 청산가와 현재가 거리 5% 이하 진입 시 안전 청산
STATE_FILE = "bot_state.json"     # 포지션 상태 저장 파일

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("DeltaNeutralBot")

# ==========================================
# 2. 텔레그램 알림 핸들러
# ==========================================
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.session: aiohttp.ClientSession = None

    async def init_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def send_message(self, text: str):
        await self.init_session()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            async with self.session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Telegram notification failed: {resp.status}")
        except Exception as e:
            logger.error(f"Error sending telegram message: {e}")

# ==========================================
# 3. 델타 뉴트럴 자동매매 봇 (수수료 극소화형)
# ==========================================
class DeltaNeutralBot:
    def __init__(self, exchange_config, notifier: TelegramNotifier):
        self.exchange_rest = ccxt.okx(exchange_config)
        self.exchange_ws = ccxtpro.okx(exchange_config)
        
        self.notifier = notifier
        self.is_active = True
        self.current_symbol = None       # 예: "BTC/USDT"
        self.current_swap_symbol = None  # 예: "BTC/USDT:USDT"
        self.entry_spot_price = 0.0
        self.entry_swap_price = 0.0
        self.initial_equity_usdt = 0.0    # 진입 시점 총 자산
        self.liquidation_price = 0.0
        self.entry_timestamp = 0.0        # 포지션 진입 시각
        self.trade_lock = asyncio.Lock()

    def save_state(self):
        """원자적(Atomic) 파일 쓰기"""
        state = {
            "current_symbol": self.current_symbol,
            "current_swap_symbol": self.current_swap_symbol,
            "entry_spot_price": self.entry_spot_price,
            "entry_swap_price": self.entry_swap_price,
            "initial_equity_usdt": self.initial_equity_usdt,
            "liquidation_price": self.liquidation_price,
            "entry_timestamp": self.entry_timestamp,
            "is_active": self.is_active
        }
        temp_file = f"{STATE_FILE}.tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(state, f, indent=4)
            os.replace(temp_file, STATE_FILE)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    async def load_and_reconcile_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                    self.current_symbol = state.get("current_symbol")
                    self.current_swap_symbol = state.get("current_swap_symbol")
                    self.entry_spot_price = state.get("entry_spot_price", 0.0)
                    self.entry_swap_price = state.get("entry_swap_price", 0.0)
                    self.initial_equity_usdt = state.get("initial_equity_usdt", 0.0)
                    self.liquidation_price = state.get("liquidation_price", 0.0)
                    self.entry_timestamp = state.get("entry_timestamp", 0.0)
                    self.is_active = state.get("is_active", True)
            except Exception as e:
                logger.error(f"Error loading state file: {e}")

        try:
            positions = await self.exchange_rest.fetch_positions()
            active_short = None
            for pos in positions:
                contracts = float(pos.get('contracts', 0))
                if pos['side'] == 'short' and contracts > 0:
                    active_short = pos
                    break

            if active_short:
                self.current_swap_symbol = active_short['symbol']
                self.current_symbol = self.current_swap_symbol.split(':')[0]
                self.entry_swap_price = float(active_short.get('entryPrice', 0.0) or 0.0)
                self.liquidation_price = float(active_short.get('liquidationPrice', 0.0) or 0.0)
                
                if self.entry_spot_price == 0.0:
                    ticker = await self.exchange_rest.fetch_ticker(self.current_symbol)
                    self.entry_spot_price = float(ticker.get('last', self.entry_swap_price))
                if self.entry_timestamp == 0.0:
                    self.entry_timestamp = time.time()
                
                logger.info(f"Reconciled existing position: {self.current_symbol} / {self.current_swap_symbol}")
            else:
                self.current_symbol = None
                self.current_swap_symbol = None
                self.entry_spot_price = 0.0
                self.entry_swap_price = 0.0
                self.liquidation_price = 0.0
                self.initial_equity_usdt = 0.0
                self.entry_timestamp = 0.0

            self.save_state()
        except Exception as e:
            logger.error(f"Error reconciling positions: {e}")

    async def init_exchange(self):
        await self.exchange_rest.load_markets()
        await self.exchange_ws.load_markets()
        
        try:
            await self.exchange_rest.setPositionMode(False)
        except Exception as e:
            logger.debug(f"Position mode set note: {e}")
            
        await self.load_and_reconcile_state()

    async def is_funding_window(self, swap_symbol: str = None) -> bool:
        if not swap_symbol:
            return False
        try:
            funding_info = await self.exchange_rest.fetch_funding_rate(swap_symbol)
            next_funding_ts = funding_info.get('nextFundingTimestamp') or funding_info.get('info', {}).get('fundingTime')
            
            if next_funding_ts:
                next_funding_time = int(next_funding_ts) / 1000.0
                now_ts = time.time()
                diff_seconds = abs(next_funding_time - now_ts)
                if diff_seconds <= 300 or (now_ts > next_funding_time and (now_ts - next_funding_time) <= 300):
                    return True
        except Exception as e:
            logger.warning(f"Error checking funding window: {e}")
        return False

    def amount_to_contracts(self, swap_symbol, coin_amount):
        market = self.exchange_rest.market(swap_symbol)
        contract_size = float(market.get('contractSize', 1.0))
        raw_contracts = coin_amount / contract_size
        contracts_str = self.exchange_rest.amount_to_precision(swap_symbol, raw_contracts)
        return float(contracts_str)

    async def ensure_trading_balance(self):
        try:
            balance = await self.exchange_rest.fetch_balance()
            trading_usdt = float(balance.get('USDT', {}).get('free', 0.0))
            
            if trading_usdt < MIN_ORDER_USDT:
                funding_balance = await self.exchange_rest.fetch_balance({'type': 'funding'})
                funding_usdt = float(funding_balance.get('USDT', {}).get('free', 0.0))
                
                if funding_usdt >= MIN_ORDER_USDT:
                    logger.info(f"Transferring {funding_usdt} USDT from Funding to Trading account...")
                    await self.exchange_rest.transfer('USDT', funding_usdt, 'funding', 'trading')
                    await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Auto-transfer check note: {e}")

    async def get_total_equity_usdt(self) -> float:
        try:
            balance = await self.exchange_rest.fetch_balance()
            total_usdt = float(balance.get('USDT', {}).get('total', 0.0))
            
            if self.current_symbol:
                base_curr = self.current_symbol.split('/')[0]
                spot_amt = float(balance.get(base_curr, {}).get('total', 0.0))
                if spot_amt > 0:
                    ticker = await self.exchange_rest.fetch_ticker(self.current_symbol)
                    total_usdt += spot_amt * float(ticker['last'])
            return total_usdt
        except Exception as e:
            logger.error(f"Error fetching total equity: {e}")
            return 0.0

    async def check_slippage(self, symbol: str, side: str, amount: float) -> bool:
        try:
            orderbook = await self.exchange_rest.fetch_order_book(symbol, limit=20)
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            
            accumulated_qty = 0.0
            accumulated_cost = 0.0
            
            for price, qty in orders:
                needed = amount - accumulated_qty
                if qty >= needed:
                    accumulated_cost += needed * price
                    accumulated_qty += needed
                    break
                else:
                    accumulated_cost += qty * price
                    accumulated_qty += qty
            
            if accumulated_qty < amount:
                logger.warning(f"Orderbook depth insufficient for {symbol}")
                return False

            expected_avg_price = accumulated_cost / amount
            best_price = orders[0][0]
            
            slippage = abs(expected_avg_price - best_price) / best_price
            if slippage > MAX_SLIPPAGE_TOLERANCE_PCT:
                logger.warning(f"Slippage too high for {symbol}: {slippage*100:.3f}% > {MAX_SLIPPAGE_TOLERANCE_PCT*100}%")
                return False
                
            return True
        except Exception as e:
            logger.error(f"Error checking slippage guard: {e}")
            return False

    async def execute_pure_maker_chasing_order(self, symbol: str, side: str, amount: float, is_swap: bool = False):
        """100% 지정가(Maker) 전용 Chasing 주문"""
        remaining_amt = amount
        total_filled = 0.0
        
        for attempt in range(CHASE_RETRY_LIMIT):
            if remaining_amt <= 0:
                break
                
            orderbook = await self.exchange_rest.fetch_order_book(symbol, limit=5)
            price = orderbook['bids'][0][0] if side == 'buy' else orderbook['asks'][0][0]
            
            amt_str = self.exchange_rest.amount_to_precision(symbol, remaining_amt)
            if float(amt_str) <= 0:
                break

            params = {'postOnly': True}
            if is_swap:
                params['posMode'] = 'net'
                
            try:
                order = await self.exchange_rest.create_order(symbol, 'limit', side, float(amt_str), price, params)
                await asyncio.sleep(CHASE_TIMEOUT_SECONDS)
                
                order_info = await self.exchange_rest.fetch_order(order['id'], symbol)
                filled = float(order_info.get('filled', 0.0))
                total_filled += filled
                remaining_amt -= filled
                
                if remaining_amt > 0:
                    await self.exchange_rest.cancel_order(order['id'], symbol)
            except Exception as e:
                logger.debug(f"Maker order retry ({attempt+1}/{CHASE_RETRY_LIMIT}): {e}")
                await asyncio.sleep(0.3)

        return total_filled

    async def get_top_funding_opportunity(self):
        try:
            tickers = await self.exchange_rest.fetch_tickers()
            funding_rates = await self.exchange_rest.fetch_funding_rates()
            
            candidates = []
            for symbol, ticker in tickers.items():
                if not symbol.endswith("/USDT:USDT"):
                    continue
                
                base_symbol = symbol.split(":")[0]
                quote_volume = float(ticker.get('quoteVolume', 0) or 0)
                
                if quote_volume < MIN_VOLUME_24H_USD:
                    continue
                    
                funding_info = funding_rates.get(symbol, {})
                funding_rate = float(funding_info.get('fundingRate', 0) or 0)
                
                if funding_rate >= MIN_FUNDING_RATE:
                    candidates.append({
                        'symbol': base_symbol,
                        'swap_symbol': symbol,
                        'funding_rate': funding_rate,
                        'volume': quote_volume
                    })

            if not candidates:
                return None

            candidates.sort(key=lambda x: x['funding_rate'], reverse=True)
            return candidates[0]
            
        except Exception as e:
            logger.error(f"Error fetching opportunities: {e}")
            return None

    async def cancel_all_open_orders(self, symbol=None):
        try:
            if symbol:
                await self.exchange_rest.cancel_all_orders(symbol)
        except Exception as e:
            logger.warning(f"Could not cancel open orders for {symbol}: {e}")

    async def close_all_positions(self):
        async with self.trade_lock:
            if not self.current_symbol or not self.current_swap_symbol:
                return

            symbol = self.current_symbol
            swap_symbol = self.current_swap_symbol
            
            try:
                await self.cancel_all_open_orders(symbol)
                await self.cancel_all_open_orders(swap_symbol)

                positions_task = self.exchange_rest.fetch_positions([swap_symbol])
                balance_task = self.exchange_rest.fetch_balance()
                positions, balance = await asyncio.gather(positions_task, balance_task, return_exceptions=True)

                close_tasks = []

                if isinstance(positions, list):
                    for pos in positions:
                        contracts = float(pos.get('contracts', 0))
                        if pos['side'] == 'short' and contracts > 0:
                            close_tasks.append(
                                self.execute_pure_maker_chasing_order(swap_symbol, 'buy', contracts, is_swap=True)
                            )

                base_currency = symbol.split('/')[0]
                if isinstance(balance, dict):
                    spot_amount = float(balance.get(base_currency, {}).get('free', 0))
                    if spot_amount > 0:
                        amount_str = self.exchange_rest.amount_to_precision(symbol, spot_amount)
                        market_info = self.exchange_rest.market(symbol)
                        min_amt = float(market_info.get('limits', {}).get('amount', {}).get('min', 0) or 0)
                        
                        if float(amount_str) >= min_amt and float(amount_str) > 0:
                            close_tasks.append(
                                self.execute_pure_maker_chasing_order(symbol, 'sell', float(amount_str), is_swap=False)
                            )

                if close_tasks:
                    await asyncio.gather(*close_tasks, return_exceptions=True)

                balance_after = await self.exchange_rest.fetch_balance()
                rem_spot = float(balance_after.get(base_currency, {}).get('free', 0))
                if rem_spot > 0:
                    rem_str = self.exchange_rest.amount_to_precision(symbol, rem_spot)
                    if float(rem_str) > 0:
                        await self.exchange_rest.create_market_sell_order(symbol, float(rem_str))

                await self.notifier.send_message(f"✅ **수수료 절감 청산 완료**: {symbol} 포지션 정리 완료.")
                self.current_symbol = None
                self.current_swap_symbol = None
                self.entry_spot_price = 0.0
                self.entry_swap_price = 0.0
                self.liquidation_price = 0.0
                self.initial_equity_usdt = 0.0
                self.entry_timestamp = 0.0
                self.save_state()

            except Exception as e:
                logger.error(f"Error closing positions: {e}")
                await self.notifier.send_message(f"❌ **청산 중 오류 발생**: {e}")

    async def open_position(self, target_opportunity, total_usdt_balance):
        async with self.trade_lock:
            await self.ensure_trading_balance()
            
            usable_usdt = total_usdt_balance * BALANCE_USAGE_RATIO
            if usable_usdt < MIN_ORDER_USDT:
                logger.warning(f"Usable USDT (${usable_usdt:.2f}) is below minimum limit.")
                return

            symbol = target_opportunity['symbol']
            swap_symbol = target_opportunity['swap_symbol']
            
            try:
                await self.exchange_rest.set_leverage(LEVERAGE, swap_symbol, params={'mgnMode': 'cross'})
                
                ticker_spot = await self.exchange_rest.fetch_ticker(symbol)
                spot_price = float(ticker_spot['last'])
                
                half_usdt = usable_usdt / 2.0
                spot_amount_raw = (half_usdt * (1.0 - SPOT_FEE_RATE)) / spot_price
                spot_amount_str = self.exchange_rest.amount_to_precision(symbol, spot_amount_raw)
                spot_amount_final = float(spot_amount_str)

                if spot_amount_final <= 0:
                    return

                swap_contracts = self.amount_to_contracts(swap_symbol, spot_amount_final)
                if swap_contracts <= 0:
                    logger.warning("Calculated swap contracts is 0. Aborting entry.")
                    return

                spot_ok = await self.check_slippage(symbol, 'buy', spot_amount_final)
                swap_ok = await self.check_slippage(swap_symbol, 'sell', swap_contracts)

                if not spot_ok or not swap_ok:
                    await self.notifier.send_message(f"⚠️ **[{symbol}] 슬리피지 한도 초과/오더북 깊이 부족으로 진입 취소**")
                    return

                actual_spot_filled = await self.execute_pure_maker_chasing_order(symbol, 'buy', spot_amount_final, is_swap=False)
                
                if actual_spot_filled <= 0:
                    logger.warning("Spot Maker order was not filled. Cancelling entry to save fees.")
                    return

                exact_swap_contracts = self.amount_to_contracts(swap_symbol, actual_spot_filled)
                
                actual_swap_filled = 0.0
                if exact_swap_contracts > 0:
                    actual_swap_filled = await self.execute_pure_maker_chasing_order(swap_symbol, 'sell', exact_swap_contracts, is_swap=True)

                balance = await self.exchange_rest.fetch_balance()
                base_curr = symbol.split('/')[0]
                real_spot_holding = float(balance.get(base_curr, {}).get('free', 0.0))
                
                target_contracts = self.amount_to_contracts(swap_symbol, real_spot_holding)
                
                positions = await self.exchange_rest.fetch_positions([swap_symbol])
                current_short_contracts = 0.0
                for pos in positions:
                    if pos['side'] == 'short':
                        current_short_contracts = float(pos.get('contracts', 0))
                        break
                
                diff_contracts = target_contracts - current_short_contracts
                
                if abs(diff_contracts) > 0:
                    logger.info(f"Rebalancing contract mismatch: {diff_contracts}")
                    diff_str = self.exchange_rest.amount_to_precision(swap_symbol, abs(diff_contracts))
                    if float(diff_str) > 0:
                        if diff_contracts > 0:
                            await self.execute_pure_maker_chasing_order(swap_symbol, 'sell', float(diff_str), is_swap=True)
                        else:
                            await self.execute_pure_maker_chasing_order(swap_symbol, 'buy', float(diff_str), is_swap=True)

                positions = await self.exchange_rest.fetch_positions([swap_symbol])
                liq_price = 0.0
                for pos in positions:
                    if pos['side'] == 'short' and float(pos.get('contracts', 0)) > 0:
                        liq_price = float(pos.get('liquidationPrice', 0) or 0)
                        break

                ticker_swap = await self.exchange_rest.fetch_ticker(swap_symbol)

                self.current_symbol = symbol
                self.current_swap_symbol = swap_symbol
                self.entry_spot_price = spot_price
                self.entry_swap_price = float(ticker_swap['last'])
                self.liquidation_price = liq_price
                self.initial_equity_usdt = await self.get_total_equity_usdt()
                self.entry_timestamp = time.time()
                self.save_state()

                await self.notifier.send_message(
                    f"🚀 **100% Maker 수수료 최적화 포지션 진입 완료**\n"
                    f"종목: {symbol}\n"
                    f"현물진입가: {spot_price}\n"
                    f"선물진입가: {self.entry_swap_price}\n"
                    f"청산가: {liq_price}\n"
                    f"순현물보유: {real_spot_holding}\n"
                    f"예상 펀딩비: {target_opportunity['funding_rate']*100:.4f}%"
                )

            except Exception as e:
                logger.error(f"Error opening position: {e}")
                await self.notifier.send_message(f"❌ **진입 처리 중 예외 발생**: {e}")
                await self.close_all_positions()

    async def run_websocket_stop_loss(self):
        last_equity_check_time = 0
        cached_equity = 0.0

        while True:
            try:
                if self.is_active and self.current_swap_symbol and self.entry_spot_price > 0:
                    swap_symbol = self.current_swap_symbol
                    
                    ticker = await self.exchange_ws.watch_ticker(swap_symbol)
                    current_swap_price = float(ticker.get('last', 0) or 0)

                    if current_swap_price > 0:
                        if self.liquidation_price > 0:
                            dist_to_liq = (self.liquidation_price - current_swap_price) / current_swap_price
                            if dist_to_liq <= LIQUIDATION_BUFFER_PCT:
                                await self.notifier.send_message(
                                    f"🚨 **[위험] 강제청산가 근접 감지!**\n종목: {self.current_symbol}\n현재가: {current_swap_price}\n청산가: {self.liquidation_price}\n긴급 청산합니다."
                                )
                                await self.close_all_positions()
                                await asyncio.sleep(5)
                                continue

                        basis_change = abs(current_swap_price - self.entry_swap_price) / self.entry_swap_price
                        if basis_change >= MAX_BASIS_DIVERGENCE_PCT:
                            await self.notifier.send_message(
                                f"⚡ **[스탑로스] 현-선물 가격 괴리율 비정상 확대!** ({basis_change*100:.2f}% 변동)\n긴급 청산합니다."
                            )
                            await self.close_all_positions()
                            await asyncio.sleep(5)
                            continue

                        now_ts = time.time()
                        if now_ts - last_equity_check_time > 10:
                            cached_equity = await self.get_total_equity_usdt()
                            last_equity_check_time = now_ts

                        if self.initial_equity_usdt > 0 and cached_equity > 0:
                            equity_loss_pct = (self.initial_equity_usdt - cached_equity) / self.initial_equity_usdt
                            if equity_loss_pct >= TOTAL_EQUITY_STOP_LOSS_PCT:
                                await self.notifier.send_message(
                                    f"🛑 **[스탑로스] 계정 총 자산 -{equity_loss_pct*100:.2f}% 손실 감지!**\n자산 보호 청산."
                                )
                                await self.close_all_positions()
                                await asyncio.sleep(5)
                                continue

                else:
                    await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"WebSocket Error: {e}")
                await asyncio.sleep(3)

    async def run_screening_loop(self):
        await self.notifier.send_message("🤖 **수수료 극소화형 델타 뉴트럴 자동 매매 봇 가동 시작**")
        
        while True:
            try:
                if self.is_active:
                    if self.current_swap_symbol and await self.is_funding_window(self.current_swap_symbol):
                        logger.info("Funding rate window active. Skipping rotation check.")
                        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                        continue

                    best_opp = await self.get_top_funding_opportunity()
                    
                    if best_opp:
                        if not self.current_symbol:
                            balance = await self.exchange_rest.fetch_balance()
                            usdt_free = float(balance.get('USDT', {}).get('free', 0))
                            
                            if usdt_free >= MIN_ORDER_USDT:
                                await self.open_position(best_opp, usdt_free)

                        elif best_opp['symbol'] != self.current_symbol:
                            held_duration = time.time() - self.entry_timestamp
                            if held_duration < MIN_HOLD_TIME_SECONDS:
                                logger.info(f"Position held for {held_duration/3600:.2f}h < 8h. Skipping rotation to save fees.")
                                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                                continue

                            curr_swap = self.current_swap_symbol
                            curr_funding_info = await self.exchange_rest.fetch_funding_rate(curr_swap)
                            curr_funding = float(curr_funding_info.get('fundingRate', 0) or 0)
                            
                            if best_opp['funding_rate'] > curr_funding * ROTATION_THRESHOLD_SCORE:
                                await self.notifier.send_message(
                                    f"🔄 **수수료 대비 고수익 종목 교체**: {self.current_symbol} -> {best_opp['symbol']}\n"
                                    f"기존 펀딩비: {curr_funding*100:.4f}% -> 신규 펀딩비: {best_opp['funding_rate']*100:.4f}%"
                                )
                                await self.close_all_positions()
                                await asyncio.sleep(2)
                                
                                balance = await self.exchange_rest.fetch_balance()
                                usdt_free = float(balance.get('USDT', {}).get('free', 0))
                                await self.open_position(best_opp, usdt_free)

            except Exception as e:
                logger.error(f"Error in screening loop: {e}")
                
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ==========================================
# 4. 메인 실행부 (Telegram Bot 비동기 루프 통합)
# ==========================================
async def main():
    notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    await notifier.init_session()
    
    bot = DeltaNeutralBot(OKX_CONFIG, notifier)
    await bot.init_exchange()

    def admin_only(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.effective_user.id != TELEGRAM_ADMIN_ID:
                await update.message.reply_text("⛔ **권한이 없습니다.**")
                return
            return await func(update, context)
        return wrapper

    @admin_only
    async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot.is_active = not bot.is_active
        bot.save_state()
        status_str = "ON 🟢" if bot.is_active else "OFF 🔴"
        await update.message.reply_text(f"스위치 상태: {status_str}")

    @admin_only
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = "작동 중 🟢" if bot.is_active else "정지 중 🔴"
        curr = bot.current_symbol if bot.current_symbol else "없음"
        equity = await bot.get_total_equity_usdt()
        held_hours = (time.time() - bot.entry_timestamp) / 3600.0 if bot.entry_timestamp > 0 else 0.0
        
        await update.message.reply_text(
            f"상태: {status}\n"
            f"현재 포지션: {curr}\n"
            f"유지 시간: {held_hours:.1f}시간\n"
            f"총 자산: ${equity:.2f} USDT\n"
            f"현물진입가: {bot.entry_spot_price}\n"
            f"선물진입가: {bot.entry_swap_price}\n"
            f"청산가: {bot.liquidation_price}"
        )

    # Telegram Application 설정
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))

    await app.initialize()
    await app.start()
    
    updater = app.updater
    await updater.start_polling(drop_pending_updates=True)
    
    screening_task = asyncio.create_task(bot.run_screening_loop())
    ws_sl_task = asyncio.create_task(bot.run_websocket_stop_loss())

    try:
        await asyncio.gather(screening_task, ws_sl_task)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutdown sequence initiated...")
        await updater.stop()
        await app.stop()
        await app.shutdown()
        await bot.exchange_rest.close()
        await bot.exchange_ws.close()
        await notifier.close_session()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Program terminated cleanly.")