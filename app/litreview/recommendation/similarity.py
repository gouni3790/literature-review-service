"""주제별 유사도 계산.

Type A, C, 수동: 주제 대표 벡터 1개 vs 수집 논문 → 단일 코사인 유사도
Type B (auto):   기존 논문 각각의 임베딩 vs 수집 논문 → max 유사도
"""

import logging

import numpy as np

from app import db
from app.models import (
    CollectedPaper,
    PaperRecommendation,
    ReferencePaper,
    Researcher,
    ResearchTopic,
)

logger = logging.getLogger(__name__)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """두 벡터 간 코사인 유사도."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _get_own_scopus_ids(researcher_id: int, topic: ResearchTopic) -> set[str]:
    """본인 논문 scopus_id 수집 (자기 논문 제외용)."""
    own_ids = set()
    for rp in ReferencePaper.query.filter(
        ReferencePaper.researcher_id == researcher_id,
        ReferencePaper.scopus_id.isnot(None),
    ).all():
        own_ids.add(rp.scopus_id)

    for trp in topic.reference_papers:
        if trp.scopus_id:
            own_ids.add(trp.scopus_id)

    return own_ids


def _exclude_own_papers(paper_ids: list[int], own_scopus_ids: set[str]) -> list[int]:
    """자기 논문 제외."""
    if not own_scopus_ids:
        return paper_ids

    excluded = {
        cp.id
        for cp in CollectedPaper.query.filter(
            CollectedPaper.id.in_(paper_ids),
            CollectedPaper.scopus_id.in_(own_scopus_ids),
        ).all()
    }
    if excluded:
        logger.info("Excluded %d own papers from %d candidates", len(excluded), len(paper_ids))
        return [pid for pid in paper_ids if pid not in excluded]
    return paper_ids


def _exclude_not_relevant(paper_ids: list[int], researcher_id: int) -> list[int]:
    """사용자가 not_relevant 표시한 논문 제외 (피드백 반영)."""
    not_relevant_ids = {
        pid
        for (pid,) in db.session.query(PaperRecommendation.paper_id)
        .filter(
            PaperRecommendation.researcher_id == researcher_id,
            PaperRecommendation.user_feedback == "not_relevant",
        )
        .distinct()
        .all()
    }
    if not not_relevant_ids:
        return paper_ids

    filtered = [pid for pid in paper_ids if pid not in not_relevant_ids]
    excluded_count = len(paper_ids) - len(filtered)
    if excluded_count:
        logger.info(
            "Excluded %d not_relevant papers for researcher %d",
            excluded_count,
            researcher_id,
        )
    return filtered


def _calculate_representative_vector_mode(
    topic: ResearchTopic,
    paper_ids: list[int],
    progress_callback,
) -> list[dict]:
    """Type A, C, 수동 주제: 대표 벡터 1개 vs 수집 논문."""
    topic_vec = np.array(topic.representative_vector, dtype=np.float32)
    total = len(paper_ids)
    results = []

    for idx, pid in enumerate(paper_ids):
        collected = db.session.get(CollectedPaper, pid)
        if not collected or not collected.embedding:
            if progress_callback:
                progress_callback(idx + 1, total)
            continue

        cp_vec = np.array(collected.embedding, dtype=np.float32)
        sim = _cosine_similarity(cp_vec, topic_vec)

        results.append({
            "paper_id": pid,
            "similarity_score": round(sim, 6),
            "similarity_details": {
                "topic_id": topic.id,
                "topic_name": topic.name,
                "mode": "representative_vector",
            },
        })

        if progress_callback:
            progress_callback(idx + 1, total)

    return results


def _calculate_max_mode(
    topic: ResearchTopic,
    researcher_id: int,
    paper_ids: list[int],
    progress_callback,
) -> list[dict]:
    """Type B: 기존 논문 각각의 임베딩 vs 수집 논문 → max.

    각 수집 논문에 대해 모든 기존 논문과의 유사도를 계산하고
    가장 높은 값을 해당 논문의 유사도 점수로 사용.
    """
    # 기존 논문 임베딩 수집
    ref_papers = (
        ReferencePaper.query.filter(
            ReferencePaper.researcher_id == researcher_id,
            ReferencePaper.embedding.isnot(None),
        ).all()
    )

    if not ref_papers:
        logger.warning(
            "Researcher %d has no embedded reference papers for max-mode",
            researcher_id,
        )
        return []

    ref_vectors = [
        {
            "id": rp.id,
            "title": rp.title,
            "embedding": np.array(rp.embedding, dtype=np.float32),
        }
        for rp in ref_papers
    ]

    total = len(paper_ids)
    results = []

    for idx, pid in enumerate(paper_ids):
        collected = db.session.get(CollectedPaper, pid)
        if not collected or not collected.embedding:
            if progress_callback:
                progress_callback(idx + 1, total)
            continue

        cp_vec = np.array(collected.embedding, dtype=np.float32)

        # 모든 기존 논문과의 유사도 계산 → max
        best_sim = 0.0
        best_ref_id = None
        best_ref_title = None

        for rv in ref_vectors:
            sim = _cosine_similarity(cp_vec, rv["embedding"])
            if sim > best_sim:
                best_sim = sim
                best_ref_id = rv["id"]
                best_ref_title = rv["title"]

        results.append({
            "paper_id": pid,
            "similarity_score": round(best_sim, 6),
            "similarity_details": {
                "topic_id": topic.id,
                "topic_name": topic.name,
                "mode": "max_individual",
                "comparison_count": len(ref_vectors),
                "best_match": {
                    "ref_id": best_ref_id,
                    "ref_title": best_ref_title,
                    "similarity": round(best_sim, 6),
                },
            },
        })

        if progress_callback:
            progress_callback(idx + 1, total)

    return results


def calculate_similarity(
    topic_id: int,
    paper_ids: list[int],
    progress_callback=None,
) -> list[dict]:
    """주제별 유사도 산출. 연구원 유형에 따라 자동 분기.

    Type A (auto topic): 대표 벡터 1개 vs 수집 논문
    Type B (auto topic): 기존 논문 각각 vs 수집 논문 → max
    Type C / 수동 주제: 대표 벡터 1개 vs 수집 논문

    Args:
        topic_id: research_topics.id
        paper_ids: collected_papers.id 리스트
        progress_callback: fn(done, total)

    Returns:
        [{paper_id, similarity_score, similarity_details}]
    """
    topic = db.session.get(ResearchTopic, topic_id)
    if not topic:
        raise ValueError(f"Topic {topic_id} not found")

    researcher = db.session.get(Researcher, topic.researcher_id)
    if not researcher:
        raise ValueError(f"Researcher for topic {topic_id} not found")

    # 본인 논문 + not_relevant 표시한 논문 제외
    own_scopus_ids = _get_own_scopus_ids(topic.researcher_id, topic)
    paper_ids = _exclude_own_papers(paper_ids, own_scopus_ids)
    paper_ids = _exclude_not_relevant(paper_ids, topic.researcher_id)

    if not paper_ids:
        return []

    # B유형 자동 주제 → max 모드
    use_max_mode = (
        researcher.researcher_type == "B"
        and topic.source_type == "auto"
    )

    if use_max_mode:
        logger.info(
            "Topic %d '%s': B-type max mode (%d candidates)",
            topic_id,
            topic.name,
            len(paper_ids),
        )
        results = _calculate_max_mode(
            topic, topic.researcher_id, paper_ids, progress_callback
        )
    else:
        # 대표 벡터 필요
        if not topic.representative_vector:
            logger.warning(
                "Topic %d '%s' has no representative vector",
                topic_id,
                topic.name,
            )
            return []

        logger.info(
            "Topic %d '%s': representative vector mode (%d candidates)",
            topic_id,
            topic.name,
            len(paper_ids),
        )
        results = _calculate_representative_vector_mode(
            topic, paper_ids, progress_callback
        )

    logger.info(
        "Calculated similarity for %d papers (topic %d '%s', mode=%s)",
        len(results),
        topic_id,
        topic.name,
        "max" if use_max_mode else "vector",
    )
    return results
