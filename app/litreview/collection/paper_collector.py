"""Scopus Search API로 논문 수집. 중복 제거 + collected_papers upsert."""

import logging
import time
from datetime import datetime
from urllib.parse import unquote

import requests as req
from flask import current_app
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.models import CollectedPaper, SearchQuery

logger = logging.getLogger(__name__)

SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
SEARCH_DELAY = 0.15
PAGE_SIZE = 25


def _create_session() -> req.Session:
    retry = Retry(
        total=5,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session = req.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config["SCOPUS_API_KEY"],
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "application/json",
    }


def _search_scopus(
    query_string: str,
    session: req.Session,
    max_results: int | None = None,
) -> list[dict]:
    """Scopus Search API 호출. cursor 페이지네이션으로 전체 수집."""
    if max_results is None:
        max_results = current_app.config.get("DEFAULT_MAX_PAPERS_PER_QUERY", 200)

    params = {
        "query": query_string,
        "count": PAGE_SIZE,
        "cursor": "*",
        "field": "dc:identifier,dc:title,author-name,prism:publicationName,"
        "prism:coverDate,prism:doi,dc:description",
        "sort": "-coverDate",
    }

    papers = []
    while len(papers) < max_results:
        time.sleep(SEARCH_DELAY)
        try:
            resp = session.get(
                SCOPUS_SEARCH_URL, headers=_headers(), params=params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json().get("search-results", {})
        except req.RequestException:
            logger.exception("Scopus search failed for query: %.100s", query_string)
            break

        entries = data.get("entry", [])
        if not entries or entries[0].get("@_fa") == "false":
            break

        for entry in entries:
            if len(papers) >= max_results:
                break

            abstract = entry.get("dc:description", "") or ""

            sid = entry.get("dc:identifier", "")
            if sid.startswith("SCOPUS_ID:"):
                sid = sid[len("SCOPUS_ID:"):]

            authors_list = entry.get("author", []) or []
            authors_str = "; ".join(
                a.get("authname", "") for a in authors_list[:10]
            )

            year = None
            cover_date = entry.get("prism:coverDate", "")
            if cover_date and len(cover_date) >= 4:
                try:
                    year = int(cover_date[:4])
                except ValueError:
                    pass

            papers.append(
                {
                    "scopus_id": sid,
                    "title": entry.get("dc:title", ""),
                    "authors": authors_str,
                    "journal": entry.get("prism:publicationName", ""),
                    "year": year,
                    "abstract": abstract,
                    "doi": entry.get("prism:doi", ""),
                }
            )

        links = data.get("link", [])
        next_link = next(
            (l for l in links if l.get("@ref") == "next"), None
        )
        if not next_link:
            break
        raw_cursor = next_link.get("@href", "").split("cursor=")[-1].split("&")[0]
        cursor = unquote(raw_cursor)
        if not cursor or cursor == params["cursor"]:
            break
        params["cursor"] = cursor

    return papers


def collect_papers(
    queries: list[dict],
    researcher_id: int | None = None,
    progress_callback=None,
) -> dict:
    """쿼리 리스트로 논문 수집 → collected_papers upsert.

    Args:
        queries: query_builder.build_queries() 의 반환값.
        researcher_id: search_queries 로그용.
        progress_callback: fn(done_queries, total_queries).

    Returns:
        {
            total_collected, new_papers, skipped_duplicates, queries_executed,
            collected_paper_ids: list[int]  # 이번 검색으로 매칭에 써야 할 paper_id 전체
                                            # (신규 + 기존 모두 포함, 중복 제거)
            new_paper_ids: list[int]        # 이번에 새로 추가된 것만 (임베딩 대상)
        }
    """
    session = _create_session()
    total_new = 0
    total_skipped = 0
    all_paper_ids: set[int] = set()   # 신규 + 기존 (이번 검색에서 만난 모든 paper)
    new_paper_ids: list[int] = []     # 이번에 새로 추가된 것만

    for idx, q in enumerate(queries):
        query_str = q["query_string"]
        topic_id = q.get("topic_id")

        logger.info(
            "Executing query %d/%d: %.100s...", idx + 1, len(queries), query_str
        )
        raw_papers = _search_scopus(query_str, session)

        new_in_query = 0
        skipped_in_query = 0

        for paper_data in raw_papers:
            if not paper_data["scopus_id"]:
                continue

            existing = CollectedPaper.query.filter_by(
                scopus_id=paper_data["scopus_id"]
            ).first()
            if existing:
                skipped_in_query += 1
                all_paper_ids.add(existing.id)  # 기존 논문도 매칭 대상에 포함
                continue

            cp = CollectedPaper(
                scopus_id=paper_data["scopus_id"],
                title=paper_data["title"],
                authors=paper_data["authors"],
                journal=paper_data["journal"],
                year=paper_data["year"],
                abstract=paper_data["abstract"],
                doi=paper_data["doi"],
                source_query=query_str,
            )
            db.session.add(cp)
            db.session.flush()  # cp.id 확보
            new_in_query += 1
            all_paper_ids.add(cp.id)
            new_paper_ids.append(cp.id)

        db.session.commit()
        total_new += new_in_query
        total_skipped += skipped_in_query

        # search_queries 로그
        if researcher_id:
            sq = SearchQuery(
                researcher_id=researcher_id,
                query_string=query_str,
                topic_id=topic_id,
                last_executed=datetime.utcnow(),
                result_count=len(raw_papers),
            )
            db.session.add(sq)
            db.session.commit()

        logger.info(
            "Query %d: %d raw, %d new, %d duplicates",
            idx + 1,
            len(raw_papers),
            new_in_query,
            skipped_in_query,
        )

        if progress_callback:
            progress_callback(idx + 1, len(queries))

    return {
        "total_collected": total_new + total_skipped,
        "new_papers": total_new,
        "skipped_duplicates": total_skipped,
        "queries_executed": len(queries),
        "collected_paper_ids": list(all_paper_ids),
        "new_paper_ids": new_paper_ids,
    }


ABSTRACT_RETRIEVAL_URL = "https://api.elsevier.com/content/abstract/scopus_id/"


def backfill_abstracts(paper_ids: list[int] | None = None) -> int:
    """초록이 없는 collected_papers 에 Scopus Abstract Retrieval API 로 보충.

    Args:
        paper_ids: 특정 논문만. None 이면 abstract 가 비어있는 전체.

    Returns:
        보충된 논문 수.
    """
    query = CollectedPaper.query.filter(
        (CollectedPaper.abstract.is_(None)) | (CollectedPaper.abstract == "")
    )
    if paper_ids:
        query = query.filter(CollectedPaper.id.in_(paper_ids))

    papers = query.all()
    if not papers:
        logger.info("No collected papers need abstract backfill")
        return 0

    session = _create_session()
    updated = 0

    for i, p in enumerate(papers):
        if not p.scopus_id:
            continue

        time.sleep(0.2)
        try:
            resp = session.get(
                f"{ABSTRACT_RETRIEVAL_URL}{p.scopus_id}",
                headers=_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                core = data.get("abstracts-retrieval-response", {}).get("coredata", {})
                abstract = core.get("dc:description", "") or ""
                if abstract:
                    p.abstract = abstract
                    updated += 1
            else:
                logger.warning(
                    "Abstract retrieval HTTP %d for %s", resp.status_code, p.scopus_id
                )
        except req.RequestException:
            logger.exception("Abstract retrieval failed for %s", p.scopus_id)

        if (i + 1) % 25 == 0:
            db.session.commit()

    db.session.commit()
    logger.info("Backfilled %d/%d abstracts", updated, len(papers))
    return updated
