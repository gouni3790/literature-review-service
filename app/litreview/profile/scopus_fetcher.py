"""Scopus API로 연구원의 논문 목록, 저자 키워드, 참고문헌 저널 수집."""

import logging
import time
from urllib.parse import unquote

import requests
from flask import current_app
from requests.adapters import HTTPAdapter
from sqlalchemy import func
from urllib3.util.retry import Retry

from app import db
from app.models import (
    PaperKeyword,
    PaperReference,
    ReferencePaper,
    Researcher,
)

logger = logging.getLogger(__name__)

SCOPUS_BASE = "https://api.elsevier.com"
AUTHOR_RETRIEVAL_URL = f"{SCOPUS_BASE}/content/author/author_id/{{author_id}}"
SEARCH_URL = f"{SCOPUS_BASE}/content/search/scopus"
ABSTRACT_URL = f"{SCOPUS_BASE}/content/abstract/scopus_id/{{scopus_id}}"

# Scopus Search API: 9 req/s, Author Retrieval: 3 req/s — 안전 마진 적용
SEARCH_DELAY = 0.15
AUTHOR_DELAY = 0.4
ABSTRACT_DELAY = 0.15


def _create_session() -> requests.Session:
    """재시도 로직 내장 HTTP 세션."""
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


def fetch_author_info(scopus_id: str) -> dict | None:
    """Scopus Author Retrieval API로 기본 정보 조회."""
    session = _create_session()
    url = AUTHOR_RETRIEVAL_URL.format(author_id=scopus_id)

    try:
        resp = session.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        profile = data.get("author-retrieval-response", [{}])[0]
        preferred = profile.get("author-profile", {}).get("preferred-name", {})
        return {
            "name": f"{preferred.get('given-name', '')} {preferred.get('surname', '')}".strip(),
            "doc_count": int(
                profile.get("coredata", {}).get("document-count", 0)
            ),
        }
    except requests.RequestException:
        logger.exception("Author retrieval failed for %s", scopus_id)
        return None


def _search_author_papers(
    scopus_id: str, session: requests.Session
) -> list[dict]:
    """AU-ID 검색으로 연구원의 전체 논문 목록 가져오기. cursor 페이지네이션."""
    query = f"AU-ID({scopus_id})"
    papers = []
    params = {
        "query": query,
        "count": 25,
        "cursor": "*",
        "field": "dc:identifier,dc:title,dc:creator,author,prism:publicationName,"
        "prism:volume,prism:coverDate,prism:doi,dc:description,authkeywords,"
        "citedby-count",
        "sort": "-coverDate",
    }

    while True:
        time.sleep(SEARCH_DELAY)
        try:
            resp = session.get(
                SEARCH_URL, headers=_headers(), params=params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json().get("search-results", {})
        except requests.RequestException:
            logger.exception("Scopus search failed for AU-ID %s", scopus_id)
            break

        entries = data.get("entry", [])
        if not entries or entries[0].get("@_fa") == "false":
            break

        for entry in entries:
            sid = entry.get("dc:identifier", "")
            if sid.startswith("SCOPUS_ID:"):
                sid = sid[len("SCOPUS_ID:"):]

            keywords_raw = entry.get("authkeywords", "") or ""
            keywords = [
                k.strip() for k in keywords_raw.split("|") if k.strip()
            ]

            # 저자 목록 (주저자 + 공저자 모두). author 배열 우선, 없으면 dc:creator.
            authors_list = entry.get("author", []) or []
            if isinstance(authors_list, dict):
                authors_list = [authors_list]
            names = []
            for a in authors_list[:50]:
                nm = (a.get("authname") or a.get("ce:indexed-name") or "").strip()
                if nm and nm not in names:
                    names.append(nm)
            if not names:
                creator = (entry.get("dc:creator") or "").strip()
                if creator:
                    names = [creator]
            authors_str = "; ".join(names)

            year = None
            cover_date = entry.get("prism:coverDate", "")
            if cover_date and len(cover_date) >= 4:
                try:
                    year = int(cover_date[:4])
                except ValueError:
                    pass

            cited_by = 0
            try:
                cited_by = int(entry.get("citedby-count", 0))
            except (ValueError, TypeError):
                pass

            papers.append(
                {
                    "scopus_id": sid,
                    "title": entry.get("dc:title", ""),
                    "authors": authors_str,
                    "journal": entry.get("prism:publicationName", ""),
                    "volume": entry.get("prism:volume", ""),
                    "year": year,
                    "abstract": entry.get("dc:description", ""),
                    "doi": entry.get("prism:doi", ""),
                    "keywords": keywords,
                    "cited_by": cited_by,
                }
            )

        # 다음 페이지
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


def _scalar(v) -> str:
    """Scopus 응답의 string/dict/list 혼합 필드를 단일 문자열로 정규화.

    Scopus는 같은 필드를 ``"foo"`` / ``{"$": "foo"}`` / ``[{"$": "foo"}, ...]``
    중 어느 형태로도 줄 수 있어 방어적 처리가 필요.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("$", "") or "")
    if isinstance(v, list):
        for item in v:
            s = _scalar(item)
            if s:
                return s
        return ""
    return str(v)


def _fetch_paper_full_metadata(
    scopus_id: str, session: requests.Session
) -> dict | None:
    """Scopus Abstract Retrieval (default view)로 논문 메타데이터 풀세트 추출.

    useful 피드백 시 interest_papers 보강용. 다음 필드를 한 번의 호출로 받아옴.

    Returns:
        {scopus_id, title, abstract, journal, year, doi, authors, keywords}
        실패 시 None.
    """
    url = ABSTRACT_URL.format(scopus_id=scopus_id)
    time.sleep(ABSTRACT_DELAY)
    try:
        resp = session.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        logger.warning("Full metadata fetch failed for %s", scopus_id)
        return None

    arr = data.get("abstracts-retrieval-response", {}) or {}
    core = arr.get("coredata", {}) or {}

    # 저자 → "Jing J.; Kim S.; ..." 형태
    authors_data = arr.get("authors", {}) or {}
    author_list = authors_data.get("author", []) if authors_data else []
    if isinstance(author_list, dict):
        author_list = [author_list]
    names = []
    for a in author_list:
        if not isinstance(a, dict):
            continue
        name = _scalar(a.get("ce:indexed-name"))
        if name:
            names.append(name)
    authors_str = "; ".join(dict.fromkeys(names))  # 순서 보존 dedup

    # 저자 키워드 → ["Stack effect", "High-rise buildings", ...]
    ak = arr.get("authkeywords", {}) or {}
    kws_data = ak.get("author-keyword", []) if ak else []
    if isinstance(kws_data, dict):
        kws_data = [kws_data]
    keywords = []
    for kw in kws_data:
        kw_str = _scalar(kw)
        if kw_str:
            keywords.append(kw_str)

    # 연도 ← prism:coverDate (YYYY-MM-DD) 첫 4자리
    cover_date = _scalar(core.get("prism:coverDate"))
    year = None
    if len(cover_date) >= 4 and cover_date[:4].isdigit():
        year = int(cover_date[:4])

    return {
        "scopus_id": _scalar(core.get("dc:identifier")).replace("SCOPUS_ID:", "") or scopus_id,
        "title": _scalar(core.get("dc:title")),
        "abstract": _scalar(core.get("dc:description")),
        "journal": _scalar(core.get("prism:publicationName")),
        "year": year,
        "doi": _scalar(core.get("prism:doi")),
        "authors": authors_str,
        "keywords": keywords,
    }


def _fetch_paper_references(
    scopus_id: str, session: requests.Session
) -> list[dict]:
    """개별 논문의 참고문헌 메타데이터 추출 (Abstract Retrieval API, view=REF).

    Returns:
        list of {ref_scopus_id, ref_title, ref_authors, ref_year, ref_journal, ref_doi}
    """
    url = ABSTRACT_URL.format(scopus_id=scopus_id)
    params = {"view": "REF"}

    time.sleep(ABSTRACT_DELAY)
    try:
        resp = session.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        logger.warning("Reference fetch failed for %s", scopus_id)
        return []

    refs_data = (
        data.get("abstracts-retrieval-response", {})
        .get("references", {})
        .get("reference", [])
    )
    if not refs_data:
        return []
    if isinstance(refs_data, dict):
        refs_data = [refs_data]

    refs = []
    for ref in refs_data:
        # 저자 → "Vaswani A.; Shazeer N.; ..." 형태 (중복 제거)
        authors_data = ref.get("author-list", {}).get("author", []) if ref.get("author-list") else []
        if isinstance(authors_data, dict):
            authors_data = [authors_data]
        names = []
        for a in authors_data:
            if not isinstance(a, dict):
                continue
            name = _scalar(a.get("ce:indexed-name"))
            if name:
                names.append(name)
        authors_str = "; ".join(dict.fromkeys(names))  # 순서 보존 dedup

        # 연도 ← prism:coverDate (YYYY-MM-DD) 첫 4자리
        cover_date = _scalar(ref.get("prism:coverDate"))
        year = None
        if len(cover_date) >= 4 and cover_date[:4].isdigit():
            year = int(cover_date[:4])

        refs.append(
            {
                "ref_scopus_id": _scalar(ref.get("scopus-id")),
                "ref_title": _scalar(ref.get("title")),
                "ref_authors": authors_str,
                "ref_year": year,
                "ref_journal": _scalar(ref.get("sourcetitle")),
                "ref_doi": _scalar(ref.get("ce:doi")),
            }
        )
    return refs


def fetch_researcher_publications(
    scopus_id: str,
    researcher_id: int,
    fetch_references: bool = True,
    progress_callback=None,
) -> dict:
    """연구원의 논문 수집 → DB 저장 → researcher_type 자동 설정.

    Args:
        scopus_id: Scopus Author ID.
        researcher_id: DB researcher.id.
        fetch_references: 참고문헌 저널 수집 여부 (느림).
        progress_callback: fn(done, total) 진행률 콜백.

    Returns:
        {researcher_type, paper_count, keyword_freq, journal_freq}
    """
    session = _create_session()

    # 1. 논문 목록 가져오기
    logger.info("Fetching papers for author %s", scopus_id)
    papers = _search_author_papers(scopus_id, session)
    total = len(papers)
    logger.info("Found %d papers for author %s", total, scopus_id)

    keyword_freq: dict[str, int] = {}
    journal_freq: dict[str, int] = {}
    saved_count = 0

    for idx, paper_data in enumerate(papers):
        # 중복 체크
        existing = ReferencePaper.query.filter_by(
            researcher_id=researcher_id,
            scopus_id=paper_data["scopus_id"],
        ).first()
        if existing:
            if progress_callback:
                progress_callback(idx + 1, total)
            continue

        # reference_papers 저장
        ref_paper = ReferencePaper(
            researcher_id=researcher_id,
            scopus_id=paper_data["scopus_id"],
            title=paper_data["title"],
            authors=paper_data["authors"],
            journal=paper_data["journal"],
            volume=paper_data.get("volume") or None,
            year=paper_data["year"],
            abstract=paper_data["abstract"],
            doi=paper_data["doi"],
            cited_by=paper_data.get("cited_by", 0),
            source="scopus",
        )
        db.session.add(ref_paper)
        db.session.flush()

        # paper_keywords 저장
        for kw in paper_data["keywords"]:
            db.session.add(
                PaperKeyword(paper_id=ref_paper.id, keyword=kw)
            )
            keyword_freq[kw] = keyword_freq.get(kw, 0) + 1

        # 저널 빈도 집계
        if paper_data["journal"]:
            j = paper_data["journal"]
            journal_freq[j] = journal_freq.get(j, 0) + 1

        # 참고문헌 수집 (저널 없는 ref도 보존; scopus_id 없으면 매칭 불가하므로 스킵)
        if fetch_references and paper_data["scopus_id"]:
            refs = _fetch_paper_references(paper_data["scopus_id"], session)
            for ref in refs:
                if not ref["ref_scopus_id"]:
                    continue
                db.session.add(
                    PaperReference(
                        paper_id=ref_paper.id,
                        ref_scopus_id=ref["ref_scopus_id"],
                        ref_title=ref["ref_title"],
                        ref_authors=ref["ref_authors"],
                        ref_year=ref["ref_year"],
                        ref_journal=ref["ref_journal"],
                        ref_doi=ref["ref_doi"],
                    )
                )

        saved_count += 1

        # 50건마다 중간 커밋 (대량 수집 시 메모리 관리)
        if saved_count % 50 == 0:
            db.session.commit()

        if progress_callback:
            progress_callback(idx + 1, total)

    db.session.commit()

    # 2. researcher_type 자동 설정
    paper_count = (
        db.session.query(func.count(ReferencePaper.id))
        .filter(ReferencePaper.researcher_id == researcher_id)
        .scalar()
    )

    if paper_count >= 10:
        rtype = "A"
    elif paper_count >= 2:
        rtype = "B"
    else:
        rtype = "C"  # 0~1편: research_description 필수

    researcher = db.session.get(Researcher, researcher_id)
    researcher.researcher_type = rtype
    db.session.commit()

    logger.info(
        "Researcher %s: %d papers, type=%s",
        scopus_id,
        paper_count,
        rtype,
    )

    # 키워드 시드 토픽 representative_vector 자동 갱신
    # (새 본인 논문이 추가됐을 수 있으므로 매칭 풀 재계산)
    try:
        from app.litreview.profile.keyword_seeded_topic import (
            refresh_all_keyword_seeded_topics,
        )
        refresh_all_keyword_seeded_topics(researcher_id)
    except Exception:
        logger.exception(
            "Keyword-seeded topic refresh failed for researcher %d",
            researcher_id,
        )

    return {
        "researcher_type": rtype,
        "paper_count": paper_count,
        "new_papers_saved": saved_count,
        "keyword_freq": dict(
            sorted(keyword_freq.items(), key=lambda x: x[1], reverse=True)
        ),
        "journal_freq": dict(
            sorted(journal_freq.items(), key=lambda x: x[1], reverse=True)
        ),
    }


def sync_researcher_publications(researcher_id: int) -> dict:
    """연구원의 출판물을 Scopus와 동기화 (주간 배치/수동 트리거용).

    _search_author_papers 1회 호출로:
      1. DB에 없는 신규 논문 → ReferencePaper + PaperKeyword 저장
      2. 기존 논문 → cited_by 변동 시 갱신
      3. 신규 논문이 있으면 researcher_type 재산정 + 키워드 시드 토픽 갱신

    fetch_researcher_publications와 달리 참고문헌은 수집하지 않는다 (느림).

    Returns:
        {new_papers, updated_citations, total_scopus}

    Raises:
        ValueError: 연구원이 없거나 scopus_id 미설정.
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher or not researcher.scopus_id:
        raise ValueError(f"Researcher {researcher_id} has no scopus_id")

    session = _create_session()
    papers = _search_author_papers(researcher.scopus_id, session)

    existing = {
        rp.scopus_id: rp
        for rp in ReferencePaper.query.filter_by(researcher_id=researcher_id).all()
        if rp.scopus_id
    }

    new_count = 0
    updated_count = 0

    for paper_data in papers:
        sid = paper_data["scopus_id"]
        if not sid:
            continue

        rp = existing.get(sid)
        if rp:
            changed = False
            new_cited = paper_data.get("cited_by", 0)
            if rp.cited_by != new_cited:
                rp.cited_by = new_cited
                changed = True
            # 기존 행에 volume 이 비어 있으면 채움 (백필)
            if not rp.volume and paper_data.get("volume"):
                rp.volume = paper_data["volume"]
                changed = True
            # 기존 행에 저자/초록이 비어 있으면 채움 (백필)
            if not (rp.authors or "").strip() and paper_data.get("authors"):
                rp.authors = paper_data["authors"]
                changed = True
            if not (rp.abstract or "").strip() and paper_data.get("abstract"):
                rp.abstract = paper_data["abstract"]
                changed = True
            if changed:
                updated_count += 1
            continue

        ref_paper = ReferencePaper(
            researcher_id=researcher_id,
            scopus_id=sid,
            title=paper_data["title"],
            authors=paper_data["authors"],
            journal=paper_data["journal"],
            volume=paper_data.get("volume") or None,
            year=paper_data["year"],
            abstract=paper_data["abstract"],
            doi=paper_data["doi"],
            cited_by=paper_data.get("cited_by", 0),
            source="scopus",
        )
        db.session.add(ref_paper)
        db.session.flush()
        for kw in paper_data["keywords"]:
            db.session.add(PaperKeyword(paper_id=ref_paper.id, keyword=kw))
        new_count += 1

    db.session.commit()

    if new_count:
        paper_count = (
            db.session.query(func.count(ReferencePaper.id))
            .filter(ReferencePaper.researcher_id == researcher_id)
            .scalar()
        )
        if paper_count >= 10:
            researcher.researcher_type = "A"
        elif paper_count >= 2:
            researcher.researcher_type = "B"
        db.session.commit()

        try:
            from app.litreview.profile.keyword_seeded_topic import (
                refresh_all_keyword_seeded_topics,
            )
            refresh_all_keyword_seeded_topics(researcher_id)
        except Exception:
            logger.exception(
                "Keyword-seeded topic refresh failed for researcher %d",
                researcher_id,
            )

    logger.info(
        "Publication sync for researcher %d: %d new, %d citation updates (scopus total %d)",
        researcher_id,
        new_count,
        updated_count,
        len(papers),
    )
    return {
        "new_papers": new_count,
        "updated_citations": updated_count,
        "total_scopus": len(papers),
    }


def backfill_citation_counts(
    researcher_id: int,
    progress_callback=None,
) -> dict:
    """기존 논문의 피인용수를 Scopus에서 일괄 업데이트.

    cited_by=0 또는 NULL인 논문만 대상. Scopus Search API로 각 논문의
    citedby-count를 조회하여 DB 업데이트.

    Args:
        researcher_id: DB researcher.id
        progress_callback: fn(done, total)

    Returns:
        {updated: int, total: int}
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher or not researcher.scopus_id:
        raise ValueError(f"Researcher {researcher_id} has no scopus_id")

    session = _create_session()

    # 해당 연구원의 모든 논문을 Scopus에서 다시 조회 (citedby-count 포함)
    papers = _search_author_papers(researcher.scopus_id, session)
    scopus_map = {p["scopus_id"]: p["cited_by"] for p in papers}

    ref_papers = ReferencePaper.query.filter_by(
        researcher_id=researcher_id,
    ).all()

    total = len(ref_papers)
    updated = 0

    for idx, rp in enumerate(ref_papers):
        if rp.scopus_id and rp.scopus_id in scopus_map:
            new_count = scopus_map[rp.scopus_id]
            if rp.cited_by != new_count:
                rp.cited_by = new_count
                updated += 1

        if progress_callback:
            progress_callback(idx + 1, total)

    db.session.commit()
    logger.info(
        "Backfilled citation counts for researcher %d: %d/%d updated",
        researcher_id,
        updated,
        total,
    )
    return {"updated": updated, "total": total}
