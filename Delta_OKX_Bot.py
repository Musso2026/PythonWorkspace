import ccxt
import time
import requests
import logging
from datetime import datetime

# ==========================================
# ⚙️ 사용자 설정 영역 (본인 정보로 수정하세요)
# ==========================================
OKX_API_KEY = "YOUR_OKX_API_KEY"
OKX_SECRET_KEY = "YOUR_OKX_SECRET_KEY"
OKX_PASSPHRASE = "YOUR_OKX_PASSPHRASE"

TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"

# 봇 기본 매매 설정
TARGET_FUNDING_RATE = 0.0001   # 목표 펀딩비 (0.01%)
EXIT_FUNDING_RATE = 0.00005     # 청산 펀딩비 (0.005%)
LEVERAGE = 3                   # 선물 레버리지 (3배)
MIN_PRICE_DIFF = -0.015        # 허용 괴리율 하한선 (-1.5%)
CHECK_INTERVAL = 300           # 펀딩비 스캔 주기 (300초 = 5분)

# 진입 대금 (0으로 설정 시 USDT 잔고의 85% 자동 계산)
TARGET_TRADE_AMOUNT = 0        

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ==========================================
# 🛠️ OKX API 거래소 초기화
# ==========================================
exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET_KEY,
    'password': OKX_PASSPHRASE,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'  # 기본 타입을 무기한 선물로 지정
    }
})

# ==========================================
# 📲 텔레그램 알림 및 상태 관리
# ==========================================
def send_telegram_msg(message):
    """텔레그램 메시지 전송"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"텔레그램 메시지 전송 실패: {e}")

def get_telegram_updates(last_update_id):
    """텔레그램 사용자 명령어 수신"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 1}
        res = requests.get(url, params=params, timeout=5).json()
        if res.get("ok"):
            return res.get("result", [])
    except Exception as e:
        pass
    return []

# ==========================================
# 📊 OKX 펀딩비 및 시세 스캔
# ==========================================
def fetch_top_funding_coin():
    """모든 OKX 무기한 선물 중 펀딩비가 가장 높은 코인 탐색"""
    try:
        tickers = exchange.fetch_tickers()
        swap_tickers = [symbol for symbol in tickers if symbol.endswith('/USDT:USDT')]
        
        best_coin = None
        max_rate = -999.0
        best_data = {}

        for swap_symbol in swap_tickers:
            try:
                funding_info = exchange.fetch_funding_rate(swap_symbol)
                rate = funding_info.get('fundingRate', 0)
                
                if rate > max_rate:
                    base_currency = swap_symbol.split('/')[0]
                    spot_symbol = f"{base_currency}/USDT"
                    
                    # 현물 시세 조회
                    spot_ticker = tickers.get(spot_symbol)
                    if not spot_ticker:
                        continue
                        
                    spot_price = spot_ticker.get('last')
                    swap_price = tickers[swap_symbol].get('last')

                    if not spot_price or not swap_price:
                        continue

                    max_rate = rate
                    best_coin = base_currency
                    best_data = {
                        'coin': base_currency,
                        'spot_symbol': spot_symbol,
                        'swap_symbol': swap_symbol,
                        'funding_rate': rate,
                        'spot_price': spot_price,
                        'swap_price': swap_price
                    }
            except Exception:
                continue

        return best_data
    except Exception as e:
        logging.error(f"펀딩비 스캔 중 오류: {e}")
        return None

# ==========================================
# 🛡️ 진입 / 청산 검증 로직
# ==========================================
def validate_entry(data):
    """진입 리스크 검증 (괴리율 검사)"""
    spot_price = data['spot_price']
    swap_price = data['swap_price']
    diff_percent = (swap_price - spot_price) / spot_price * 100

    if diff_percent < (MIN_PRICE_DIFF * 100):
        logging.warning(f"⚠️ 괴리율 과다 ({diff_percent:.2f}%): 진입 보류")
        return False
    return True

# ==========================================
# 🚀 주문 실행 (오류 자동 대응 적용)
# ==========================================
def execute_entry(data):
    """실제 주문 실행 (마진 부족 및 tdMode 예외 자동 처리)"""
    try:
        coin = data['coin']
        spot_symbol = data['spot_symbol']
        swap_symbol = data['swap_symbol']
        spot_price = data['spot_price']

        # 마켓 메타데이터 로드
        markets = exchange.load_markets()
        swap_market = markets.get(swap_symbol, {})
        contract_size = float(swap_market.get('contractSize', 1.0))

        # 1. 실시간 USDT 가능 잔고 조회
        balance = exchange.fetch_balance({'type': 'trading'})
        usdt_free = float(balance.get('USDT', {}).get('free', 0))

        # 2. 거래 자본금(trade_capital) 결정 (안전 마진을 위해 잔고의 85% 사용)
        if TARGET_TRADE_AMOUNT and TARGET_TRADE_AMOUNT > 0:
            trade_capital = TARGET_TRADE_AMOUNT
        else:
            trade_capital = usdt_free * 0.85

        # 최소 1계약 필요 금액 체크
        min_required_usd = spot_price * contract_size
        if usdt_free < min_required_usd:
            logging.error(f"❌ 잔고 부족: 최소 1계약 필요금액(${min_required_usd:.2f}) > 보유잔고(${usdt_free:.2f})")
            send_telegram_msg(f"❌ 진입 실패: 잔고 부족 (필요: ${min_required_usd:.2f} / 보유: ${usdt_free:.2f})")
            return False

        if trade_capital > usdt_free:
            trade_capital = usdt_free * 0.85

        # 3. 계약 수량 계산
        raw_amount = trade_capital / spot_price
        swap_contracts = int(raw_amount / contract_size)
        if swap_contracts < 1:
            swap_contracts = 1

        spot_amount = swap_contracts * contract_size

        # 4. 레버리지 설정 (OKX 교차 모드)
        try:
            exchange.set_leverage(LEVERAGE, swap_symbol, params={'marginMode': 'cross'})
        except Exception as e:
            logging.warning(f"레버리지 설정 참고: {e}")

        # 5. 선물 숏(Sell) 진입 (마진 부족 시 계약 수량 자동 차감 재시도)
        swap_order = None
        try:
            swap_order = exchange.create_market_sell_order(
                swap_symbol, 
                swap_contracts, 
                params={'tdMode': 'cross'}
            )
        except Exception as order_err:
            err_msg = str(order_err)
            # 마진 부족(51008) 에러 발생 시 수량 1계약 차감 후 재시도
            if "51008" in err_msg or "available margin" in err_msg.lower():
                if swap_contracts > 1:
                    swap_contracts -= 1
                    spot_amount = swap_contracts * contract_size
                    logging.info(f"🔄 마진 여유 확보를 위해 {swap_contracts}계약으로 재시도 중...")
                    swap_order = exchange.create_market_sell_order(
                        swap_symbol, 
                        swap_contracts, 
                        params={'tdMode': 'cross'}
                    )
                else:
                    raise order_err
            elif "tdMode" in err_msg:
                swap_order = exchange.create_market_sell_order(swap_symbol, swap_contracts)
            else:
                raise order_err

        # 6. 현물 시장가 매수
        spot_order = exchange.create_market_buy_order(spot_symbol, spot_amount)

        used_usdt = spot_amount * spot_price
        logging.info(f"✅ {coin} 실제 포지션 진입 성공! (선물: {swap_contracts}계약 / 현물: {spot_amount} {coin} / 약 ${used_usdt:.2f})")
        send_telegram_msg(
            f"🚀 [{coin}] 델타 뉴트럴 포지션 진입 완료!\n"
            f"- 펀딩비: {data['funding_rate']*100:.4f}%\n"
            f"- 진입 수량: {spot_amount} {coin} ({swap_contracts} 계약)\n"
            f"- 사용 금액: 약 ${used_usdt:.2f}"
        )
        return True
    except Exception as e:
        logging.error(f"❌ 실제 주문 실행 중 오류 발생: {e}")
        send_telegram_msg(f"❌ 주문 실행 실패: {e}")
        return False

def execute_exit(position):
    """포지션 청산 (현물 매도 + 선물 숏 청산/매수)"""
    try:
        coin = position['coin']
        spot_symbol = position['spot_symbol']
        swap_symbol = position['swap_symbol']

        # 1. 현물 전량 매도
        balance = exchange.fetch_balance()
        spot_amount = float(balance.get(coin, {}).get('free', 0))
        if spot_amount > 0:
            exchange.create_market_sell_order(spot_symbol, spot_amount)

        # 2. 선물 숏 포지션 청산 (매수)
        positions = exchange.fetch_positions([swap_symbol])
        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts > 0:
                try:
                    exchange.create_market_buy_order(
                        swap_symbol, 
                        contracts, 
                        params={'tdMode': 'cross'}
                    )
                except Exception:
                    exchange.create_market_buy_order(swap_symbol, contracts)

        logging.info(f"🧹 {coin} 포지션 전량 청산 완료!")
        send_telegram_msg(f"🧹 [{coin}] 펀딩비 하락으로 인한 델타 뉴트럴 포지션 전량 청산 완료!")
        return True
    except Exception as e:
        logging.error(f"❌ 청산 실행 중 오류 발생: {e}")
        send_telegram_msg(f"❌ 청산 실패: {e}")
        return False

# ==========================================
# 🔄 메인 루프 (자동 매매)
# ==========================================
def main():
    current_position = None  # 현재 보유 포지션 데이터
    last_update_id = 0
    global TARGET_TRADE_AMOUNT

    send_telegram_msg("🤖 OKX 델타 뉴트럴 봇이 성공적으로 시작되었습니다!")

    while True:
        try:
            # 1. 텔레그램 원격 명령어 수신
            updates = get_telegram_updates(last_update_id)
            for update in updates:
                last_update_id = update['update_id']
                msg = update.get('message', {}).get('text', '')
                
                if msg == '/status':
                    if current_position:
                        send_telegram_msg(f"📌 [현재 포지션 보유 중]\n코인: {current_position['coin']}\n진입 펀딩비: {current_position['funding_rate']*100:.4f}%")
                    else:
                        send_telegram_msg("📌 [포지션 미보유] 조건 충족 코인을 탐색 중입니다.")
                elif msg.startswith('/setamount'):
                    try:
                        val = float(msg.split()[1])
                        TARGET_TRADE_AMOUNT = val
                        send_telegram_msg(f"⚙️ 진입 대금이 ${TARGET_TRADE_AMOUNT}로 변경되었습니다.")
                    except:
                        send_telegram_msg("⚠️ 올바른 형식: /setamount 100")
                elif msg == '/exit':
                    if current_position:
                        if execute_exit(current_position):
                            current_position = None
                    else:
                        send_telegram_msg("⚠️ 청산할 포지션이 없습니다.")

            # 2. 보유 포지션 관리 및 청산 조건 감시
            if current_position:
                funding_info = exchange.fetch_funding_rate(current_position['swap_symbol'])
                current_rate = funding_info.get('fundingRate', 0)
                
                logging.info(f"[{current_position['coin']}] 보유 중 | 현재 펀딩비: {current_rate*100:.4f}% (청산 목표: {EXIT_FUNDING_RATE*100:.4f}%)")
                
                if current_rate <= EXIT_FUNDING_RATE:
                    logging.info("📉 펀딩비가 청산 목표치 이하로 하락하여 청산을 시도합니다.")
                    if execute_exit(current_position):
                        current_position = None

            # 3. 신규 진입 탐색 (포지션 없을 시)
            else:
                best_data = fetch_top_funding_coin()
                if best_data:
                    coin = best_data['coin']
                    rate = best_data['funding_rate']
                    spot_p = best_data['spot_price']
                    swap_p = best_data['swap_price']
                    
                    amount_str = f"${TARGET_TRADE_AMOUNT}" if TARGET_TRADE_AMOUNT > 0 else "잔고 자동(85%)"
                    logging.info(
                        f"[최고 펀딩비 코인: {coin}] 레버리지: {LEVERAGE}x | 현물: ${spot_p} | 선물: ${swap_p} | "
                        f"현재 펀딩비: {rate*100:.4f}% (목표: {TARGET_FUNDING_RATE*100:.4f}%) | 진입대금 설정: {amount_str} | 포지션: 미보유"
                    )

                    if rate >= TARGET_FUNDING_RATE:
                        logging.info(f"🚀 {coin} 진입 조건 충족! (현재 펀딩비: {rate*100:.4f}%)")
                        if validate_entry(best_data):
                            logging.info(f"✅ {coin} 리스크 검증 통과! 매수/숏 포지션 진입을 시도합니다.")
                            if execute_entry(best_data):
                                current_position = best_data

        except Exception as e:
            logging.error(f"메인 루프 예외 발생: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()