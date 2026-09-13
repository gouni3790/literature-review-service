"""useful 피드백 처리 — interest_papers + refs + 미니 쿼리 후보 수집.

흐름:
    1. Scopus default view → 메타데이터 풀세트 (authors, keywords 보강)
    2. OpenAI embed_text → abstract 임베딩 생성
    3. interest_papers UPSERT (이미 있으면 갱신, created_at은 보존)
    4. Scopus view=REF → 참고문헌 수집 → paper_references INSERT
       (interest_paper_id로 연결)
    5. useful 키워드로 미니 Scopus 검색 → 신규 후보를 collected_papers로
    6. 신규 후보 임베딩 생성

응답 지연 방지를 위해 모두 백그라운드 스레드로 실행.
"""

import logging
import threading
from datetime import datetime

from flask import Flask

from app import db
from app.models import InterestPaper, PaperReference, Researcher, TargetJournal
from app.litreview.collection.paper_collector import backfill_abstracts, collect_papers
from app.litreview.profile.scopus_fetcher import (
    _create_session,
    _fetch_paper_full_metadata,
    _fetch_paper_references,
)
from app.litreview.recommendation.embedder import Embedder

logger = logging.getLogger(__name__)

MINI_QUERY_MAX_KEYWORDS = 8  # 너무 많으면 쿼리가 무거워짐


def upsert_interest_paper(researcher_id: int, scopus_id: str) -> dict:
    """useful 논문을 interest_papers에 upsert.

    Args:
        researcher_id: 피드백한 연구원
        scopus_id: 논문 Scopus ID

    Returns:
        {action, id, scopus_id, has_embedding}
        action ∈ {'created', 'updated', 'fetch_failed'}
    """
    if not scopus_id:
        return {"action": "skip_no_scopus_id"}

    # 1. Scopus 메타데이터 풀세트 가져오기 (authors/keywords 보강)
    session = _create_session()
    meta = _fetch_paper_full_metadata(scopus_id, session)
    if not meta:
        logger.warning(
            "Failed to fetch metadata for scopus_id=%s (researcher=%d)",
            scopus_id,
            researcher_id,
        )
        return {"action": "fetch_failed", "scopus_id": scopus_id}

    # 2. abstract 있으면 임베딩 생성
    embedding = None
    if meta.get("abstract"):
        try:
            embedder = Embedder()
            embedding = embedder.embed_text(meta["abstract"])
        except Exception as e:
            logger.exception(
                "Embedding failed for scopus_id=%s: %s", scopus_id, e
            )
            embedding = None

    # 3. UPSERT
    existing = InterestPaper.query.filter_by(
        researcher_id=researcher_id, scopus_id=scopus_id
    ).first()

    if existing:
        # 갱신 (created_at은 보존)
        existing.title = meta.get("title")
        existing.authors = meta.get("authors")
        existing.journal = meta.get("journal")
        existing.year = meta.get("year")
        existing.doi = meta.get("doi")
        existing.abstract = meta.get("abstract")
        existing.keywords = meta.get("keywords") or []
        if embedding:
            existing.embedding = embedding
        db.session.commit()
        action = "updated"
        ip_id = existing.id
    else:
        ip = InterestPaper(
            researcher_id=researcher_id,
            scopus_id=scopus_id,
            title=meta.get("title"),
            authors=meta.get("authors"),
            journal=meta.get("journal"),
            year=meta.get("year"),
            doi=meta.get("doi"),
            abstract=meta.get("abstract"),
            keywords=meta.get("keywords") or [],
            embedding=embedding,
        )
        db.session.add(ip)
        db.session.commit()
        action = "created"
        ip_id = ip.id

    logger.info(
        "Interest paper %s: researcher=%d, scopus_id=%s, embedding=%s",
        action,
        researcher_id,
        scopus_id,
        "yes" if embedding else "no",
    )
    return {
        "action": action,
        "id": ip_id,
        "scopus_id": scopus_id,
        "has_embedding": bool(embedding),
    }


def collect_refs_for_interest_paper(
    interest_paper_id: int, scopus_id: str
) -> int:
    """useful 논문의 refs를 paper_references에 적재 (interest_paper_id로 연결).

    이미 같은 (interest_paper_id, ref_scopus_id)가 있으면 skip.

    Returns:
        새로 추가된 ref 행 수.
    """
    if not scopus_id:
        return 0

    session = _create_session()
    refs = _fetch_paper_references(scopus_id, session)
    if not refs:
        logger.info(
            "No refs fetched for interest_paper_id=%d (scopus_id=%s)",
            interest_paper_id,
            scopus_id,
        )
        return 0

    # 이미 적재된 ref_scopus_id 집합 (중복 방지)
    existing_ref_ids = {
        r[0]
        for r in db.session.query(PaperReference.ref_scopus_id)
        .filter(PaperReference.interest_paper_id == interest_paper_id)
        .all()
    }

    added = 0
    for ref in refs:
        if not ref["ref_scopus_id"]:
            continue
        if ref["ref_scopus_id"] in existing_ref_ids:
            continue
        db.session.add(
            PaperReference(
                paper_id=None,
                interest_paper_id=interest_paper_id,
                ref_scopus_id=ref["ref_scopus_id"],
                ref_title=ref["ref_title"],
                ref_authors=ref["ref_authors"],
                ref_year=ref["ref_year"],
                ref_journal=ref["ref_journal"],
                ref_doi=ref["ref_doi"],
            )
        )
        added += 1

    db.session.commit()
    logger.info(
        "Collected %d refs for interest_paper_id=%d (scopus_id=%s)",
        added,
        interest_paper_id,
        scopus_id,
    )
    return added


def _build_useful_mini_query(
    interest_paper: InterestPaper, researcher_id: int, year_after: int
) -> str | None:
    """useful 논문 키워드 OR 결합 + 연구원 target_journals 제약.

    형식: (SRCTITLE OR ...) AND TITLE-ABS-KEY(kw1 OR kw2 OR ...) AND PUBYEAR > X

    저널이 분야를 좁히고, 키워드 OR로 광범위 발굴.

    Returns:
        Scopus 쿼리 문자열 또는 None (키워드/저널 없으면).
    """
    keywords = interest_paper.keywords or []
    if not keywords:
        return None

    import re
    kw_items = []
    for kw in keywords[:MINI_QUERY_MAX_KEYWORDS]:
        clean = re.sub(r"[()]", "", str(kw)).strip()
        if not clean:
            continue
        kw_items.append(f'"{clean}"' if " " in clean else clean)

    if not kw_items:
        return None
    kw_clause = " OR ".join(kw_items)

    # 연구원의 활성 target_journals
    targets = TargetJournal.query.filter_by(
        researcher_id=researcher_id, is_active=True
    ).all()
    if not targets:
        logger.warning(
            "Researcher %d has no active target_journals — mini query skipped",
            researcher_id,
        )
        return None

    journal_clause = " OR ".join(
        f'SRCTITLE("{tj.journal_name}")' for tj in targets
    )

    return (
        f"({journal_clause}) "
        f"AND TITLE-ABS-KEY({kw_clause}) "
        f"AND PUBYEAR > {year_after}"
    )


def collect_papers_from_useful_mini_query(
    interest_paper_id: int, researcher_id: int
) -> dict:
    """useful 논문 키워드로 미니 쿼리 → 신규 후보 수집 + 임베딩 생성.

    Returns:
        {query, new_papers, total_collected, embedded}
    """
    ip = db.session.get(InterestPaper, interest_paper_id)
    if not ip:
        return {"error": "interest_paper not found"}

    researcher = db.session.get(Researcher, researcher_id)
    year_range = (researcher.target_year_range or 3) if researcher else 3
    year_after = datetime.utcnow().year - year_range

    query_str = _build_useful_mini_query(ip, researcher_id, year_after)
    if not query_str:
        logger.info(
            "Useful mini query skipped (no keywords) for interest_paper=%d",
            interest_paper_id,
        )
        return {"skipped": "no keywords"}

    logger.info(
        "Useful mini query for interest_paper=%d: %s",
        interest_paper_id,
        query_str[:200],
    )

    result = collect_papers(
        queries=[{"query_string": query_str, "topic_id": None}],
        researcher_id=researcher_id,
    )

    # Search API는 abstract를 truncate하므로 Abstract Retrieval로 보강 후 임베딩
    new_ids = result.get("new_paper_ids", [])
    abstracts_filled = 0
    embedded = 0
    if new_ids:
        try:
            abstracts_filled = backfill_abstracts(paper_ids=new_ids)
        except Exception:
            logger.exception(
                "backfill_abstracts failed (interest_paper=%d)", interest_paper_id
            )
        try:
            embedder = Embedder()
            embedded = embedder.embed_paper_abstracts(
                paper_ids=new_ids, table="collected_papers"
            )
        except Exception:
            logger.exception(
                "Embedding new candidates failed (interest_paper=%d)",
                interest_paper_id,
            )

    summary = {
        "query": query_str,
        "new_papers": result.get("new_papers", 0),
        "total_collected": result.get("total_collected", 0),
        "abstracts_filled": abstracts_filled,
        "embedded": embedded,
    }
    logger.info(
        "Mini query result for interest_paper=%d: %s", interest_paper_id, summary
    )
    return summary


def _run_in_app_context(app: Flask, researcher_id: int, scopus_id: str) -> None:
    """백그라운드 스레드용 wrapper. Flask app_context 안에서 모든 단계 실행."""
    with app.app_context():
        try:
            # 1~3: interest_papers UPSERT (메타+임베딩)
            result = upsert_interest_paper(researcher_id, scopus_id)
            ip_id = result.get("id")
            if not ip_id or result.get("action") not in ("created", "updated"):
                return

            # 4: useful 논문의 refs 수집
            try:
                collect_refs_for_interest_paper(ip_id, scopus_id)
            except Exception:
                logger.exception("collect_refs_for_interest_paper failed")

            # 5~6: useful 미니 쿼리 → 신규 후보 + 임베딩
            try:
                collect_papers_from_useful_mini_query(ip_id, researcher_id)
            except Exception:
                logger.exception("collect_papers_from_useful_mini_query failed")
        except Exception:
            logger.exception(
                "Background useful feedback processing failed for "
                "researcher=%d scopus_id=%s",
                researcher_id,
                scopus_id,
            )


def trigger_useful_feedback_async(
    app: Flask, researcher_id: int, scopus_id: str
) -> None:
    """useful 피드백 시 백그라운드 처리 시작 (응답은 즉시 반환).

    호출 측은 current_app._get_current_object()를 app으로 전달.
    """
    if not scopus_id:
        return

    thread = threading.Thread(
        target=_run_in_app_context,
        args=(app, researcher_id, scopus_id),
        daemon=True,
        name=f"interest-upsert-{researcher_id}-{scopus_id}",
    )
    thread.start()
