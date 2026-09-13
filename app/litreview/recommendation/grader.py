"""등급 분류. 주제별 단일 유사도 상위 N%로 순차 선정.

core:      상위 core_percent% → 전문 다운로드 + LLM 요약
related:   상위 related_percent%, core 제외 → 메타데이터만
reference: 상위 reference_percent%, core·related 제외 → 메타데이터만
"""

import logging

from app import db
from app.models import PaperRecommendation, ResearchTopic, Researcher

logger = logging.getLogger(__name__)


def assign_grades(
    topic_id: int,
    similarities: list[dict],
) -> list[dict]:
    """유사도 점수 기반 등급 분류 → paper_recommendations 저장.

    Args:
        topic_id: research_topics.id
        similarities: calculate_similarity() 반환값
            [{paper_id, similarity_score, similarity_details}]

    Returns:
        [{paper_id, grade, similarity_score, percentile_rank, grade_reason}]
    """
    if not similarities:
        return []

    topic = db.session.get(ResearchTopic, topic_id)
    if not topic:
        raise ValueError(f"Topic {topic_id} not found")

    researcher = db.session.get(Researcher, topic.researcher_id)
    if not researcher:
        raise ValueError(f"Researcher for topic {topic_id} not found")

    core_pct = researcher.core_percent or 5
    related_pct = researcher.related_percent or 15
    reference_pct = researcher.reference_percent or 25
    total = len(similarities)
    cap = researcher.max_per_grade  # None or integer, 등급별 상한

    # 유사도 내림차순 정렬
    sorted_sims = sorted(
        similarities, key=lambda x: x["similarity_score"], reverse=True
    )

    # 백분위 산출
    rank_map = {}
    for i, sim in enumerate(sorted_sims):
        rank_map[sim["paper_id"]] = (i + 1) / total

    # --- 순차 등급 선정 ---
    # core (상위 core_pct%, max_per_grade가 있으면 cap 적용)
    core_cutoff = max(1, int(total * core_pct / 100))
    if cap is not None:
        core_cutoff = min(core_cutoff, cap)
    core_ids = set()
    graded = []

    for rank, sim in enumerate(sorted_sims):
        if rank >= core_cutoff:
            break
        core_ids.add(sim["paper_id"])
        graded.append({
            **sim,
            "grade": "core",
            "percentile_rank": round(rank_map[sim["paper_id"]], 4),
            "grade_reason": f"유사도 상위 {round((rank+1)/total*100, 1)}%",
        })

    # related (상위 related_pct%, core 제외, cap 적용)
    remaining = [s for s in sorted_sims if s["paper_id"] not in core_ids]
    related_cutoff = max(1, int(total * related_pct / 100))
    if cap is not None:
        related_cutoff = min(related_cutoff, cap)
    related_ids = set()

    for rank, sim in enumerate(remaining):
        if rank >= related_cutoff:
            break
        related_ids.add(sim["paper_id"])
        pct = round((rank + 1) / len(remaining) * 100, 1) if remaining else 0
        graded.append({
            **sim,
            "grade": "related",
            "percentile_rank": round(rank_map[sim["paper_id"]], 4),
            "grade_reason": f"유사도 상위 {pct}% (core 제외)",
        })

    # reference (상위 reference_pct%, core·related 제외, cap 적용)
    excluded = core_ids | related_ids
    remaining2 = [s for s in sorted_sims if s["paper_id"] not in excluded]
    reference_cutoff = max(1, int(total * reference_pct / 100))
    if cap is not None:
        reference_cutoff = min(reference_cutoff, cap)

    for rank, sim in enumerate(remaining2):
        if rank >= reference_cutoff:
            break
        pct = round((rank + 1) / len(remaining2) * 100, 1) if remaining2 else 0
        graded.append({
            **sim,
            "grade": "reference",
            "percentile_rank": round(rank_map[sim["paper_id"]], 4),
            "grade_reason": f"유사도 상위 {pct}% (core·related 제외)",
        })

    # --- DB 저장 ---
    results = []
    for item in graded:
        pid = item["paper_id"]

        existing = PaperRecommendation.query.filter_by(
            paper_id=pid,
            researcher_id=topic.researcher_id,
            topic_id=topic_id,
        ).first()

        rec_data = {
            "similarity_score": item["similarity_score"],
            "percentile_rank": item["percentile_rank"],
            "similarity_details": item.get("similarity_details"),
            "grade": item["grade"],
            "grade_reason": item.get("grade_reason", ""),
        }

        if existing:
            for k, v in rec_data.items():
                setattr(existing, k, v)
        else:
            rec = PaperRecommendation(
                paper_id=pid,
                researcher_id=topic.researcher_id,
                topic_id=topic_id,
                **rec_data,
            )
            db.session.add(rec)

        results.append({
            "paper_id": pid,
            "grade": item["grade"],
            "similarity_score": item["similarity_score"],
            "percentile_rank": item["percentile_rank"],
            "grade_reason": item.get("grade_reason", ""),
        })

    db.session.commit()

    grade_counts = {}
    for r in results:
        grade_counts[r["grade"]] = grade_counts.get(r["grade"], 0) + 1

    logger.info(
        "Graded %d papers for topic %d '%s': %s (from %d total)",
        len(results),
        topic_id,
        topic.name,
        grade_counts,
        total,
    )
    return results
