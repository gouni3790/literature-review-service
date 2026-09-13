"""키워드 시드 토픽 (source_type='keyword_seeded') 관리.

토픽 키워드 ↔ 본인 논문(reference_papers) author keywords 매칭 →
매칭된 논문들의 임베딩 평균을 representative_vector로 사용.

본인 논문이 추가될 때 자동 갱신해 vector를 최신화.
"""

import logging

import numpy as np

from app import db
from app.models import (
    PaperKeyword,
    ReferencePaper,
    ResearchTopic,
)

logger = logging.getLogger(__name__)

KEYWORD_SEEDED_SOURCE = "keyword_seeded"


def _matches_any(text: str, kw_set: set[str]) -> bool:
    """text가 kw_set 중 하나라도 부분 포함하면 True."""
    t = text.lower()
    return any(k in t for k in kw_set)


def _matched_own_papers(researcher_id: int, topic_keywords: list[str]) -> list[ReferencePaper]:
    """본인 논문 중 토픽 키워드와 paper_keywords가 매칭되는 것."""
    if not topic_keywords:
        return []
    kw_set = {k.lower() for k in topic_keywords}

    own = ReferencePaper.query.filter_by(researcher_id=researcher_id).all()
    matched = []
    for p in own:
        paper_kws = [
            k.keyword for k in PaperKeyword.query.filter_by(paper_id=p.id).all()
        ]
        if any(_matches_any(pk, kw_set) for pk in paper_kws):
            matched.append(p)
    return matched


def refresh_keyword_seeded_topic(topic: ResearchTopic) -> bool:
    """단일 토픽의 representative_vector를 본인 매칭 논문 임베딩 평균으로 갱신.

    Returns:
        True if vector updated, False if skipped.
    """
    matched = _matched_own_papers(topic.researcher_id, topic.keywords or [])
    embs = [
        np.array(p.embedding, dtype=np.float32)
        for p in matched
        if p.embedding
    ]
    if not embs:
        logger.warning(
            "Keyword-seeded topic %d (%s): no matched own papers with embedding",
            topic.id,
            topic.name,
        )
        return False

    avg = np.mean(embs, axis=0)
    topic.representative_vector = avg.tolist()
    if topic.description and "[KEYWORD_SEEDED]" not in topic.description:
        topic.description = f"[KEYWORD_SEEDED] {topic.description}"
    db.session.commit()
    logger.info(
        "Refreshed keyword-seeded topic %d (%s): %d matched own papers",
        topic.id,
        topic.name,
        len(matched),
    )
    return True


def refresh_all_keyword_seeded_topics(researcher_id: int) -> int:
    """연구원의 모든 source_type='keyword_seeded' 토픽 자동 갱신.

    Returns:
        갱신된 토픽 수.
    """
    topics = ResearchTopic.query.filter_by(
        researcher_id=researcher_id,
        source_type=KEYWORD_SEEDED_SOURCE,
    ).all()
    refreshed = 0
    for t in topics:
        if refresh_keyword_seeded_topic(t):
            refreshed += 1
    if topics:
        logger.info(
            "Refreshed %d/%d keyword-seeded topics for researcher %d",
            refreshed,
            len(topics),
            researcher_id,
        )
    return refreshed


def create_keyword_seeded_topic(
    researcher_id: int,
    name: str,
    keywords: list[str],
    description: str | None = None,
    sort_order: int = 99,
) -> ResearchTopic:
    """새 키워드 시드 토픽 생성 + representative_vector 즉시 계산."""
    topic = ResearchTopic(
        researcher_id=researcher_id,
        name=name,
        keywords=keywords,
        source_type=KEYWORD_SEEDED_SOURCE,
        cluster_label=None,
        sort_order=sort_order,
        description=f"[KEYWORD_SEEDED] {description or ''}".strip(),
    )
    db.session.add(topic)
    db.session.flush()
    refresh_keyword_seeded_topic(topic)
    return topic
