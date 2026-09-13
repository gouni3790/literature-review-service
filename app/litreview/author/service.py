"""관심 저자 저장·팔로우·신규 논문 추적.

식별 기준은 openalex_author_id 하나다. 이름은 표시용이며,
같은 저자가 논문마다 다르게 표기돼도(S. Yoon / Yoon, S. / Sung-Min Yoon)
Author ID가 같으면 동일 저자로 처리된다.
"""

import logging
from datetime import datetime, timedelta

from app import db
from app.litreview.author import openalex
from app.models import (
    Author,
    AuthorFollow,
    AuthorPaper,
    CollectedPaper,
)

logger = logging.getLogger(__name__)

# 처음 팔로우한 저자는 과거 논문을 통째로 끌어오지 않고 이 기간만 본다.
FIRST_TRACK_WINDOW_DAYS = 180


# ---------------------------------------------------------------------------
# 저장 (upsert)
# ---------------------------------------------------------------------------


def upsert_author(brief: dict) -> Author:
    """OpenAlex 저자 정보를 authors 테이블에 반영하고 Author를 돌려준다.

    openalex_author_id로 찾고, 없으면 새로 만든다. 이름은 갱신만 하고
    식별에는 쓰지 않는다.
    """
    oa_id = brief.get("openalex_author_id")
    if not oa_id:
        raise ValueError("openalex_author_id가 없어 저자를 저장할 수 없습니다.")

    author = Author.query.filter_by(openalex_author_id=oa_id).first()
    if author is None:
        author = Author(openalex_author_id=oa_id)
        db.session.add(author)

    author.display_name = brief.get("display_name") or author.display_name or oa_id
    author.normalized_name = openalex.normalize_name(author.display_name)
    author.orcid = brief.get("orcid") or author.orcid
    author.affiliation = brief.get("affiliation") or author.affiliation
    if brief.get("research_topics"):
        author.research_topics = brief["research_topics"]
    if brief.get("name_alternatives"):
        author.name_alternatives = brief["name_alternatives"]
    if brief.get("representative_papers"):
        author.representative_papers = brief["representative_papers"]
    if brief.get("works_count") is not None:
        author.works_count = brief["works_count"]
    if brief.get("cited_by_count") is not None:
        author.cited_by_count = brief["cited_by_count"]
    author.updated_at = datetime.utcnow()

    db.session.commit()
    return author


def _ensure_author_detail(author: Author) -> Author:
    """대표 논문 등 상세 정보가 비어 있으면 OpenAlex에서 채운다."""
    if author.representative_papers:
        return author
    try:
        papers = openalex.author_top_papers(author.openalex_author_id, 3)
        if papers:
            author.representative_papers = papers
            db.session.commit()
    except Exception:
        logger.exception("대표 논문 조회 실패: author %s", author.openalex_author_id)
    return author


# ---------------------------------------------------------------------------
# 팔로우
# ---------------------------------------------------------------------------


def follow_author(
    researcher_id: int, brief: dict, source: str = "name_search"
) -> dict:
    """관심 저자 등록. 이미 등록돼 있으면 그대로 둔다.

    Args:
        brief: openalex 모듈이 만든 저자 dict (openalex_author_id 필수)
        source: name_search | paper_detail | orcid
    """
    author = upsert_author(brief)
    _ensure_author_detail(author)

    existing = AuthorFollow.query.filter_by(
        researcher_id=researcher_id, author_id=author.id
    ).first()
    if existing:
        return {"already_following": True, "author": author_dict(author, True)}

    follow = AuthorFollow(
        researcher_id=researcher_id, author_id=author.id, source=source
    )
    db.session.add(follow)
    db.session.commit()
    logger.info(
        "관심 저자 등록: researcher=%d, author=%s (%s), 경로=%s",
        researcher_id, author.openalex_author_id, author.display_name, source,
    )
    return {"already_following": False, "author": author_dict(author, True)}


def unfollow_author(researcher_id: int, author_id: int) -> bool:
    follow = AuthorFollow.query.filter_by(
        researcher_id=researcher_id, author_id=author_id
    ).first()
    if not follow:
        return False
    db.session.delete(follow)
    db.session.commit()
    return True


def followed_author_ids(researcher_id: int) -> set[str]:
    """이 연구원이 팔로우 중인 OpenAlex Author ID 집합 (UI 버튼 상태용)."""
    rows = (
        db.session.query(Author.openalex_author_id)
        .join(AuthorFollow, AuthorFollow.author_id == Author.id)
        .filter(AuthorFollow.researcher_id == researcher_id)
        .all()
    )
    return {r[0] for r in rows if r[0]}


def author_dict(author: Author, following: bool = False, follow=None) -> dict:
    return {
        "internal_author_id": author.id,
        "display_name": author.display_name,
        "normalized_name": author.normalized_name,
        "openalex_author_id": author.openalex_author_id,
        "orcid": author.orcid,
        "semantic_scholar_author_id": author.semantic_scholar_author_id,
        "affiliation": author.affiliation,
        "research_topics": author.research_topics or [],
        "representative_papers": author.representative_papers or [],
        "name_alternatives": author.name_alternatives or [],
        "works_count": author.works_count,
        "cited_by_count": author.cited_by_count,
        "last_checked_at": str(author.last_checked_at) if author.last_checked_at else None,
        "following": following,
        "followed_at": str(follow.followed_at) if follow and follow.followed_at else None,
        "follow_source": follow.source if follow else None,
        "new_paper_count": AuthorPaper.query.filter_by(author_id=author.id).count(),
    }


def list_followed_authors(researcher_id: int) -> list[dict]:
    rows = (
        db.session.query(Author, AuthorFollow)
        .join(AuthorFollow, AuthorFollow.author_id == Author.id)
        .filter(AuthorFollow.researcher_id == researcher_id)
        .order_by(AuthorFollow.followed_at.desc())
        .all()
    )
    return [author_dict(a, True, f) for a, f in rows]


# ---------------------------------------------------------------------------
# 신규 논문 추적
# ---------------------------------------------------------------------------


def _link_to_collected_paper(work: dict) -> int | None:
    """추적된 논문을 collected_papers에 편입해 추천 파이프라인에 태운다.

    DOI가 같은 논문이 이미 있으면 그것을 재사용한다(Scopus 수집분과 중복 방지).
    초록이 없으면 임베딩이 불가능해 추천 대상이 되지 못하므로 건너뛴다.
    """
    abstract = (work.get("abstract") or "").strip()
    if not abstract:
        return None

    doi = (work.get("doi") or "").strip()
    existing = None
    if doi:
        existing = CollectedPaper.query.filter(
            db.func.lower(CollectedPaper.doi) == doi.lower()
        ).first()
    if existing:
        return existing.id

    paper = CollectedPaper(
        scopus_id=None,  # OpenAlex 출처라 Scopus ID가 없다
        title=work.get("title") or "",
        journal=work.get("journal"),
        year=work.get("year"),
        abstract=abstract,
        doi=doi or None,
        source_query=f"followed_author:{work.get('_author_openalex_id', '')}",
    )
    db.session.add(paper)
    db.session.flush()
    return paper.id


def track_author(author: Author) -> dict:
    """한 저자의 신규 논문을 Author ID 기준으로 확인해 저장.

    이름 문자열이 아니라 author.id 필터로 조회하므로 표기 흔들림과 무관하다.
    """
    since = None
    if author.last_checked_at:
        since = author.last_checked_at.strftime("%Y-%m-%d")
    else:
        since = (
            datetime.utcnow() - timedelta(days=FIRST_TRACK_WINDOW_DAYS)
        ).strftime("%Y-%m-%d")

    works = openalex.author_works_since(author.openalex_author_id, since_date=since)

    new_count, linked_count = 0, 0
    new_paper_ids = []
    for w in works:
        work_id = w.get("openalex_work_id")
        if not work_id:
            continue
        exists = AuthorPaper.query.filter_by(
            author_id=author.id, openalex_work_id=work_id
        ).first()
        if exists:
            if exists.collected_paper_id:
                new_paper_ids.append(exists.collected_paper_id)
            continue

        w["_author_openalex_id"] = author.openalex_author_id
        collected_id = _link_to_collected_paper(w)

        ap = AuthorPaper(
            author_id=author.id,
            openalex_work_id=work_id,
            doi=w.get("doi") or None,
            title=w.get("title"),
            journal=w.get("journal"),
            year=w.get("year"),
            publication_date=w.get("publication_date"),
            abstract=w.get("abstract"),
            cited_by_count=w.get("cited_by_count"),
            collected_paper_id=collected_id,
        )
        db.session.add(ap)
        new_count += 1
        if collected_id:
            linked_count += 1
            new_paper_ids.append(collected_id)

    author.last_checked_at = datetime.utcnow()
    db.session.commit()

    logger.info(
        "저자 추적 %s (%s): 조회 %d편, 신규 %d편, 파이프라인 편입 %d편",
        author.openalex_author_id, author.display_name,
        len(works), new_count, linked_count,
    )
    return {
        "author_id": author.id,
        "openalex_author_id": author.openalex_author_id,
        "display_name": author.display_name,
        "checked": len(works),
        "new_papers": new_count,
        "linked": linked_count,
        "collected_paper_ids": new_paper_ids,
    }


def track_followed_authors(researcher_id: int) -> dict:
    """연구원이 팔로우한 모든 저자의 신규 논문 확인.

    Returns:
        {authors: [...], collected_paper_ids: [...]}  — 파이프라인에 넘길 논문 ID 포함
    """
    authors = (
        db.session.query(Author)
        .join(AuthorFollow, AuthorFollow.author_id == Author.id)
        .filter(AuthorFollow.researcher_id == researcher_id)
        .all()
    )
    results, all_ids = [], []
    for a in authors:
        try:
            r = track_author(a)
            results.append(r)
            all_ids.extend(r["collected_paper_ids"])
        except Exception:
            logger.exception("저자 추적 실패: %s", a.openalex_author_id)
            db.session.rollback()

    return {
        "researcher_id": researcher_id,
        "authors": results,
        "collected_paper_ids": sorted(set(all_ids)),
    }
