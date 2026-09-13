"""API 사용량·비용 추적.

임베딩·요약 호출의 토큰 수를 모으고, 모델 단가로 USD 비용을 계산한 뒤
호출 시점 환율로 원화를 함께 기록한다.
"""

from app.litreview.usage.recorder import record, summarize, weekly_series  # noqa: F401
