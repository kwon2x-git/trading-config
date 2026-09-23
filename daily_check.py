"""
daily_check.py
================
매일 실행하면:
  1) watchlist.json에 등록된 종목의 현재가를 조회해 손절선/승격선 근접·터치 여부 확인
  2) 실적 발표 임박 종목 확인 (향후 N일 이내)
  3) 금요일이면 "ATR 손절선 재계산 필요" 정기 리마인더
  4) 보유/관심 종목 최신 뉴스 헤드라인 수집
  5) 위 내용을 텔레그램 + 이메일로 동시 발송

⚠️ 이 스크립트는 "확인이 필요하다"는 신호만 줍니다.
   손절/승격 여부의 최종 판단과 집행은 반드시 Claude 대화창(momentum-trading-rules 스킬)에서
   원칙에 따라 재계산 후 결정하세요. 이 스크립트의 숫자는 참고용입니다.

필요 라이브러리: requests  (pip install requests)
"""

import json
import os
import csv
import io
import sys
import datetime
import time
import smtplib
from email.mime.text import MIMEText
import requests

try:
    from deep_translator import GoogleTranslator
    _translator = GoogleTranslator(source="en", target="ko")
except Exception:
    _translator = None

_TRANSLATE_MIN_INTERVAL = 0.3  # 초 — Google 무료 엔드포인트 "초당 5회" 제한 회피용 최소 호출 간격
_last_translate_time = 0.0


def translate_ko(text: str) -> str:
    """
    뉴스 제목을 한글로 번역 (Google, deep_translator).
    2026-09-14: 호출 사이에 최소 간격(_TRANSLATE_MIN_INTERVAL)을 둬서
    "Server Error: too many requests" (초당 5회 제한) 재발을 방지.
    실패하거나 라이브러리 없으면 원문 그대로 반환.
    """
    global _last_translate_time
    if not _translator or not text:
        return text
    elapsed = time.time() - _last_translate_time
    if elapsed < _TRANSLATE_MIN_INTERVAL:
        time.sleep(_TRANSLATE_MIN_INTERVAL - elapsed)
    try:
        result = _translator.translate(text)
        _last_translate_time = time.time()
        return result
    except Exception as e:
        _last_translate_time = time.time()
        print(f"[번역 실패] {e}")
        return text

# ============================================================
# 설정 — 환경변수로 채우는 것을 권장 (코드에 직접 쓰지 말 것)
# ============================================================

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")

TOSS_CLIENT_ID = os.environ.get("TOSS_CLIENT_ID", "")
TOSS_CLIENT_SECRET = os.environ.get("TOSS_CLIENT_SECRET", "")
TOSS_BASE = "https://openapi.tossinvest.com"
_toss_token_cache = {"token": None, "expires_at": 0}

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_BASE = "https://api.binance.com"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_FROM)
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
WATCHLIST_GIST_URL = "https://gist.githubusercontent.com/kwon2x-git/fe73341e32ade12b81c546619118f36d/raw/watchlist.json"
ALERT_STATE_FILE = os.path.join(os.path.dirname(__file__), "alert_state.json")


def load_alert_state():
    try:
        with open(ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_alert_state(state: dict):
    try:
        with open(ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[알림이력 저장 실패] {e}")


def get_cached_close(state: dict, symbol: str):
    return state.get("_last_close", {}).get(symbol)


def set_cached_close(state: dict, symbol: str, price: float):
    state.setdefault("_last_close", {})[symbol] = price

# 손절/승격선에 "근접"으로 간주할 여유폭 (%) — 터치 전 사전 경보용
PROXIMITY_PCT = 1.5

# 실적 발표를 "임박"으로 볼 기준 (일)
EARNINGS_LOOKAHEAD_DAYS = 7

def alert_priority(alert_text: str) -> int:
    if alert_text.startswith("🔴"):
        return 0
    if alert_text.startswith("🟡"):
        return 1
    if alert_text.startswith("⚪"):
        return 2
    return 3


AV_BASE = "https://www.alphavantage.co/query"
AV_USAGE_FILE = os.path.join(os.path.dirname(__file__), "av_usage.json")
AV_DAILY_LIMIT = 25  # 무료 플랜 일일 한도
AV_WARN_AT = 20      # 이 횟수부터 콘솔에 경고 표시


def _load_av_usage() -> dict:
    try:
        with open(AV_USAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_av_usage(state: dict):
    try:
        with open(AV_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[AV 사용량 저장 실패] {e}")


def av_request_allowed(purpose: str) -> bool:
    """
    Alpha Vantage 호출 전 오늘 누적 호출 수를 확인·기록하고, 호출 가능 여부를 반환.
    2026-09-15: AV 무료 키는 로그인 대시보드가 없어 외부에서 잔여 한도를 확인할 수
    없으므로, 스크립트가 자체적으로 날짜별 호출 횟수를 기록해 한도 초과를 사전에
    막는다(무의미한 요청으로 429/한도메시지만 받는 것을 방지).
    날짜가 바뀌면 카운트는 자동으로 0부터 다시 시작(리셋 시각을 몰라도 자정 기준
    로컬 날짜 변경으로 대체 — 실제 AV 리셋 시각과 다를 수 있음에 유의).
    """
    today = datetime.date.today().isoformat()
    state = _load_av_usage()
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    count = state.get("count", 0)
    if count >= AV_DAILY_LIMIT:
        print(f"[AV 호출 차단] {purpose}: 오늘(로컬 날짜 기준) 누적 {count}회로 한도({AV_DAILY_LIMIT}) 도달 — 호출 생략")
        return False
    state["count"] = count + 1
    _save_av_usage(state)
    if state["count"] >= AV_WARN_AT:
        print(f"[AV 한도 근접] {purpose}: 오늘 누적 {state['count']}/{AV_DAILY_LIMIT}회")
    return True


def get_run_context():
    """
    실행 phase('close'/'open')와 시장 region('us'/'kr') 판별.
    작업 스케줄러 인수: us_close / us_open / kr_close / kr_open (우선), 없으면 시각으로 추정.
    """
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        parts = arg.split("_")
        if len(parts) == 2 and parts[0] in ("us", "kr") and parts[1] in ("close", "open"):
            return parts[1], parts[0]
        if arg in ("close", "open"):
            return arg, None

    now = datetime.datetime.now()
    hm = now.hour * 60 + now.minute
    windows = [
        ((4 * 60 + 30, 6 * 60), "close", "us"),      # 05:10 전후
        ((21 * 60 + 30, 23 * 60), "open", "us"),     # 22:20 전후
        ((8 * 60, 9 * 60 + 30), "open", "kr"),       # 08:50 전후
        ((15 * 60, 16 * 60 + 30), "close", "kr"),    # 15:40 전후
    ]
    for (start, end), phase, region in windows:
        if start <= hm <= end:
            return phase, region
    return "open", None


# ============================================================
# 데이터 조회
# ============================================================

def load_watchlist_data():
    """1순위: GitHub Gist에서 최신 watchlist 전체(dict) 다운로드. 실패 시 로컬 파일로 폴백."""
    try:
        r = requests.get(WATCHLIST_GIST_URL, timeout=10)
        if r.status_code == 200:
            return r.json()
        print(f"[Gist 다운로드 실패] {r.status_code}, 로컬 파일로 대체")
    except Exception as e:
        print(f"[Gist 다운로드 오류] {e}, 로컬 파일로 대체")
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_toss_token():
    """토스 OAuth2 액세스 토큰 발급 (24시간 캐싱, 만료 5분 전 자동 재발급)"""
    now = time.time()
    if _toss_token_cache["token"] and now < _toss_token_cache["expires_at"] - 300:
        return _toss_token_cache["token"]
    if not TOSS_CLIENT_ID or not TOSS_CLIENT_SECRET:
        return None
    try:
        r = requests.post(
            f"{TOSS_BASE}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": TOSS_CLIENT_ID,
                "client_secret": TOSS_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[토스 토큰발급 실패] {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        _toss_token_cache["token"] = data["access_token"]
        _toss_token_cache["expires_at"] = now + data.get("expires_in", 86400)
        return _toss_token_cache["token"]
    except Exception as e:
        print(f"[토스 토큰발급 오류] {e}")
        return None


def get_price_toss(symbol: str):
    """토스 API로 현재가 조회 (국내/미국 종목 공통 지원)"""
    token = get_toss_token()
    if not token:
        return None
    try:
        r = requests.get(
            f"{TOSS_BASE}/api/v1/prices",
            params={"symbols": symbol},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[토스 가격조회 실패] {symbol} {r.status_code}: {r.text[:200]}")
            return None
        result = r.json().get("result", [])
        if not result:
            return None
        return float(result[0]["lastPrice"])
    except Exception as e:
        print(f"[토스 가격조회 오류] {symbol}: {e}")
        return None


def get_toss_exchange_rate():
    """USD/KRW 환율 조회 (토스, 1분 주기 갱신, 참고용 표시환율). 실패 시 None."""
    token = get_toss_token()
    if not token:
        return None
    try:
        r = requests.get(
            f"{TOSS_BASE}/api/v1/exchange-rate",
            params={"baseCurrency": "USD", "quoteCurrency": "KRW"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[환율조회 실패] {r.status_code}: {r.text[:200]}")
            return None
        return float(r.json()["result"]["rate"])
    except Exception as e:
        print(f"[환율조회 오류] {e}")
        return None


_WARNING_LABELS = {
    "LIQUIDATION_TRADING": "정리매매",
    "OVERHEATED": "단기과열",
    "INVESTMENT_WARNING": "투자경고",
    "INVESTMENT_RISK": "투자위험",
    "VI_STATIC": "VI(정적)",
    "VI_DYNAMIC": "VI(동적)",
    "VI_STATIC_AND_DYNAMIC": "VI(정적+동적)",
    "STOCK_WARRANTS": "신주인수권",
}


def get_stock_warnings(symbol: str):
    """
    종목의 매수 유의사항(정리매매/단기과열/투자경고·위험/VI 등) 조회.
    반환값: 활성 유의사항 리스트(빈 리스트 = 확인됨, 없음) / None = 조회 자체가 실패(레이트리밋 등, "없음"과 다름)
    """
    token = get_toss_token()
    if not token:
        return None
    try:
        r = requests.get(
            f"{TOSS_BASE}/api/v1/stocks/{symbol}/warnings",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 404:
            return []  # 종목 자체를 못 찾음(코드 표기 차이 등) — 유의사항 없음과 동일하게 처리
        if r.status_code != 200:
            print(f"[유의사항조회 실패] {symbol} {r.status_code}: {r.text[:200]}")
            return None
        items = r.json().get("result", [])
        return [_WARNING_LABELS.get(w["warningType"], w["warningType"]) for w in items]
    except Exception as e:
        print(f"[유의사항조회 오류] {symbol}: {e}")
        return None


def get_price_alphavantage(symbol: str):
    """GLOBAL_QUOTE로 최신가 조회 (Alpha Vantage)"""
    if not av_request_allowed(f"GLOBAL_QUOTE {symbol}"):
        return None
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHA_VANTAGE_API_KEY}
    try:
        r = requests.get(AV_BASE, params=params, timeout=10)
        payload = r.json()
        data = payload.get("Global Quote", {})
        price = data.get("05. price")
        if price is None and not data:
            notice = payload.get("Information") or payload.get("Note") or payload.get("Error Message") or payload
            print(f"[AV 응답 이상] {symbol}: Global Quote 없음 — {notice}")
        return float(price) if price else None
    except Exception as e:
        print(f"[AV 가격조회 실패] {symbol}: {e}")
        return None


def get_price_binance(symbol: str):
    """Binance 현물 현재가 조회 (심볼+USDT 페어). 공개 엔드포인트라 인증 불필요."""
    pair = f"{symbol.upper()}USDT"
    try:
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY} if BINANCE_API_KEY else {}
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": pair},
            headers=headers,
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[Binance 가격조회 실패] {pair} {r.status_code}: {r.text[:200]}")
            return None
        return float(r.json()["price"])
    except Exception as e:
        print(f"[Binance 가격조회 오류] {symbol}: {e}")
        return None


def get_regular_close(symbol: str, market: str = "stock"):
    """
    장마감(close phase) 체크 전용. 실시간가 대신 확정 일봉 종가를 사용해
    시간외(애프터마켓) 가격 혼입으로 인한 오판정을 방지한다.
    (HOOD 사례: 05:10 실시간가가 이미 시간외 하락분을 반영해 정규장 하회처럼 보였던 문제)

    2026-09-14 수정: 경과시간(hours_since_close) 기반 인덱스 추정 방식은
    "지금이 세션 종료 후 12시간 이내인가"라는 간접 추정이라 오작동(IBM 사례:
    실제 최신 확정 종가가 아닌 한 세션 이전 값을 반환)을 일으켰음.
    각 캔들의 날짜 라벨을 "지금 시점 기준으로 마지막에 완결된 세션의 날짜"와
    직접 비교해서 선택하는 방식으로 교체.
    """
    if market == "crypto":
        return get_price_binance(symbol)  # 24시간 시장이라 확정종가 개념 없음, 실시간가 사용
    token = get_toss_token()
    if token:
        try:
            r = requests.get(
                f"{TOSS_BASE}/api/v1/candles",
                params={"symbol": symbol, "interval": "1d", "count": 3, "adjusted": True},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if r.status_code == 200:
                candles = r.json().get("result", {}).get("candles", [])
                if candles:
                    # 정규장 한 세션은 한국시간 밤 22:30 ~ 다음날 새벽 05:00.
                    # 마지막으로 "완결된" 세션의 종가 날짜 라벨 = 지금이 05:00 이전이면 어제,
                    # 05:00 이후면 오늘.
                    now = datetime.datetime.now()
                    target_date = now.date()
                    if now.hour < 5:
                        target_date -= datetime.timedelta(days=1)

                    # candles는 최신순으로 정렬되어 있다고 가정.
                    # target_date 이하인 캔들 중 가장 최신(=날짜가 가장 큰) 캔들을 선택.
                    # (주말/휴장일에는 target_date와 정확히 일치하는 캔들이 없을 수 있으므로
                    #  <= 비교로 가장 가까운 과거 확정 종가를 찾는다.)
                    for candle in candles:
                        try:
                            candle_date = datetime.datetime.fromisoformat(candle["timestamp"]).date()
                        except (KeyError, ValueError) as e:
                            print(f"[캔들 날짜 파싱 실패] {symbol}: {candle} ({e})")
                            continue
                        if candle_date <= target_date:
                            return float(candle["closePrice"])

                    print(f"[종가 매칭 실패] {symbol}: target_date={target_date}에 해당하는 확정 캔들 없음, candles={candles}")
            else:
                print(f"[토스 일봉조회 실패] {symbol} {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[토스 일봉조회 오류] {symbol}: {e}")
    return get_price_alphavantage(symbol)


def get_price(symbol: str, market: str = "stock"):
    """실시간가 조회(장전 체크용). market='crypto'면 Binance, 그 외는 1순위 토스 → 2순위 Alpha Vantage"""
    if market == "crypto":
        return get_price_binance(symbol)
    price = get_price_toss(symbol)
    if price is not None:
        return price
    return get_price_alphavantage(symbol)


def get_earnings_calendar():
    """향후 3개월 실적 발표 캘린더 (CSV 형식)"""
    if not av_request_allowed("EARNINGS_CALENDAR"):
        return []
    params = {"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": ALPHA_VANTAGE_API_KEY}
    try:
        r = requests.get(AV_BASE, params=params, timeout=15)
        reader = csv.DictReader(io.StringIO(r.text))
        return list(reader)
    except Exception as e:
        print(f"[실적캘린더 조회 실패] {e}")
        return []


EARNINGS_KEYWORDS = [
    "earnings", "quarterly", "q1", "q2", "q3", "q4", "eps", "guidance",
    "beats", "misses", "revenue", "conference call", "investor day",
    "analyst day", "results", "outlook"
]


def get_news_headlines_batch(symbols: list, phase: str, limit_per_symbol: int = 3) -> dict:
    """
    2026-09-23: Alpha Vantage NEWS_SENTIMENT는 가격체크 등 다른 함수와 하루 25회 한도를
    공유해서 뉴스 차례까지 한도가 안 남는 문제가 반복됨(반복 재발). API 한도와 완전히
    무관한 야후 파이낸스 종목별 RSS로 교체(키/한도 없음, 종목당 개별 요청).

    phase='close' -> 방금 끝난 세션 구간(최근 8시간) 뉴스만
    phase='open'  -> 직전 장마감 이후~지금(최근 17시간) 뉴스만
    실적/컨퍼런스 관련 키워드가 있으면 앞에 배치하고 표시를 다르게 함.
    반환값: {symbol: [헤드라인, ...]} — 헤드라인 없는 종목은 키 자체가 없음.
    """
    if not symbols:
        return {}
    import xml.etree.ElementTree as ET
    window_hours = 8 if phase == "close" else 17
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=window_hours)
    result = {}
    for symbol in symbols:
        try:
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                print(f"[야후 뉴스 실패] {symbol} {r.status_code}")
                continue
            root = ET.fromstring(r.content)
            titles = []
            for it in root.findall(".//item"):
                title_el = it.find("title")
                if title_el is None or not title_el.text:
                    continue
                pub_el = it.find("pubDate")
                if pub_el is not None and pub_el.text:
                    try:
                        pub_dt = datetime.datetime.strptime(
                            pub_el.text, "%a, %d %b %Y %H:%M:%S %z"
                        ).astimezone(datetime.timezone.utc).replace(tzinfo=None)
                        if pub_dt < cutoff:
                            continue
                    except ValueError:
                        pass  # 날짜 파싱 실패 시 시간창 필터 없이 포함
                titles.append(title_el.text)
                if len(titles) >= limit_per_symbol:
                    break
            if titles:
                is_earn = lambda t: any(k in t.lower() for k in EARNINGS_KEYWORDS)
                result[symbol] = [f"📊 {translate_ko(t)}" if is_earn(t) else translate_ko(t) for t in titles]
        except Exception as e:
            print(f"[야후 뉴스 오류] {symbol}: {e}")
        time.sleep(0.2)
    return result


# ============================================================
# 체크 로직
# ============================================================

def check_price_thresholds(item: dict, price: float, state: dict, phase: str):
    """
    손절선 체크 전용(승격/재진입/2차추매는 check_breakout()/check_single_confirm()이 별도 처리).
    2026-09-16 재설계(사용자 지정 규칙):
    - 근접(1.5% 이내)은 모든 손절선에 대해 phase 무관하게 항상 알림.
    - 2차 손절 하회: 계좌 구분 없이 확정 후 매 실행마다 계속 알림(집행 전까지).
    - 1차 손절 하회: 계좌2 종목은 집행 확인 전까지 계속 알림. 그 외(계좌4/ISA/IRP 등)는
      장마감 1회 + 장전 1회, 딱 2번만 알리고 이후 억제(회복해서 다시 하회하면 카운트 리셋).
    """
    alerts = []
    symbol = item["symbol"]
    account = item.get("account", "")
    sl1 = item.get("stop_loss_1")
    sl2 = item.get("stop_loss_2")
    price_label = "현재가" if item.get("market") == "crypto" else "종가"

    touched_level = None
    if sl2 is not None and price <= sl2:
        touched_level = 2
        alerts.append(f"🔴 {symbol}: 2차 손절선({sl2}) 하회 — {price_label} {price} — 정규장 손절 집행 필요")
    elif sl1 is not None and price <= sl1:
        touched_level = 1
        key1 = f"stop1cap::{symbol}::{sl1}"
        if account == "계좌2":
            alerts.append(f"🔴 {symbol}: 1차 손절선({sl1}) 하회 — {price_label} {price} — 정규장 50% 손절 집행 필요")
        else:
            stage = state.get(key1, {}).get("stage")
            if stage is None:
                state[key1] = {"stage": "alerted1"}
                alerts.append(f"🔴 {symbol}: 1차 손절선({sl1}) 하회 — {price_label} {price} — 정규장 50% 손절 집행 필요")
            elif stage == "alerted1":
                state[key1]["stage"] = "alerted2"
                alerts.append(f"🔴 {symbol}: 1차 손절선({sl1}) 하회 — {price_label} {price} — 정규장 50% 손절 집행 필요(재알림, 이후 억제)")
            # stage == 'alerted2'면 이미 2번 다 알렸으므로 조용히 억제

    if sl1 is not None and price > sl1:
        state.pop(f"stop1cap::{symbol}::{sl1}", None)  # 회복 시 카운트 리셋 — 다음 하회 때 다시 2번부터

    # 근접 — phase 무관, 항상 노출
    if touched_level != 2 and sl2 is not None and price <= sl2 * (1 + PROXIMITY_PCT / 100):
        alerts.append(f"⚪ {symbol}: 2차 손절선({sl2}) 근접 — {price_label} {price} — 장중 실시간 확인 필요(증권사 알림 활용)")
    if touched_level is None and sl1 is not None and price <= sl1 * (1 + PROXIMITY_PCT / 100):
        alerts.append(f"⚪ {symbol}: 1차 손절선({sl1}) 근접 — {price_label} {price} — 모니터링 필요")

    return alerts


def check_breakout(item: dict, price: float, state: dict, phase: str, level_field: str, label: str):
    """
    2차 추매(2번계좌) 전용 — 당일 종가돌파 + 익일 종가유지 2단계 확인.
    2026-09-16: 유지 확정('maintained') 후에는 실제 집행(watchlist 반영)되기 전까지
    매 실행마다 계속 집행 알람. 확정 후라도 종가가 다시 레벨 아래로 재하강하면 확정상태를
    해제하고, 그 아래에서 근접 범위면 근접 알림만(돌파 알림 재발동 없음) 표시.
    """
    level = item.get(level_field)
    if level is None:
        return []
    symbol = item["symbol"]
    key = f"breakout::{symbol}::{level_field}::{level}"
    stage = state.get(key, {}).get("stage")
    price_label = "현재가" if item.get("market") == "crypto" else "종가"
    alerts = []

    if stage == "maintained":
        if price > level:
            alerts.append(f"🔴 {symbol}: {label}({level}) 유지 확정 — 집행하세요")
        else:
            state.pop(key, None)  # 재하강 — 확정상태 해제
            if price >= level * (1 - PROXIMITY_PCT / 100):
                alerts.append(f"⚪ {symbol}: {label}({level}) 근접 — {price_label} {price} — 모니터링 필요")
        return alerts

    if phase == "close":
        if stage is None:
            if price > level:
                state[key] = {"stage": "breakout"}
                alerts.append(f"🟡 {symbol}: {label}({level}) 종가 기준 돌파 확정 — 종가 {price} — 익일 종가 유지 확인 필요")
            elif price >= level * (1 - PROXIMITY_PCT / 100):
                alerts.append(f"⚪ {symbol}: {label}({level}) 근접 — {price_label} {price} — 모니터링 필요")
        elif stage == "breakout":
            if price > level:
                state[key]["stage"] = "maintained"
                alerts.append(f"🔴 {symbol}: {label}({level}) 유지 확정 — 집행하세요")
            else:
                state.pop(key, None)  # 유지 실패, 조건 리셋
                if price >= level * (1 - PROXIMITY_PCT / 100):
                    alerts.append(f"⚪ {symbol}: {label}({level}) 근접 — {price_label} {price} — 모니터링 필요")
    elif phase == "open":
        if stage == "breakout":
            alerts.append(f"🟡 {symbol}: {label}({level}) 돌파 상태 — 오늘 종가까지 유지되어야 확정")
        elif stage is None and price >= level * (1 - PROXIMITY_PCT / 100) and price <= level:
            alerts.append(f"⚪ {symbol}: {label}({level}) 근접 — {price_label} {price} — 모니터링 필요")

    return alerts


def check_single_confirm(item: dict, price: float, state: dict, phase: str, level_field: str, label: str, execution_required: bool = False):
    """
    승격(2번계좌) / 재진입 전용 — 익일 유지 요건 없음(2026-09-01·09-07 개정, 2026-09-16 재확인).
    2026-09-16 재설계(사용자 지정 규칙):
    - execution_required=True(예: 슬롯이 열려 실제 집행이 필요한 승격 후보, 재진입 신호)면
      집행 확인 전까지 매 실행마다 계속 알림(상태 없이 매번 재평가).
    - execution_required=False(예: 슬롯이 없어 지금 당장 집행할 수 없는 승격 후보)면
      돌파 확정 시점에 **딱 1번만** 참고 알림 후 억제(익일 유지 확인 자체가 불필요한 규칙이므로
      다음날 재알림 없음). 재하강 후 다시 돌파하면 다시 1번.
    - 근접(1.5%)은 phase·execution_required와 무관하게 항상 노출.
    """
    level = item.get(level_field)
    if level is None:
        return []
    symbol = item["symbol"]
    price_label = "현재가" if item.get("market") == "crypto" else "종가"
    alerts = []

    if price > level:
        if execution_required:
            alerts.append(f"🔴 {symbol}: {label}({level}) 돌파 확정 — 집행하세요")
        else:
            key = f"singleconfirm2::{symbol}::{level_field}::{level}"
            if key not in state:
                state[key] = {"stage": "alerted"}
                alerts.append(f"🟡 {symbol}: {label}({level}) 종가 돌파 확정(참고, 현재 집행대상 아님, 익일 재알림 없음) — 종가 {price}")
            # 이미 알렸으면 조용히 억제
    else:
        state.pop(f"singleconfirm2::{symbol}::{level_field}::{level}", None)  # 재하강 — 카운트 리셋
        if price >= level * (1 - PROXIMITY_PCT / 100):
            alerts.append(f"⚪ {symbol}: {label}({level}) 근접 — {price_label} {price} — 모니터링 필요")

    return alerts


def check_dip_addon(item: dict, price: float, state: dict, phase: str, level_field: str, label: str):
    """
    계좌1(고정 적립) 추매 전용 — 승격/재진입/2차추매와 반대 방향(하회 시 매수).
    2026-09-16: 하회 알림을 '오늘 처음 감지'로 제한하던 로직 제거(손절선과 동일한 버그) —
    실제 추매 집행 전까지 매 실행마다 계속 알람. 근접은 항상 노출.
    """
    level = item.get(level_field)
    if level is None:
        return []
    symbol = item["symbol"]
    price_label = "현재가" if item.get("market") == "crypto" else "종가"
    alerts = []

    if price <= level:
        alerts.append(f"🔴 {symbol}: {label}({level}) 하회 — {price_label} {price} — 정규장 추매진행 하세요")
    elif price <= level * (1 + PROXIMITY_PCT / 100):
        alerts.append(f"⚪ {symbol}: {label}({level}) 근접 — {price_label} {price} — 모니터링 필요")

    return alerts


def check_earnings(watchlist: list, calendar: list):
    alerts = []
    today = datetime.date.today()
    symbols = {item["symbol"] for item in watchlist}
    for row in calendar:
        symbol = row.get("symbol", "")
        report_date_str = row.get("reportDate", "")
        if symbol not in symbols or not report_date_str:
            continue
        try:
            report_date = datetime.datetime.strptime(report_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        days_left = (report_date - today).days
        if 0 <= days_left <= EARNINGS_LOOKAHEAD_DAYS:
            alerts.append(f"📅 {symbol}: 실적 발표 {report_date_str} (D-{days_left}) — 확인 필요")
    return alerts


def friday_reminder():
    if datetime.date.today().weekday() == 4:  # 0=월 ... 4=금
        return ["🗓️ 금요일: ATR(20) 손절선 정기 재계산 필요"]
    return []


# ============================================================
# 메시지 구성 및 발송
# ============================================================

def build_message(checklist_alerts: list, news_by_symbol: dict, phase: str, exchange_rate=None, warnings_by_symbol=None, warnings_failed=None) -> str:
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    lines = [f"📋 {today_str} 체크리스트"]

    if exchange_rate is not None:
        lines.append(f"💵 환율: {exchange_rate:,.2f}원/달러")
    lines.append("")

    if checklist_alerts:
        lines.extend(checklist_alerts)
    else:
        lines.append("오늘 특이 트리거 없음")

    if warnings_by_symbol:
        lines.append("\n⚠️ 매수 유의사항")
        for symbol, warns in warnings_by_symbol.items():
            lines.append(f"  - {symbol}: {', '.join(warns)}")

    if warnings_failed:
        lines.append(f"\n⚠️ 유의사항 조회 실패(확인 안 됨, '없음'과 다름): {', '.join(warnings_failed)}")

    if news_by_symbol:
        news_title = "📰 장중 뉴스 브리핑" if phase == "close" else "📰 장전 뉴스 브리핑"
        lines.append(f"\n{news_title}")
        for symbol, headlines in news_by_symbol.items():
            lines.append(f"\n[{symbol}]")
            for h in headlines:
                lines.append(f"  - {h}")

    return "\n".join(lines)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램 미설정] 건너뜀")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"[텔레그램 발송 실패] {e}")


def send_email(message: str):
    if not EMAIL_FROM or not EMAIL_APP_PASSWORD:
        print("[이메일 미설정] 건너뜀")
        return
    msg = MIMEText(message)
    msg["Subject"] = f"[매매 체크리스트] {datetime.date.today()}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    except Exception as e:
        print(f"[이메일 발송 실패] {e}")


# ============================================================
# 메인
# ============================================================

def main():
    phase, region = get_run_context()
    print(f"[실행 컨텍스트] argv={sys.argv[1:]} -> phase={phase!r}, region={region!r}")
    watchlist_data = load_watchlist_data()
    watchlist = watchlist_data["watchlist"]
    account2_open_slots = watchlist_data.get("account2_open_slots", 0)
    alert_state = load_alert_state()

    friday_alerts = friday_reminder()
    item_alerts = []

    for item in watchlist:
        market = item.get("market", "stock")
        item_region = item.get("region")
        # 코인은 항상 포함. 주식은 region이 지정된 실행이면 해당 지역만.
        if market != "crypto" and region is not None and item_region is not None and item_region.lower() != region.lower():
            print(f"[가격체크 제외] {item['symbol']}: item_region={item_region!r} != 실행 region={region!r}")
            continue

        if phase == "close":
            price = get_regular_close(item["symbol"], market)
            print(f"[종가 계산] {item['symbol']}: {price}")
            if price is not None:
                set_cached_close(alert_state, item["symbol"], price)
        else:
            price = get_cached_close(alert_state, item["symbol"])
            print(f"[캐시 종가] {item['symbol']}: {price}")
            if price is None:
                # 캐시 없음(신규 종목 등) — 실시간가 대신 정규장 종가 API를 다시 호출
                price = get_regular_close(item["symbol"], market)
                print(f"[종가 계산(캐시없음 재조회)] {item['symbol']}: {price}")
                if price is not None:
                    set_cached_close(alert_state, item["symbol"], price)
                # 이마저 실패하면 캐시도 없으니 그대로 None -> 이번 체크는 건너뜀
        if price is None:
            continue
        item_alerts.extend(check_price_thresholds(item, price, alert_state, phase))
        if market != "crypto":
            item_alerts.extend(check_single_confirm(item, price, alert_state, phase, "promotion_line", "승격선", execution_required=(account2_open_slots > 0)))
            item_alerts.extend(check_single_confirm(item, price, alert_state, phase, "reentry_level", "재진입 기준가", execution_required=True))
            item_alerts.extend(check_breakout(item, price, alert_state, phase, "addon_level", "2차추매선"))
            item_alerts.extend(check_dip_addon(item, price, alert_state, phase, "dip_addon_level", "추매선"))

    save_alert_state(alert_state)

    calendar = get_earnings_calendar()
    item_alerts.extend(check_earnings(watchlist, calendar))

    item_alerts.sort(key=alert_priority)
    checklist_alerts = friday_alerts + item_alerts

    news_symbols = []
    for item in watchlist:
        market = item.get("market", "stock")
        if market == "crypto":
            continue
        item_region = item.get("region")
        if region is not None and item_region is not None and item_region.lower() != region.lower():
            print(f"[뉴스 제외] {item['symbol']}: item_region={item_region!r} != 실행 region={region!r}")
            continue
        news_symbols.append(item["symbol"])

    news_by_symbol = get_news_headlines_batch(news_symbols, phase)
    for symbol in news_symbols:
        if symbol not in news_by_symbol:
            print(f"[뉴스 0건] {symbol}: phase={phase!r} 시간창 내 헤드라인 없음(API 응답 자체가 빈 경우 포함)")

    # 2026-09-15: 환율은 국내장/미국장 구분 없이 매번 포함
    exchange_rate = get_toss_exchange_rate()
    print(f"[환율] USD/KRW: {exchange_rate}")

    # 2026-09-15: 매수 유의사항(정리매매/단기과열/투자경고·위험/VI) — 이번 실행 대상 종목(news_symbols와 동일 필터)만 조회
    # 호출 간 0.2초 딜레이로 토스 API 초당 요청 한도(버스트) 회피
    warnings_by_symbol = {}
    warnings_failed = []
    for symbol in news_symbols:
        warns = get_stock_warnings(symbol)
        time.sleep(0.2)
        if warns is None:
            warnings_failed.append(symbol)
        elif warns:
            warnings_by_symbol[symbol] = warns
            print(f"[유의사항] {symbol}: {warns}")
    if warnings_failed:
        print(f"[유의사항조회 실패 종목] {warnings_failed} — '유의사항 없음'이 아니라 조회 자체가 안 된 것")

    message = build_message(checklist_alerts, news_by_symbol, phase, exchange_rate, warnings_by_symbol, warnings_failed)
    print(message)

    send_telegram(message)
    send_email(message)


if __name__ == "__main__":
    main()
