"""연구 주제 기반 Scopus 검색 쿼리 생성.

각 주제(ResearchTopic)가 독립적인 쿼리를 생성한다.
자동 주제: 클러스터 키워드 + 클러스터 저널
수동 주제: 사용자 키워드 + 레퍼런스 논문 참고문헌 저널
"""

import logging
from datetime import datetime

from sqlalchemy import func

from app import db
from app.models import (
    CollectedPaper,
    CustomJournal,
    PaperKeyword,
    PaperRecommendation,
    ReferencePaper,
    ResearchTopic,
    Researcher,
    TargetJournal,
    TopicReferencePaper,
)

logger = logging.getLogger(__name__)


def _get_cluster_journals(cluster_db_id: int, top_n: int = 3) -> list[str]:
    """클러스터 소속 논문의 게재 저널 빈도 상위 N개."""
    rows = (
        db.session.query(ReferencePaper.journal, func.count(ReferencePaper.id))
        .filter(
            ReferencePaper.cluster_id == cluster_db_id,
            ReferencePaper.journal.isnot(None),
            ReferencePaper.journal != "",
        )
        .group_by(ReferencePaper.journal)
        .order_by(func.count(ReferencePaper.id).desc())
        .limit(top_n)
        .all()
    )
    return [j for j, _ in rows]


def _get_overall_journals(researcher_id: int, top_n: int = 5) -> list[str]:
    """연구원 전체 논문의 게재 저널 빈도 상위 N개 (B유형 자동 주제용)."""
    rows = (
        db.session.query(ReferencePaper.journal, func.count(ReferencePaper.id))
        .filter(
            ReferencePaper.researcher_id == researcher_id,
            ReferencePaper.journal.isnot(None),
            ReferencePaper.journal != "",
        )
        .group_by(ReferencePaper.journal)
        .order_by(func.count(ReferencePaper.id).desc())
        .limit(top_n)
        .all()
    )
    return [j for j, _ in rows]


def _get_target_journal_names(researcher_id: int) -> list[str]:
    return [
        j.journal_name
        for j in TargetJournal.query.filter_by(
            researcher_id=researcher_id, is_active=True
        ).all()
    ]


def _get_custom_journals(researcher_id: int) -> list[str]:
    return [
        j.journal_name
        for j in CustomJournal.query.filter_by(researcher_id=researcher_id).all()
    ]


def _format_scopus_query(
    journals: list[str], keywords: list[str], year_after: int
) -> str:
    """SOURCE-ID/저널명 + TITLE-ABS-KEY + PUBYEAR 형식 쿼리 생성."""
    parts = []

    if journals:
        j_clause = " OR ".join(f'SRCTITLE("{j}")' for j in journals[:10])
        parts.append(f"({j_clause})")

    if keywords:
        kw_items = []
        for kw in keywords[:15]:
            if " " in kw:
                kw_items.append(f'"{kw}"')
            else:
                kw_items.append(kw)
        kw_clause = " OR ".join(kw_items)
        parts.append(f"TITLE-ABS-KEY({kw_clause})")

    parts.append(f"PUBYEAR > {year_after}")

    return " AND ".join(parts)


def build_queries(researcher_id: int) -> list[dict]:
    """연구원의 모든 주제에서 검색 쿼리 생성.

    각 주제가 독립적인 쿼리를 생성한다.

    Returns:
        [{query_string, topic_id, description}]
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        raise ValueError(f"Researcher {researcher_id} not found")

    year_after = datetime.now().year - researcher.target_year_range

    # 공통 저널: custom + target (중복 제거)
    custom_journals = _get_custom_journals(researcher_id)
    target_journals = _get_target_journal_names(researcher_id)
    extra_journals = list(dict.fromkeys(custom_journals + target_journals))

    # 연구원의 모든 주제
    topics = (
        ResearchTopic.query.filter_by(researcher_id=researcher_id)
        .order_by(ResearchTopic.sort_order)
        .all()
    )

    if not topics:
        logger.warning("Researcher %d has no research topics", researcher_id)
        return []

    queries = []
    for topic in topics:
        query_data = _build_topic_query(topic, year_after, extra_journals)
        if query_data:
            queries.append(query_data)

    logger.info(
        "Built %d queries for researcher %d (type %s, topics=%d)",
        len(queries),
        researcher_id,
        researcher.researcher_type,
        len(topics),
    )
    return queries


def _build_topic_query(
    topic: ResearchTopic,
    year_after: int,
    extra_journals: list[str],
) -> dict | None:
    """단일 ��제에서 쿼리 생성.

    자동 주제: 클러스터 키워드 + 클러스터 저널
    수동 주제 (DOI 있음): 사용자 키워드 + 레퍼런스 논문 참고문헌 저널 상위 3개
    수동 주제 (DOI 없음): 사용자 키워드 + 사용자 설정 저널
    """
    keywords = topic.keywords or []

    if topic.source_type == "auto":
        if topic.cluster_label is not None:
            # A유형 자동 주제: 클러스터 저널 상위 3개
            from app.models import ResearchCluster
            cluster = ResearchCluster.query.filter_by(
                researcher_id=topic.researcher_id,
                cluster_label=topic.cluster_label,
            ).first()

            auto_journals = []
            if cluster:
                auto_journals = _get_cluster_journals(cluster.id)
        else:
            # B유형 자동 주제: 전체 게재 저널 빈도 상위 5개
            auto_journals = _get_overall_journals(topic.researcher_id, top_n=5)

        all_journals = list(dict.fromkeys(auto_journals + extra_journals))
        desc = f"Auto topic: {topic.name}"

    else:
        # 수동 주제
        topic_journals = []

        # DOI 기반: 레퍼런스 논문 참고문헌 저널 상위 3개
        if topic.reference_papers:
            ref_journal_freq: dict[str, int] = {}
            for trp in topic.reference_papers:
                if trp.ref_journals:
                    for rj in trp.ref_journals:
                        ref_journal_freq[rj] = ref_journal_freq.get(rj, 0) + 1

            top_ref_journals = sorted(
                ref_journal_freq.items(), key=lambda x: x[1], reverse=True
            )[:3]
            topic_journals = [j for j, _ in top_ref_journals]

        all_journals = list(dict.fromkeys(topic_journals + extra_journals))
        desc = f"Manual topic: {topic.name}"

    if not keywords:
        logger.info("Topic %d '%s' has no keywords, skipping", topic.id, topic.name)
        return None

    query_str = _format_scopus_query(all_journals, keywords, year_after)
    return {
        "query_string": query_str,
        "topic_id": topic.id,
        "description": desc,
    }
