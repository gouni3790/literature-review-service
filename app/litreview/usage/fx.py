"""USD → KRW 환율 조회.

"결제 당시의 환율"을 원화 기록에 쓰기 위한 모듈이다. API 과금은 호출 시점에
계속 발생하므로, **호출 시점의 시장 환율**을 그때그때 붙여 저장한다. 저장된
행은 그 시점 환율을 그대로 갖고 있어 나중에 환율이 변해도 소급되지 않는다.

밝혀 둘 것: 이것은 시장 기준환율(ECB)이지 **카드사가 실제로 청구한 환율이
아니다.** 카드 청구액은 카드사 환율과 해외결제 수수료(보통 1~2%)가 더해져
여기 기록된 금액보다 조금 높다. 정확한 청구액이 필요하면 카드 명세서를
대조해야 한다.

출처는 두 곳을 쓴다. 인증이 필요 없고 무료다.
    1) frankfurter.app  — ECB 기준환율 (영업일 기준, 주말엔 직전 영업일)
    2) open.er-api.com  — 폴백
"""

import logging
from datetime import date, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15

SOURCES = (
    (
        "frankfurter",
        "https://api.frankfurter.app/latest?from=USD&to=KRW",
        lambda j: ((j.get("rates") or {}).get("KRW"), j.get("date")),
    ),
    (
        "open.er-api",
        "https://open.er-api.com/v6/latest/USD",
        lambda j: ((j.get("rates") or {}).get("KRW"), None),
    ),
)

# 프로세스 수명 동안의 캐시 — (환율, 기준일, 출처, 조회시각)
_cache: tuple[float, date | None, str, datetime] | None = None
CACHE_TTL = timedelta(hours=6)


def fetch_usd_krw(force: bool = False) -> tuple[float, date | None, str] | None:
    """USD/KRW 환율을 가져온다.

    Returns:
        (환율, 기준일, 출처) 또는 None(모든 출처 실패).
    """
    global _cache

    if not force and _cache is not None:
        rate, ref_date, src, fetched = _cache
        if datetime.utcnow() - fetched < CACHE_TTL:
            return rate, ref_date, src

    for name, url, parse in SOURCES:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            rate, raw_date = parse(resp.json())
            if not rate:
                continue
            ref_date = None
            if raw_date:
                try:
                    ref_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                except ValueError:
                    ref_date = None
            _cache = (float(rate), ref_date, name, datetime.utcnow())
            logger.info(
                "USD/KRW = %.2f (출처 %s, 기준일 %s)", rate, name, ref_date or "-"
            )
            return float(rate), ref_date, name
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("환율 조회 실패 (%s): %s", name, exc)

    logger.error(
        "환율을 가져오지 못했습니다. 원화 금액이 비어 있게 됩니다 "
        "(USD 금액은 정상 기록)."
    )
    return None


def to_krw(usd: float | None) -> tuple[float | None, float | None, date | None, str | None]:
    """USD 금액을 원화로. 실패 시 원화만 None 이고 나머지는 그대로 둔다.

    Returns:
        (krw, rate, rate_date, source)
    """
    if usd is None:
        return None, None, None, None
    fx = fetch_usd_krw()
    if fx is None:
        return None, None, None, None
    rate, ref_date, src = fx
    return usd * rate, rate, ref_date, src


def clear_cache() -> None:
    global _cache
    _cache = None
