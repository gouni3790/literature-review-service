"""OpenAlex API 클라이언트.

OpenAlex는 API 키가 없지만, User-Agent에 연락처를 넣으면 polite pool로 처리되어
응답이 안정적이다 (https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication).

여기서는 HTTP 호출과 응답 정규화까지만 담당하고, DB 저장은 service.py가 한다.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"
TIMEOUT = 20
DEFAULT_MAILTO = "rhdnwkd@skku.edu"


def _headers() -> dict:
    mailto = DEFAULT_MAILTO
    try:
        mailto = current_app.config.get("OPENALEX_MAILTO") or DEFAULT_MAILTO
    except RuntimeError:
        pass  # 앱 컨텍스트 밖에서 호출된 경우 기본값 사용
    return {"User-Agent": f"BIST-LitReview/1.0 (mailto:{mailto})"}


def _get(path: str, **params) -> dict | None:
    """OpenAlex GET. 실패 시 None (호출부에서 빈 결과로 처리)."""
    try:
        resp = requests.get(
            f"{BASE_URL}{path}", params=params, headers=_headers(), timeout=TIMEOUT
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        logger.exception("OpenAlex 요청 실패: %s %s", path, params)
        return None


def short_id(url_or_id: str | None) -> str | None:
    """'https://openalex.org/A5045676373' → 'A5045676373'."""
    if not url_or_id:
        return None
    return url_or_id.rstrip("/").rsplit("/", 1)[-1]


def normalize_orcid(orcid: str | None) -> str | None:
    """입력된 ORCID를 0000-0000-0000-0000 형태로 정규화.

    'https://orcid.org/0000-...', '0000...', 공백/하이픈 누락 등을 흡수한다.
    """
    if not orcid:
        return None
    digits = re.sub(r"[^0-9xX]", "", orcid).upper()
    if len(digits) != 16:
        return None
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


def normalize_name(name: str | None) -> str:
    """비교 보조용 정규화 — 식별자가 아니라 로컬 검색·중복 감지에만 쓴다.

    'Sung-Min Yoon' / 'Yoon, S. M.' 같은 표기 흔들림을 줄이지만,
    동명이인을 구분하지 못하므로 이것으로 저자를 특정해서는 안 된다.
    """
    if not name:
        return ""
    s = name.lower().replace(",", " ")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """OpenAlex의 abstract_inverted_index를 원문 초록으로 복원.

    OpenAlex는 저작권 문제로 초록을 {단어: [위치...]} 형태로만 제공한다.
    임베딩에 쓰려면 위치 순서대로 다시 이어 붙여야 한다.
    """
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in positions)


# ---------------------------------------------------------------------------
# 저자
# ---------------------------------------------------------------------------


def _author_brief(raw: dict) -> dict:
    """OpenAlex author 객체 → 화면/DB에서 쓰는 공통 형태."""
    insts = raw.get("last_known_institutions") or []
    if not insts and raw.get("last_known_institution"):
        insts = [raw["last_known_institution"]]
    affiliation = insts[0].get("display_name") if insts else None

    return {
        "openalex_author_id": short_id(raw.get("id")),
        "display_name": raw.get("display_name") or "",
        "orcid": normalize_orcid(raw.get("orcid")),
        "affiliation": affiliation,
        "research_topics": [
            t.get("display_name") for t in (raw.get("topics") or [])[:5]
            if t.get("display_name")
        ],
        "name_alternatives": (raw.get("display_name_alternatives") or [])[:8],
        "works_count": raw.get("works_count"),
        "cited_by_count": raw.get("cited_by_count"),
    }


def _work_brief(raw: dict) -> dict:
    loc = raw.get("primary_location") or {}
    source = (loc.get("source") or {}) if isinstance(loc, dict) else {}
    doi = raw.get("doi") or ""
    return {
        "openalex_work_id": short_id(raw.get("id")),
        "title": raw.get("display_name") or "",
        "journal": source.get("display_name"),
        "year": raw.get("publication_year"),
        "publication_date": raw.get("publication_date"),
        "doi": doi.replace("https://doi.org/", "") if doi else "",
        "cited_by_count": raw.get("cited_by_count"),
    }


def get_author(openalex_author_id: str) -> dict | None:
    """Author ID로 저자 조회."""
    data = _get(f"/authors/{short_id(openalex_author_id)}")
    return _author_brief(data) if data else None


def get_author_by_orcid(orcid: str) -> dict | None:
    """ORCID로 저자 조회. 형식이 잘못됐거나 없으면 None."""
    norm = normalize_orcid(orcid)
    if not norm:
        return None
    data = _get(f"/authors/https://orcid.org/{norm}")
    return _author_brief(data) if data else None


def author_top_papers(openalex_author_id: str, n: int = 3) -> list[dict]:
    """피인용 상위 논문 (대표 논문)."""
    d = _get(
        "/works",
        filter=f"author.id:{short_id(openalex_author_id)}",
        sort="cited_by_count:desc",
        per_page=n,
    )
    return [_work_brief(w) for w in (d or {}).get("results", [])]


def author_recent_papers(openalex_author_id: str, n: int = 2) -> list[dict]:
    """최근 발표 논문."""
    d = _get(
        "/works",
        filter=f"author.id:{short_id(openalex_author_id)}",
        sort="publication_date:desc",
        per_page=n,
    )
    return [_work_brief(w) for w in (d or {}).get("results", [])]


def search_authors(name: str, limit: int = 5, with_papers: bool = True) -> list[dict]:
    """이름으로 후보 저자 검색.

    동명이인이 많으므로(예: 'Sungmin Yoon' 33명) 사용자가 고를 수 있도록
    소속·연구분야·대표논문·최근논문을 함께 붙여 반환한다.
    대표/최근 논문은 후보마다 별도 호출이 필요해 병렬로 가져온다.
    """
    name = (name or "").strip()
    if not name:
        return []

    data = _get("/authors", search=name, per_page=max(1, min(limit, 25)))
    if not data:
        return []

    candidates = [_author_brief(a) for a in data.get("results", [])]
    candidates = [c for c in candidates if c["openalex_author_id"]]
    if not candidates or not with_papers:
        for c in candidates:
            c.setdefault("representative_papers", [])
            c.setdefault("recent_papers", [])
        return candidates

    def fetch(c):
        aid = c["openalex_author_id"]
        c["representative_papers"] = author_top_papers(aid, 3)
        c["recent_papers"] = author_recent_papers(aid, 2)
        return c

    # 후보 수만큼 2회씩 호출되므로 순차 처리하면 느리다. OpenAlex 권장 속도(10 req/s)
    # 안에서 동작하도록 동시 실행 수를 제한한다.
    with ThreadPoolExecutor(max_workers=5) as pool:
        candidates = list(pool.map(fetch, candidates))

    logger.info("OpenAlex 저자 검색 '%s': 후보 %d명", name, len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# 논문
# ---------------------------------------------------------------------------


def authors_of_doi(doi: str) -> list[dict]:
    """DOI로 논문을 찾아 저자 목록(Author ID 포함)을 반환.

    논문 상세에서 'Follow' 버튼을 붙이려면 이름이 아니라 Author ID가 필요한데,
    현재 Scopus 수집 경로는 저자를 저장하지 못하므로 여기서 해석한다.
    """
    doi = (doi or "").strip().replace("https://doi.org/", "")
    if not doi:
        return []

    data = _get(f"/works/https://doi.org/{doi}")
    if not data:
        return []

    out = []
    for pos, au in enumerate(data.get("authorships") or []):
        a = au.get("author") or {}
        aid = short_id(a.get("id"))
        if not aid:
            continue
        insts = [i.get("display_name") for i in (au.get("institutions") or []) if i.get("display_name")]
        out.append({
            "openalex_author_id": aid,
            "display_name": a.get("display_name") or "",
            "orcid": normalize_orcid(a.get("orcid")),
            "affiliation": insts[0] if insts else None,
            "position": au.get("author_position"),  # first | middle | last
            "order": pos,
        })
    return out


def author_works_since(
    openalex_author_id: str, since_date: str | None = None, limit: int = 50
) -> list[dict]:
    """저자의 논문을 발표일 내림차순으로 조회 (신규 논문 추적용).

    Args:
        since_date: 'YYYY-MM-DD'. 지정하면 그 날짜 이후 발표분만.
    """
    aid = short_id(openalex_author_id)
    filt = f"author.id:{aid}"
    if since_date:
        filt += f",from_publication_date:{since_date}"

    d = _get(
        "/works",
        filter=filt,
        sort="publication_date:desc",
        per_page=max(1, min(limit, 200)),
    )
    if not d:
        return []

    works = []
    for w in d.get("results", []):
        brief = _work_brief(w)
        brief["abstract"] = reconstruct_abstract(w.get("abstract_inverted_index"))
        works.append(brief)
    return works
