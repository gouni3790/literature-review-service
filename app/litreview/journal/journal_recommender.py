"""저널 발굴 파이프라인 — 연구원이 모르던 저널을 찾아 순위를 매긴다.

    [1] 주제        reco_topics 의 키워드 + 대표벡터
    [2] Scopus 검색  연도별 5회 분할 (Growth 를 전수로 계산하기 위함)
    [3] 메타 필터    영어 · 문헌유형 · 초록 존재
    [4] 임베딩·유사도 주제 대표벡터 ↔ 논문 초록      ★ 저장하지 않음
    [5] 관련 논문 선정 유사도 상위 N편
    [6] 저널별 집계
    [7] 점수화       journal_scoring
    [8] 저장         reco_journal_recommendations 상위 N종 + 근거 3편

[4]의 임베딩을 저장하지 않는 것이 이 설계의 핵심이다. 논문 임베딩은 행당 약
30KB 라, 주제당 2,000편을 저장하면 60MB·23주제면 1.4GB 가 reco_papers.db 로
들어간다. 저널 추천은 집계만 필요하므로 메모리에서 계산하고 버린다. 영구
저장은 저널 20행과 근거 논문 3편뿐이다.

현재는 A유형(대표벡터 보유) 주제만 처리한다. B유형은 대표벡터 대신 개별 논문
임베딩 max 유사도를 쓰는데(similarity.py 와 동일한 분기), 키워드 품질 문제가
먼저 해결되어야 해서 뒤로 미뤘다.
"""

import logging
import time
import uuid
from datetime import datetime

import numpy as np
import requests
from flask import current_app
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.models import (
    JournalRecommendation,
    PaperReference,
    ReferencePaper,
    Researcher,
    ResearchTopic,
    TargetJournal,
)
from app.litreview.journal import journal_scoring as scoring

logger = logging.getLogger(__name__)

SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
SEARCH_DELAY = 0.15
PAGE_SIZE = 25
REQUEST_TIMEOUT = 30

# --- 파이프라인 파라미터 ---
SEARCH_YEARS = 5            # 최근 몇 년을 훑을지
PER_YEAR_LIMIT = 400        # 연도당 수집 상한 (5년 × 400 = 2,000편)
RELATED_TOP_N = 400         # [5] 관련 논문 선정 수
RESULT_TOP_N = 20           # [8] 저장할 저널 수
MIN_RELATED_PER_JOURNAL = 3 # 관련 논문이 이 미만인 저널은 후보 제외
MAX_KEYWORDS = 12           # 쿼리에 넣을 키워드 상한


class ScopusUnavailable(RuntimeError):
    """Scopus 검색이 아예 되지 않는 상태 (키/네트워크). 배치를 실패로 기록한다."""


# ---------------------------------------------------------------------------
# [2] 검색
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config.get("SCOPUS_API_KEY", ""),
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "application/json",
    }


def build_keyword_clause(keywords: list[str]) -> str:
    """키워드를 Scopus TITLE-ABS-KEY 절로 만든다.

    괄호는 Scopus 문법 문자라 제거한다. 공백이 있으면 따옴표로 묶어 구(phrase)로
    검색한다.
    """
    items = []
    for kw in keywords[:MAX_KEYWORDS]:
        clean = (kw or "").replace("(", " ").replace(")", " ").strip()
        if not clean:
            continue
        items.append(f'"{clean}"' if " " in clean else clean)
    return " OR ".join(items)


def search_year(
    keyword_clause: str,
    year: int,
    session: requests.Session,
    limit: int = PER_YEAR_LIMIT,
) -> tuple[list[dict], int]:
    """한 연도를 검색한다.

    연도별로 나누는 이유: 한 번에 2,000편을 최신순으로 받으면 최근 몇 달치만
    오고 5년 분포가 사라져 Growth 를 계산할 수 없다. 연도로 쪼개면 분포가
    보존되고, opensearch:totalResults 로 **그 해 전체 건수**를 덤으로 얻는다.

    Returns:
        (논문 리스트, 그 해 전체 건수)
    """
    query = f"TITLE-ABS-KEY({keyword_clause}) AND PUBYEAR IS {year}"
    params = {
        "query": query,
        "count": PAGE_SIZE,
        "cursor": "*",
        "field": (
            "dc:identifier,dc:title,prism:publicationName,prism:coverDate,"
            "prism:doi,dc:description,source-id,prism:issn,subtypeDescription"
        ),
    }

    papers: list[dict] = []
    total = 0
    first_page = True

    while len(papers) < limit:
        time.sleep(SEARCH_DELAY)
        try:
            resp = session.get(
                SCOPUS_SEARCH_URL, headers=_headers(), params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json().get("search-results", {})
        except requests.RequestException as exc:
            if first_page:
                raise ScopusUnavailable(
                    f"Scopus 검색 실패 (year={year}): {exc}"
                ) from exc
            logger.warning("Scopus 페이지 실패 (year=%d): %s", year, exc)
            break

        if first_page:
            try:
                total = int(data.get("opensearch:totalResults") or 0)
            except (TypeError, ValueError):
                total = 0
            first_page = False

        entries = data.get("entry", []) or []
        if not entries or entries[0].get("@_fa") == "false":
            break

        for e in entries:
            if len(papers) >= limit:
                break
            paper = _parse_entry(e, year)
            if paper:
                papers.append(paper)

        link = next(
            (l for l in data.get("link", []) if l.get("@ref") == "next"), None
        )
        if not link:
            break
        cursor = link.get("@href", "").split("cursor=")[-1].split("&")[0]
        if not cursor or cursor == params["cursor"]:
            break
        params["cursor"] = cursor

    return papers, total


def _count_only(query: str, session: requests.Session) -> int | None:
    """검색 건수만 센다 (count=1, totalResults 만 읽음). 실패 시 None."""
    time.sleep(SEARCH_DELAY)
    try:
        resp = session.get(
            SCOPUS_SEARCH_URL,
            headers=_headers(),
            params={"query": query, "count": 1},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return int(
            resp.json().get("search-results", {}).get("opensearch:totalResults") or 0
        )
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.debug("건수 조회 실패: %s", exc)
        return None


def fetch_journal_year_counts(
    keyword_clause: str,
    aggs: dict,
    current_year: int,
    session: requests.Session,
    max_journals: int = 25,
) -> dict[str, dict[int, int]]:
    """후보 저널별 최근/이전 구간의 **정확한** 논문 수를 Scopus 에서 센다.

    왜 필요한가: 검색 결과 표본만으로 Growth 를 계산하면, 연도당 PER_YEAR_LIMIT
    상한에 잘린 만큼 분포가 왜곡된다. 상위 후보에 한해 실제 건수를 세면 Growth 가
    표본이 아니라 전수 기준이 된다.

    비용은 저널당 2회(최근 구간·이전 구간)다. 상위 max_journals 종에만 적용한다.

    SOURCE-ID 가 없는 저널은 건너뛴다. SRCTITLE 은 부분 매칭이라
    (`SRCTITLE("Energy")` 가 Nano Energy·Wind Energy 를 전부 잡는다) 건수를
    부풀리므로 대용으로 쓸 수 없다. 건너뛴 저널은 표본 기반 Growth 를 쓴다.

    Returns:
        정규화 저널명 -> {최근구간_대표연도: 건수, 이전구간_대표연도: 건수}
    """
    cutoff = current_year - scoring.RECENT_YEARS + 1   # 최근 구간 시작 연도
    start = current_year - SEARCH_YEARS + 1            # 전체 구간 시작 연도

    ordered = sorted(
        (a for a in aggs.values() if a.related_count >= MIN_RELATED_PER_JOURNAL),
        key=lambda a: a.related_count,
        reverse=True,
    )[:max_journals]

    out: dict[str, dict[int, int]] = {}
    skipped = 0
    for agg in ordered:
        sid = agg.scopus_source_id
        if not sid:
            skipped += 1
            continue
        key = scoring.normalize_journal_name(agg.journal_name)

        recent = _count_only(
            f"TITLE-ABS-KEY({keyword_clause}) AND SOURCE-ID({sid})"
            f" AND PUBYEAR > {cutoff - 1}",
            session,
        )
        older = _count_only(
            f"TITLE-ABS-KEY({keyword_clause}) AND SOURCE-ID({sid})"
            f" AND PUBYEAR > {start - 1} AND PUBYEAR < {cutoff}",
            session,
        )
        if recent is None or older is None:
            continue
        # compute_growth 는 cutoff 기준으로 연도를 가르므로 대표 연도에 몰아 넣는다
        out[key] = {cutoff: recent, cutoff - 1: older}

    if skipped:
        logger.info(
            "정확 건수 조회: %d종 완료, source-id 없는 %d종은 표본 기반으로 대체",
            len(out), skipped,
        )
    return out


# ---------------------------------------------------------------------------
# [3] 메타 필터
# ---------------------------------------------------------------------------

ALLOWED_SUBTYPES = {"Article", "Review", "Conference Paper"}


def _parse_entry(entry: dict, year: int) -> dict | None:
    """Scopus 항목 → 내부 표현. 필터를 통과하지 못하면 None."""
    abstract = (entry.get("dc:description") or "").strip()
    if not abstract:
        return None  # 임베딩할 수 없으므로 제외

    journal = (entry.get("prism:publicationName") or "").strip()
    if not journal:
        return None

    subtype = entry.get("subtypeDescription")
    if subtype and subtype not in ALLOWED_SUBTYPES:
        return None

    sid = entry.get("dc:identifier", "") or ""
    if sid.startswith("SCOPUS_ID:"):
        sid = sid[len("SCOPUS_ID:"):]

    cover = entry.get("prism:coverDate") or ""
    try:
        y = int(cover[:4]) if len(cover) >= 4 else year
    except ValueError:
        y = year

    return {
        "scopus_id": sid,
        "title": (entry.get("dc:title") or "").strip(),
        "journal": journal,
        "source_id": entry.get("source-id"),
        "issn": entry.get("prism:issn"),
        "year": y,
        "doi": entry.get("prism:doi"),
        "abstract": abstract,
    }


# ---------------------------------------------------------------------------
# familiarity 재료 (연구실 단위)
# ---------------------------------------------------------------------------

def load_lab_familiarity() -> dict[str, float]:
    """연구실 전체 기준 familiarity 지수.

    연구원별이 아니라 연구실 단위로 집계한다 (결정 사항). 신입 연구원에게도
    연구실이 이미 보는 저널은 '새 저널'로 치지 않는다.
    """
    citation_counts = dict(
        db.session.query(PaperReference.ref_journal, db.func.count(PaperReference.id))
        .filter(
            PaperReference.ref_journal.isnot(None),
            PaperReference.ref_journal != "",
        )
        .group_by(PaperReference.ref_journal)
        .all()
    )
    published_counts = dict(
        db.session.query(ReferencePaper.journal, db.func.count(ReferencePaper.id))
        .filter(ReferencePaper.journal.isnot(None), ReferencePaper.journal != "")
        .group_by(ReferencePaper.journal)
        .all()
    )
    target_names = {
        n for (n,) in db.session.query(TargetJournal.journal_name).distinct().all()
    }

    fam = scoring.build_familiarity(
        citation_counts=citation_counts,
        published_counts=published_counts,
        target_journal_names=target_names,
    )
    logger.info(
        "familiarity: 인용 %d종 / 게재 %d종 / 타겟 %d종 → 지수 %d종",
        len(citation_counts), len(published_counts), len(target_names), len(fam),
    )
    return fam


# ---------------------------------------------------------------------------
# 파이프라인 본체
# ---------------------------------------------------------------------------

def recommend_journals_for_topic(
    topic_id: int,
    familiarity: dict[str, float],
    run_id: str,
    apply_quality_gate: bool = True,
    progress_callback=None,
) -> dict:
    """주제 하나에 대해 [2]~[8]을 수행하고 결과를 저장한다.

    Args:
        topic_id: reco_topics.id (대표벡터 보유 주제여야 한다)
        familiarity: load_lab_familiarity() 결과 — 주제마다 다시 만들지 않는다
        run_id: 실행 회차 식별자
        apply_quality_gate: Scopus Serial Title 조회로 CiteScore 게이트 적용
        progress_callback: fn(stage: str, done: int, total: int)

    Returns:
        {topic_id, topic_name, searched, related, journals, saved, quality_checked}
    """
    from app.litreview.recommendation.embedder import Embedder

    topic = db.session.get(ResearchTopic, topic_id)
    if not topic:
        raise ValueError(f"Topic {topic_id} not found")
    if not topic.representative_vector:
        raise ValueError(
            f"Topic {topic_id} has no representative_vector "
            "(B유형 주제는 아직 지원하지 않음)"
        )

    keywords = topic.keywords or []
    clause = build_keyword_clause(keywords)
    if not clause:
        raise ValueError(f"Topic {topic_id} has no usable keywords")

    result = {
        "topic_id": topic_id,
        "topic_name": topic.name,
        "researcher_id": topic.researcher_id,
        "keywords": keywords[:MAX_KEYWORDS],
        "searched": 0,
        "related": 0,
        "journals": 0,
        "saved": 0,
        "quality_checked": False,
    }

    # ---- [2][3] 연도별 검색 + 필터 ----
    current_year = datetime.utcnow().year
    years = list(range(current_year - SEARCH_YEARS + 1, current_year + 1))
    papers: list[dict] = []

    session = _session()
    try:
        for i, year in enumerate(years, 1):
            found, total = search_year(clause, year, session)
            papers.extend(found)
            logger.info(
                "  topic %d, %d년: %d편 수집 (전체 %d편)",
                topic_id, year, len(found), total,
            )
            if progress_callback:
                progress_callback("search", i, len(years))
    finally:
        session.close()

    result["searched"] = len(papers)
    if not papers:
        logger.warning("Topic %d: 검색 결과 없음", topic_id)
        return result

    # ---- [4] 임베딩 + 유사도 (저장하지 않는다) ----
    embedder = Embedder()
    topic_vec = np.array(topic.representative_vector, dtype=np.float32)
    topic_norm = topic_vec / (np.linalg.norm(topic_vec) + 1e-10)

    texts = [
        f"Title: {p['title']}\nAbstract: {p['abstract']}" if p["title"]
        else p["abstract"]
        for p in papers
    ]
    embeddings = embedder.embed_batch(texts)
    if progress_callback:
        progress_callback("embed", len(papers), len(papers))

    scored: list[dict] = []
    for p, emb in zip(papers, embeddings):
        if not emb:
            continue
        v = np.array(emb, dtype=np.float32)
        sim = float(v @ topic_norm / (np.linalg.norm(v) + 1e-10))
        if sim < scoring.SIMILARITY_FLOOR:
            continue
        scored.append({**p, "similarity": sim})

    # ---- [5] 관련 논문 선정 ----
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    related = scored[:RELATED_TOP_N]
    result["related"] = len(related)
    if not related:
        logger.warning(
            "Topic %d: 유사도 %.2f 이상인 논문이 없음",
            topic_id, scoring.SIMILARITY_FLOOR,
        )
        return result

    # ---- [6] 저널별 집계 ----
    aggs = scoring.aggregate_by_journal(related)
    result["journals"] = len(aggs)

    # 상위 후보의 Growth 를 표본이 아니라 전수로 계산한다 (저널당 2회 조회)
    session = _session()
    try:
        year_totals_by_journal = fetch_journal_year_counts(
            clause, aggs, current_year, session
        )
    except Exception:
        logger.exception("정확 건수 조회 실패 — 표본 기반 Growth 로 진행")
        year_totals_by_journal = {}
    finally:
        session.close()

    for key, agg in aggs.items():
        if key in year_totals_by_journal:
            agg.year_totals = year_totals_by_journal[key]
    result["growth_exact"] = len(year_totals_by_journal)

    # ---- [7] 점수화 ----
    gate = None
    if apply_quality_gate:
        from app.litreview.journal.serial_api import build_quality_gate

        candidate_names = [
            a.journal_name
            for a in aggs.values()
            if a.related_count >= MIN_RELATED_PER_JOURNAL
        ]
        gate, meta, checked = build_quality_gate(candidate_names)
        result["quality_checked"] = checked
        if not checked:
            gate = None  # 전부 통과이므로 굳이 곱하지 않는다
    else:
        meta = {}

    ranked = scoring.score_journals(
        aggs,
        familiarity=familiarity,
        current_year=current_year,
        quality_gate=gate,
        min_related=MIN_RELATED_PER_JOURNAL,
    )

    # ---- [8] 저장 ----
    top = ranked[:RESULT_TOP_N]
    saved = _persist(topic, top, meta, run_id)
    result["saved"] = saved
    result["top"] = [
        {
            "journal_name": r["journal_name"],
            "score": r["score"],
            "related": r["related_paper_count"],
            "novelty": r["novelty"],
        }
        for r in top[:10]
    ]

    logger.info(
        "Topic %d '%s': 수집 %d → 관련 %d → 저널 %d종 → 저장 %d종",
        topic_id, topic.name[:40], result["searched"], result["related"],
        result["journals"], saved,
    )
    return result


def _persist(
    topic: ResearchTopic,
    ranked: list[dict],
    meta: dict[str, dict],
    run_id: str,
) -> int:
    """상위 저널을 reco_journal_recommendations 에 저장한다.

    같은 (연구원, 주제, 저널, run_id) 가 있으면 갱신한다. 사용자가 이미
    구독/기각한 행의 status 는 건드리지 않는다.
    """
    from app.litreview.journal.journal_scoring import normalize_journal_name

    saved = 0
    for row in ranked:
        key = normalize_journal_name(row["journal_name"])
        info = meta.get(key) or {}

        existing = JournalRecommendation.query.filter_by(
            researcher_id=topic.researcher_id,
            topic_id=topic.id,
            journal_name=row["journal_name"],
            run_id=run_id,
        ).first()

        values = {
            "scopus_source_id": row.get("scopus_source_id") or info.get("source_id"),
            "issn": row.get("issn") or info.get("issn"),
            "related_paper_count": row["related_paper_count"],
            "avg_similarity": row["avg_similarity"],
            "growth_rate": row["growth_rate"],
            "familiarity": row["familiarity"],
            "novelty": row["novelty"],
            "citescore": info.get("citescore"),
            "quality": row["quality"],
            "score": row["score"],
            "rank": row["rank"],
            "evidence": row["evidence"],
        }

        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
        else:
            db.session.add(
                JournalRecommendation(
                    researcher_id=topic.researcher_id,
                    topic_id=topic.id,
                    journal_name=row["journal_name"],
                    run_id=run_id,
                    status="new",
                    **values,
                )
            )
        saved += 1

    db.session.commit()
    return saved


def run_journal_discovery(
    researcher_ids: list[int] | None = None,
    topic_ids: list[int] | None = None,
    apply_quality_gate: bool = True,
    progress_callback=None,
) -> dict:
    """저널 발굴 배치 — 대표벡터를 가진 모든 주제를 처리한다.

    Args:
        researcher_ids: 특정 연구원만. None 이면 전체.
        topic_ids: 특정 주제만. 지정하면 researcher_ids 는 무시된다.
        apply_quality_gate: CiteScore 게이트 적용 여부

    Returns:
        {run_id, topics_processed, topics_failed, journals_saved, results}
    """
    run_id = uuid.uuid4().hex[:16]

    query = ResearchTopic.query.filter(
        ResearchTopic.representative_vector.isnot(None)
    )
    if topic_ids:
        query = query.filter(ResearchTopic.id.in_(topic_ids))
    elif researcher_ids:
        query = query.filter(ResearchTopic.researcher_id.in_(researcher_ids))
    topics = query.order_by(
        ResearchTopic.researcher_id, ResearchTopic.sort_order
    ).all()

    logger.info("저널 발굴 시작 run_id=%s, 주제 %d개", run_id, len(topics))

    familiarity = load_lab_familiarity()

    out = {
        "run_id": run_id,
        "topics_total": len(topics),
        "topics_processed": 0,
        "topics_failed": 0,
        "journals_saved": 0,
        "results": [],
    }

    for i, topic in enumerate(topics, 1):
        try:
            res = recommend_journals_for_topic(
                topic.id,
                familiarity=familiarity,
                run_id=run_id,
                apply_quality_gate=apply_quality_gate,
            )
            out["results"].append(res)
            out["topics_processed"] += 1
            out["journals_saved"] += res["saved"]
        except ScopusUnavailable:
            db.session.rollback()
            logger.exception("Scopus 사용 불가 — 배치 중단 (topic %d)", topic.id)
            out["topics_failed"] += 1
            out["aborted"] = "scopus_unavailable"
            break
        except Exception:
            db.session.rollback()
            logger.exception("Topic %d 실패", topic.id)
            out["topics_failed"] += 1
            out["results"].append({"topic_id": topic.id, "error": True})

        if progress_callback:
            progress_callback("topic", i, len(topics))

    logger.info(
        "저널 발굴 완료 run_id=%s — 처리 %d, 실패 %d, 저장 %d종",
        run_id, out["topics_processed"], out["topics_failed"],
        out["journals_saved"],
    )
    return out


def subscribe_journal(recommendation_id: int) -> dict:
    """추천 저널을 수집 타겟(reco_target_journals)으로 편입한다.

    저널 발굴과 논문 추천을 잇는 유일한 통로. 사용자 승인이 있어야만 넘어간다.
    """
    rec = db.session.get(JournalRecommendation, recommendation_id)
    if not rec:
        raise ValueError(f"JournalRecommendation {recommendation_id} not found")

    existing = TargetJournal.query.filter_by(
        researcher_id=rec.researcher_id, journal_name=rec.journal_name
    ).first()

    if not existing:
        db.session.add(
            TargetJournal(
                researcher_id=rec.researcher_id,
                journal_name=rec.journal_name,
                issn=rec.issn,
                scopus_source_id=rec.scopus_source_id,
                source_type="journal_discovery",
                is_active=True,
            )
        )

    rec.status = "subscribed"
    db.session.commit()
    return {
        "recommendation_id": rec.id,
        "journal_name": rec.journal_name,
        "already_target": bool(existing),
    }
