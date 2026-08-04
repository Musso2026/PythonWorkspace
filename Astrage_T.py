import os
import sys
import asyncio
import platform
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv

import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# .env 파일 로드
load_dotenv()

# ==========================================
# 1. 로깅 및 시스템 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# OS 절전 모드 방지 설정
def prevent_sleep():
    try:
        if platform.system() == "Windows":
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
            logger.info("🖥️ [시스템] Windows 백그라운드 절전 방지 활성화 완료.")
        elif platform.system() == "Darwin":  # macOS
            import subprocess
            subprocess.Popen(["caffeinate", "-s"])
            logger.info("🖥️ [시스템] macOS 백그라운드 절전 방지(caffeinate) 활성화 완료.")
    except Exception as e:
        logger.warning(f"⚠️ 절전 방지 설정 중 오류 발생: {e}")

# ==========================================
# 2. 거래 설정 및 상수 정의
# ==========================================
TARGET_THRESHOLD = 0.00498  # 0.498% 이상 시 차익거래 실행
MAX_BALANCE_USAGE = 0.95    # 보유 자금의 95% 이내 사용

# 거래 대상 코인 목록
COINS = [
    'BTC', 'ETH', 'XRP', 'SOL', 'ADA', 'DOGE', 'AVAX', 'DOT',
    'LINK', 'MATIC', 'TRX', 'BCH', 'NEAR', 'APT', 'STX', 'ONDO',
    'ALGO', 'SEI', 'SUI', 'SHIB', 'PEPE', 'SAND', 'MANA', 'EOS'
]

# ==========================================
# 3. 아비트라지 봇 클래스
# ==========================================
class AsyncArbitrageBot:
    def __init__(self):
        self.is_running = True
        self.upbit: Optional[ccxt.upbit] = None
        self.okx: Optional[ccxt.okx] = None
        self.usdt_krw_rate = 1380.0  # 기본 고정/추정 환율
        self.stop_event = asyncio.Event()  # 프로그램 원격 종료 이벤트

    async def initialize_exchanges(self):
        """거래소 API 초기화 및 연결"""
        self.upbit = ccxt.upbit({
            'apiKey': os.getenv('UPBIT_ACCESS_KEY'),
            'secret': os.getenv('UPBIT_SECRET_KEY'),
            'enableRateLimit': True,
            'options': {'createMarketBuyOrderRequiresPrice': False}
        })
        
        self.okx = ccxt.okx({
            'apiKey': os.getenv('OKX_API_KEY'),
            'secret': os.getenv('OKX_SECRET_KEY'),
            'password': os.getenv('OKX_PASSWORD'),
            'enableRateLimit': True,
        })
        logger.info("✅ 업비트 및 OKX Async API 연결 완료.")

    async def close_exchanges(self):
        """거래소 세션 안전 종료"""
        if self.upbit:
            await self.upbit.close()
        if self.okx:
            await self.okx.close()
        logger.info("🔒 거래소 비동기 세션이 안전하게 종료되었습니다.")

    async def fetch_usdt_rate(self) -> float:
        """업비트 USDT/KRW 실시간 시세 조회"""
        try:
            ticker = await self.upbit.fetch_ticker('USDT/KRW')
            if ticker and ticker.get('last'):
                self.usdt_krw_rate = float(ticker['last'])
        except Exception as e:
            logger.warning(f"⚠️ USDT 환율 조회 실패 (기본값 {self.usdt_krw_rate}원 사용): {e}")
        return self.usdt_krw_rate

    async def get_exchange_balances(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """각 거래소의 잔고 가져오기"""
        upbit_bal, okx_bal = {}, {}
        try:
            u_res, o_res = await asyncio.gather(
                self.upbit.fetch_balance(),
                self.okx.fetch_balance(),
                return_exceptions=True
            )
            if not isinstance(u_res, Exception):
                for k, v in u_res['total'].items():
                    if v > 0:
                        upbit_bal[k] = v
            if not isinstance(o_res, Exception):
                for k, v in o_res['total'].items():
                    if v > 0:
                        okx_bal[k] = v
        except Exception as e:
            logger.error(f"❌ 잔고 조회 에러: {e}")
        return upbit_bal, okx_bal

    async def analyze_opportunity(self, coin: str) -> Optional[dict]:
        """단일 코인에 대한 호가창 분석 및 프리미엄 계산"""
        upbit_symbol = f"{coin}/KRW"
        okx_symbol = f"{coin}/USDT"

        try:
            u_orderbook, o_orderbook = await asyncio.gather(
                self.upbit.fetch_order_book(upbit_symbol, limit=5),
                self.okx.fetch_order_book(okx_symbol, limit=5),
                return_exceptions=True
            )

            if isinstance(u_orderbook, Exception) or isinstance(o_orderbook, Exception):
                return None

            u_ask, u_bid = u_orderbook['asks'][0][0], u_orderbook['bids'][0][0]
            o_ask, o_bid = o_orderbook['asks'][0][0], o_orderbook['bids'][0][0]

            o_ask_krw = o_ask * self.usdt_krw_rate
            o_bid_krw = o_bid * self.usdt_krw_rate

            # 케이스 A: OKX 매수 -> 업비트 매도
            diff_a = (u_bid - o_ask_krw) / o_ask_krw
            
            # 케이스 B: 업비트 매수 -> OKX 매도
            diff_b = (o_bid_krw - u_ask) / u_ask

            if diff_a >= TARGET_THRESHOLD:
                return {
                    'coin': coin, 'direction': 'BUY_OKX_SELL_UPBIT',
                    'diff': diff_a, 'buy_price': o_ask_krw, 'sell_price': u_bid,
                    'okx_price_usdt': o_ask, 'upbit_price_krw': u_bid
                }
            elif diff_b >= TARGET_THRESHOLD:
                return {
                    'coin': coin, 'direction': 'BUY_UPBIT_SELL_OKX',
                    'diff': diff_b, 'buy_price': u_ask, 'sell_price': o_bid_krw,
                    'okx_price_usdt': o_bid, 'upbit_price_krw': u_ask
                }
        except Exception:
            pass
        return None

    async def execute_arbitrage(self, opp: dict, bot_app: Application):
        """초고속 초격차 실체결 주문 실행 및 완료 리포팅"""
        coin = opp['coin']
        direction = opp['direction']
        diff_pct = opp['diff'] * 100
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        u_bal, o_bal = await self.get_exchange_balances()

        upbit_symbol = f"{coin}/KRW"
        okx_symbol = f"{coin}/USDT"

        if direction == 'BUY_OKX_SELL_UPBIT':
            available_usdt = o_bal.get('USDT', 0) * MAX_BALANCE_USAGE
            available_coin = u_bal.get(coin, 0) * MAX_BALANCE_USAGE

            if available_usdt < 10:
                msg = f"⚠️ <b>[잔고 부족]</b> OKX USDT 잔고가 부족하여 {coin} 차익거래를 진행할 수 없습니다.\n• 보유 USDT: {o_bal.get('USDT', 0):,.2f}"
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                return

            if available_coin <= 0:
                msg = f"⚠️ <b>[자산 부족]</b> 업비트에 매도할 {coin} 코인 잔고가 없습니다.\n• 보유 수량: {u_bal.get(coin, 0):,.4f}"
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                return

            trade_amount = min(available_usdt / opp['okx_price_usdt'], available_coin)

            # 초고속 병렬 매매 실행 (OKX 시장가 매수 / 업비트 시장가 매도)
            try:
                order_okx, order_upbit = await asyncio.gather(
                    self.okx.create_market_buy_order(okx_symbol, trade_amount),
                    self.upbit.create_market_sell_order(upbit_symbol, trade_amount)
                )

                profit_krw = (opp['upbit_price_krw'] - (opp['okx_price_usdt'] * self.usdt_krw_rate)) * trade_amount

                report_msg = (
                    f"⚡ <b>[초고속 매매 체결 완료]</b>\n"
                    f"• 코인: <b>{coin}</b>\n"
                    f"• 방향: <b>OKX 매수 ➔ 업비트 매도</b>\n"
                    f"• 수량: <b>{trade_amount:,.4f} {coin}</b>\n"
                    f"• 포착 차익률: <b>+{diff_pct:.3f}%</b>\n"
                    f"• 💰 <b>실질 예상 수익금: +{profit_krw:,.0f} 원</b>\n"
                    f"• 시각: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}"
                )
                logger.info(f"✅ [체결 완료] {coin} | 차익률: {diff_pct:.3f}% | 예상수익: {profit_krw:,.0f}원")
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=report_msg, parse_mode='HTML')

            except Exception as e:
                err_msg = f"❌ <b>[주문 체결 오류]</b> {coin} 차익거래 실행 실패: {e}"
                logger.error(err_msg)
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=err_msg, parse_mode='HTML')

        elif direction == 'BUY_UPBIT_SELL_OKX':
            available_krw = u_bal.get('KRW', 0) * MAX_BALANCE_USAGE
            available_coin = o_bal.get(coin, 0) * MAX_BALANCE_USAGE

            if available_krw < 10000:
                msg = f"⚠️ <b>[잔고 부족]</b> 업비트 원화(KRW) 잔고가 부족합니다.\n• 보유 KRW: {u_bal.get('KRW', 0):,.0f}원"
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                return

            if available_coin <= 0:
                msg = f"⚠️ <b>[자산 부족]</b> OKX에 매도할 {coin} 코인 잔고가 없습니다.\n• 보유 수량: {o_bal.get(coin, 0):,.4f}"
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                return

            trade_amount = min(available_krw / opp['upbit_price_krw'], available_coin)

            # 초고속 병렬 매매 실행 (업비트 시장가 매수 / OKX 시장가 매도)
            try:
                order_upbit, order_okx = await asyncio.gather(
                    self.upbit.create_market_buy_order(upbit_symbol, trade_amount * opp['upbit_price_krw']),
                    self.okx.create_market_sell_order(okx_symbol, trade_amount)
                )

                profit_krw = ((opp['okx_price_usdt'] * self.usdt_krw_rate) - opp['upbit_price_krw']) * trade_amount

                report_msg = (
                    f"⚡ <b>[초고속 매매 체결 완료]</b>\n"
                    f"• 코인: <b>{coin}</b>\n"
                    f"• 방향: <b>업비트 매수 ➔ OKX 매도</b>\n"
                    f"• 수량: <b>{trade_amount:,.4f} {coin}</b>\n"
                    f"• 포착 차익률: <b>+{diff_pct:.3f}%</b>\n"
                    f"• 💰 <b>실질 예상 수익금: +{profit_krw:,.0f} 원</b>\n"
                    f"• 시각: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}"
                )
                logger.info(f"✅ [체결 완료] {coin} | 차익률: {diff_pct:.3f}% | 예상수익: {profit_krw:,.0f}원")
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=report_msg, parse_mode='HTML')

            except Exception as e:
                err_msg = f"❌ <b>[주문 체결 오류]</b> {coin} 차익거래 실행 실패: {e}"
                logger.error(err_msg)
                if chat_id:
                    await bot_app.bot.send_message(chat_id=chat_id, text=err_msg, parse_mode='HTML')

    async def main_loop(self, bot_app: Application):
        """실시간 백그라운드 초고속 시세 감지 메인 루프"""
        logger.info(f"🔄 백그라운드 시세 감지 시작 (목표 차익률: {TARGET_THRESHOLD*100:.3f}%)...")
        while True:
            try:
                if self.is_running:
                    await self.fetch_usdt_rate()
                    
                    tasks = [self.analyze_opportunity(coin) for coin in COINS]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for res in results:
                        if res and isinstance(res, dict):
                            await self.execute_arbitrage(res, bot_app)
                            await asyncio.sleep(1)

                await asyncio.sleep(0.1)  # 초고속 100ms 반응 간격

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ 시세 스캐닝 루프 예외: {e}")
                await asyncio.sleep(1)

# ==========================================
# 4. 텔레그램 봇 명령어 핸들러
# ==========================================
bot_instance = AsyncArbitrageBot()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램 /start 명령어: 매매 감지 재개"""
    if bot_instance.is_running:
        await update.message.reply_text("⚠️ 이미 시세 스캐너가 백그라운드에서 실행 중입니다.")
        return

    bot_instance.is_running = True
    await update.message.reply_html(
        "🚀 <b>[자동매매 스캐너 감지 재개]</b>\n"
        f"• 감지 코인: 총 {len(COINS)}개\n"
        f"• 목표 차익률: <b>{TARGET_THRESHOLD*100:.3f}%</b>\n"
        "• 상태: 초고속 실시간 스캐닝 진행 중...\n\n"
        "💡 <i><b>/stop</b> 입력 시 감지 일시정지, <b>/exit</b> 입력 시 프로그램을 종료합니다.</i>"
    )

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램 /stop 명령어: 매매 감지 일시 정지"""
    if not bot_instance.is_running:
        await update.message.reply_text("⚠️ 이미 감지 시스템이 일시 정지된 상태입니다.")
        return

    bot_instance.is_running = False
    await update.message.reply_text("🛑 차익거래 감지 스캐너가 일시 정지되었습니다.")

async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램 /exit 또는 /quit 명령어: 전체 파이썬 프로그램 안전 종료"""
    await update.message.reply_html("🛑 <b>[시스템 종료 알림]</b>\n파이썬 프로그램 및 비동기 루프를 모두 종료합니다.")
    logger.info("👋 텔레그램 명령에 의해 프로그램을 종료합니다.")
    bot_instance.stop_event.set()  # 메인 이벤트 해제하여 완전 종료

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램 /balance 명령어: 실시간 계좌 대시보드 출력"""
    await update.message.reply_text("🔍 실시간 자산 대시보드를 조회하고 있습니다...")
    u_bal, o_bal = await bot_instance.get_exchange_balances()
    rate = bot_instance.usdt_krw_rate

    msg = f"📊 <b>[실시간 자산 현황 대시보드]</b>\n"
    msg += f"🗓️ 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    msg += f"💵 기준 USDT 환율: <b>{rate:,.1f} 원</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━\n\n"

    msg += "🇰🇷 <b>[ 업비트 보유 자산 ]</b>\n"
    if u_bal:
        for coin, amt in u_bal.items():
            msg += f"• {coin:<5} : {amt:>12,.4f}\n"
    else:
        msg += "• 보유 자산 없음\n"

    msg += "\n🌐 <b>[ OKX 보유 자산 ]</b>\n"
    if o_bal:
        for coin, amt in o_bal.items():
            msg += f"• {coin:<5} : {amt:>12,.4f}\n"
    else:
        msg += "• 보유 자산 없음\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━━━\n✅ 거래소 시스템 정상 모니터링 중"
    await update.message.reply_html(msg)

# ==========================================
# 5. 메인 실행 함수
# ==========================================
async def main():
    prevent_sleep()
    await bot_instance.initialize_exchanges()

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        logger.error("❌ .env 파일에 TELEGRAM_BOT_TOKEN 설정이 필요합니다.")
        return

    app = Application.builder().token(telegram_token).build()

    # 텔레그램 명령어 핸들러 등록
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler(["exit", "quit"], cmd_exit))

    logger.info("🤖 텔레그램 봇 및 백그라운드 모니터링 시스템 개시 완료.")

    async with app:
        await app.start()
        await app.updater.start_polling()
        
        scan_task = asyncio.create_task(bot_instance.main_loop(app))
        
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if chat_id:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🤖 <b>[시스템 알림]</b> 차익거래 프로그램이 개시되었습니다.\n"
                        f"• 목표 차익률: <b>{TARGET_THRESHOLD*100:.3f}%</b>\n"
                        "• 백그라운드 실시간 초고속 스캐닝 작동 중...\n\n"
                        "💡 <i>명령어: /start, /stop, /balance, /exit</i>"
                    ),
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.warning(f"⚠️ 최초 텔레그램 알림 전송 실패: {e}")

        # 종료 명령(/exit)이 오거나 예외 발생 시까지 대기
        try:
            await bot_instance.stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scan_task.cancel()
            await bot_instance.close_exchanges()
            await app.updater.stop()
            await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 프로그램을 종료합니다.")