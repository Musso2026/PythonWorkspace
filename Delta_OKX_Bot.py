import os
import sys
import time
import math
import logging
import asyncio
from datetime import datetime, timezone
import ccxt.async_support as ccxt_async  # 초고속 비동기 처리를 위한 async ccxt
import ccxt
from dotenv import load_dotenv

# Telegram Bot API (python-telegram-bot v20+)
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# ==========================================
# 1. 환경 변수 및 기본 설정
# ==========================================
workspace_env = os.path.expanduser('~/PythonWorkspace/.env')
home_env = os.path.expanduser('~/.env')

if os.path.exists(workspace_env):
    load_dotenv(dotenv_path=workspace_env)
elif os.path.exists(home_env):
    load_dotenv(dotenv_path=home_env)
else:
    load_dotenv()

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")

# 💰 [원화 약 20만원 상당 USDT 입금 원금 설정]
INITIAL_DEPOSIT_USDT = float(os.getenv("INITIAL_DEPOSIT_USDT", 145.00))

# 🎯 펀딩비 및 안전 하한선 설정
MIN_FUNDING_RATE = 0.0001    # 실시간 동적 설정 (기본값)
EXIT_FUNDING_RATE = 0.00005   # 청산 하한선 (기본 0.005%)
ABSOLUTE_MIN_FUNDING = 0.0001 # 🚨 절댓값 하한선 0.01% (이하는 절대 진입 금지)

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

# 동기식 CCXT (텔레그램 단순 스펙 변경용) 및 비동기 CCXT (초고속 매매용)
def get_okx_client():
    return ccxt.okx({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'createMarketBuyOrderRequiresPrice': False
        }
    })

def get_async_okx_client():
    return ccxt_async.okx({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'createMarketBuyOrderRequiresPrice': False
        }
    })

okx = get_okx_client()
async_okx = get_async_okx_client()

# ==========================================
# 글로벌 변수 및 코인 스펙 상태 관리
# ==========================================
BOT_SWITCH = True
TARGET_COIN = "DOGE"
SYMBOL_SPOT = "DOGE/USDT"
SYMBOL_SWAP = "DOGE/USDT:USDT"
TARGET_LEVERAGE = 3  # 기본 레버리지 3배

COIN_SPEC = {
    'ctVal': 10.0,
    'spot_amount_prec': 2,
    'swap_amount_prec': 0,
    'spot_min_amount': 1.0,
    'swap_min_amount': 1.0,
}

POSITION_BASE_USDT = 0.0

# ==========================================
# 2. 정밀도 및 거래소 설정 함수
# ==========================================
def truncate_value(val: float, precision: int) -> float:
    """소수점 버림 처리"""
    if precision == 0:
        return float(math.floor(val))
    factor = 10 ** precision
    return math.floor(val * factor) / factor

def setup_exchange_account_mode(swap_symbol: str, leverage: int = 3):
    """OKX 레버리지 및 Cross(교차) 마진 모드 설정"""
    try:
        okx.set_leverage(leverage, swap_symbol, params={'mgnMode': 'cross'})
        logging.info(f"✅ {swap_symbol} 교차 마진 {leverage}배 설정 완료")
    except Exception as e:
        logging.warning(f"⚠️ 마진 모드 설정 참고사항: {e}")

def update_coin_spec(coin_symbol: str):
    """코인 스펙 파싱 및 스위칭"""
    global COIN_SPEC, TARGET_COIN, SYMBOL_SPOT, SYMBOL_SWAP
    
    markets = okx.load_markets(reload=True)
    spot_sym = f"{coin_symbol}/USDT"
    swap_sym = f"{coin_symbol}/USDT:USDT"

    if spot_sym not in markets or swap_sym not in markets:
        raise ValueError(f"OKX 거래소에서 {coin_symbol} 마켓을 찾을 수 없습니다.")

    spot_m = markets[spot_sym]
    swap_m = markets[swap_sym]

    TARGET_COIN = coin_symbol
    SYMBOL_SPOT = spot_sym
    SYMBOL_SWAP = swap_sym

    COIN_SPEC['ctVal'] = float(swap_m['info'].get('ctVal', 1.0))
    COIN_SPEC['spot_amount_prec'] = int(spot_m['precision']['amount']) if spot_m['precision']['amount'] is not None else 2
    COIN_SPEC['swap_amount_prec'] = int(swap_m['precision']['amount']) if swap_m['precision']['amount'] is not None else 0
    COIN_SPEC['spot_min_amount'] = float(spot_m['limits']['amount']['min']) if spot_m['limits']['amount']['min'] else 0.0001
    COIN_SPEC['swap_min_amount'] = float(swap_m['limits']['amount']['min']) if swap_m['limits']['amount']['min'] else 1.0

    setup_exchange_account_mode(SYMBOL_SWAP, TARGET_LEVERAGE)

# ==========================================
# 3. 텔레그램 및 환율 정보 조회
# ==========================================
async def send_telegram_msg_async(app: Application, text: str):
    """비동기 텔레그램 알림 전송"""
    if TELEGRAM_TOKEN and TELEGRAM_ADMIN_ID:
        try:
            await app.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=text)
        except Exception as e:
            logging.error(f"텔레그램 메시지 전송 실패: {e}")

def get_usdt_krw_rate() -> float:
    """실시간 업비트/빗썸 USDT/KRW 환율 조회"""
    try:
        upbit = ccxt.upbit()
        ticker = upbit.fetch_ticker('USDT/KRW')
        return float(ticker['last'])
    except Exception:
        try:
            bithumb = ccxt.bithumb()
            ticker = bithumb.fetch_ticker('USDT/KRW')
            return float(ticker['last'])
        except Exception as e:
            logging.error(f"환율 조회 실패 (기본 환율 1,350원 적용): {e}")
            return 1350.0

async def get_ticker_prices_async():
    """비동기 초고속 현물 및 선물 현재가 조회"""
    try:
        spot_task = async_okx.fetch_ticker(SYMBOL_SPOT)
        swap_task = async_okx.fetch_ticker(SYMBOL_SWAP)
        spot_ticker, swap_ticker = await asyncio.gather(spot_task, swap_task)
        return float(spot_ticker['last']), float(swap_ticker['last'])
    except Exception as e:
        logging.error(f"시세 조회 에러: {e}")
        return 0.0, 0.0

def get_ticker_prices():
    """동기 시세 조회 (텔레그램 명령 처리용)"""
    try:
        spot_ticker = okx.fetch_ticker(SYMBOL_SPOT)
        swap_ticker = okx.fetch_ticker(SYMBOL_SWAP)
        return float(spot_ticker['last']), float(swap_ticker['last'])
    except Exception as e:
        logging.error(f"시세 조회 에러: {e}")
        return 0.0, 0.0

# ==========================================
# 4. 정밀 자산 및 포지션 조회
# ==========================================
async def get_positions_async():
    """비동기 초고속 선물 포지션 조회"""
    try:
        positions = await async_okx.fetch_positions([SYMBOL_SWAP])
        for pos in positions:
            if pos['symbol'] == SYMBOL_SWAP and float(pos['contracts']) > 0:
                return pos
        return None
    except Exception as e:
        logging.error(f"포지션 조회 에러: {e}")
        return None

def get_positions():
    """동기 포지션 조회"""
    try:
        positions = okx.fetch_positions([SYMBOL_SWAP])
        for pos in positions:
            if pos['symbol'] == SYMBOL_SWAP and float(pos['contracts']) > 0:
                return pos
        return None
    except Exception as e:
        logging.error(f"포지션 조회 에러: {e}")
        return None

def get_balance():
    """동기 자산 정밀 합산"""
    try:
        balance = okx.fetch_balance()
        krw_rate = get_usdt_krw_rate()
        spot_price, swap_price = get_ticker_prices()

        usdt_free = float(balance['free'].get('USDT', 0.0))
        coin_total = float(balance['total'].get(TARGET_COIN, 0.0))
        
        spot_val_usdt = coin_total * spot_price
        spot_val_krw = spot_val_usdt * krw_rate
        
        pos = get_positions()
        swap_pnl_usdt = 0.0
        if pos:
            swap_pnl_usdt = float(pos.get('unrealizedPnl', 0.0))
            if swap_pnl_usdt == 0.0 and pos.get('entryPrice'):
                entry_p = float(pos.get('entryPrice', 0.0))
                contracts = float(pos.get('contracts', 0.0))
                swap_pnl_usdt = (entry_p - swap_price) * (contracts * COIN_SPEC['ctVal'])

        pure_bot_equity = usdt_free + spot_val_usdt + swap_pnl_usdt
        return usdt_free, pure_bot_equity, coin_total, spot_val_krw
    except Exception as e:
        logging.error(f"잔고 조회 에러: {e}")
        return 0.0, 0.0, 0.0, 0.0

async def get_balance_async():
    """비동기 초고속 자산 합산"""
    try:
        balance_task = async_okx.fetch_balance()
        price_task = get_ticker_prices_async()
        pos_task = get_positions_async()
        
        balance, (spot_price, swap_price), pos = await asyncio.gather(balance_task, price_task, pos_task)
        
        usdt_free = float(balance['free'].get('USDT', 0.0))
        coin_total = float(balance['total'].get(TARGET_COIN, 0.0))
        spot_val_usdt = coin_total * spot_price
        
        swap_pnl_usdt = 0.0
        if pos:
            swap_pnl_usdt = float(pos.get('unrealizedPnl', 0.0))
            if swap_pnl_usdt == 0.0 and pos.get('entryPrice'):
                entry_p = float(pos.get('entryPrice', 0.0))
                contracts = float(pos.get('contracts', 0.0))
                swap_pnl_usdt = (entry_p - swap_price) * (contracts * COIN_SPEC['ctVal'])

        pure_bot_equity = usdt_free + spot_val_usdt + swap_pnl_usdt
        return usdt_free, pure_bot_equity, coin_total
    except Exception as e:
        logging.error(f"비동기 잔고 조회 에러: {e}")
        return 0.0, 0.0, 0.0

def has_open_orders():
    """미체결 주문 존재 여부 확인"""
    try:
        orders_spot = okx.fetch_open_orders(SYMBOL_SPOT)
        orders_swap = okx.fetch_open_orders(SYMBOL_SWAP)
        return len(orders_spot) > 0 or len(orders_swap) > 0
    except Exception as e:
        logging.error(f"미체결 주문 조회 에러: {e}")
        return True

def cancel_all_open_orders():
    """해당 마켓 미체결 주문 취소"""
    try:
        okx.cancel_all_orders(SYMBOL_SPOT)
        okx.cancel_all_orders(SYMBOL_SWAP)
        logging.info("🧹 미체결 주문 일괄 취소 완료")
    except Exception as e:
        logging.error(f"미체결 주문 취소 중 에러: {e}")

async def get_funding_rate_async():
    """비동기 초고속 펀딩비 조회"""
    try:
        funding_info = await async_okx.fetch_funding_rate(SYMBOL_SWAP)
        return float(funding_info.get('fundingRate', 0.0))
    except Exception as e:
        logging.error(f"펀딩비 조회 에러: {e}")
        return 0.0

def get_funding_rate():
    """동기 펀딩비 조회"""
    try:
        funding_info = okx.fetch_funding_rate(SYMBOL_SWAP)
        return float(funding_info.get('fundingRate', 0.0))
    except Exception as e:
        logging.error(f"펀딩비 조회 에러: {e}")
        return 0.0

# ==========================================
# 5. 🔥 거래대금 & 펀딩비 복합 자동 탐색 (나노초 스피드 스캔)
# ==========================================
async def find_best_funding_coin(min_volume_usdt: float = 10000000.0):
    """
    거래대금과 펀딩비의 복합 가중치를 계산하여 최우선 순위 코인 탐색.
    0.01% (0.0001) 초과 종목 중 거래대금과 펀딩비가 높은 최적 코인 선택.
    실시간 펀딩비의 95% 선을 진입 기준으로 자동 설정.
    """
    global MIN_FUNDING_RATE, EXIT_FUNDING_RATE, TARGET_COIN
    try:
        t_start_ns = time.perf_counter_ns()
        
        pos = await get_positions_async()
        if pos is not None or has_open_orders():
            return

        tickers = await async_okx.fetch_tickers()
        
        candidate_symbols = []
        for symbol, ticker in tickers.items():
            if symbol.endswith(':USDT') and '/USDT' in symbol:
                quote_volume = float(ticker.get('quoteVolume', 0.0) or 0.0)
                if quote_volume >= min_volume_usdt:
                    candidate_symbols.append((symbol, symbol.split('/')[0], quote_volume))

        if not candidate_symbols:
            return

        # 병렬 비동기 펀딩비 일괄 스캔
        funding_tasks = [async_okx.fetch_funding_rate(sym) for sym, _, _ in candidate_symbols]
        funding_results = await asyncio.gather(*funding_tasks, return_exceptions=True)

        best_coin = None
        best_funding = -999.0
        best_score = -999.0

        for (sym, coin, volume), result in zip(candidate_symbols, funding_results):
            if isinstance(result, dict) and 'fundingRate' in result:
                rate = float(result.get('fundingRate', 0.0) or 0.0)
                
                # 🚨 [하한선 조건] 실시간 펀딩비가 0.01% (0.0001) 이하일 경우 무조건 제외
                if rate > ABSOLUTE_MIN_FUNDING:
                    # 복합 점수 계산: (펀딩비 * 10,000) * log10(거래대금)
                    score = (rate * 10000.0) * math.log10(volume)
                    if score > best_score:
                        best_score = score
                        best_coin = coin
                        best_funding = rate

        t_scan_ms = (time.perf_counter_ns() - t_start_ns) / 1_000_000.0

        # 조건 충족하는 최우선 순위 코인 반영
        if best_coin and best_funding > ABSOLUTE_MIN_FUNDING:
            if best_coin != TARGET_COIN:
                logging.info(
                    f"🔎 [우선순위 코인 변경] {best_coin} 선정! "
                    f"(실시간 펀딩비: {best_funding*100:.4f}%, 스캔소요: {t_scan_ms:.2f}ms)"
                )
                await asyncio.to_thread(update_coin_spec, best_coin)

            # 💡 [핵심] 진입 기준 = 해당 코인 실시간 펀딩비의 95%
            calculated_entry_rate = best_funding * 0.95
            
            # 0.01% 이하로 떨어지지 않도록 안전 하한선 적용
            MIN_FUNDING_RATE = max(calculated_entry_rate, ABSOLUTE_MIN_FUNDING)
            EXIT_FUNDING_RATE = max(MIN_FUNDING_RATE * 0.2, 0.00005)

            logging.info(
                f"🎯 [{TARGET_COIN} 세팅 완료] 실시간: {best_funding*100:.4f}% | "
                f"진입기준(95%): {MIN_FUNDING_RATE*100:.4f}% | 청산기준: {EXIT_FUNDING_RATE*100:.4f}%"
            )

    except Exception as e:
        logging.error(f"최적 코인 탐색 에러: {e}")

# ==========================================
# 6. 텔레그램 명령어 핸들러
# ==========================================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    spot_price, swap_price = await asyncio.to_thread(get_ticker_prices)
    funding_rate = (await asyncio.to_thread(get_funding_rate)) * 100
    pos = await asyncio.to_thread(get_positions)
    _, total_eq_usdt, coin_total, spot_val_krw = await asyncio.to_thread(get_balance)
    krw_rate = await asyncio.to_thread(get_usdt_krw_rate)

    pnl_usdt = total_eq_usdt - INITIAL_DEPOSIT_USDT
    pnl_pct = (pnl_usdt / INITIAL_DEPOSIT_USDT) * 100 if INITIAL_DEPOSIT_USDT > 0 else 0.0
    total_krw = total_eq_usdt * krw_rate
    pnl_krw = pnl_usdt * krw_rate

    switch_str = "🟢 ON (가동 중)" if BOT_SWITCH else "🔴 OFF (일시 정지)"

    msg = f"📊 [봇 현재 상태 보고 - {TARGET_COIN}]\n\n"
    msg += f"• 스위치 상태: {switch_str}\n"
    msg += f"• 설정 레버리지: {TARGET_LEVERAGE}배\n"
    msg += f"• 진입 기준 펀딩비: {MIN_FUNDING_RATE * 100:.4f}%\n"
    msg += f"• 청산 기준 펀딩비: {EXIT_FUNDING_RATE * 100:.4f}%\n"
    msg += f"• 입금 원금 자산: ${INITIAL_DEPOSIT_USDT:.2f} USDT (약 {INITIAL_DEPOSIT_USDT * krw_rate:,.0f}원)\n"
    msg += f"• 현재 통합 총자산: ${total_eq_usdt:.2f} USDT (약 {total_krw:,.0f}원)\n"
    msg += f"• 실시간 누적 손익: ${pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}% / {pnl_krw:+,.0f}원)\n"
    msg += f"• 보유 현물({TARGET_COIN}): {coin_total:.3f} 개 (약 {spot_val_krw:,.0f}원)\n"
    msg += f"• 현물/선물 가격: ${spot_price:.4f} / ${swap_price:.4f}\n"
    msg += f"• 현재 펀딩비: {funding_rate:.4f}%\n\n"

    if pos:
        contracts = float(pos.get('contracts', 0))
        entry_price = float(pos.get('entryPrice', 0.0))
        liq_price = float(pos.get('liquidationPrice', 0.0)) if pos.get('liquidationPrice') else 0.0
        liq_distance = abs((swap_price - liq_price) / swap_price) * 100 if swap_price > 0 and liq_price > 0 else 0.0

        msg += f"📦 [포지션 정보 (숏)]\n"
        msg += f"• 수량: {contracts} Cont ({contracts * COIN_SPEC['ctVal']:.2f} {TARGET_COIN})\n"
        msg += f"• 진입가: ${entry_price:.4f}\n"
        msg += f"• 청산가: ${liq_price:.4f} (안전거리: {liq_distance:.2f}%)\n"
    else:
        msg += f"📦 현재 보유 중인 포지션이 없습니다 (관망 중)."

    await update.message.reply_text(msg)

async def setlev_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    global TARGET_LEVERAGE
    if not context.args:
        await update.message.reply_text(f"ℹ️ **현재 레버리지**: {TARGET_LEVERAGE}배\n⚠️ **사용법**: `/setlev 3`")
        return
    try:
        new_lev = int(context.args[0])
        if new_lev < 1 or new_lev > 10:
            await update.message.reply_text("❌ 레버리지는 1배~10배만 설정 가능합니다.")
            return
        await asyncio.to_thread(setup_exchange_account_mode, SYMBOL_SWAP, new_lev)
        TARGET_LEVERAGE = new_lev
        await update.message.reply_text(f"✅ **선물 레버리지가 {TARGET_LEVERAGE}배로 설정되었습니다.**")
    except Exception as e:
        await update.message.reply_text(f"❌ 설정 실패: {e}")

async def setfund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    global MIN_FUNDING_RATE, EXIT_FUNDING_RATE
    if len(context.args) < 1:
        await update.message.reply_text("⚠️ **사용법**: `/setfund 0.025` 또는 `/setfund 0.025 0.005`")
        return
    try:
        new_min = float(context.args[0]) / 100.0
        new_exit = float(context.args[1]) / 100.0 if len(context.args) >= 2 else EXIT_FUNDING_RATE

        if new_min <= ABSOLUTE_MIN_FUNDING:
            await update.message.reply_text("⚠️ 경고: 진입 펀딩비가 0.01% 이하일 경우 강제 하한 0.01%가 적용됩니다.")
            new_min = ABSOLUTE_MIN_FUNDING

        MIN_FUNDING_RATE = new_min
        EXIT_FUNDING_RATE = new_exit
        await update.message.reply_text(
            f"🎯 **펀딩비 변경 완료**\n진입: {MIN_FUNDING_RATE * 100:.4f}%\n청산: {EXIT_FUNDING_RATE * 100:.4f}%"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 설정 실패: {e}")

async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    usdt_free, total_eq_usdt, coin_total, spot_val_krw = await asyncio.to_thread(get_balance)
    krw_rate = await asyncio.to_thread(get_usdt_krw_rate)
    pnl_usdt = total_eq_usdt - INITIAL_DEPOSIT_USDT
    pnl_pct = (pnl_usdt / INITIAL_DEPOSIT_USDT) * 100 if INITIAL_DEPOSIT_USDT > 0 else 0.0
    
    msg = f"💰 [수익 현황]\n"
    msg += f"• 원금: ${INITIAL_DEPOSIT_USDT:.2f} USDT\n"
    msg += f"• 총자산: ${total_eq_usdt:.2f} USDT (약 {total_eq_usdt * krw_rate:,.0f}원)\n"
    msg += f"• 손익: ${pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)\n"
    msg += f"• 가용 잔고: ${usdt_free:.2f} USDT"
    await update.message.reply_text(msg)

async def setcoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    if not context.args:
        await update.message.reply_text("⚠️ 사용법: `/setcoin SUI`")
        return
    new_coin = context.args[0].upper()
    pos = await asyncio.to_thread(get_positions)
    if pos:
        await update.message.reply_text("❌ 포지션 열림 상태에서는 변경 불가합니다.")
        return
    try:
        await asyncio.to_thread(update_coin_spec, new_coin)
        await update.message.reply_text(f"✅ 코인이 **{TARGET_COIN}**으로 수동 변경되었습니다.")
    except Exception as e:
        await update.message.reply_text(f"❌ 코인 변경 실패: {e}")

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    await update.message.reply_text("⏳ 수동 청산 시작...")
    success = await execute_delta_neutral_exit_async("관리자 수동 요청 (/close)")
    await update.message.reply_text("✅ 수동 청산 완료!" if success else "❌ 청산 실패")

async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    global BOT_SWITCH
    BOT_SWITCH = not BOT_SWITCH
    await update.message.reply_text(f"🔄 스위치: {'🟢 ON' if BOT_SWITCH else '🔴 OFF'}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    global BOT_SWITCH
    BOT_SWITCH = True
    await update.message.reply_text(f"▶️ **봇 매매 재개** (타겟: {TARGET_COIN})")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    await update.message.reply_text("🔄 재시작 중...")
    os.execv(sys.executable, ['python3'] + sys.argv)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    global BOT_SWITCH
    BOT_SWITCH = False
    await update.message.reply_text("⏸️ **봇 매매 일시 정지**")

async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    await update.message.reply_text("💀 **프로세스 종료**")
    os._exit(0)

# ==========================================
# 7. 🔥 초고속 주문 및 청산 집행 (나노초 벤치마킹 적용)
# ==========================================
async def execute_delta_neutral_entry_async():
    """가용 잔고 95% 기반 나노초 측정 초고속 진입"""
    global POSITION_BASE_USDT
    
    usdt_free, total_eq, _ = await get_balance_async()
    available_usdt = usdt_free * 0.95

    if available_usdt < 10.0:
        logging.warning("⚠️ 가용 USDT 잔고 부족 (최소 10 USDT 필요)")
        return False

    spot_price, swap_price = await get_ticker_prices_async()
    if spot_price <= 0 or swap_price <= 0:
        return False

    cost_per_contract = (swap_price * COIN_SPEC['ctVal']) / TARGET_LEVERAGE
    max_contracts = math.floor(available_usdt / cost_per_contract)
    swap_contracts = truncate_value(float(max_contracts), COIN_SPEC['swap_amount_prec'])
    spot_amount = truncate_value(swap_contracts * COIN_SPEC['ctVal'], COIN_SPEC['spot_amount_prec'])

    if spot_amount < COIN_SPEC['spot_min_amount'] or swap_contracts < COIN_SPEC['swap_min_amount']:
        logging.warning(f"⚠️ 최소 주문 수량 미달 (현물: {spot_amount}, 선물: {swap_contracts})")
        return False

    try:
        t_start_ns = time.perf_counter_ns() # 나노초 타임스탬프
        logging.info(f"🚀 [초고속 진입 시작] {TARGET_COIN} 선물 숏: {swap_contracts} Cont | 현물 매수: {spot_amount}")

        # 병렬 비동기 주문 발송 (속도 최우선)
        swap_order_task = async_okx.create_order(
            symbol=SYMBOL_SWAP, type='market', side='sell', amount=swap_contracts, params={'tdMode': 'cross'}
        )
        spot_order_task = async_okx.create_order(
            symbol=SYMBOL_SPOT, type='market', side='buy', amount=spot_amount, params={'tdMode': 'cross', 'tgtCcy': 'base_ccy'}
        )

        results = await asyncio.gather(swap_order_task, spot_order_task, return_exceptions=True)
        t_end_ns = time.perf_counter_ns()
        
        elapsed_ms = (t_end_ns - t_start_ns) / 1_000_000.0
        logging.info(f"⚡ 주문 처리 완료 시간: {elapsed_ms:.3f} ms (나노초: {t_end_ns - t_start_ns} ns)")

        if isinstance(results[0], Exception) or isinstance(results[1], Exception):
            logging.error(f"❌ 동시 주문 체결 중 일부 에러 발생: {results}")
            await execute_delta_neutral_exit_async("비상 롤백 청산")
            return False

        POSITION_BASE_USDT = total_eq
        return True

    except Exception as e:
        logging.error(f"❌ 주문 집행 에러: {e}")
        return False

async def execute_delta_neutral_exit_async(reason: str = "펀딩비 청산") -> bool:
    """델타 뉴트럴 포지션 비동기 초고속 청산"""
    global POSITION_BASE_USDT
    try:
        t_start_ns = time.perf_counter_ns()
        await asyncio.to_thread(cancel_all_open_orders)
        
        pos = await get_positions_async()
        _, _, coin_total = await get_balance_async()

        if not pos and coin_total <= 0:
            return True

        logging.info(f"🚨 [포지션 청산] 사유: {reason}")

        tasks = []
        if pos:
            contracts = float(pos.get('contracts', 0))
            if contracts > 0:
                tasks.append(async_okx.create_order(
                    symbol=SYMBOL_SWAP, type='market', side='buy', amount=contracts, params={'reduceOnly': True, 'tdMode': 'cross'}
                ))

        spot_amount_to_sell = truncate_value(coin_total, COIN_SPEC['spot_amount_prec'])
        if spot_amount_to_sell >= COIN_SPEC['spot_min_amount']:
            tasks.append(async_okx.create_order(
                symbol=SYMBOL_SPOT, type='market', side='sell', amount=spot_amount_to_sell, params={'tdMode': 'cross', 'tgtCcy': 'base_ccy'}
            ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        t_elapsed_ms = (time.perf_counter_ns() - t_start_ns) / 1_000_000.0
        POSITION_BASE_USDT = 0.0
        logging.info(f"🎉 초고속 전량 청산 완료! (소요시간: {t_elapsed_ms:.3f} ms)")
        return True

    except Exception as e:
        logging.error(f"❌ 청산 에러: {e}")
        return False

# ==========================================
# 8. 🔥 매매 주기 비동기 실행 루프
# ==========================================
async def trade_logic_cycle_async():
    """매 2초마다 초고속 매매 조건 체크"""
    if not BOT_SWITCH:
        return

    spot_price, swap_price = await get_ticker_prices_async()
    funding_rate = await get_funding_rate_async()
    pos = await get_positions_async()

    logging.info(
        f"[{TARGET_COIN} 감시 중] 레버리지: {TARGET_LEVERAGE}x | 현물: ${spot_price:.4f} | 선물: ${swap_price:.4f} | "
        f"현재 펀딩비: {funding_rate*100:.4f}% (목표: {MIN_FUNDING_RATE*100:.4f}%) | "
        f"포지션: {'보유' if pos else '미보유'}"
    )

    # 🚨 [조건] 포지션 미보유 + 펀딩비가 목표(95%) 이상 + 절댓값 0.01% 초과 시 진입
    if not pos and funding_rate >= MIN_FUNDING_RATE and funding_rate > ABSOLUTE_MIN_FUNDING:
        logging.info(f"🚀 {TARGET_COIN} 진입 조건 충족! (현재 펀딩비: {funding_rate*100:.4f}%)")
        await execute_delta_neutral_entry_async()

    elif pos and funding_rate <= EXIT_FUNDING_RATE:
        logging.info(f"📉 {TARGET_COIN} 청산 조건 충족! (현재: {funding_rate*100:.4f}% <= 목표: {EXIT_FUNDING_RATE*100:.4f}%)")
        await execute_delta_neutral_exit_async("펀딩비 하락 청산")

# ==========================================
# 9. 30분 정기 알림 & 비동기 백그라운드 루프
# ==========================================
async def periodic_log_reporter(app: Application):
    """30분 정기 상태 보고"""
    while True:
        try:
            await asyncio.sleep(1800)
            spot_price, swap_price = await get_ticker_prices_async()
            funding_rate = (await get_funding_rate_async()) * 100
            _, total_eq_usdt, coin_total = await get_balance_async()
            pos = await get_positions_async()
            krw_rate = await asyncio.to_thread(get_usdt_krw_rate)

            pnl_usdt = total_eq_usdt - INITIAL_DEPOSIT_USDT
            pnl_pct = (pnl_usdt / INITIAL_DEPOSIT_USDT) * 100 if INITIAL_DEPOSIT_USDT > 0 else 0.0

            log_msg = f"⏰ [30분 정기 상태 알림 - {TARGET_COIN}]\n\n"
            log_msg += f"• 현재 통합 총자산: ${total_eq_usdt:.2f} USDT (약 {total_eq_usdt * krw_rate:,.0f}원)\n"
            log_msg += f"• 누적 손익: ${pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)\n"
            log_msg += f"• 보유 현물: {coin_total:.3f} {TARGET_COIN}\n"
            log_msg += f"• 현재가: 현물 ${spot_price:.4f} / 선물 ${swap_price:.4f}\n"
            log_msg += f"• 실시간 펀딩비: {funding_rate:.4f}% (진입기준: {MIN_FUNDING_RATE*100:.4f}%)\n"
            log_msg += f"• 포지션 상태: {'보유 중' if pos else '관망 중'}\n"

            await send_telegram_msg_async(app, log_msg)
        except Exception as e:
            logging.error(f"정기 리포트 에러: {e}")

async def auto_scanner_task():
    """포지션 미보유 시 30초마다 거래량 & 펀딩비 우량 코인 자동 탐색"""
    while True:
        try:
            if BOT_SWITCH:
                await find_best_funding_coin(min_volume_usdt=10000000.0)
        except Exception as e:
            logging.error(f"자동 스캐너 에러: {e}")
        await asyncio.sleep(30)

# ==========================================
# 10. 비동기 메인 이벤트 루프
# ==========================================
async def main():
    try:
        await asyncio.to_thread(update_coin_spec, TARGET_COIN)
        logging.info(f"초기 코인 스펙 설정 완료: {TARGET_COIN} ({TARGET_LEVERAGE}배)")
    except Exception as e:
        logging.error(f"초기 설정 에러: {e}")

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("profit", profit_command))
    application.add_handler(CommandHandler("setfund", setfund_command))
    application.add_handler(CommandHandler("setcoin", setcoin_command))
    application.add_handler(CommandHandler("setlev", setlev_command))
    application.add_handler(CommandHandler("close", close_command))
    application.add_handler(CommandHandler("switch", switch_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("kill", kill_command))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logging.info(f"🤖 초고속 차익거래 봇 가동 시작 (원금: ${INITIAL_DEPOSIT_USDT} USDT)")
    
    krw_rate = await asyncio.to_thread(get_usdt_krw_rate)
    await send_telegram_msg_async(
        application, 
        f"🤖 **자동 종목 탐색 & 초고속 차익거래 봇 가동!**\n\n"
        f"• 기본 타겟: {TARGET_COIN}\n"
        f"• 설정 레버리지: {TARGET_LEVERAGE}배\n"
        f"• 최소 진입 펀딩비 안전선: 0.0100%\n"
        f"• 자동 스위칭: 거래량 $1,000만 이상 상위 펀딩비 코인 추적"
    )

    # 백그라운드 비동기 루프 생성
    asyncio.create_task(periodic_log_reporter(application))
    asyncio.create_task(auto_scanner_task())

    try:
        while True:
            try:
                await trade_logic_cycle_async()
            except Exception as e:
                logging.error(f"매매 루프 오류: {e}")

            # 초고속 2초 주기 체크
            await asyncio.sleep(2)
            
    except (KeyboardInterrupt, SystemExit):
        logging.info("봇 종료 요청을 받았습니다.")
    finally:
        await async_okx.close()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass