import os
import time
import logging
import ccxt
import requests
from dotenv import load_dotenv

# dotenv 환경 변수 로드
load_dotenv()

# ==========================================
# 1. 로깅 및 환경 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# CCXT OKX 객체 생성
exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET_KEY,
    'password': OKX_PASSPHRASE,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    }
})

# ==========================================
# 2. 전역 설정 변수 (기존 구조 100% 유지)
# ==========================================
# 감시할 주요 멀티 코인 리스트 (원하는 코인을 추가/삭제할 수 있습니다)
MONITOR_COINS = ["DOGE", "XRP", "BTC", "ETH", "SOL", "ADA", "AVAX", "SUI"]

LEVERAGE = 3
TOTAL_CAPITAL = 145.0  # 원금 ($)

# 펀딩비 설정 (%)
TARGET_FUNDING_RATE = 0.0100   # 진입 목표 펀딩비 (0.01%)
MIN_FUNDING_LIMIT = 0.0100     # 최저 하한 펀딩비 (0.01%)
EXIT_FUNDING_RATE = 0.0050     # 청산 펀딩비 (0.005%)

has_position = False
current_position_coin = None   # 현재 포지션 보유 중인 코인
last_update_id = None

# ==========================================
# 3. 텔레그램 연동 함수 (기존 코드 100% 유지)
# ==========================================
def send_telegram_msg(message):
    """텔레그램 메시지 발송"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"텔레그램 메시지 전송 실패: {e}")

def delete_webhook():
    """Conflict 방지를 위한 웹훅 삭제"""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    try:
        requests.post(url, timeout=5)
    except Exception as e:
        logging.error(f"웹훅 삭제 실패: {e}")

def handle_telegram_commands():
    """텔레그램 명령어 처리 (/status, /setfund)"""
    global last_update_id, TARGET_FUNDING_RATE, EXIT_FUNDING_RATE, MIN_FUNDING_LIMIT
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1, "offset": last_update_id}
    
    try:
        resp = requests.get(url, params=params, timeout=3)
        data = resp.json()
        
        if not data.get("ok"):
            return

        for result in data.get("result", []):
            last_update_id = result["update_id"] + 1
            message = result.get("message", {})
            text = message.get("text", "").strip()

            if text == "/status":
                pos_str = f"보유 중 ({current_position_coin})" if has_position else "미보유"
                status_msg = (
                    f"📊 [봇 현재 상태 보고]\n"
                    f"- 레버리지: {LEVERAGE}x\n"
                    f"- 목표 펀딩비: {TARGET_FUNDING_RATE:.4f}%\n"
                    f"- 최저 하한 펀딩비: {MIN_FUNDING_LIMIT:.4f}%\n"
                    f"- 청산 펀딩비: {EXIT_FUNDING_RATE:.4f}%\n"
                    f"- 현재 포지션: {pos_str}"
                )
                send_telegram_msg(status_msg)

            elif text.startswith("/setfund"):
                parts = text.split()
                if len(parts) >= 4:
                    try:
                        TARGET_FUNDING_RATE = float(parts[1])
                        EXIT_FUNDING_RATE = float(parts[2])
                        MIN_FUNDING_LIMIT = float(parts[3])
                        send_telegram_msg(
                            f"✅ 펀딩비 설정 변경 완료!\n"
                            f"- 목표: {TARGET_FUNDING_RATE:.4f}%\n"
                            f"- 청산: {EXIT_FUNDING_RATE:.4f}%\n"
                            f"- 최저하한: {MIN_FUNDING_LIMIT:.4f}%"
                        )
                    except ValueError:
                        send_telegram_msg("❌ 올바른 숫자를 입력하세요. 예: /setfund 0.01 0.005 0.005")
                else:
                    send_telegram_msg("💡 사용법: /setfund [목표] [청산] [최저하한]\n예: /setfund 0.01 0.005 0.005")
    except Exception as e:
        pass

# ==========================================
# 4. 리스크 검증 함수 (기존 코드 100% 유지)
# ==========================================
def validate_risk(spot_price, swap_price, funding_rate):
    """
    리스크 검증을 수행하고 (is_valid, reason, details) 3개의 값을 반환함.
    """
    try:
        # 괴리율 검증 (현물과 선물 가격 차이 1% 이상 시 리스크 거부)
        price_diff_pct = abs(spot_price - swap_price) / spot_price * 100
        if price_diff_pct > 1.0:
            return False, f"현/선물 괴리율 초과 ({price_diff_pct:.2f}%)", {"diff": price_diff_pct}

        # 펀딩비 최저 하한선 검증
        if funding_rate < MIN_FUNDING_LIMIT:
            return False, f"펀딩비 최저 하한 미달 ({funding_rate:.4f}% < {MIN_FUNDING_LIMIT:.4f}%)", {"funding": funding_rate}

        return True, "리스크 검증 통과", {"diff": price_diff_pct, "funding": funding_rate}
    except Exception as e:
        return False, f"검증 내부 에러: {str(e)}", {}

# ==========================================
# 5. 멀티 코인 스캔 및 매매 실행 함수 (잔고 부족 방지 보완)
# ==========================================
def get_best_opportunity():
    """모니터링 대상 코인 중 가장 높은 펀딩비를 가진 코인을 탐색"""
    best_coin = None
    max_rate = -999.0
    best_data = None

    for coin in MONITOR_COINS:
        try:
            symbol_spot = f"{coin}/USDT"
            symbol_swap = f"{coin}/USDT:USDT"

            spot_ticker = exchange.fetch_ticker(symbol_spot)
            swap_ticker = exchange.fetch_ticker(symbol_swap)
            funding_info = exchange.fetch_funding_rate(symbol_swap)

            spot_price = spot_ticker['last']
            swap_price = swap_ticker['last']
            funding_rate = funding_info['fundingRate'] * 100

            if funding_rate > max_rate:
                max_rate = funding_rate
                best_coin = coin
                best_data = {
                    "coin": coin,
                    "spot_symbol": symbol_spot,
                    "swap_symbol": symbol_swap,
                    "spot_price": spot_price,
                    "swap_price": swap_price,
                    "funding_rate": funding_rate
                }
        except Exception as e:
            continue

    return best_data

def execute_entry(data):
    """실제 주문 실행 (잔고 부족 에러 및 tdMode 완벽 반영)"""
    try:
        coin = data['coin']
        spot_symbol = data['spot_symbol']
        swap_symbol = data['swap_symbol']
        spot_price = data['spot_price']

        # 1. 레버리지 설정 (Cross 모드 기본)
        try:
            exchange.set_leverage(LEVERAGE, swap_symbol, params={'mgnMode': 'cross'})
        except Exception as e:
            logging.warning(f"레버리지 설정 경고: {e}")

        # 2. 진입 수량 계산 (수수료 및 오차 방지를 위해 0.5% 여유분 차감)
        trade_capital = (TOTAL_CAPITAL / 2) * 0.995
        amount = trade_capital / spot_price

        # 3. 현물 시장가 매수
        spot_order = exchange.create_market_buy_order(spot_symbol, amount)
        
        # 4. 선물 시장가 숏(Sell) 진입 (Cross 모드)
        swap_order = exchange.create_market_sell_order(
            swap_symbol, 
            amount, 
            params={
                'tdMode': 'cross'
            }
        )

        logging.info(f"✅ {coin} 실제 포지션 진입 성공! (수량: {amount:.4f})")
        send_telegram_msg(f"🚀 [{coin}] 델타 뉴트럴 포지션 진입 완료!\n- 펀딩비: {data['funding_rate']:.4f}%\n- 수량: {amount:.4f}")
        return True
    except Exception as e:
        # Cross 모드 실패 시 Isolated(격리)로 재시도
        try:
            logging.warning("Cross 모드 실패, Isolated(격리) 모드로 재시도합니다.")
            swap_order = exchange.create_market_sell_order(
                swap_symbol, 
                amount, 
                params={
                    'tdMode': 'isolated'
                }
            )
            logging.info(f"✅ {coin} 실제 포지션 진입 성공 (Isolated)! (수량: {amount:.4f})")
            send_telegram_msg(f"🚀 [{coin}] 델타 뉴트럴 포지션 진입 완료!\n- 펀딩비: {data['funding_rate']:.4f}%\n- 수량: {amount:.4f}")
            return True
        except Exception as retry_err:
            logging.error(f"❌ 실제 주문 실행 중 오류 발생: {retry_err}")
            send_telegram_msg(f"❌ 주문 실행 실패: {retry_err}")
            return False

def execute_exit(coin):
    """실제 포지션 청산 (선물 숏 닫기 & 현물 매도)"""
    try:
        spot_symbol = f"{coin}/USDT"
        swap_symbol = f"{coin}/USDT:USDT"

        # 1. 현물 잔고 조회 후 매도
        spot_balance = exchange.fetch_balance({'type': 'spot'})
        spot_amount = spot_balance['total'].get(coin, 0)

        if spot_amount > 0:
            exchange.create_market_sell_order(spot_symbol, spot_amount)

        # 2. 선물 포지션 잔고 조회 후 청산(Buy)
        positions = exchange.fetch_positions([swap_symbol])
        for pos in positions:
            pos_amount = float(pos.get('contracts', 0))
            mgn_mode = pos.get('marginMode', 'cross')
            if pos_amount > 0:
                exchange.create_market_buy_order(
                    swap_symbol, 
                    pos_amount, 
                    params={
                        'tdMode': mgn_mode
                    }
                )

        logging.info(f"💡 {coin} 포지션 완벽 청산 완료!")
        send_telegram_msg(f"💡 [{coin}] 포지션 청산 완료!")
        return True
    except Exception as e:
        logging.error(f"❌ 청산 중 오류 발생: {e}")
        send_telegram_msg(f"❌ 청산 실패: {e}")
        return False

# ==========================================
# 6. 메인 자동매매 루프 (기존 코드 100% 유지)
# ==========================================
def main():
    global has_position, current_position_coin
    
    delete_webhook()
    send_telegram_msg(f"🤖 OKX 멀티코인 자동매매 봇 가동 시작 (원금: ${TOTAL_CAPITAL} USDT)")

    while True:
        try:
            # 텔레그램 명령어 수신 체크
            handle_telegram_commands()

            # 최고 펀딩비 코인 검색
            best_opportunity = get_best_opportunity()

            if best_opportunity:
                coin = best_opportunity['coin']
                spot_price = best_opportunity['spot_price']
                swap_price = best_opportunity['swap_price']
                funding_rate = best_opportunity['funding_rate']

                logging.info(
                    f"[최고 펀딩비 코인: {coin}] 레버리지: {LEVERAGE}x | 현물: ${spot_price:.4f} | 선물: ${swap_price:.4f} | "
                    f"현재 펀딩비: {funding_rate:.4f}% (목표: {TARGET_FUNDING_RATE:.4f}%, 최저하한: {MIN_FUNDING_LIMIT:.4f}%) | "
                    f"포지션: {'보유 중 (' + str(current_position_coin) + ')' if has_position else '미보유'}"
                )

                # ----------------------------------
                # 진입 조건 검사 (미보유 시 최고 조건 코인 진입)
                # ----------------------------------
                if not has_position and funding_rate >= TARGET_FUNDING_RATE:
                    logging.info(f"🚀 {coin} 진입 조건 충족! (현재 펀딩비: {funding_rate:.4f}%)")

                    try:
                        is_valid, reason, details = validate_risk(spot_price, swap_price, funding_rate)
                    except Exception as unpack_err:
                        logging.error(f"리스크 검증 언팩 실패: {unpack_err}")
                        is_valid = False
                        reason = "언팩 오류 발생"

                    if is_valid:
                        logging.info(f"✅ {coin} 리스크 검증 통과! 매수/숏 포지션 진입을 시도합니다.")
                        if execute_entry(best_opportunity):
                            has_position = True
                            current_position_coin = coin
                    else:
                        logging.error(f"리스크 검증 중 오류: {reason}")

                # ----------------------------------
                # 청산 조건 검사 (보유 중인 코인의 펀딩비 체크)
                # ----------------------------------
                elif has_position:
                    # 현재 보유 중인 코인의 펀딩비 가져오기
                    pos_swap_symbol = f"{current_position_coin}/USDT:USDT"
                    pos_funding_info = exchange.fetch_funding_rate(pos_swap_symbol)
                    pos_funding_rate = pos_funding_info['fundingRate'] * 100

                    if pos_funding_rate <= EXIT_FUNDING_RATE:
                        logging.info(f"💡 {current_position_coin} 청산 조건 충족! (현재 펀딩비: {pos_funding_rate:.4f}%)")
                        if execute_exit(current_position_coin):
                            has_position = False
                            current_position_coin = None

        except Exception as e:
            logging.error(f"루프 실행 중 예외 발생: {e}")

        time.sleep(5)

if __name__ == "__main__":
    main()