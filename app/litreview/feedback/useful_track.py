"""useful 트랙 추천 — 각 useful 논문 1대1 매칭 기반 단일 점수.

기존 주제별 추천 시스템과 분리된 별도 트랙.
score = cosine(candidate.embedding, useful.embedding)
threshold (기본 0.6) 이상만 추천. useful 논문별 그룹핑.
"""

import logging

import numpy as np

from app import db
from app.models import (
    CollectedPaper,
    InterestPaper,
    PaperRecommendation,
    ReferencePaper,
)

logger = logging.getLogger(__name__)

USEFUL_TRACK_THRESHOLD = 0.6


def _excluded_scopus_ids(researcher_id: int) -> set[str]:
    """본인 논문 + useful 논문 scopus_id (후보에서 제외)."""
    own = db.session.query(ReferencePaper.scopus_id).filter(
        ReferencePaper.researcher_id == researcher_id,
        ReferencePaper.scopus_id.isnot(None),
    ).all()
    useful = db.session.query(InterestPaper.scopus_id).filter(
        InterestPaper.researcher_id == researcher_id,
        InterestPaper.scopus_id.isnot(None),
    ).all()
    return {sid for (sid,) in own} | {sid for (sid,) in useful}


def _not_relevant_paper_ids(researcher_id: int) -> set[int]:
    """not_relevant 표시한 추천의 collected_papers.id."""
    rows = db.session.query(PaperRecommendation.paper_id).filter(
        PaperRecommendation.researcher_id == researcher_id,
        PaperRecommendation.user_feedback == "not_relevant",
    ).all()
    return {pid for (pid,) in rows}


def _useful_dict(u: InterestPaper) -> dict:
    return {
        "id": u.id,
        "scopus_id": u.scopus_id,
        "title": u.title,
        "journal": u.journal,
        "year": u.year,
        "doi": u.doi,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def calculate_useful_recommendations(
    researcher_id: int, threshold: float = USEFUL_TRACK_THRESHOLD
) -> list[dict]:
    """useful별 1대1 매칭 추천.

    Args:
        researcher_id: 연구원 id
        threshold: cosine 임계치 (기본 0.6)

    Returns:
        [
          {
            "useful_paper": {id, scopus_id, title, journal, year, doi, created_at},
            "recommendations": [
              {paper_id, score, title, journal, year, scopus_id, doi},
              ...
            ]  # threshold 이상, score 내림차순
          },
          ...  # useful_paper.created_at 내림차순 (최근 useful 먼저)
        ]
    """
    useful_papers = (
        InterestPaper.query.filter(
            InterestPaper.researcher_id == researcher_id,
            InterestPaper.embedding.isnot(None),
        )
        .order_by(InterestPaper.created_at.desc())
        .all()
    )
    if not useful_papers:
        return []

    excluded_sids = _excluded_scopus_ids(researcher_id)
    excluded_pids = _not_relevant_paper_ids(researcher_id)

    cand_query = CollectedPaper.query.filter(
        CollectedPaper.embedding.isnot(None)
    )
    if excluded_sids:
        cand_query = cand_query.filter(
            ~CollectedPaper.scopus_id.in_(excluded_sids)
        )
    if excluded_pids:
        cand_query = cand_query.filter(~CollectedPaper.id.in_(excluded_pids))
    candidates = cand_query.all()

    if not candidates:
        return [{"useful_paper": _useful_dict(u), "recommendations": []} for u in useful_papers]

    # 후보 임베딩 행렬 (정규화 1회)
    cand_embs = np.array([c.embedding for c in candidates], dtype=np.float32)
    cand_norms = np.linalg.norm(cand_embs, axis=1, keepdims=True)
    cand_norms[cand_norms == 0] = 1
    cand_normed = cand_embs / cand_norms

    results: list[dict] = []
    total_recs = 0

    for u in useful_papers:
        u_emb = np.array(u.embedding, dtype=np.float32)
        u_norm = np.linalg.norm(u_emb)
        if u_norm == 0:
            results.append({"useful_paper": _useful_dict(u), "recommendations": []})
            continue
        u_normed = u_emb / u_norm

        sims = cand_normed @ u_normed  # shape (N,)

        passed = np.where(sims >= threshold)[0]
        if passed.size == 0:
            results.append({"useful_paper": _useful_dict(u), "recommendations": []})
            continue

        order = passed[np.argsort(-sims[passed])]
        recs = []
        for idx in order:
            c = candidates[idx]
            recs.append({
                "paper_id": c.id,
                "score": round(float(sims[idx]), 4),
                "title": c.title,
                "journal": c.journal,
                "year": c.year,
                "scopus_id": c.scopus_id,
                "doi": c.doi,
            })
        total_recs += len(recs)
        results.append({"useful_paper": _useful_dict(u), "recommendations": recs})

    logger.info(
        "Useful track for researcher %d: %d useful, %d candidates, %d total recs (threshold=%.2f)",
        researcher_id,
        len(useful_papers),
        len(candidates),
        total_recs,
        threshold,
    )
    return results
