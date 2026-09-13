"""API 사용량·비용 기록.

임베딩·요약 호출부에서 이 모듈의 record() 를 부르면 토큰 수와 비용이
reco_api_usage 에 쌓인다. 대시보드는 이 테이블을 집계해 보여 준다.

설계 원칙
---------
기록 실패가 본래 작업을 망가뜨리면 안 된다. record() 는 어떤 예외도 밖으로
내보내지 않고 경고만 남긴다 — 비용 집계가 안 되는 것보다 추천 파이프라인이
멈추는 쪽이 훨씬 나쁘다.
"""

import logging
from datetime import date, datetime

from app import db
from app.litreview.usage import fx, pricing

logger = logging.getLogger(__name__)


def record(
    provider: str,
    model: str,
    operation: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    researcher_id: int | None = None,
    topic_id: int | None = None,
    note: str | None = None,
    commit: bool = True,
):
    """API 호출 1건의 사용량과 비용을 남긴다.

    Args:
        provider:  'openai' | 'anthropic'
        model:     모델 ID (단가표 조회 키)
        operation: 'embedding' | 'summary' | 기타 구분자
        cache_read_tokens: 프롬프트 캐시 히트분 (참고용 — 현재 비용 계산엔
                   반영하지 않는다. 캐시 단가를 별도로 다루기 시작하면 그때
                   pricing 에 추가할 것)

    Returns:
        저장된 ApiUsage 또는 None(실패).
    """
    try:
        from app.models import ApiUsage

        occurred = datetime.utcnow()
        usd = pricing.compute_usd(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            on=occurred.date(),
        )
        krw, rate, rate_date, rate_source = fx.to_krw(usd)

        row = ApiUsage(
            occurred_at=occurred,
            provider=provider,
            model=model,
            operation=operation,
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cache_read_tokens=cache_read_tokens or 0,
            total_tokens=(input_tokens or 0) + (output_tokens or 0),
            usd_cost=usd,
            usd_krw_rate=rate,
            rate_date=rate_date,
            rate_source=rate_source,
            krw_cost=krw,
            researcher_id=researcher_id,
            topic_id=topic_id,
            note=note,
        )
        db.session.add(row)
        if commit:
            db.session.commit()
        return row

    except Exception:
        logger.warning("사용량 기록 실패 (본래 작업은 계속 진행)", exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def summarize(since: datetime | None = None, until: datetime | None = None) -> dict:
    """기간 집계 — 대시보드용.

    Returns:
        {total_tokens, usd, krw, by_operation, by_model, latest_rate, ...}
    """
    from app.models import ApiUsage

    q = ApiUsage.query
    if since:
        q = q.filter(ApiUsage.occurred_at >= since)
    if until:
        q = q.filter(ApiUsage.occurred_at < until)

    rows = q.all()
    if not rows:
        return {
            "calls": 0, "total_tokens": 0, "usd": 0.0, "krw": 0.0,
            "by_operation": {}, "by_model": {},
            "latest_rate": None, "rate_date": None, "unpriced_calls": 0,
        }

    by_op: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total_tokens = usd = krw = 0
    unpriced = 0

    for r in rows:
        total_tokens += r.total_tokens or 0
        if r.usd_cost is None:
            unpriced += 1
        else:
            usd += r.usd_cost
        krw += r.krw_cost or 0.0

        for bucket, key in ((by_op, r.operation), (by_model, r.model)):
            b = bucket.setdefault(
                key, {"calls": 0, "tokens": 0, "usd": 0.0, "krw": 0.0}
            )
            b["calls"] += 1
            b["tokens"] += r.total_tokens or 0
            b["usd"] += r.usd_cost or 0.0
            b["krw"] += r.krw_cost or 0.0

    latest = max(rows, key=lambda r: r.occurred_at)
    return {
        "calls": len(rows),
        "total_tokens": total_tokens,
        "usd": round(usd, 6),
        "krw": round(krw, 2),
        "by_operation": by_op,
        "by_model": by_model,
        "latest_rate": latest.usd_krw_rate,
        "rate_date": str(latest.rate_date) if latest.rate_date else None,
        "rate_source": latest.rate_source,
        "unpriced_calls": unpriced,
    }


def weekly_series(weeks: int = 8) -> list[dict]:
    """주 단위 비용 추이 — 대시보드 그래프용. 최신 주가 마지막."""
    from app.models import ApiUsage

    rows = (
        ApiUsage.query.order_by(ApiUsage.occurred_at.asc()).all()
    )
    if not rows:
        return []

    buckets: dict[date, dict] = {}
    for r in rows:
        d = r.occurred_at.date()
        # 그 주의 월요일로 정렬
        monday = d - __import__("datetime").timedelta(days=d.weekday())
        b = buckets.setdefault(
            monday, {"week": str(monday), "calls": 0, "tokens": 0, "usd": 0.0, "krw": 0.0}
        )
        b["calls"] += 1
        b["tokens"] += r.total_tokens or 0
        b["usd"] += r.usd_cost or 0.0
        b["krw"] += r.krw_cost or 0.0

    series = [buckets[k] for k in sorted(buckets)][-weeks:]
    for b in series:
        b["usd"] = round(b["usd"], 6)
        b["krw"] = round(b["krw"], 2)
    return series
