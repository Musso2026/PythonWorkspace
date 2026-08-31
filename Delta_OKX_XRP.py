import os
import sys
import time
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
# 1) PythonWorkspace 폴더 내 .env 우선 확인, 없으면 홈 디렉토리 ~/.env 확인
workspace_env = os.path.expanduser('~/PythonWorkspace/.env')
home_env = os.path.expanduser('~/.env')

if os.path.exists(workspace_env):
    load_dotenv(dotenv_path=workspace_env)
elif os.path.exists(home_env):
    load_dotenv(dotenv_path=home_env)
else:
    load_dotenv()  # 기본 로드

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")

# 🎯 펀딩비 진입 기준 설정 (0.030% = 0.0003)
MIN_FUNDING_RATE = 0.0003 

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
        'options': {'defaultType': 'swap'}
    })

okx = get_okx_client()

# 글로벌 변수 및 상태
BOT_SWITCH = True  # 봇 가동 스위치 (ON: True, OFF: False)
SYMBOL_SPOT = "XRP/USDT"
SYMBOL_SWAP = "XRP/USDT:USDT"
POSITION_BASE_USDT = 0.0  # 포지션 진입 시점 원금

# ==========================================
# 2. 텔레그램 메세지 전송 함수
# ==========================================
async def send_telegram_msg_async(app: Application, text: str):
    """비동기 텔레그램 알림 전송"""
    if TELEGRAM_TOKEN and TELEGRAM_ADMIN_ID:
        try:
            await app.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=text)
        except Exception as e:
            logging.error(f"텔레그램 메시지 전송 실패: {e}")

# ==========================================
# 3. 데이터 및 포지션 조회 함수
# ==========================================
def get_balance():
    """잔고 조회"""
    try:
        balance = okx.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0.0)
        usdt_total = balance['total'].get('USDT', 0.0)
        return usdt_free, usdt_total
    except Exception as e:
        logging.error(f"잔고 조회 에러: {e}")
        return 0.0, 0.0

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
# 4. 텔레그램 명령어 핸들러
# ==========================================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    spot_price, swap_price = get_ticker_prices()
    funding_rate = get_funding_rate() * 100
    pos = get_positions()
    _, usdt_total = get_balance()

    switch_str = "🟢 ON (가동 중)" if BOT_SWITCH else "🔴 OFF (일시 정지)"
    
    msg = f"📊 [봇 현재 상태 보고]\n\n"
    msg += f"• 스위치 상태: {switch_str}\n"
    msg += f"• 진입 기준 펀딩비: {MIN_FUNDING_RATE * 100:.3f}%\n"
    msg += f"• 총 자산: ${usdt_total:.2f} USDT\n"
    msg += f"• 현물(XRP) 가격: ${spot_price:.4f}\n"
    msg += f"• 선물(XRP) 가격: ${swap_price:.4f}\n"
    msg += f"• 현재 펀딩비: {funding_rate:.4f}%\n\n"

    if pos:
        contracts = pos.get('contracts', 0)
        entry_price = float(pos.get('entryPrice', 0.0))
        liq_price = float(pos.get('liquidationPrice', 0.0)) if pos.get('liquidationPrice') else 0.0
        
        liq_distance = 0.0
        if swap_price > 0 and liq_price > 0:
            liq_distance = abs((swap_price - liq_price) / swap_price) * 100

        msg += f"📦 [포지션 정보 (숏)]\n"
        msg += f"• 수량: {contracts} Cont\n"
        msg += f"• 진입가: ${entry_price:.4f}\n"
        msg += f"• 청산가: ${liq_price:.4f} (안전거리: {liq_distance:.2f}%)\n"
    else:
        msg += f"📦 현재 보유 중인 포지션이 없습니다 (관망 중)."

    await update.message.reply_text(msg)

async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profit 명령어"""
    if str(update.effective_user.id) != str(TELEGRAM_ADMIN_ID):
        return

    usdt_free, usdt_total = get_balance()
    global POSITION_BASE_USDT

    msg = f"💰 [수익 및 수익률 현황]\n\n"
    msg += f"• 총 평가 자산: ${usdt_total:.2f} USDT\n"
    msg += f"• 가용 가능 USDT: ${usdt_free:.2f} USDT\n"

    if POSITION_BASE_USDT > 0:
        pnl = usdt_total - POSITION_BASE_USDT
        pnl_pct = (pnl / POSITION_BASE_USDT) * 100
        msg += f"• 포지션 초기 자산: ${POSITION_BASE_USDT:.2f} USDT\n"
        msg += f"• 진입 대비 손익: ${pnl:+.2f} USDT ({pnl_pct:+.2f}%)\n"
    else:
        msg += f"• 포지션 초기 자산: $0.00 USDT\n"
        msg += f"• 진입 대비 손익: 기준 자산 정보 없음 (포지션 미보유 중)\n"

    await update.message.reply_text(msg)

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
# 5. 핵심 매매 로직 주기 실행 함수
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
        f"[감시 중] 현물: ${spot_price:.4f} | 선물: ${swap_price:.4f} | "
        f"현재 펀딩비: {funding_rate*100:.4f}% (목표: {MIN_FUNDING_RATE*100:.3f}%) | "
        f"포지션: {'보유' if pos else '미보유'}"
    )

    # 🎯 펀딩비 조건 판단
    if not pos and funding_rate >= MIN_FUNDING_RATE:
        logging.info(f"🚀 진입 조건 충족! (현재 펀딩비: {funding_rate*100:.4f}% >= 목표: {MIN_FUNDING_RATE*100:.3f}%)")
        # 실제 매수/숏 주문 로직 실행 위치

# ==========================================
# 6. 비동기 백그라운드 태스크 (30분 주기 로그 알림)
# ==========================================
async def periodic_log_reporter(app: Application):
    """30분마다 텔레그램으로 봇 현황 로그 자동 전송"""
    while True:
        try:
            await asyncio.sleep(1800)  # 30분 (1800초) 대기
            
            spot_price, swap_price = get_ticker_prices()
            funding_rate = get_funding_rate() * 100
            _, usdt_total = get_balance()
            pos = get_positions()

            log_msg = f"⏰ [30분 정기 상태 알림]\n\n"
            log_msg += f"• 자산: ${usdt_total:.2f} USDT\n"
            log_msg += f"• 현물/선물: ${spot_price:.4f} / ${swap_price:.4f}\n"
            log_msg += f"• 현재 펀딩비: {funding_rate:.4f}% (목표: {MIN_FUNDING_RATE*100:.3f}%)\n"
            log_msg += f"• 포지션: {'보유 중 (숏)' if pos else '미보유 (관망 중)'}\n"
            log_msg += f"• 스위치: {'🟢 ON' if BOT_SWITCH else '🔴 OFF'}"

            await send_telegram_msg_async(app, log_msg)
            logging.info("📢 30분 정기 텔레그램 로그 전송 완료")
        except Exception as e:
            logging.error(f"30분 정기 로그 전송 에러: {e}")

# ==========================================
# 7. 비동기 메인 이벤트 루프
# ==========================================
async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # 명령어 핸들러 등록
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("profit", profit_command))
    application.add_handler(CommandHandler("switch", switch_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # 1. 텔레그램 봇 비동기 시작
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logging.info(f"🤖 델타 뉴트럴 자동 매매 봇 시작 (목표 펀딩비: {MIN_FUNDING_RATE*100:.3f}%)")
    await send_telegram_msg_async(application, f"🤖 델타 뉴트럴 자동 매매 봇 시작되었습니다.\n(최소 진입 펀딩비 기준: {MIN_FUNDING_RATE*100:.3f}%)")

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