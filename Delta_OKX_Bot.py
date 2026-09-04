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

# ==========================================
# 1. 환경 변수 및 기본 설정 (.env 경로 자동 감지)
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

# 🎯 펀딩비 진입 기준 설정 (0.010% = 0.0001)
MIN_FUNDING_RATE = 0.0001 

# 🎯 펀딩비 청산 기준 설정 (0.000% 이하로 떨어지면 청산)
EXIT_FUNDING_RATE = 0.0000 

# 💰 1회 진입 시 사용할 USDT 금액
ENTRY_USDT_AMOUNT = 150.0 

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

# OKX CCXT 객체 생성 (tdMode 자동 주입 방지를 위해 defaultType을 spot으로 지정)
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
BOT_SWITCH = True  # 봇 가동 스위치 (ON: True, OFF: False)
TARGET_COIN = "XRP"
SYMBOL_SPOT = "XRP/USDT"
SYMBOL_SWAP = "XRP/USDT:USDT"

COIN_SPEC = {
    'ctVal': 10.0,              # 선물 1장당 코인 개수
    'spot_amount_prec': 2,     # 현물 수량 소수점 자릿수
    'swap_amount_prec': 0,     # 선물 수량 소수점 자릿수
    'spot_min_amount': 1.0,    # 현물 최소 주문 수량
    'swap_min_amount': 1.0,    # 선물 최소 주문 수량
}

POSITION_BASE_USDT = 0.0  # 포지션 진입 시점 원금

# ==========================================
# 2. 정밀도(Precision) 및 마진 모드 설정 함수
# ==========================================
def truncate_value(val: float, precision: int) -> float:
    """소수점 아래 지정한 자릿수에서 버림(Down-Rounding) 처리"""
    if precision == 0:
        return float(math.floor(val))
    factor = 10 ** precision
    return math.floor(val * factor) / factor

def setup_exchange_account_mode(swap_symbol: str):
    """OKX 레버리지(1x) 및 Cross(교차) 마진 모드 자동 설정 및 체크"""
    try:
        okx.set_leverage(1, swap_symbol, params={'mgnMode': 'cross'})
        logging.info(f"✅ {swap_symbol} 교차(Cross) 마진 1배 설정 완료")
    except Exception as e:
        logging.warning(f"⚠️ 마진 모드 설정 중 참고사항: {e}")

def update_coin_spec(coin_symbol: str):
    """OKX에서 해당 코인의 정밀도, 최소 주문 수량, 마진 모드 설정"""
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

    # 스펙 파싱
    COIN_SPEC['ctVal'] = float(swap_m['info'].get('ctVal', 1.0))
    COIN_SPEC['spot_amount_prec'] = int(spot_m['precision']['amount']) if spot_m['precision']['amount'] is not None else 2
    COIN_SPEC['swap_amount_prec'] = int(swap_m['precision']['amount']) if swap_m['precision']['amount'] is not None else 0
    COIN_SPEC['spot_min_amount'] = float(spot_m['limits']['amount']['min']) if spot_m['limits']['amount']['min'] else 0.0001
    COIN_SPEC['swap_min_amount'] = float(swap_m['limits']['amount']['min']) if swap_m['limits']['amount']['min'] else 1.0

    # 마진 모드 검증/설정
    setup_exchange_account_mode(SYMBOL_SWAP)

# ==========================================
# 3. 텔레그램 메세지 및 환율 정보 함수
# ==========================================
async def send_telegram_msg_async(app: Application, text: str):
    """비동기 텔레그램 알림 전송"""
    if TELEGRAM_TOKEN and TELEGRAM_ADMIN_ID:
        try:
            await app.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=text)
        except Exception as e:
            logging.error(f"텔레그램 메시지 전송 실패: {e}")

def get_usdt_krw_rate() -> float:
    """실시간 USDT/KRW 환율 조회"""
    try:
        ticker = ccxt.exchangerate().fetch_ticker('USD/KRW')
        return float(ticker['last'])
    except Exception:
        try:
            upbit = ccxt.upbit()
            ticker = upbit.fetch_ticker('USDT/KRW')
            return float(ticker['last'])
        except Exception as e:
            logging.error(f"환율 조회 실패 (기본 환율 1,350원 적용): {e}")
            return 1350.0

# ==========================================
# 4. 데이터 및 포지션 조회 함수
# ==========================================
def get_balance():
    """잔고 조회"""
    try:
        balance = okx.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0.0)
        usdt_total = balance['total'].get('USDT', 0.0)
        coin_free = balance['free'].get(TARGET_COIN, 0.0)
        return usdt_free, usdt_total, coin_free
    except Exception as e:
        logging.error(f"잔고 조회 에러: {e}")
        return 0.0, 0.0, 0.0

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

def has_open_orders():
    """미체결 주문 존재 여부 확인"""
    try:
        orders_spot = okx.fetch_open_orders(SYMBOL_SPOT)
        orders_swap = okx.fetch_open_orders(SYMBOL_SWAP)
        return len(orders_spot) > 0 or len(orders_swap) > 0
    except Exception as e:
        logging.error(f"미체결 주문 조회 에러: {e}")
        return True

def get_funding_rate():
    """현재 펀딩비 조회"""
    try:
        funding_info = okx.fetch_funding_rate(SYMBOL_SWAP)
        return float(funding_info.get('fundingRate', 0.0))
    except Exception as e:
        logging.error(f"펀딩비 조회 에러: {e}")
        return 0.0

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
# 5. 텔레그램 명령어 핸들러
# ==========================================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    spot_price, swap_price = get_ticker_prices()
    funding_rate = get_funding_rate() * 100
    pos = get_positions()
    _, usdt_total, _ = get_balance()
    krw_rate = get_usdt_krw_rate()

    switch_str = "🟢 ON (가동 중)" if BOT_SWITCH else "🔴 OFF (일시 정지)"
    krw_total = usdt_total * krw_rate

    msg = f"📊 [봇 현재 상태 보고 - {TARGET_COIN}]\n\n"
    msg += f"• 스위치 상태: {switch_str}\n"
    msg += f"• 진입 기준 펀딩비: {MIN_FUNDING_RATE * 100:.3f}%\n"
    msg += f"• 청산 기준 펀딩비: {EXIT_FUNDING_RATE * 100:.3f}%\n"
    msg += f"• 총 자산: ${usdt_total:.2f} USDT (약 {krw_total:,.0f}원)\n"
    msg += f"• 현물({TARGET_COIN}) 가격: ${spot_price:.4f}\n"
    msg += f"• 선물({TARGET_COIN}) 가격: ${swap_price:.4f}\n"
    msg += f"• 선물 1장 가치: {COIN_SPEC['ctVal']} {TARGET_COIN}\n"
    msg += f"• 현재 펀딩비: {funding_rate:.4f}%\n\n"

    if pos:
        contracts = float(pos.get('contracts', 0))
        entry_price = float(pos.get('entryPrice', 0.0))
        liq_price = float(pos.get('liquidationPrice', 0.0)) if pos.get('liquidationPrice') else 0.0
        
        liq_distance = 0.0
        if swap_price > 0 and liq_price > 0:
            liq_distance = abs((swap_price - liq_price) / swap_price) * 100

        msg += f"📦 [포지션 정보 (숏)]\n"
        msg += f"• 수량: {contracts} Cont ({contracts * COIN_SPEC['ctVal']:.2f} {TARGET_COIN})\n"
        msg += f"• 진입가: ${entry_price:.4f}\n"
        msg += f"• 청산가: ${liq_price:.4f} (안전거리: {liq_distance:.2f}%)\n"
    else:
        msg += f"📦 현재 보유 중인 포지션이 없습니다 (관망 중)."

    await update.message.reply_text(msg)

async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profit 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    usdt_free, usdt_total, _ = get_balance()
    krw_rate = get_usdt_krw_rate()
    global POSITION_BASE_USDT

    krw_total = usdt_total * krw_rate
    krw_free = usdt_free * krw_rate

    msg = f"💰 [수익 및 손익 현황]\n\n"
    msg += f"• 총 평가 자산: ${usdt_total:.2f} USDT (약 {krw_total:,.0f}원)\n"
    msg += f"• 가용 가능 자산: ${usdt_free:.2f} USDT (약 {krw_free:,.0f}원)\n"
    msg += f"• 적용 환율: 1 USDT = {krw_rate:,.1f}원\n\n"

    if POSITION_BASE_USDT > 0:
        pnl_usdt = usdt_total - POSITION_BASE_USDT
        pnl_krw = pnl_usdt * krw_rate
        pnl_pct = (pnl_usdt / POSITION_BASE_USDT) * 100
        base_krw = POSITION_BASE_USDT * krw_rate
        
        msg += f"• 진입 초기 자산: ${POSITION_BASE_USDT:.2f} USDT (약 {base_krw:,.0f}원)\n"
        msg += f"• 진입 대비 손익: ${pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)\n"
        msg += f"• 💵 실시간 손익 금액: {pnl_krw:+,.0f}원\n"
    else:
        msg += f"• 진입 대비 손익: 포지션 미보유 중 (기준 자산 정보 없음)\n"

    await update.message.reply_text(msg)

async def setcoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setcoin [심볼] 명령어 (예: /setcoin ETH)"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    if not context.args:
        await update.message.reply_text("⚠️ 사용법: `/setcoin ETH` 또는 `/setcoin BTC` 형태로 입력하세요.")
        return

    new_coin = context.args[0].upper()

    if get_positions():
        await update.message.reply_text(f"❌ 코인 변경 실패: 현재 {TARGET_COIN} 포지션이 열려있습니다. 먼저 청산하세요.")
        return

    if has_open_orders():
        await update.message.reply_text(f"❌ 코인 변경 실패: 미체결 주문이 남아있습니다. 주문을 모두 취소한 후 시도하세요.")
        return

    await update.message.reply_text(f"⏳ OKX에서 {new_coin} 마켓 스펙 및 마진 모드를 설정 중입니다...")

    try:
        update_coin_spec(new_coin)
        spot_price, swap_price = get_ticker_prices()

        reply_msg = f"✅ **매매 대상 코인이 {TARGET_COIN}으로 변경되었습니다!**\n\n"
        reply_msg += f"• 현물 심볼: `{SYMBOL_SPOT}`\n"
        reply_msg += f"• 선물 심볼: `{SYMBOL_SWAP}`\n"
        reply_msg += f"• 선물 1장 가치: {COIN_SPEC['ctVal']} {TARGET_COIN}\n"
        reply_msg += f"• 현물 수량 정밀도: 소수점 {COIN_SPEC['spot_amount_prec']}자리\n"
        reply_msg += f"• 선물 계약 정밀도: 소수점 {COIN_SPEC['swap_amount_prec']}자리\n"
        reply_msg += f"• 현재가: 현물 ${spot_price:.4f} / 선물 ${swap_price:.4f}"

        await update.message.reply_text(reply_msg)
        logging.info(f"대상 코인 전환 성공: {TARGET_COIN} (스펙: {COIN_SPEC})")

    except Exception as e:
        logging.error(f"코인 변경 도중 에러 발생: {e}")
        await update.message.reply_text(f"⚠️ 코인 변경 실패: {str(e)}")

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/close 명령어 (수동 포지션 전량 청산)"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    pos = get_positions()
    _, _, coin_free = get_balance()
    if not pos and coin_free <= 0:
        await update.message.reply_text("ℹ️ 현재 청산할 포지션이 없습니다.")
        return

    await update.message.reply_text("⏳ 수동 청산을 시작합니다...")
    success = execute_delta_neutral_exit("관리자 수동 요청 (/close)")
    
    if success:
        await update.message.reply_text("✅ 수동 청산이 완료되었습니다.")
    else:
        await update.message.reply_text("❌ 청산 도중 오류가 발생했습니다. 로그를 확인하세요.")

async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/switch 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    global BOT_SWITCH
    BOT_SWITCH = not BOT_SWITCH
    status_str = "🟢 ON (가동)" if BOT_SWITCH else "🔴 OFF (일시 정지)"
    await update.message.reply_text(f"🔄 봇 매매 스위치가 {status_str} 상태로 변경되었습니다.")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    await update.message.reply_text("🔄 봇 프로세스를 재시작합니다...")
    os.execv(sys.executable, ['python3'] + sys.argv)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    await update.message.reply_text("🛑 봇을 완전히 종료합니다.")
    os._exit(0)

# ==========================================
# 6. 정밀 주문 및 청산 집행 함수 (tdMode: cross 적용)
# ==========================================
def execute_delta_neutral_entry():
    """소수점 버림 보정을 적용한 델타 0(뉴트럴) 진입 주문 (선물 우선 체결 및 안전 롤백 적용)"""
    global POSITION_BASE_USDT
    
    usdt_free, usdt_total, _ = get_balance()
    target_usdt = min(ENTRY_USDT_AMOUNT, usdt_free * 0.95)

    if target_usdt < 10.0:
        logging.warning("⚠️ 주문 가능한 USDT 잔고가 부족합니다 (최소 10 USDT 필요).")
        return False

    spot_price, swap_price = get_ticker_prices()
    if spot_price <= 0 or swap_price <= 0:
        return False

    raw_spot_amount = target_usdt / spot_price
    spot_amount = truncate_value(raw_spot_amount, COIN_SPEC['spot_amount_prec'])
    
    raw_swap_contracts = spot_amount / COIN_SPEC['ctVal']
    swap_contracts = truncate_value(raw_swap_contracts, COIN_SPEC['swap_amount_prec'])

    if spot_amount < COIN_SPEC['spot_min_amount'] or swap_contracts < COIN_SPEC['swap_min_amount']:
        logging.warning(f"⚠️ 주문 수량이 거래소 최소 주문 단위보다 적습니다. (현물: {spot_amount}, 선물: {swap_contracts})")
        return False

    try:
        logging.info(f"🚀 [주문 시도] 선물 숏: {swap_contracts} 계약 | 현물 매수: {spot_amount} {TARGET_COIN}")

        # 1. 선물 숏 매도 우선 집행 (mgnMode 및 tdMode 명시적 교차 설정)
        swap_order = okx.create_order(
            symbol=SYMBOL_SWAP,
            type='market',
            side='sell',
            amount=swap_contracts,
            params={'tdMode': 'cross'}
        )
        logging.info(f"✅ 선물 숏 체결 성공: {swap_contracts} 계약")

        # 2. 현물 매수 집행 (tdMode: 'cross'로 지정하여 마진/교차 계정의 51000 에러 해결)
        try:
            spot_order = okx.create_order(
                symbol=SYMBOL_SPOT,
                type='market',
                side='buy',
                amount=spot_amount,
                params={
                    'tdMode': 'cross',
                    'tgtCcy': 'base_ccy'
                }
            )
            logging.info(f"✅ 현물 매수 체결 성공: {spot_amount} {TARGET_COIN}")
            
            POSITION_BASE_USDT = usdt_total
            logging.info("🎉 델타 뉴트럴 진입 완벽 체결 완료!")
            return True

        except Exception as spot_err:
            # 현물 매수 실패 시 즉시 선물 숏 롤백 (긴급 청산)
            logging.error(f"❌ 현물 매수 실패! 선물 포지션 긴급 롤백(청산) 시도: {spot_err}")
            okx.create_order(
                symbol=SYMBOL_SWAP,
                type='market',
                side='buy',
                amount=swap_contracts,
                params={'reduceOnly': True, 'tdMode': 'cross'}
            )
            logging.info("🛡️ 선물 숏 포지션 롤백 완료 (자산 보호 완료)")
            time.sleep(30)  # 무한 반복 방지를 위한 30초 대기
            return False

    except Exception as e:
        logging.error(f"❌ 주문 집행 중 에러 발생: {e}")
        return False

def execute_delta_neutral_exit(reason: str = "펀딩비 청산 조건 도달") -> bool:
    """델타 뉴트럴 포지션 전량 청산 (현물 매도 + 선물 숏 매수 청산)"""
    global POSITION_BASE_USDT
    
    try:
        pos = get_positions()
        _, _, coin_free = get_balance()

        if not pos and coin_free <= 0:
            logging.info("청산할 포지션이나 현물 코인 잔고가 없습니다.")
            return True

        logging.info(f"🚨 [포지션 청산 시작] 사유: {reason}")

        # 1. 선물 숏 포지션 청산 (Buy / Close Short)
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
                logging.info(f"✅ 선물 숏 포지션 청산 완료: {contracts} 계약")

        # 2. 보유 현물 매도 (Sell Spot, tdMode: 'cross' 적용)
        spot_amount_to_sell = truncate_value(coin_free, COIN_SPEC['spot_amount_prec'])
        if spot_amount_to_sell >= COIN_SPEC['spot_min_amount']:
            okx.create_order(
                symbol=SYMBOL_SPOT,
                type='market',
                side='sell',
                amount=spot_amount_to_sell,
                params={
                    'tdMode': 'cross',
                    'tgtCcy': 'base_ccy'
                }
            )
            logging.info(f"✅ 현물 매도 완료: {spot_amount_to_sell} {TARGET_COIN}")

        POSITION_BASE_USDT = 0.0  # 초기 진입 자산 리셋
        logging.info("🎉 델타 뉴트럴 전량 청산 완료!")
        return True

    except Exception as e:
        logging.error(f"❌ 청산 집행 중 에러 발생: {e}")
        return False

# ==========================================
# 7. 핵심 매매 로직 주기 실행 함수
# ==========================================
def trade_logic_cycle():
    """매 10초마다 실행되는 단일 매매 체크 사이클"""
    if not BOT_SWITCH:
        return

    spot_price, swap_price = get_ticker_prices()
    funding_rate = get_funding_rate()
    pos = get_positions()

    # 모니터링 로그 출력
    logging.info(
        f"[{TARGET_COIN} 감시 중] 현물: ${spot_price:.4f} | 선물: ${swap_price:.4f} | "
        f"현재 펀딩비: {funding_rate*100:.4f}% (진입목표: {MIN_FUNDING_RATE*100:.3f}%) | "
        f"포지션: {'보유' if pos else '미보유'}"
    )

    # 1. 포지션 미보유 시 진입 조건 판단
    if not pos and funding_rate >= MIN_FUNDING_RATE:
        logging.info(f"🚀 {TARGET_COIN} 진입 조건 충족! (펀딩비: {funding_rate*100:.4f}%)")
        execute_delta_neutral_entry()

    # 2. 포지션 보유 시 청산 조건 판단 (펀딩비 0.000% 이하 하락 시)
    elif pos and funding_rate <= EXIT_FUNDING_RATE:
        logging.info(f"📉 {TARGET_COIN} 청산 조건 충족! (현재 펀딩비: {funding_rate*100:.4f}% <= 목표: {EXIT_FUNDING_RATE*100:.3f}%)")
        execute_delta_neutral_exit("펀딩비 하락 청산")

# ==========================================
# 8. 비동기 백그라운드 태스크 (30분 주기 로그 알림)
# ==========================================
async def periodic_log_reporter(app: Application):
    """30분마다 텔레그램으로 봇 현황 로그 자동 전송"""
    while True:
        try:
            await asyncio.sleep(1800)
            
            spot_price, swap_price = get_ticker_prices()
            funding_rate = get_funding_rate() * 100
            _, usdt_total, _ = get_balance()
            pos = get_positions()
            krw_rate = get_usdt_krw_rate()
            krw_total = usdt_total * krw_rate

            log_msg = f"⏰ [30분 정기 상태 알림 - {TARGET_COIN}]\n\n"
            log_msg += f"• 자산: ${usdt_total:.2f} USDT (약 {krw_total:,.0f}원)\n"
            log_msg += f"• 현물/선물: ${spot_price:.4f} / ${swap_price:.4f}\n"
            log_msg += f"• 현재 펀딩비: {funding_rate:.4f}%\n"
            log_msg += f"• 포지션: {'보유 중 (숏)' if pos else '미보유 (관망 중)'}\n"
            log_msg += f"• 스위치: {'🟢 ON' if BOT_SWITCH else '🔴 OFF'}"

            await send_telegram_msg_async(app, log_msg)
            logging.info("📢 30분 정기 텔레그램 로그 전송 완료")
        except Exception as e:
            logging.error(f"30분 정기 로그 전송 에러: {e}")

# ==========================================
# 9. 비동기 메인 이벤트 루프
# ==========================================
async def main():
    # 최초 가동 시 타겟 코인(XRP) 스펙 및 마진 모드 자동 조회
    try:
        update_coin_spec(TARGET_COIN)
        logging.info(f"기본 코인 스펙 설정 완료: {TARGET_COIN} (1ct = {COIN_SPEC['ctVal']})")
    except Exception as e:
        logging.error(f"초기 스펙 설정 에러: {e}")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # 명령어 핸들러 등록
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("profit", profit_command))
    application.add_handler(CommandHandler("setcoin", setcoin_command))
    application.add_handler(CommandHandler("close", close_command))  # 수동 청산 명령어
    application.add_handler(CommandHandler("switch", switch_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # 1. 텔레그램 봇 비동기 시작
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logging.info(f"🤖 델타 뉴트럴 자동 매매 봇 시작 (진입 펀딩비: {MIN_FUNDING_RATE*100:.3f}%)")
    await send_telegram_msg_async(
        application, 
        f"🤖 델타 뉴트럴 자동 매매 봇이 시작되었습니다.\n"
        f"• 기본 타겟: {TARGET_COIN}\n"
        f"• 진입 펀딩비: {MIN_FUNDING_RATE*100:.3f}%\n"
        f"• 청산 펀딩비: {EXIT_FUNDING_RATE*100:.3f}%"
    )

    # 2. 30분 정기 로그 보고 백그라운드 태스크 시작
    asyncio.create_task(periodic_log_reporter(application))

    # 3. 매매 로직 동시 수행 루프
    try:
        while True:
            try:
                trade_logic_cycle()
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