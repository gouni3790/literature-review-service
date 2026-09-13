"""타겟 저널 신규 논문 감지 → 임베딩 → 연구원 매칭 → 등급 → core 요약."""

import logging
import time
from datetime import datetime

import requests
from flask import current_app
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.models import CollectedPaper, Researcher, TargetJournal

logger = logging.getLogger(__name__)

SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
SEARCH_DELAY = 0.15


def _create_session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config["SCOPUS_API_KEY"],
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "application/json",
    }


def _search_new_papers(
    journal_name: str,
    since: datetime | None,
    session: requests.Session,
) -> list[dict]:
    """저널의 신규 논문 검색."""
    query_parts = [f'SRCTITLE("{journal_name}")']
    if since:
        date_str = since.strftime("%Y-%m-%d")
        query_parts.append(f"ORIG-LOAD-DATE AFT {date_str}")

    query = " AND ".join(query_parts)

    params = {
        "query": query,
        "count": 25,
        "cursor": "*",
        "field": "dc:identifier,dc:title,author-name,prism:publicationName,"
        "prism:coverDate,prism:doi,dc:description",
        "sort": "-coverDate",
    }

    papers = []
    max_results = current_app.config.get("DEFAULT_MAX_PAPERS_PER_QUERY", 200)

    while len(papers) < max_results:
        time.sleep(SEARCH_DELAY)
        try:
            resp = session.get(
                SCOPUS_SEARCH_URL, headers=_headers(), params=params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json().get("search-results", {})
        except requests.RequestException:
            logger.exception("Journal monitor search failed for '%s'", journal_name)
            break

        entries = data.get("entry", [])
        if not entries or entries[0].get("@_fa") == "false":
            break

        for entry in entries:
            abstract = entry.get("dc:description", "")
            if not abstract:
                continue

            sid = entry.get("dc:identifier", "")
            if sid.startswith("SCOPUS_ID:"):
                sid = sid[len("SCOPUS_ID:"):]

            authors_list = entry.get("author", []) or []
            authors_str = "; ".join(a.get("authname", "") for a in authors_list[:10])

            year = None
            cover_date = entry.get("prism:coverDate", "")
            if cover_date and len(cover_date) >= 4:
                try:
                    year = int(cover_date[:4])
                except ValueError:
                    pass

            papers.append({
                "scopus_id": sid,
                "title": entry.get("dc:title", ""),
                "authors": authors_str,
                "journal": entry.get("prism:publicationName", ""),
                "year": year,
                "abstract": abstract,
                "doi": entry.get("prism:doi", ""),
            })

        links = data.get("link", [])
        next_link = next((l for l in links if l.get("@ref") == "next"), None)
        if not next_link:
            break
        cursor = next_link.get("@href", "").split("cursor=")[-1].split("&")[0]
        if not cursor or cursor == params["cursor"]:
            break
        params["cursor"] = cursor

    return papers


def _save_new_papers(raw_papers: list[dict]) -> list[int]:
    """신규 논문 저장. 이미 있으면 skip. 저장된 paper_id 리스트 반환."""
    new_ids = []
    for pd in raw_papers:
        if not pd["scopus_id"]:
            continue
        existing = CollectedPaper.query.filter_by(scopus_id=pd["scopus_id"]).first()
        if existing:
            continue
        cp = CollectedPaper(
            scopus_id=pd["scopus_id"],
            title=pd["title"],
            authors=pd["authors"],
            journal=pd["journal"],
            year=pd["year"],
            abstract=pd["abstract"],
            doi=pd["doi"],
            source_query=f"journal_monitor:{pd['journal']}",
        )
        db.session.add(cp)
        db.session.flush()
        new_ids.append(cp.id)

    db.session.commit()
    return new_ids


def monitor_target_journals(
    researcher_id: int | None = None,
    progress_callback=None,
) -> dict:
    """타겟 저널 신규 논문 모니터링.

    Args:
        researcher_id: 특정 연구원만. None이면 모든 활성 저널.
        progress_callback: fn(done, total).

    Returns:
        {journals_checked, new_papers_found, new_paper_ids}
    """
    from app.litreview.recommendation.embedder import Embedder
    from app.litreview.recommendation.grader import assign_grades
    from app.litreview.recommendation.similarity import calculate_similarity

    query = TargetJournal.query.filter_by(is_active=True)
    if researcher_id:
        query = query.filter_by(researcher_id=researcher_id)
    journals = query.all()

    if not journals:
        return {"journals_checked": 0, "new_papers_found": 0, "new_paper_ids": []}

    session = _create_session()
    all_new_ids: list[int] = []
    total = len(journals)

    # 저널별 → 연구원 매핑 (같은 저널을 여러 연구원이 가질 수 있음)
    journal_researchers: dict[str, list[int]] = {}
    for tj in journals:
        journal_researchers.setdefault(tj.journal_name, []).append(tj.researcher_id)

    checked = 0
    for journal_name, rids in journal_researchers.items():
        # last_checked 중 가장 오래된 것 사용
        tjs = [tj for tj in journals if tj.journal_name == journal_name]
        since = min(
            (tj.last_checked for tj in tjs if tj.last_checked),
            default=None,
        )

        logger.info("Monitoring journal '%s' (since %s)", journal_name, since)
        raw_papers = _search_new_papers(journal_name, since, session)
        new_ids = _save_new_papers(raw_papers)
        all_new_ids.extend(new_ids)

        # last_checked 업데이트
        now = datetime.utcnow()
        for tj in tjs:
            tj.last_checked = now
        db.session.commit()

        # 신규 논문 임베딩
        if new_ids:
            embedder = Embedder()
            embedder.embed_paper_abstracts(paper_ids=new_ids, table="collected_papers")

            # 각 연구원에 대해 유사도 + 등급
            for rid in set(rids):
                sims = calculate_similarity(rid, new_ids)
                if sims:
                    assign_grades(rid, sims)

        checked += 1
        if progress_callback:
            progress_callback(checked, len(journal_researchers))

    logger.info(
        "Journal monitoring: %d journals checked, %d new papers",
        len(journal_researchers),
        len(all_new_ids),
    )

    return {
        "journals_checked": len(journal_researchers),
        "new_papers_found": len(all_new_ids),
        "new_paper_ids": all_new_ids,
    }
