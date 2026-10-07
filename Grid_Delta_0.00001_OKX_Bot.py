import os
import time
import math
import logging
import asyncio
import requests
import pandas as pd
import numpy as np
from dotenv import load_dotenv
import ccxt

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSWORD = os.getenv("OKX_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")

# 전역 제어 변수 (마스터피스 핵심 기능 유지)
BOT_RUNNING = False
LEVERAGE = 3
MIN_FUNDING_RATE = 0.01  # 0.01%
SELECTED_COINS = []
LAST_NOTIFIED_POSITIONS = set()

# 자금 제어 규칙 (코인당 최대 750 USDT, 총 3000 USDT)
MAX_USDT_PER_COIN = 750.0
MAX_USDT_TOTAL = 3000.0

# 초단타 그리드 세부 설정
GRID_PERCENT_SPACING = 0.002  # 0.2% 간격
GRID_LEVELS = 3               

# 🛡️ [무결점 안전 객체 생성] 애트리뷰트 에러 원천 차단
try:
    okx_class = getattr(ccxt, 'okx', None)
    if not okx_class:
        okx_class = ccxt.Exchange
        
    exchange = okx_class({
        'apiKey': OKX_API_KEY,
        'secret': OKX_SECRET_KEY,
        'password': OKX_PASSWORD,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    # 만약 Exchange 제네릭 클래스로 생성된 경우 명시적 ID 부여
    if hasattr(exchange, 'id') and exchange.id != 'okx':
        exchange.id = 'okx'
        exchange.hostname = 'okx.com'
except Exception as e:
    logger.critical(f"거래소 초기화 치명적 에러: {e}")
    raise e

def get_fear_and_greed_index():
    try:
        url = "https://api.alternative.me/fng/"
        res = requests.get(url, timeout=3).json()
        return int(res['data'][0]['value'])
    except:
        return 50

def get_krw_rate():
    try:
        res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=3).json()
        return res['rates']['KRW']
    except:
        return 1350.0

def get_news_sentiment_score(coin_symbol=""):
    try:
        base_currency = coin_symbol.split('/')[0] if coin_symbol else ""
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&public=true"
        if base_currency:
            url += f"&currencies={base_currency}"
            
        res = requests.get(url, timeout=3).json()
        posts = res.get('results', [])
        
        sentiment_score = 0
        bearish_keywords = ['ban', 'hack', 'sec', 'lawsuit', 'crackdown', 'war', 'inflation', 'drop', 'regulation', 'investigation', 'tariff', 'election']
        bullish_keywords = ['approval', 'partnership', 'launch', 'adopt', 'bull', 'upgrade', 'etf', 'surge', 'rally']
        
        for post in posts[:10]:
            title = post.get('title', '').lower()
            for kw in bearish_keywords:
                if kw in title: sentiment_score -= 5
            for kw in bullish_keywords:
                if kw in title: sentiment_score += 5
                    
        return sentiment_score
    except:
        return 0

async def dynamic_coin_screening():
    global SELECTED_COINS
    try:
        logger.info("스마트 동적 코인 스크리닝 시작...")
        markets = exchange.load_markets()
        symbols = [symbol for symbol, market in markets.items() if market['swap'] and symbol.endswith('/USDT:USDT')]
        
        fng_score = get_fear_and_greed_index()
        scored_coins = []
        
        for symbol in symbols[:15]:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
                if len(ohlcv) < 50: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                volatility = df['close'].pct_change().std() * 100
                news_score = get_news_sentiment_score(symbol)
                
                total_score = (volatility * 0.6) + (news_score * 0.4)
                if total_score > 1.5:
                    scored_coins.append((symbol, total_score))
            except:
                continue
                
        scored_coins.sort(key=lambda x: x[1], reverse=True)
        
        if fng_score < 30 or len(scored_coins) == 0:
            target_count = min(2, len(scored_coins)) if len(scored_coins) > 0 else 1
            SELECTED_COINS = [item[0] for item in scored_coins[:target_count]] if target_count > 0 else ['BTC/USDT:USDT']
        else:
            target_count = max(1, min(4, len(scored_coins)))
            SELECTED_COINS = [item[0] for item in scored_coins[:target_count]]
            
        logger.info(f"타겟 코인 선정 완료 ({len(SELECTED_COINS)}개): {SELECTED_COINS}")
    except Exception as e:
        logger.error(f"스크리닝 오류: {e}")
        SELECTED_COINS = ['BTC/USDT:USDT']

async def check_global_kill_switch(application=None):
    try:
        balance = exchange.fetch_balance()
        margin_ratio = float(balance.get('info', {}).get('mrgRatio', 0) or 0)
        
        if margin_ratio > 0.75:
            logger.critical("⚠️ 마진 위험율 초과! 긴급 청산 및 봇 비상 정지")
            close_all_positions()
            global BOT_RUNNING
            BOT_RUNNING = False
            if application and TELEGRAM_CHAT_ID:
                await application.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text="🚨 **[긴급 비상 헷지 발동]** 마진 위험으로 모든 포지션을 청산하고 봇을 비상 정지했습니다!"
                )
            return True
    except Exception as e:
        logger.error(f"킬스위치 오류: {e}")
    return False

def close_all_positions():
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            if float(pos['contracts']) > 0:
                symbol = pos['symbol']
                if symbol.replace(':USDT', '') not in ['ONDO/USDT']: # ONDO 자산 보호 철저 유지
                    side = 'sell' if pos['side'] == 'long' else 'buy'
                    exchange.create_order(symbol, 'market', side, pos['contracts'])
        logger.info("포지션 안전 청산 완료 (ONDO 보호됨)")
    except Exception as e:
        logger.error(f"긴급 청산 실패: {e}")

async def execute_ultra_fast_scalping(symbol, allocated_usdt):
    try:
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        
        try:
            open_orders = exchange.fetch_open_orders(symbol)
            if len(open_orders) >= (GRID_LEVELS * 2):
                return
            for order in open_orders:
                exchange.cancel_order(order['id'], symbol)
        except:
            pass

        per_grid_amount = (allocated_usdt * LEVERAGE) / GRID_LEVELS
        
        for i in range(1, GRID_LEVELS + 1):
            buy_price = current_price * (1 - (GRID_PERCENT_SPACING * i))
            sell_price = current_price * (1 + (GRID_PERCENT_SPACING * i))
            
            buy_amount = per_grid_amount / buy_price
            sell_amount = per_grid_amount / sell_price
            
            exchange.create_order(symbol, 'limit', 'buy', buy_amount, buy_price)
            exchange.create_order(symbol, 'limit', 'sell', sell_amount, sell_price)
            
    except ccxt.RateLimitExceeded:
        logger.warning("⚠️ 레이트 리밋 감지 - 1초 대기")
        await asyncio.sleep(1.0)
    except Exception as e:
        logger.error(f"[{symbol}] 스캘핑 집행 에러: {e}")

async def trading_bot_loop(application=None):
    global BOT_RUNNING
    logger.info("트레이딩 루프 시작됨")
    
    screening_counter = 0
    while True:
        if not BOT_RUNNING:
            await asyncio.sleep(1)
            continue
            
        try:
            if await check_global_kill_switch(application):
                break
                
            if screening_counter <= 0 or not SELECTED_COINS:
                await dynamic_coin_screening()
                screening_counter = 10
            else:
                screening_counter -= 1

            balance = exchange.fetch_balance()
            usdt_total = balance['USDT']['total']
            
            num_coins = max(1, len(SELECTED_COINS))
            target_total_allocation = min(MAX_USDT_TOTAL, usdt_total)
            allocated_usdt_per_coin = min(MAX_USDT_PER_COIN, target_total_allocation / num_coins)

            for symbol in SELECTED_COINS:
                funding_info = exchange.fetch_funding_rate(symbol)
                funding_rate = funding_info.get('fundingRate', 0) * 100
                
                if funding_rate < MIN_FUNDING_RATE:
                    continue
                
                try:
                    exchange.set_leverage(LEVERAGE, symbol)
                except:
                    pass

                await execute_ultra_fast_scalping(symbol, allocated_usdt_per_coin)

            if application and TELEGRAM_CHAT_ID:
                await check_and_send_realtime_trade_alerts(application)

            await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"메인 루프 예외: {e}")
            await asyncio.sleep(2)

async def check_and_send_realtime_trade_alerts(application):
    global LAST_NOTIFIED_POSITIONS
    try:
        positions = exchange.fetch_positions()
        krw_rate = get_krw_rate()
        active_symbols = set()
        
        for pos in positions:
            contracts = float(pos.get('contracts', 0) or 0)
            if contracts > 0:
                symbol = pos['symbol']
                if symbol.replace(':USDT', '') in ['ONDO/USDT']: continue
                
                active_symbols.add(symbol)
                entry_price = float(pos.get('entryPrice', 0) or 0)
                mark_price = float(pos.get('markPrice', 0) or 0)
                pnl = float(pos.get('unrealizedPnl', 0) or 0)
                side = pos.get('side', 'long').upper()
                
                roe = 0
                if entry_price > 0:
                    if side == 'LONG':
                        roe = ((mark_price - entry_price) / entry_price) * 100 * LEVERAGE
                    else:
                        roe = ((entry_price - mark_price) / entry_price) * 100 * LEVERAGE

                pnl_krw = pnl * krw_rate
                
                if symbol not in LAST_NOTIFIED_POSITIONS:
                    msg = (
                        f"🚨 **[실시간 거래 체결 알림]**\n"
                        f"- 종목: `{symbol}` ({side})\n"
                        f"- 진입가: `{entry_price:,.4f}` USDT\n"
                        f"- 현재가: `{mark_price:,.4f}` USDT\n"
                        f"- ROE: `{roe:+.2f}%`\n"
                        f"- 평가 손익: `{pnl:+.4f} USDT` (약 `{pnl_krw:+,.0f} 원`)"
                    )
                    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
                    
        LAST_NOTIFIED_POSITIONS = active_symbols
    except Exception as e:
        logger.error(f"알림 에러: {e}")

async def telegram_heartbeat_job(application):
    while True:
        await asyncio.sleep(1800)
        if TELEGRAM_CHAT_ID and BOT_RUNNING:
            try:
                balance = exchange.fetch_balance()
                usdt_total = balance['USDT']['total']
                usdt_free = balance['USDT']['free']
                krw_rate = get_krw_rate()
                total_krw = usdt_total * krw_rate
                
                positions = exchange.fetch_positions()
                total_unrealized_pnl = sum([float(p.get('unrealizedPnl', 0) or 0) for p in positions if float(p.get('contracts', 0) or 0) > 0])
                total_pnl_krw = total_unrealized_pnl * krw_rate

                msg = (
                    f"⏰ **[30분 정기 통합 리포트]**\n"
                    f"초고속 그리드 봇 정상 구동 중 🟢\n\n"
                    f"💰 **[자금 현황]**\n"
                    f"- 총 잔고: `{usdt_total:,.2f} USDT` (약 `{total_krw:,.0f} 원`)\n"
                    f"- 가용 자금: `{usdt_free:,.2f} USDT`\n"
                    f"- 평가 손익: `{total_unrealized_pnl:+.2f} USDT` (약 `{total_pnl_krw:+,.0f} 원`)\n\n"
                    f"📊 **[타겟 코인]** {', '.join(SELECTED_COINS) if SELECTED_COINS else '없음'}\n"
                    f"🔒 **보호 자산**: ONDO (안전 격리)"
                )
                await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"헬스체크 오류: {e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    global BOT_RUNNING
    BOT_RUNNING = True
    await update.message.reply_text("✅ 초고속 스캘핑 봇이 가동되었습니다! (750 USDT 제한, ONDO 보호 활성화)")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    global BOT_RUNNING
    BOT_RUNNING = False
    await update.message.reply_text("🛑 자동매매 봇이 안전하게 중지되었습니다.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    try:
        balance = exchange.fetch_balance()
        usdt_total = balance['USDT']['total']
        usdt_free = balance['USDT']['free']
        krw_rate = get_krw_rate()
        total_krw = usdt_total * krw_rate
        
        positions = exchange.fetch_positions()
        active_pos_msgs = []
        total_pnl = 0
        
        for pos in positions:
            contracts = float(pos.get('contracts', 0) or 0)
            if contracts > 0:
                symbol = pos['symbol']
                entry = float(pos.get('entryPrice', 0) or 0)
                mark = float(pos.get('markPrice', 0) or 0)
                pnl = float(pos.get('unrealizedPnl', 0) or 0)
                total_pnl += pnl
                side = pos.get('side', 'LONG').upper()
                roe = ((mark - entry) / entry) * 100 * LEVERAGE if side == 'LONG' else ((entry - mark) / entry) * 100 * LEVERAGE
                active_pos_msgs.append(f"• `{symbol}` ({side}) | 진입: {entry:,.4f} | ROE: **{roe:+.2f}%** | 손익: {pnl:+.2f} USDT")

        pos_str = "\n".join(active_pos_msgs) if active_pos_msgs else "현재 진행 중인 거래 없음"
        
        status_msg = (
            f"📊 **[프로 봇 실시간 현황]**\n"
            f"- 상태: {'실행 중 🟢' if BOT_RUNNING else '정지 중 🔴'}\n"
            f"- 레버리지: `{LEVERAGE}x`\n"
            f"- 코인당 한도: `{MAX_USDT_PER_COIN} USDT`\n"
            f"- 총 잔고: `{usdt_total:,.2f} USDT` (약 `{total_krw:,.0f} 원`)\n"
            f"- 총 평가 손익: `{total_pnl:+.2f} USDT`\n\n"
            f"📈 **[오픈 포지션]**\n{pos_str}\n\n"
            f"🔒 **보호 자산**: ONDO (매매 제외)"
        )
        await update.message.reply_text(status_msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"상태 조회 실패: {e}")

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    
    await app.initialize()
    await app.start()
    app.updater.start_polling()
    
    asyncio.create_task(trading_bot_loop(app))
    asyncio.create_task(telegram_heartbeat_job(app))
    
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        app.stop()

if __name__ == '__main__':
    asyncio.run(main())