import os
import time
import math
import logging
import asyncio
import requests
import pandas as pd
import numpy as np
from dotenv import load_dotenv
import ccxt.async_support as ccxt

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

# 전역 제어 변수
BOT_RUNNING = False
LEVERAGE = 3
MIN_FUNDING_RATE = 0.01  # 0.01%
SELECTED_COINS = []

# 자금 한도 설정 (원화 기준)
MAX_KRW_PER_COIN = 1_000_000  # 100만 원
MAX_KRW_TOTAL = 4_000_000     # 400만 원

# 초단타 그리드 세부 설정
GRID_PERCENT_SPACING = 0.003  # 0.3% 간격
GRID_LEVELS = 3               # 상/하 3단계 격자

exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET_KEY,
    'password': OKX_PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

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

# ==================== 실시간 뉴스 및 감성 분석 ====================
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
    except Exception:
        return 0

# ==================== 자율 판단 동적 스크리닝 (1~4개 유연 조절) ====================
async def dynamic_coin_screening():
    """봇이 실시간 분석하여 기회가 좋은 코인만 1개~4개 자율 선정"""
    global SELECTED_COINS
    try:
        logger.info("자율 스마트 코인 스크리닝 및 기회 분석 중...")
        markets = await exchange.load_markets()
        symbols = [symbol for symbol, market in markets.items() if market['swap'] and symbol.endswith('/USDT:USDT')]
        
        fng_score = get_fear_and_greed_index()
        scored_coins = []
        
        # 샘플링을 통한 고속 분석 (레이트 리밋 방어)
        for symbol in symbols[:25]:
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1d', limit=100)
                if len(ohlcv) < 100: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                volatility = df['close'].pct_change().std() * 100
                avg_volume = df['volume'].mean()
                
                news_score = get_news_sentiment_score(symbol)
                total_score = (volatility * 0.4) + (avg_volume / 1000000 * 0.3) + (fng_score / 10 * 0.1) + (news_score * 0.5)
                
                # 최소 유의미한 점수를 넘은 코인만 후보에 등록
                if total_score > 2.0:
                    scored_coins.append((symbol, total_score))
            except:
                continue
                
        scored_coins.sort(key=lambda x: x[1], reverse=True)
        
        # 시장 상황에 따른 자율 개수 조절 (무조건 4개를 채우지 않고, 조건이 좋은 것만 유연하게 채택: 최소 1개 ~ 최대 4개)
        if not scored_coins:
            SELECTED_COINS = ['BTC/USDT:USDT'] # 기회가 없으면 안전하게 대장주 1개만
        else:
            # 상위 코인들의 점수 편차를 분석하여 자율 결정
            top_score = scored_coins[0][1]
            adaptive_count = 1
            for i in range(1, min(4, len(scored_coins))):
                if scored_coins[i][1] >= (top_score * 0.7): # 대장주 대비 유의미한 모멘텀을 가진 경우만 포함
                    adaptive_count += 1
                else:
                    break
            SELECTED_COINS = [item[0] for item in scored_coins[:adaptive_count]]
            
        logger.info(f"🎯 실시간 자율 판단 최종 채택된 타겟 코인 개수 및 목록 ({len(SELECTED_COINS)}개): {SELECTED_COINS}")
    except Exception as e:
        logger.error(f"스크리닝 오류 발생: {e}")
        SELECTED_COINS = ['BTC/USDT:USDT', 'ETH/USDT:USDT']

# ==================== 1순위: 킬스위치 및 돌발 상황 방어 ====================
async def check_global_kill_switch():
    try:
        balance = await exchange.fetch_balance()
        margin_ratio = balance.get('info', {}).get('mrgRatio', 0)
        if margin_ratio and float(margin_ratio) > 0.8:
            logger.critical("⚠️ 증거금 위험 수준 도달! 모든 포지션 긴급 청산 및 봇 정지")
            await close_all_positions()
            global BOT_RUNNING
            BOT_RUNNING = False
            return True
    except Exception as e:
        logger.error(f"킬스위치 체크 오류: {e}")
    return False

async def close_all_positions():
    try:
        positions = await exchange.fetch_positions()
        for pos in positions:
            if float(pos['contracts']) > 0:
                symbol = pos['symbol']
                if symbol.replace(':USDT', '') not in ['ONDO/USDT']: # ONDO 수동 보유분 완벽 보호
                    side = 'sell' if pos['side'] == 'long' else 'buy'
                    await exchange.create_order(symbol, 'market', side, pos['contracts'])
        logger.info("봇 관리 포지션 긴급 청산 완료 (ONDO 자산 안전 보호됨)")
    except Exception as e:
        logger.error(f"긴급 청산 실패: {e}")

# ==================== 3순위: 초고속 비동기 그리드 스캘핑 엔진 ====================
async def execute_grid_scalping_for_coin(symbol, allocated_usdt):
    """지수 백오프 및 예외 처리가 장착된 초고속 그리드 주문 집행 함수"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 1. 시세 조회 및 기존 미체결 그리드 주문 고속 취소 병행
            ticker_task = exchange.fetch_ticker(symbol)
            orders_task = exchange.fetch_open_orders(symbol)
            ticker, open_orders = await asyncio.gather(ticker_task, orders_task)
            
            current_price = ticker['last']
            
            if open_orders:
                cancel_tasks = [exchange.cancel_order(order['id'], symbol) for order in open_orders]
                await asyncio.gather(*cancel_tasks, return_exceptions=True)

            # 2. 촘촘한 단타 그리드 지정가 계산
            per_grid_amount = (allocated_usdt * LEVERAGE) / GRID_LEVELS
            order_tasks = []
            
            for i in range(1, GRID_LEVELS + 1):
                buy_price = current_price * (1 - (GRID_PERCENT_SPACING * i))
                sell_price = current_price * (1 + (GRID_PERCENT_SPACING * i))
                
                buy_amount = per_grid_amount / buy_price
                sell_amount = per_grid_amount / sell_price
                
                # 비동기 지정가 매수/매도 주문 동시 생성 준비
                order_tasks.append(exchange.create_order(symbol, 'limit', 'buy', buy_amount, buy_price))
                order_tasks.append(exchange.create_order(symbol, 'limit', 'sell', sell_amount, sell_price))
            
            # 3. 0.00001초 극한 속도의 병렬 주문 전송 (API 레이트 리밋 보호 가드 포함)
            await asyncio.gather(*order_tasks, return_exceptions=True)
            logger.debug(f"[{symbol}] 초고속 그리드 주문 배치 완료 (기준가: {current_price})")
            return
            
        except ccxt.RateLimitExceeded:
            wait_time = (attempt + 1) * 1.5
            logger.warning(f"[{symbol}] API 레이트 리밋 감지. {wait_time}초 후 재시도...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logger.error(f"[{symbol}] 그리드 주문 집행 중 돌발 에러 (시도 {attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(1)

# ==================== 메인 통합 트레이딩 루프 (하이퍼-스피드) ====================
async def trading_bot_loop():
    global BOT_RUNNING
    logger.info("하이퍼-스피드 프로 트레이딩 봇 루프 시작됨")
    
    screening_counter = 0
    
    while True:
        if not BOT_RUNNING:
            await asyncio.sleep(2)
            continue
            
        try:
            # [1순위] 킬스위치 상시 감시
            if await check_global_kill_switch():
                break
                
            # [2순위] 주기적 또는 시장 변화에 따른 자율 코인 스크리닝 갱신 (10분에 한 번 혹은 최초)
            if not SELECTED_COINS or screening_counter >= 40:
                await dynamic_coin_screening()
                screening_counter = 0
            screening_counter += 1
                
            krw_rate = get_krw_rate()
            balance = await exchange.fetch_balance()
            usdt_free = balance['USDT']['free']
            
            max_usdt_per_coin = MAX_KRW_PER_COIN / krw_rate
            num_coins = max(1, len(SELECTED_COINS))
            allocated_usdt_per_coin = min(max_usdt_per_coin, usdt_free / num_coins)

            # 타겟 코인들에 대한 펀딩비 방어 및 초고속 그리드 실행을 병렬 처리
            tasks = []
            for symbol in SELECTED_COINS:
                try:
                    funding_info = await exchange.fetch_funding_rate(symbol)
                    funding_rate = funding_info.get('fundingRate', 0) * 100
                    
                    if funding_rate < MIN_FUNDING_RATE:
                        logger.warning(f"[{symbol}] 펀딩비({funding_rate:.4f}%)가 최소 기준 미만. 이번 회차 진입 제외.")
                        continue
                    
                    try:
                        await exchange.set_leverage(LEVERAGE, symbol)
                    except:
                        pass

                    # [3순위] 초고속 비동기 그리드 스캘핑 작업 추가
                    tasks.append(execute_grid_scalping_for_coin(symbol, allocated_usdt_per_coin))
                except Exception as e:
                    logger.error(f"[{symbol}] 전처리 과정 에러: {e}")

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # 잦은 호출 방지 및 서버 부하를 완벽히 차단하면서도 스캘핑 속도를 유지하는 최적의 딜레이 (3초~5초)
            await asyncio.sleep(4)

        except Exception as e:
            logger.error(f"메인 통합 루프 돌발 에러: {e}")
            await asyncio.sleep(3)

# ==================== 텔레그램 실시간 제어 핸들러 ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    global BOT_RUNNING
    BOT_RUNNING = True
    await update.message.reply_text("🚀 하이퍼-스피드 프로 자동매매 봇(자율 코인 개수 조절 + 극한 스캘핑 + 레이트리밋 방어)이 시작되었습니다!")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    global BOT_RUNNING
    BOT_RUNNING = False
    await update.message.reply_text("🛑 자동매매 봇이 안전하게 중지되었습니다.")

async def cmd_set_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    global LEVERAGE
    try:
        LEVERAGE = int(context.args[0])
        await update.message.reply_text(f"⚙️ 레버리지가 {LEVERAGE}x 로 변경되었습니다.")
    except:
        await update.message.reply_text("사용법: /set_leverage 3")

async def cmd_set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    global MAX_KRW_PER_COIN, MAX_KRW_TOTAL
    try:
        val = float(context.args[0])
        MAX_KRW_PER_COIN = val
        MAX_KRW_TOTAL = val * 4
        await update.message.reply_text(f"💰 코인당 최대 한도가 {MAX_KRW_PER_COIN:,.0f}원으로 변경되었습니다.")
    except:
        await update.message.reply_text("사용법: /set_limit 1000000 (단위: 원)")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID): return
    try:
        balance = await exchange.fetch_balance()
        usdt_total = balance['USDT']['total']
        usdt_free = balance['USDT']['free']
        krw_rate = get_krw_rate()
        total_krw = usdt_total * krw_rate
        fng = get_fear_and_greed_index()
        
        status_msg = (
            f"📊 **[하이퍼-스피드 봇 실시간 상태]**\n"
            f"- 상태: {'실행 중 🟢' if BOT_RUNNING else '정지 중 🔴'}\n"
            f"- 레버리지: {LEVERAGE}x\n"
            f"- 코인당 할당 한도: {MAX_KRW_PER_COIN:,.0f}원\n"
            f"- 공포탐욕지수: {fng}\n"
            f"- 총 잔고: {usdt_total:.2f} USDT (가용: {usdt_free:.2f} USDT)\n"
            f"- 자산 평가: 약 {total_krw:,.0f} 원\n"
            f"- 🎯 **현재 자율 채택된 타겟 코인 ({len(SELECTED_COINS)}개)**: {', '.join(SELECTED_COINS) if SELECTED_COINS else '분석 중'}\n"
            f"- 🔒 **보호 중인 자산**: ONDO (매매 제외)"
        )
        await update.message.reply_text(status_msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"상태 조회 실패: {e}")

async def telegram_heartbeat_job(application):
    while True:
        await asyncio.sleep(1800)
        if TELEGRAM_CHAT_ID and BOT_RUNNING:
            try:
                balance = await exchange.fetch_balance()
                usdt_total = balance['USDT']['total']
                krw_rate = get_krw_rate()
                total_krw = usdt_total * krw_rate
                
                msg = (
                    f"⏰ **[30분 정기 헬스체크]**\n"
                    f"하이퍼-스피드 그리드 봇 정상 구동 중 🟢\n"
                    f"현재 거래 중인 코인 수: {len(SELECTED_COINS)}개\n"
                    f"총 자산: {usdt_total:.2f} USDT / {total_krw:,.0f} KRW\n"
                    f"ONDO 코인 안전 보호 중 🔒"
                )
                await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"헬스체크 오류: {e}")

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("set_leverage", cmd_set_leverage))
    app.add_handler(CommandHandler("set_limit", cmd_set_limit))
    app.add_handler(CommandHandler("status", cmd_status))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    asyncio.create_task(trading_bot_loop())
    asyncio.create_task(telegram_heartbeat_job(app))
    
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await exchange.close()
        await app.stop()

if __name__ == '__main__':
    asyncio.run(main())