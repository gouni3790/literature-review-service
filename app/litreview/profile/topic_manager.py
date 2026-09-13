"""주제 관리 — 수동 CRUD + B유형 자동 주제 + DOI 기반 레퍼런스 수집."""

import logging
import time
from datetime import datetime

import requests
from flask import current_app
from sqlalchemy import func

from app import db
from app.models import (
    PaperKeyword,
    PaperRecommendation,
    ReferencePaper,
    ResearchTopic,
    Researcher,
    TopicReferencePaper,
)

logger = logging.getLogger(__name__)

ABSTRACT_URL = "https://api.elsevier.com/content/abstract/doi/"
SEARCH_DELAY = 0.2

# keyword_seeded_topic 모듈과 값을 맞춘다 (순환 import 방지를 위해 상수만 복제)
KEYWORD_SEEDED_SOURCE = "keyword_seeded"


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config["SCOPUS_API_KEY"],
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "application/json",
    }


def _fetch_paper_by_doi(doi: str) -> dict | None:
    """DOI로 Scopus Abstract Retrieval API 호출 → 논문 정보 수집."""
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
    bibliography = (
        data.get("item", {})
        .get("bibrecord", {})
        .get("tail", {})
        .get("bibliography", {})
    )
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
        "doi": doi,
        "ref_journals": ref_journals,
    }


def create_manual_topic(
    researcher_id: int,
    name: str,
    keywords: list[str] | None = None,
    dois: list[str] | None = None,
    description: str | None = None,
) -> dict:
    """수동 주제 생성 + DOI 기반 레퍼런스 수집 + 대표 벡터 생성.

    Args:
        researcher_id: researchers.id
        name: 주제 이름
        keywords: 키워드 리스트
        dois: DOI 리스트
        description: 주제 설명

    Returns:
        {topic_id, name, keywords, reference_papers, has_vector}
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        raise ValueError(f"Researcher {researcher_id} not found")

    if not keywords and not dois and not description:
        raise ValueError("키워드, DOI, 설명 중 하나 이상 입력해야 합니다.")

    # 최대 sort_order + 1
    from sqlalchemy import func
    max_order = (
        db.session.query(func.max(ResearchTopic.sort_order))
        .filter_by(researcher_id=researcher_id)
        .scalar()
    ) or 0

    topic = ResearchTopic(
        researcher_id=researcher_id,
        name=name.strip(),
        source_type="manual",
        description=(description or "").strip() or None,
        keywords=keywords or [],
        sort_order=max_order + 1,
    )
    db.session.add(topic)
    db.session.flush()

    # DOI 기반 레퍼런스 수집
    ref_results = []
    if dois:
        for doi in dois:
            doi = doi.strip()
            if not doi:
                continue

            # 중복 체크
            existing = TopicReferencePaper.query.filter_by(
                topic_id=topic.id, doi=doi
            ).first()
            if existing:
                ref_results.append({"doi": doi, "status": "duplicate"})
                continue

            time.sleep(SEARCH_DELAY)
            paper_data = _fetch_paper_by_doi(doi)

            if paper_data:
                trp = TopicReferencePaper(
                    topic_id=topic.id,
                    scopus_id=paper_data["scopus_id"],
                    title=paper_data["title"],
                    authors=paper_data["authors"],
                    journal=paper_data["journal"],
                    year=paper_data["year"],
                    doi=doi,
                    abstract=paper_data["abstract"],
                    ref_journals=paper_data["ref_journals"],
                )
                db.session.add(trp)
                ref_results.append({
                    "doi": doi,
                    "status": "fetched",
                    "title": paper_data["title"],
                })
            else:
                ref_results.append({"doi": doi, "status": "failed"})

    db.session.commit()

    # 대표 벡터 생성
    has_vector = False
    try:
        from app.litreview.recommendation.embedder import Embedder
        embedder = Embedder()
        has_vector = embedder.embed_topic_representative(topic.id)
    except Exception:
        logger.exception("Failed to generate representative vector for topic %d", topic.id)

    logger.info(
        "Created manual topic %d '%s' for researcher %d "
        "(keywords=%d, refs=%d, vector=%s)",
        topic.id,
        topic.name,
        researcher_id,
        len(keywords or []),
        len(ref_results),
        has_vector,
    )

    return {
        "topic_id": topic.id,
        "name": topic.name,
        "keywords": topic.keywords,
        "reference_papers": ref_results,
        "has_vector": has_vector,
    }


def vector_is_text_based(topic) -> bool:
    """대표 벡터가 '주제명 + 키워드 + 설명' 텍스트에서 만들어지는 주제인가.

    embed_topic_representative()는 레퍼런스 논문 초록이 하나라도 있으면 그 초록을
    쓰고, 없을 때만 텍스트로 넘어간다. 따라서 초록이 있는 주제는 키워드/설명을
    고쳐도 벡터가 달라지지 않으므로 재임베딩할 필요가 없다.

    keyword_seeded 주제는 벡터가 '키워드와 매칭된 본인 논문들의 임베딩 평균'이라
    텍스트 임베딩으로 덮어쓰면 의미가 달라진다 — 여기서 제외하고,
    갱신이 필요하면 refresh_keyword_seeded_topic()을 써야 한다.
    """
    if topic.source_type == KEYWORD_SEEDED_SOURCE:
        return False
    for trp in topic.reference_papers or []:
        if trp.abstract:
            return False
    return True


def update_manual_topic(
    topic_id: int,
    name: str | None = None,
    keywords: list[str] | None = None,
    new_dois: list[str] | None = None,
    description: str | None = None,
) -> dict:
    """수동 주제 수정. 새 DOI 추가 시 Scopus 수집 + 대표 벡터 재생성."""
    topic = db.session.get(ResearchTopic, topic_id)
    if not topic:
        raise ValueError(f"Topic {topic_id} not found")
    if topic.source_type != "manual":
        raise ValueError("자동 생성 주제는 수정할 수 없습니다.")

    if name is not None:
        topic.name = name.strip()
    if keywords is not None:
        topic.keywords = keywords
    if description is not None:
        topic.description = description.strip() or None

    # 새 DOI 추가
    ref_results = []
    if new_dois:
        for doi in new_dois:
            doi = doi.strip()
            if not doi:
                continue

            existing = TopicReferencePaper.query.filter_by(
                topic_id=topic.id, doi=doi
            ).first()
            if existing:
                ref_results.append({"doi": doi, "status": "duplicate"})
                continue

            time.sleep(SEARCH_DELAY)
            paper_data = _fetch_paper_by_doi(doi)
            if paper_data:
                trp = TopicReferencePaper(
                    topic_id=topic.id,
                    scopus_id=paper_data["scopus_id"],
                    title=paper_data["title"],
                    authors=paper_data["authors"],
                    journal=paper_data["journal"],
                    year=paper_data["year"],
                    doi=doi,
                    abstract=paper_data["abstract"],
                    ref_journals=paper_data["ref_journals"],
                )
                db.session.add(trp)
                ref_results.append({"doi": doi, "status": "fetched"})
            else:
                ref_results.append({"doi": doi, "status": "failed"})

    db.session.commit()

    # --- 대표 벡터 재생성 ---
    # DOI가 추가되면 벡터 출처 자체가 '설명 텍스트'에서 '레퍼런스 초록'으로 바뀌므로
    # 무조건 재생성한다. DOI 변경이 없더라도 벡터가 이름/키워드/설명에서 나오는
    # 주제라면 그 텍스트를 고친 순간 벡터가 낡으므로 함께 재생성해야 한다.
    # (이 재생성이 없으면 화면의 초록과 실제 추천 기준이 어긋난다)
    has_vector = topic.representative_vector is not None
    text_changed = any(v is not None for v in (name, keywords, description))
    needs_reembed = bool(new_dois) or (text_changed and vector_is_text_based(topic))

    if needs_reembed:
        try:
            from app.litreview.recommendation.embedder import Embedder
            embedder = Embedder()
            has_vector = embedder.embed_topic_representative(topic.id)
        except Exception:
            logger.exception("Failed to regenerate vector for topic %d", topic.id)

    return {
        "topic_id": topic.id,
        "name": topic.name,
        "keywords": topic.keywords,
        "description": topic.description or "",
        "new_references": ref_results,
        "has_vector": has_vector,
        "revectorized": needs_reembed,
    }


def delete_topic(topic_id: int) -> bool:
    """주제 삭제.

    추천 이력은 **삭제하지 않는다.** topic_id 만 NULL 이 되어 "어느 주제에서
    나왔는지"만 잃고, 논문·요약·사용자 피드백은 그대로 남는다
    (models.py 의 ResearchTopic.recommendations 관계 참고).

    주제에 딸린 레퍼런스 논문(reco_topic_papers)은 주제 소유 데이터이므로
    함께 사라진다.
    """
    topic = db.session.get(ResearchTopic, topic_id)
    if not topic:
        return False

    name = topic.name
    detached = PaperRecommendation.query.filter_by(topic_id=topic_id).count()

    db.session.delete(topic)
    db.session.commit()

    logger.info(
        "Deleted topic %d '%s' — 추천 %d건은 보존(topic_id=NULL)",
        topic_id, name, detached,
    )
    return True


def get_topic_detail(topic_id: int) -> dict | None:
    """주제 상세 정보."""
    topic = db.session.get(ResearchTopic, topic_id)
    if not topic:
        return None

    return {
        "id": topic.id,
        "name": topic.name,
        "source_type": topic.source_type,
        "description": topic.description or "",
        "keywords": topic.keywords or [],
        "cluster_label": topic.cluster_label,
        "has_vector": topic.representative_vector is not None,
        "sort_order": topic.sort_order,
        "reference_papers": [
            {
                "id": trp.id,
                "title": trp.title,
                "journal": trp.journal,
                "year": trp.year,
                "doi": trp.doi,
                "has_abstract": bool(trp.abstract),
            }
            for trp in topic.reference_papers
        ],
        "recommendation_count": len(topic.recommendations),
        "created_at": str(topic.created_at) if topic.created_at else "",
        "updated_at": str(topic.updated_at) if topic.updated_at else "",
    }


# =========================================================================
# B유형 자동 주제 생성
# =========================================================================


def create_auto_topic_type_b(researcher_id: int) -> dict | None:
    """B유형 연구원의 자동 주제를 만들거나 **갱신**한다.

    키워드: 전체 논문 저자 키워드 빈도 상위 5개
    저널: query_builder에서 실시간 집계 (전체 게재 저널 빈도 상위 5개)
    유사도: 개별 논문 임베딩 vs 수집 논문 → max (similarity.py에서 처리)

    B유형은 대표 벡터를 사용하지 않으므로 representative_vector=None.

    삭제하지 않고 갱신하는 이유
    --------------------------
    이 함수는 주간 파이프라인 첫 단계에서 매주 호출된다. 예전에는 기존 자동
    주제를 지우고 새로 만들었는데, ResearchTopic.recommendations 에 걸린
    cascade 때문에 **그 주제의 추천 이력·LLM 요약·사용자 피드백이 함께
    삭제**됐다. B유형 연구원의 이력이 매주 초기화되고, 같은 논문의 요약 비용을
    반복 지불하게 된다.

    이제는 같은 행을 제자리에서 갱신하므로 topic_id 가 유지되고 이력이 남는다.
    (models.py 의 관계에서도 cascade 를 걷어내 이중으로 막았다)
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        raise ValueError(f"Researcher {researcher_id} not found")

    if researcher.researcher_type != "B":
        logger.warning(
            "create_auto_topic_type_b called for non-B researcher %d (type=%s)",
            researcher_id,
            researcher.researcher_type,
        )
        return None

    auto_topics = ResearchTopic.query.filter_by(
        researcher_id=researcher_id, source_type="auto",
    ).order_by(ResearchTopic.id).all()

    # 사용자가 마이페이지에서 키워드를 직접 편집한 주제는 건드리지 않는다.
    # (이 잡은 매주 실행되므로, 잠금이 없으면 편집분이 일주일 만에 사라진다)
    locked = [t for t in auto_topics if t.keywords_locked]
    if locked:
        topic = locked[0]
        logger.info(
            "Researcher %d (type B): auto topic %d is user-locked, skipping refresh",
            researcher_id,
            topic.id,
        )
        return {
            "topic_id": topic.id,
            "name": topic.name,
            "keywords": topic.keywords or [],
            "skipped": "keywords_locked",
        }

    # --- 키워드를 먼저 구한다 ---
    # 예전에는 기존 주제를 지운 뒤에 키워드를 조회해서, 키워드가 없으면
    # "주제는 이미 지워졌는데 새로 만들지도 못한" 상태로 빠져나갔다.
    # 어떤 변경도 하기 전에 필요한 값을 먼저 확보한다.
    kw_rows = (
        db.session.query(PaperKeyword.keyword, func.count(PaperKeyword.id))
        .join(ReferencePaper, PaperKeyword.paper_id == ReferencePaper.publication_id)
        .filter(ReferencePaper.researcher_id == researcher_id)
        .group_by(PaperKeyword.keyword)
        .order_by(func.count(PaperKeyword.id).desc())
        .limit(5)
        .all()
    )
    top_keywords = [kw for kw, _ in kw_rows]

    if not top_keywords:
        logger.warning(
            "Researcher %d (type B) has no keywords — 기존 자동 주제 %d개는 "
            "그대로 둔다 (삭제하지 않음)",
            researcher_id,
            len(auto_topics),
        )
        return None

    topic_name = f"전체 연구 ({', '.join(top_keywords[:3])})"

    if auto_topics:
        # 추천 이력이 가장 많은 주제를 본체로 삼는다 (동률이면 가장 오래된 것).
        # 정상 상태라면 자동 주제는 1개뿐이고, 여러 개인 것은 과거 버그의 잔재다.
        def _rec_count(t):
            return PaperRecommendation.query.filter_by(topic_id=t.id).count()

        primary = max(auto_topics, key=lambda t: (_rec_count(t), -t.id))
        extras = [t.id for t in auto_topics if t.id != primary.id]

        primary.name = topic_name
        primary.keywords = top_keywords
        primary.sort_order = 0
        primary.updated_at = datetime.utcnow()
        db.session.commit()

        if extras:
            logger.warning(
                "Researcher %d (type B): 자동 주제가 %d개입니다. t%d 를 갱신했고 "
                "나머지 %s 는 이력 보존을 위해 삭제하지 않았습니다. "
                "정리가 필요하면 수동으로 확인하세요.",
                researcher_id, len(auto_topics), primary.id, extras,
            )

        logger.info(
            "Updated auto topic %d for B-type researcher %d: keywords=%s",
            primary.id, researcher_id, top_keywords,
        )
        return {
            "topic_id": primary.id,
            "name": primary.name,
            "keywords": top_keywords,
            "source_type": "auto",
            "updated": True,
            "extra_auto_topics": extras,
        }

    topic = ResearchTopic(
        researcher_id=researcher_id,
        name=topic_name,
        source_type="auto",
        keywords=top_keywords,
        sort_order=0,
        # representative_vector=None → B유형은 개별 논문 max 비교
    )
    db.session.add(topic)
    db.session.commit()

    logger.info(
        "Created auto topic %d for B-type researcher %d: keywords=%s",
        topic.id,
        researcher_id,
        top_keywords,
    )

    return {
        "topic_id": topic.id,
        "name": topic.name,
        "keywords": top_keywords,
        "source_type": "auto",
        "updated": False,
    }
