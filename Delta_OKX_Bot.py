import os
import sys
import time
import math
import logging
import asyncio
from datetime import datetime, timezone
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

# 🎯 펀딩비 설정 (진입 0.025%, 청산 0.005%)
MIN_FUNDING_RATE = 0.00025  # 0.025%
EXIT_FUNDING_RATE = 0.00005 # 0.005% (수수료 및 손익 방어 안정선)

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

# OKX CCXT 객체 생성
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

okx = get_okx_client()

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

    # 코인이 바뀌어도 설정된 레버리지 고정 반영
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

def get_ticker_prices():
    """현물 및 선물 현재가 조회"""
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
def get_positions():
    """현재 선물 포지션 조회"""
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
    """
    (현물 보유 수량 전체 가치 + 가용 USDT + 선물 미실현 손익) 정밀 합산
    Returns: (usdt_free, pure_bot_equity, coin_total, spot_val_krw)
    """
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

def get_funding_rate():
    """현재 펀딩비 조회"""
    try:
        funding_info = okx.fetch_funding_rate(SYMBOL_SWAP)
        return float(funding_info.get('fundingRate', 0.0))
    except Exception as e:
        logging.error(f"펀딩비 조회 에러: {e}")
        return 0.0

# ==========================================
# 5. 텔레그램 명령어 핸들러
# ==========================================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status 명령어"""
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
    msg += f"• 진입 기준 펀딩비: {MIN_FUNDING_RATE * 100:.3f}%\n"
    msg += f"• 청산 기준 펀딩비: {EXIT_FUNDING_RATE * 100:.3f}%\n"
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
    """/setlev [배수] 명령어 (예: /setlev 3)"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    global TARGET_LEVERAGE

    if not context.args:
        await update.message.reply_text(
            f"ℹ️ **현재 레버리지**: {TARGET_LEVERAGE}배\n"
            "⚠️ **사용법**: `/setlev 3` (숫자로 변경할 배수 입력)"
        )
        return

    try:
        new_lev = int(context.args[0])
        if new_lev < 1 or new_lev > 10:
            await update.message.reply_text("❌ 레버리지는 1배 이상 10배 이하로만 설정 가능합니다.")
            return

        pos = await asyncio.to_thread(get_positions)
        if pos:
            await update.message.reply_text("⚠️ 주의: 현재 포지션이 열려 있는 상태에서 레버리지를 변경합니다.")

        # OKX 거래소 레버리지 변경 적용
        await asyncio.to_thread(setup_exchange_account_mode, SYMBOL_SWAP, new_lev)
        TARGET_LEVERAGE = new_lev

        await update.message.reply_text(f"✅ **선물 레버리지가 {TARGET_LEVERAGE}배로 설정되었습니다.**")
        logging.info(f"텔레그램 명령어 레버리지 변경: {TARGET_LEVERAGE}배")

    except ValueError:
        await update.message.reply_text("❌ 수치를 정수로 입력해주세요. (예: `/setlev 3`)")
    except Exception as e:
        await update.message.reply_text(f"❌ 레버리지 변경 실패: {e}")

async def setfund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setfund 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    global MIN_FUNDING_RATE, EXIT_FUNDING_RATE

    if len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ **펀딩비 변경 사용법**:\n"
            "• `/setfund 0.025` (진입 펀딩비를 0.025%로 변경)\n"
            "• `/setfund 0.025 0.005` (진입 0.025%, 청산 0.005%로 변경)"
        )
        return

    try:
        new_min = float(context.args[0]) / 100.0
        new_exit = float(context.args[1]) / 100.0 if len(context.args) >= 2 else EXIT_FUNDING_RATE

        MIN_FUNDING_RATE = new_min
        EXIT_FUNDING_RATE = new_exit

        msg = f"🎯 **펀딩비 기준이 성공적으로 변경되었습니다!**\n\n"
        msg += f"• 진입 기준 펀딩비: {MIN_FUNDING_RATE * 100:.3f}%\n"
        msg += f"• 청산 기준 펀딩비: {EXIT_FUNDING_RATE * 100:.3f}%"
        await update.message.reply_text(msg)
        logging.info(f"펀딩비 변경: 진입={MIN_FUNDING_RATE*100:.3f}%, 청산={EXIT_FUNDING_RATE*100:.3f}%")

    except Exception as e:
        await update.message.reply_text(f"❌ 설정 실패: 수치를 확인해주세요 (예: `/setfund 0.025`)\n오류: {e}")

async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profit 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    usdt_free, total_eq_usdt, coin_total, spot_val_krw = await asyncio.to_thread(get_balance)
    krw_rate = await asyncio.to_thread(get_usdt_krw_rate)

    pnl_usdt = total_eq_usdt - INITIAL_DEPOSIT_USDT
    pnl_pct = (pnl_usdt / INITIAL_DEPOSIT_USDT) * 100 if INITIAL_DEPOSIT_USDT > 0 else 0.0
    
    deposit_krw = INITIAL_DEPOSIT_USDT * krw_rate
    total_krw = total_eq_usdt * krw_rate
    pnl_krw = pnl_usdt * krw_rate

    msg = f"💰 [수익 및 자산 현황 보고]\n\n"
    msg += f"• 입금 원금 자산: ${INITIAL_DEPOSIT_USDT:.2f} USDT (약 {deposit_krw:,.0f}원)\n"
    msg += f"• 현재 통합 총자산: ${total_eq_usdt:.2f} USDT (약 {total_krw:,.0f}원)\n"
    msg += f"• 실시간 누적 손익: ${pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}% / {pnl_krw:+,.0f}원)\n"
    msg += f"• 현물({TARGET_COIN}) 평가금: {coin_total:.3f} {TARGET_COIN} (약 {spot_val_krw:,.0f}원)\n"
    msg += f"• 가용 가능 잔고: ${usdt_free:.2f} USDT\n"
    msg += f"• 적용 환율: 1 USDT = {krw_rate:,.1f}원"

    await update.message.reply_text(msg)

async def setcoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setcoin [심볼] 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    if not context.args:
        await update.message.reply_text("⚠️ 사용법: `/setcoin DOGE` 형태로 입력하세요.")
        return

    new_coin = context.args[0].upper()

    pos = await asyncio.to_thread(get_positions)
    if pos:
        await update.message.reply_text(f"❌ 실패: 현재 {TARGET_COIN} 포지션이 열려있습니다. 먼저 /close 명령어로 청산하세요.")
        return

    has_orders = await asyncio.to_thread(has_open_orders)
    if has_orders:
        await update.message.reply_text("❌ 실패: 미체결 주문이 남아있습니다.")
        return

    await update.message.reply_text(f"⏳ OKX에서 {new_coin} 마켓 스펙 설정 중...")

    try:
        await asyncio.to_thread(update_coin_spec, new_coin)
        spot_price, swap_price = await asyncio.to_thread(get_ticker_prices)

        reply_msg = f"✅ **매매 대상 코인이 {TARGET_COIN}으로 변경되었습니다!**\n\n"
        reply_msg += f"• 현물: `{SYMBOL_SPOT}` | 선물: `{SYMBOL_SWAP}`\n"
        reply_msg += f"• 설정 레버리지: {TARGET_LEVERAGE}배\n"
        reply_msg += f"• 현재가: 현물 ${spot_price:.4f} / 선물 ${swap_price:.4f}"

        await update.message.reply_text(reply_msg)
    except Exception as e:
        await update.message.reply_text(f"⚠️ 코인 변경 실패: {str(e)}")

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/close 수동 청산 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    pos = await asyncio.to_thread(get_positions)
    _, _, coin_total, _ = await asyncio.to_thread(get_balance)
    if not pos and coin_total <= 0:
        await update.message.reply_text("ℹ️ 현재 청산할 포지션이 없습니다.")
        return

    await update.message.reply_text("⏳ 수동 청산을 시작합니다...")
    success = await asyncio.to_thread(execute_delta_neutral_exit, "관리자 수동 요청 (/close)")
    
    if success:
        await update.message.reply_text("✅ 수동 청산이 완료되었습니다.")
    else:
        await update.message.reply_text("❌ 청산 도중 오류가 발생했습니다.")

async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/switch 스위치 변경 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    global BOT_SWITCH
    BOT_SWITCH = not BOT_SWITCH
    status_str = "🟢 ON (가동)" if BOT_SWITCH else "🔴 OFF (일시 정지)"
    await update.message.reply_text(f"🔄 봇 매매 스위치가 {status_str} 상태로 변경되었습니다.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 명령어 (일시정지 후 재개)"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    global BOT_SWITCH
    BOT_SWITCH = True
    await update.message.reply_text(f"▶️ **봇 동작을 시작/재개합니다.** (현재 타겟: {TARGET_COIN})")
    logging.info(f"텔레그램 명령어 봇 재개 (/start) - 대상: {TARGET_COIN}")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart 명령"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    await update.message.reply_text("🔄 봇 프로세스를 재시작합니다...")
    os.execv(sys.executable, ['python3'] + sys.argv)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop 명령 (프로세스 종료 대신 일시 정지)"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    global BOT_SWITCH
    BOT_SWITCH = False
    await update.message.reply_text("⏸️ **봇 매매를 일시 정지합니다.**\n(텔레그램 명령어 수신은 계속 유지되며, `/start`로 재개할 수 있습니다.)")
    logging.info("텔레그램 명령어 봇 일시 정지 (/stop)")

async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kill 명령 (프로세스 완전 종료)"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return
    await update.message.reply_text("💀 **봇 프로세스를 완전히 종료합니다.**")
    logging.info("텔레그램 명령어 봇 완전 종료 (/kill)")
    os._exit(0)

# ==========================================
# 6. 정밀 주문 및 청산 집행 함수
# ==========================================
def execute_delta_neutral_entry():
    """가용 잔고 95% 기반 정밀 진입"""
    global POSITION_BASE_USDT
    
    usdt_free, total_eq, _, _ = get_balance()
    available_usdt = usdt_free * 0.95

    if available_usdt < 10.0:
        logging.warning("⚠️ 주문 가능한 USDT 잔고가 부족합니다 (최소 10 USDT 필요).")
        return False

    spot_price, swap_price = get_ticker_prices()
    if spot_price <= 0 or swap_price <= 0:
        return False

    # 레버리지를 고려한 계약당 실제 필요 증거금 계산
    cost_per_contract = (swap_price * COIN_SPEC['ctVal']) / TARGET_LEVERAGE
    max_contracts = math.floor(available_usdt / cost_per_contract)
    swap_contracts = truncate_value(float(max_contracts), COIN_SPEC['swap_amount_prec'])
    spot_amount = truncate_value(swap_contracts * COIN_SPEC['ctVal'], COIN_SPEC['spot_amount_prec'])

    if spot_amount < COIN_SPEC['spot_min_amount'] or swap_contracts < COIN_SPEC['swap_min_amount']:
        logging.warning(f"⚠️ 최소 주문 수량 미달 (현물: {spot_amount}, 선물: {swap_contracts})")
        return False

    try:
        logging.info(f"🚀 [주문 시도] 선물 숏({TARGET_LEVERAGE}x): {swap_contracts} 계약 | 현물 매수: {spot_amount} {TARGET_COIN}")

        okx.create_order(
            symbol=SYMBOL_SWAP,
            type='market',
            side='sell',
            amount=swap_contracts,
            params={'tdMode': 'cross'}
        )
        logging.info(f"✅ 선물 숏 체결 성공: {swap_contracts} 계약")

        try:
            okx.create_order(
                symbol=SYMBOL_SPOT,
                type='market',
                side='buy',
                amount=spot_amount,
                params={'tdMode': 'cross', 'tgtCcy': 'base_ccy'}
            )
            logging.info(f"✅ 현물 매수 체결 성공: {spot_amount} {TARGET_COIN}")
            POSITION_BASE_USDT = total_eq
            return True

        except Exception as spot_err:
            logging.error(f"❌ 현물 매수 실패! 선물 숏 포지션 롤백 시도: {spot_err}")
            okx.create_order(
                symbol=SYMBOL_SWAP,
                type='market',
                side='buy',
                amount=swap_contracts,
                params={'reduceOnly': True, 'tdMode': 'cross'}
            )
            time.sleep(30)
            return False

    except Exception as e:
        logging.error(f"❌ 주문 집행 중 에러 발생: {e}")
        return False

def execute_delta_neutral_exit(reason: str = "펀딩비 청산 조건 도달") -> bool:
    """델타 뉴트럴 포지션 전량 청산"""
    global POSITION_BASE_USDT
    try:
        cancel_all_open_orders()
        
        pos = get_positions()
        _, _, coin_total, _ = get_balance()

        if not pos and coin_total <= 0:
            return True

        logging.info(f"🚨 [포지션 청산 시작] 사유: {reason}")

        if pos:
            contracts = float(pos.get('contracts', 0))
            if contracts > 0:
                okx.create_order(
                    symbol=SYMBOL_SWAP,
                    type='market',
                    side='buy',
                    amount=contracts,
                    params={'reduceOnly': True, 'tdMode': 'cross'}
                )

        spot_amount_to_sell = truncate_value(coin_total, COIN_SPEC['spot_amount_prec'])
        if spot_amount_to_sell >= COIN_SPEC['spot_min_amount']:
            okx.create_order(
                symbol=SYMBOL_SPOT,
                type='market',
                side='sell',
                amount=spot_amount_to_sell,
                params={'tdMode': 'cross', 'tgtCcy': 'base_ccy'}
            )

        POSITION_BASE_USDT = 0.0
        logging.info("🎉 델타 뉴트럴 전량 청산 완료!")
        return True

    except Exception as e:
        logging.error(f"❌ 청산 집행 중 에러 발생: {e}")
        return False

# ==========================================
# 7. 핵심 매매 로직 주기 실행 함수
# ==========================================
def trade_logic_cycle():
    """매 10초마다 매매 조건 체크"""
    if not BOT_SWITCH:
        return

    spot_price, swap_price = get_ticker_prices()
    funding_rate = get_funding_rate()
    pos = get_positions()

    logging.info(
        f"[{TARGET_COIN} 감시 중] 레버리지: {TARGET_LEVERAGE}x | 현물: ${spot_price:.4f} | 선물: ${swap_price:.4f} | "
        f"현재 펀딩비: {funding_rate*100:.4f}% (목표: {MIN_FUNDING_RATE*100:.3f}%) | "
        f"포지션: {'보유' if pos else '미보유'}"
    )

    if not pos and funding_rate >= MIN_FUNDING_RATE:
        logging.info(f"🚀 {TARGET_COIN} 진입 조건 충족! (펀딩비: {funding_rate*100:.4f}%)")
        execute_delta_neutral_entry()

    elif pos and funding_rate <= EXIT_FUNDING_RATE:
        logging.info(f"📉 {TARGET_COIN} 청산 조건 충족! (현재 펀딩비: {funding_rate*100:.4f}% <= 목표: {EXIT_FUNDING_RATE*100:.3f}%)")
        execute_delta_neutral_exit("펀딩비 하락 청산")

# ==========================================
# 8. 비동기 30분 정기 로그 알림 (레버리지 정보 포함)
# ==========================================
async def periodic_log_reporter(app: Application):
    """30분마다 전체 자산/원화환율/가치 반영 텔레그램 리포트"""
    while True:
        try:
            await asyncio.sleep(1800)
            
            spot_price, swap_price = await asyncio.to_thread(get_ticker_prices)
            funding_rate = (await asyncio.to_thread(get_funding_rate)) * 100
            _, total_eq_usdt, coin_total, spot_val_krw = await asyncio.to_thread(get_balance)
            pos = await asyncio.to_thread(get_positions)
            krw_rate = await asyncio.to_thread(get_usdt_krw_rate)

            pnl_usdt = total_eq_usdt - INITIAL_DEPOSIT_USDT
            pnl_pct = (pnl_usdt / INITIAL_DEPOSIT_USDT) * 100 if INITIAL_DEPOSIT_USDT > 0 else 0.0
            
            deposit_krw = INITIAL_DEPOSIT_USDT * krw_rate
            total_krw = total_eq_usdt * krw_rate
            pnl_krw = pnl_usdt * krw_rate

            log_msg = f"⏰ [30분 정기 상태 알림 - {TARGET_COIN}]\n\n"
            log_msg += f"• 입금 원금 자산: ${INITIAL_DEPOSIT_USDT:.2f} USDT (약 {deposit_krw:,.0f}원)\n"
            log_msg += f"• 현재 통합 총자산: ${total_eq_usdt:.2f} USDT (약 {total_krw:,.0f}원)\n"
            log_msg += f"• 실시간 누적 손익: ${pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}% / {pnl_krw:+,.0f}원)\n"
            log_msg += f"• 보유 현물({TARGET_COIN}): {coin_total:.3f} 개 (약 {spot_val_krw:,.0f}원)\n"
            log_msg += f"• 현물/선물 가격: ${spot_price:.4f} / ${swap_price:.4f}\n"
            log_msg += f"• 설정 레버리지: {TARGET_LEVERAGE}배\n"
            log_msg += f"• 현재 펀딩비: {funding_rate:.4f}% (진입기준: {MIN_FUNDING_RATE*100:.3f}%)\n"
            log_msg += f"• 포지션 상태: {'보유 중 (숏)' if pos else '미보유 (관망 중)'}\n"
            log_msg += f"• 스위치 상태: {'🟢 ON' if BOT_SWITCH else '🔴 OFF'}"

            await send_telegram_msg_async(app, log_msg)
            logging.info("📢 30분 정기 텔레그램 로그 전송 완료")
        except Exception as e:
            logging.error(f"30분 정기 로그 전송 에러: {e}")

# ==========================================
# 9. 비동기 메인 이벤트 루프
# ==========================================
async def main():
    try:
        await asyncio.to_thread(update_coin_spec, TARGET_COIN)
        logging.info(f"기본 코인 스펙 설정 완료: {TARGET_COIN} (레버리지 {TARGET_LEVERAGE}배)")
    except Exception as e:
        logging.error(f"초기 스펙 설정 에러: {e}")

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

    logging.info(f"🤖 델타 뉴트럴 자동 매매 봇 시작 (원금: ${INITIAL_DEPOSIT_USDT} USDT)")
    
    krw_rate = await asyncio.to_thread(get_usdt_krw_rate)
    await send_telegram_msg_async(
        application, 
        f"🤖 **델타 뉴트럴 자동 매매 봇이 업데이트 되었습니다.**\n\n"
        f"• 기본 타겟: {TARGET_COIN}\n"
        f"• 설정 레버리지: {TARGET_LEVERAGE}배\n"
        f"• 입금 원금 설정: ${INITIAL_DEPOSIT_USDT:.2f} USDT (약 {INITIAL_DEPOSIT_USDT * krw_rate:,.0f}원)\n"
        f"• 진입 펀딩비: {MIN_FUNDING_RATE*100:.3f}%\n"
        f"• 청산 펀딩비: {EXIT_FUNDING_RATE*100:.3f}%"
    )

    asyncio.create_task(periodic_log_reporter(application))

    try:
        while True:
            try:
                await asyncio.to_thread(trade_logic_cycle)
            except Exception as e:
                logging.error(f"매매 루프 오류: {e}")

            await asyncio.sleep(10)
            
    except (KeyboardInterrupt, SystemExit):
        logging.info("봇 종료 요청을 받았습니다.")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass