# ==========================================
# 📊 OKX 펀딩비 및 시세 스캔 (초고속 Direct REST API)
# ==========================================
def fetch_top_funding_coin():
    """OKX Direct REST API를 호출하여 0.3초 만에 최고 펀딩비 코인 탐색"""
    try:
        # User-Agent 헤더를 추가하여 OKX 서버의 차단 회피
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        url = "https://www.okx.com/api/v5/public/funding-rate-current?instType=SWAP"
        res = requests.get(url, headers=headers, timeout=3).json()
        
        if res.get("code") != "0" or not res.get("data"):
            logging.error(f"OKX 펀딩비 API 응답 에러: {res}")
            return None

        # 2. 가장 펀딩비가 높은 코인(SWAP) 탐색
        best_inst_id = None
        max_rate = -999.0

        for item in res["data"]:
            inst_id = item.get("instId", "")
            # USDT 마진 선물만 대상 (예: BTC-USDT-SWAP)
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            
            funding_rate = float(item.get("fundingRate", 0))
            if funding_rate > max_rate:
                max_rate = funding_rate
                best_inst_id = inst_id

        if not best_inst_id:
            return None

        # 3. 코인 이름 추출 (예: BTC-USDT-SWAP -> BTC)
        base_currency = best_inst_id.split("-")[0]
        spot_symbol = f"{base_currency}/USDT"
        swap_symbol = f"{base_currency}/USDT:USDT"

        # 4. 해당 코인의 현물/선물 현재가 정밀 조회
        spot_ticker = exchange.fetch_ticker(spot_symbol)
        swap_ticker = exchange.fetch_ticker(swap_symbol)

        spot_price = spot_ticker.get('last')
        swap_price = swap_ticker.get('last')

        if not spot_price or not swap_price:
            return None

        return {
            'coin': base_currency,
            'spot_symbol': spot_symbol,
            'swap_symbol': swap_symbol,
            'funding_rate': max_rate,
            'spot_price': spot_price,
            'swap_price': swap_price
        }

    except Exception as e:
        logging.error(f"초고속 펀딩비 스캔 중 오류: {e}")
        return None