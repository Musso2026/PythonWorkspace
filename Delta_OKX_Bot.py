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
# 2. 전역 설정 변수
# ==========================================
SYMBOL_SPOT = "DOGE/USDT"
SYMBOL_SWAP = "DOGE/USDT:USDT"

LEVERAGE = 3
TOTAL_CAPITAL = 145.0  # 원금 ($)

# 펀딩비 설정 (%)
TARGET_FUNDING_RATE = 0.0100   # 진입 목표 펀딩비 (0.01%)
MIN_FUNDING_LIMIT = 0.0100     # 최저 하한 펀딩비 (0.01%)
EXIT_FUNDING_RATE = 0.0050     # 청산 펀딩비 (0.005%)

has_position = False
last_update_id = None

# ==========================================
# 3. 텔레그램 연동 함수
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
                status_msg = (
                    f"📊 [봇 현재 상태 보고]\n"
                    f"- 레버리지: {LEVERAGE}x\n"
                    f"- 목표 펀딩비: {TARGET_FUNDING_RATE:.4f}%\n"
                    f"- 최저 하한 펀딩비: {MIN_FUNDING_LIMIT:.4f}%\n"
                    f"- 청산 펀딩비: {EXIT_FUNDING_RATE:.4f}%\n"
                    f"- 현재 포지션: {'보유 중' if has_position else '미보유'}"
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
# 4. 리스크 검증 함수 (오류 보완 핵심 부분)
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
# 5. 메인 자동매매 루프
# ==========================================
def main():
    global has_position
    
    delete_webhook()
    send_telegram_msg(f"🤖 OKX 실전 자동매매 봇 가동 시작 (원금: ${TOTAL_CAPITAL} USDT)")

    while True:
        try:
            # 텔레그램 명령어 수신 체크
            handle_telegram_commands()

            # 시세 및 펀딩비 조회
            spot_ticker = exchange.fetch_ticker(SYMBOL_SPOT)
            swap_ticker = exchange.fetch_ticker(SYMBOL_SWAP)
            funding_info = exchange.fetch_funding_rate(SYMBOL_SWAP)

            spot_price = spot_ticker['last']
            swap_price = swap_ticker['last']
            funding_rate = funding_info['fundingRate'] * 100  # 퍼센트 변환

            logging.info(
                f"[DOGE 감시 중] 레버리지: {LEVERAGE}x | 현물: ${spot_price:.4f} | 선물: ${swap_price:.4f} | "
                f"현재 펀딩비: {funding_rate:.4f}% (목표: {TARGET_FUNDING_RATE:.4f}%, 최저하한: {MIN_FUNDING_LIMIT:.4f}%) | "
                f"포지션: {'보유 중' if has_position else '미보유'}"
            )

            # ----------------------------------
            # 진입 조건 검샤
            # ----------------------------------
            if not has_position and funding_rate >= TARGET_FUNDING_RATE:
                logging.info(f"🚀 DOGE 진입 조건 충족! (현재 펀딩비: {funding_rate:.4f}%)")

                # ★ [수정 보완 포인트] 반환값 3개(is_valid, reason, details)를 정확히 언팩함 ★
                try:
                    is_valid, reason, details = validate_risk(spot_price, swap_price, funding_rate)
                except Exception as unpack_err:
                    logging.error(f"리스크 검증 언팩 실패: {unpack_err}")
                    is_valid = False
                    reason = "언팩 오류 발생"

                if is_valid:
                    logging.info("✅ 리스크 검증 통과! 매수/숏 포지션 진입을 시도합니다.")
                    # TODO: 실제 주문 실행 로직 (현물 매수 & 선물 숏)
                    # has_position = True
                    # send_telegram_msg("🚀 DOGE 델타 뉴트럴 포지션 진입 완료!")
                else:
                    logging.error(f"리스크 검증 중 오류: {reason}")

            # ----------------------------------
            # 청산 조건 검사
            # ----------------------------------
            elif has_position and funding_rate <= EXIT_FUNDING_RATE:
                logging.info(f"💡 DOGE 청산 조건 충족! (현재 펀딩비: {funding_rate:.4f}%)")
                # TODO: 실제 청산 로직 (선물 숏 청산 & 현물 매도)
                # has_position = False
                # send_telegram_msg("💡 DOGE 포지션 청산 완료!")

        except Exception as e:
            logging.error(f"루프 실행 중 예외 발생: {e}")

        time.sleep(5)

if __name__ == "__main__":
    main()