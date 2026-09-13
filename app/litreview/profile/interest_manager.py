"""관심 논문 관리 — DOI 기반 Scopus 수집 + 임베딩 + CRUD."""

import logging
import time

import requests
from flask import current_app

from app import db
from app.models import InterestPaper, Researcher

logger = logging.getLogger(__name__)

ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/"
SEARCH_DELAY = 0.2


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config["SCOPUS_API_KEY"],
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "application/json",
    }


def _fetch_paper_by_doi(doi: str) -> dict | None:
    """DOI로 Scopus Abstract Retrieval API 호출 → 논문 정보 수집.

    수집 항목: 제목, 저자, 저널, 연도, 초록, 저자 키워드, 참고문헌 저널 목록.
    """
    try:
        resp = requests.get(
            f"{ABSTRACT_URL}{doi}",
            headers=_headers(),
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning("Scopus DOI lookup failed: %s (HTTP %d)", doi, resp.status_code)
            return None

        data = resp.json().get("abstracts-retrieval-response", {})
    except requests.RequestException:
        logger.exception("Scopus DOI lookup error: %s", doi)
        return None

    coredata = data.get("coredata", {})
    scopus_id = coredata.get("dc:identifier", "")
    if scopus_id.startswith("SCOPUS_ID:"):
        scopus_id = scopus_id[len("SCOPUS_ID:"):]

    # 저자 키워드
    keywords = []
    auth_kw = data.get("authkeywords", {})
    if auth_kw and isinstance(auth_kw, dict):
        kw_list = auth_kw.get("author-keyword", [])
        if isinstance(kw_list, list):
            keywords = [k.get("$", "") for k in kw_list if k.get("$")]
        elif isinstance(kw_list, dict):
            keywords = [kw_list.get("$", "")]

    # 저자
    authors_raw = data.get("authors", {}).get("author", []) or []
    if isinstance(authors_raw, dict):
        authors_raw = [authors_raw]
    authors_str = "; ".join(
        a.get("ce:indexed-name", "") for a in authors_raw[:10]
    )

    # 연도
    year = None
    cover_date = coredata.get("prism:coverDate", "")
    if cover_date and len(cover_date) >= 4:
        try:
            year = int(cover_date[:4])
        except ValueError:
            pass

    # 참고문헌 저널 목록
    ref_journals = []
    bibliography = data.get("item", {}).get("bibrecord", {}).get("tail", {}).get("bibliography", {})
    references = bibliography.get("reference", []) or []
    if isinstance(references, dict):
        references = [references]

    for ref in references:
        ref_info = ref.get("ref-info", {})
        ref_source = ref_info.get("ref-sourcetitle", "")
        if ref_source:
            ref_journals.append(ref_source)

    return {
        "scopus_id": scopus_id,
        "title": coredata.get("dc:title", ""),
        "authors": authors_str,
        "journal": coredata.get("prism:publicationName", ""),
        "year": year,
        "abstract": coredata.get("dc:description", "") or "",
        "keywords": keywords,
        "ref_journals": ref_journals,
    }


def add_interest_paper(researcher_id: int, doi: str) -> dict:
    """DOI로 관심 논문 추가 → Scopus 수집 → 임베딩 → DB 저장.

    Args:
        researcher_id: researchers.id
        doi: 논문 DOI (예: "10.1016/j.enbuild.2024.114500")

    Returns:
        {id, title, journal, keywords, ref_journals_count, embedded}
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        raise ValueError(f"Researcher {researcher_id} not found")

    # 중복 체크
    existing = InterestPaper.query.filter_by(
        researcher_id=researcher_id, doi=doi
    ).first()
    if existing:
        raise ValueError(f"Interest paper with DOI {doi} already exists")

    # Scopus에서 수집
    paper_data = _fetch_paper_by_doi(doi)
    if not paper_data:
        raise ValueError(f"Could not fetch paper with DOI {doi} from Scopus")

    ip = InterestPaper(
        researcher_id=researcher_id,
        scopus_id=paper_data["scopus_id"],
        title=paper_data["title"],
        authors=paper_data["authors"],
        journal=paper_data["journal"],
        year=paper_data["year"],
        doi=doi,
        abstract=paper_data["abstract"],
        keywords=paper_data["keywords"],
        ref_journals=paper_data["ref_journals"],
    )
    db.session.add(ip)
    db.session.flush()

    # 임베딩
    embedded = False
    if paper_data["abstract"]:
        try:
            from app.litreview.recommendation.embedder import Embedder

            embedder = Embedder()
            emb = embedder.embed_text(paper_data["abstract"])
            if emb:
                ip.embedding = emb
                embedded = True
        except Exception:
            logger.exception("Failed to embed interest paper %d", ip.id)

    db.session.commit()

    logger.info(
        "Added interest paper %d for researcher %d: %s (keywords=%d, refs=%d)",
        ip.id,
        researcher_id,
        ip.title[:60],
        len(paper_data["keywords"]),
        len(paper_data["ref_journals"]),
    )

    return {
        "id": ip.id,
        "title": ip.title,
        "journal": ip.journal,
        "year": ip.year,
        "keywords": paper_data["keywords"],
        "ref_journals_count": len(paper_data["ref_journals"]),
        "embedded": embedded,
    }


def get_interest_papers(researcher_id: int) -> list[dict]:
    """관심 논문 목록 조회."""
    papers = InterestPaper.query.filter_by(researcher_id=researcher_id).all()
    return [
        {
            "id": p.id,
            "title": p.title,
            "journal": p.journal,
            "year": p.year,
            "doi": p.doi,
            "keywords": p.keywords or [],
            "ref_journals": p.ref_journals or [],
            "has_embedding": p.embedding is not None,
            "created_at": str(p.created_at),
        }
        for p in papers
    ]


def delete_interest_paper(researcher_id: int, paper_id: int) -> bool:
    """관심 논문 삭제."""
    paper = InterestPaper.query.filter_by(
        id=paper_id, researcher_id=researcher_id
    ).first()
    if not paper:
        return False

    db.session.delete(paper)
    db.session.commit()
    logger.info("Deleted interest paper %d for researcher %d", paper_id, researcher_id)
    return True
