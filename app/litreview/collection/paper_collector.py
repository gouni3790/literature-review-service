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
ABSTRACT_DELAY = 0.2

# Abstract Retrieval 은 Search 와 **쿼터가 별개**다 (주 10,000회 수준).
# 한 번의 호출로 다 써버리지 않도록 기본 상한을 둔다. 필요하면 호출부에서 조정.
ABSTRACT_MAX_CALLS_DEFAULT = 2000


def fetch_abstracts(
    scopus_ids: list[str],
    max_calls: int | None = None,
    progress_callback=None,
) -> dict[str, str]:
    """Scopus Abstract Retrieval API 로 초록을 받아온다. DB 를 건드리지 않는다.

    Scopus Search API 는 `dc:description`(초록)을 요청해도 대부분 빈 값을
    돌려준다. 초록은 논문 1건씩 Abstract Retrieval 로 따로 받아야 한다.
    이 단계가 없으면 수집 논문 대부분이 임베딩 불가 상태로 쌓이고 추천 후보에서
    영구 제외된다 (운영 실측: 5,479편 중 3,916편(71%)이 그렇게 유실).

    저장 없이 초록만 필요한 쪽(저널 발굴 파이프라인)과 DB 보충(backfill_abstracts)이
    같은 구현을 쓰도록 여기서 분리했다.

    Args:
        scopus_ids: 조회할 Scopus ID 목록 (중복·빈 값은 알아서 걸러낸다)
        max_calls: 호출 상한. None 이면 ABSTRACT_MAX_CALLS_DEFAULT.
        progress_callback: fn(done, total)

    Returns:
        {scopus_id: abstract} — 못 받은 것은 키 자체가 없다.
    """
    ids = [s for s in dict.fromkeys(scopus_ids) if s]
    if not ids:
        return {}

    cap = ABSTRACT_MAX_CALLS_DEFAULT if max_calls is None else max_calls
    if len(ids) > cap:
        logger.warning(
            "초록 조회 대상 %d건이 상한 %d건을 초과 — 앞에서 %d건만 조회합니다",
            len(ids), cap, cap,
        )
        ids = ids[:cap]

    session = _create_session()
    out: dict[str, str] = {}
    failed = 0
    try:
        for i, sid in enumerate(ids, 1):
            time.sleep(ABSTRACT_DELAY)
            try:
                resp = session.get(
                    f"{ABSTRACT_RETRIEVAL_URL}{sid}",
                    headers=_headers(),
                    timeout=15,
                )
                if resp.status_code == 200:
                    core = (
                        resp.json()
                        .get("abstracts-retrieval-response", {})
                        .get("coredata", {})
                    )
                    abstract = (core.get("dc:description") or "").strip()
                    if abstract:
                        out[sid] = abstract
                else:
                    failed += 1
                    logger.debug(
                        "Abstract retrieval HTTP %d for %s", resp.status_code, sid
                    )
            except req.RequestException:
                failed += 1
                logger.debug("Abstract retrieval 실패: %s", sid, exc_info=True)

            if progress_callback and i % 25 == 0:
                progress_callback(i, len(ids))
    finally:
        session.close()

    logger.info(
        "초록 조회: %d/%d 성공 (실패 %d)", len(out), len(ids), failed
    )
    return out


def backfill_abstracts(
    paper_ids: list[int] | None = None,
    max_calls: int | None = None,
) -> int:
    """초록이 없는 collected_papers 에 Scopus Abstract Retrieval 로 보충.

    Args:
        paper_ids: 특정 논문만. None 이면 초록이 비어있는 전체.
        max_calls: Abstract Retrieval 호출 상한.

    Returns:
        보충된 논문 수.
    """
    query = CollectedPaper.query.filter(
        (CollectedPaper.abstract.is_(None)) | (CollectedPaper.abstract == "")
    )
    if paper_ids:
        query = query.filter(CollectedPaper.id.in_(paper_ids))

    papers = [p for p in query.all() if p.scopus_id]
    if not papers:
        logger.info("초록 보충 대상 없음")
        return 0

    by_sid = {p.scopus_id: p for p in papers}
    fetched = fetch_abstracts(list(by_sid), max_calls=max_calls)

    updated = 0
    for sid, abstract in fetched.items():
        p = by_sid.get(sid)
        if p is not None:
            p.abstract = abstract
            updated += 1

    db.session.commit()
    logger.info("초록 보충 완료: %d/%d편", updated, len(papers))
    return updated
