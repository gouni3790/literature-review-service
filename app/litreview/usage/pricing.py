"""모델별 토큰 단가표 (USD / 1M 토큰).

단가는 코드에 박지 않고 여기 한곳에 모은다. 벤더가 가격을 바꾸면 이 표만
고치면 되고, 과거 기록은 저장 시점의 계산 결과(usd_cost)를 그대로 보존하므로
소급 변경되지 않는다.

기간 단가
---------
Claude Sonnet 5 는 2026-08-31 까지 인트로 가격이 적용된다($2/$10, 이후
$3/$15). 그래서 단가를 단일 값이 아니라 (시작일, 종료일, 단가) 구간 목록으로
둔다. 호출 시각이 어느 구간에 드는지 보고 고른다.

출처
----
Claude: Anthropic 공식 단가 (2026-06-24 기준 표)
OpenAI: text-embedding-3-small $0.02 / 1M

단가가 바뀌면 PRICES 를 갱신하고, 확인한 날짜를 VERIFIED_AT 에 적어 둔다.
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

VERIFIED_AT = "2026-08-21"

# model -> [(시작일, 종료일 또는 None, input $/1M, output $/1M)]
# 종료일 None = 무기한. 목록은 시작일 오름차순.
PRICES: dict[str, list[tuple[date, date | None, float, float]]] = {
    # --- Anthropic ---
    "claude-sonnet-5": [
        # 인트로 가격 — 2026-08-31 까지
        (date(2025, 1, 1), date(2026, 8, 31), 2.00, 10.00),
        (date(2026, 9, 1), None, 3.00, 15.00),
    ],
    "claude-opus-5": [(date(2025, 1, 1), None, 5.00, 25.00)],
    "claude-sonnet-4-6": [(date(2025, 1, 1), None, 3.00, 15.00)],
    "claude-haiku-4-5": [(date(2025, 1, 1), None, 1.00, 5.00)],
    # --- OpenAI (임베딩은 output 토큰이 없다) ---
    "text-embedding-3-small": [(date(2024, 1, 1), None, 0.02, 0.0)],
    "text-embedding-3-large": [(date(2024, 1, 1), None, 0.13, 0.0)],
}

# 단가표에 없는 모델의 기본값. 0 이면 비용이 조용히 0으로 잡혀 오해를 부르므로
# None 을 돌려주고 호출부가 경고를 남기게 한다.
UNKNOWN_MODEL_COST = None


def get_rate(model: str, on: date | None = None) -> tuple[float, float] | None:
    """해당 시점의 (input $/1M, output $/1M). 모르는 모델이면 None."""
    on = on or date.today()
    windows = PRICES.get(model)
    if not windows:
        return UNKNOWN_MODEL_COST

    for start, end, cin, cout in windows:
        if start <= on and (end is None or on <= end):
            return cin, cout

    # 구간을 못 찾으면 가장 최근 구간을 쓴다 (미래 날짜 등)
    _, _, cin, cout = windows[-1]
    return cin, cout


def compute_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    on: date | None = None,
) -> float | None:
    """토큰 수 → USD 비용. 단가를 모르면 None."""
    rate = get_rate(model, on)
    if rate is None:
        logger.warning(
            "단가표에 없는 모델이라 비용을 계산하지 못했습니다: %s "
            "(app/litreview/usage/pricing.py 의 PRICES 에 추가하세요)",
            model,
        )
        return None
    cin, cout = rate
    return (input_tokens / 1_000_000) * cin + (output_tokens / 1_000_000) * cout


def known_models() -> list[str]:
    return sorted(PRICES)
