"""API 라우트 — 주제 기반 통합 구조."""

import logging
from datetime import datetime

from flask import jsonify, request
from sqlalchemy import func

from app import db
from app.litreview import litreview_bp
from app.models import (
    BatchLog,
    CollectedPaper,
    JournalRecommendation,
    CustomJournal,
    CustomKeyword,
    PaperKeyword,
    PaperRecommendation,
    ReferencePaper,
    Researcher,
    ResearchCluster,
    ResearchTopic,
    TargetJournal,
)

logger = logging.getLogger(__name__)


# =========================================================================
# 페이지 라우트
# =========================================================================


@litreview_bp.route("/api/dashboard")
def dashboard():
    """메인 대시보드."""
    researchers = Researcher.query.order_by(Researcher.name_ko).all()
    return jsonify({
        "researchers": [
            {
                "id": r.id,
                "name": r.name,
                "email": r.email,
                "researcher_type": r.researcher_type,
                "paper_count": ReferencePaper.query.filter_by(researcher_id=r.id).count(),
            }
            for r in researchers
        ]
    })


@litreview_bp.route("/researcher/<int:researcher_id>")
def researcher_detail(researcher_id: int):
    """연구원 상세."""
    r = db.session.get(Researcher, researcher_id)
    if not r:
        return jsonify({"error": "Researcher not found"}), 404

    paper_count = ReferencePaper.query.filter_by(researcher_id=r.id).count()
    cluster_count = ResearchCluster.query.filter_by(researcher_id=r.id).count()
    topic_count = ResearchTopic.query.filter_by(researcher_id=r.id).count()
    rec_counts = (
        db.session.query(PaperRecommendation.grade, func.count(PaperRecommendation.id))
        .filter_by(researcher_id=r.id)
        .group_by(PaperRecommendation.grade)
        .all()
    )

    return jsonify({
        "id": r.id,
        "name": r.name,
        "email": r.email,
        "scopus_id": r.scopus_id,
        "group": r.group,
        "researcher_type": r.researcher_type,
        "research_description": r.research_description,
        "paper_count": paper_count,
        "cluster_count": cluster_count,
        "topic_count": topic_count,
        "recommendation_count": sum(cnt for _, cnt in rec_counts),
        "recommendation_counts": {g: cnt for g, cnt in rec_counts},
        "saved_count": PaperRecommendation.query.filter_by(researcher_id=r.id, is_saved=True).count(),
        "updated_at": str(r.updated_at) if r.updated_at else None,
        "settings": {
            "core_percent": r.core_percent,
            "related_percent": r.related_percent,
            "reference_percent": r.reference_percent,
            "target_year_range": r.target_year_range,
        },
    })


# =========================================================================
# 연구원 관리
# =========================================================================


@litreview_bp.route("/api/researchers", methods=["GET"])
def list_researchers():
    """연구원 목록."""
    researchers = Researcher.query.order_by(Researcher.created_at.desc()).all()
    return jsonify([
        {
            "id": r.id,
            "name": r.name,
            "email": r.email,
            "scopus_id": r.scopus_id,
            "researcher_type": r.researcher_type,
            "created_at": str(r.created_at),
        }
        for r in researchers
    ])


@litreview_bp.route("/api/researchers", methods=["POST"])
def enable_researcher():
    """기존 홈페이지 회원을 추천 대상으로 등록한다.

    이전에는 여기서 members 에 INSERT 를 시도했다. members 는 홈페이지가
    소유하는 테이블이라 이 앱이 회원을 만들면 안 되고, 실제로는 name 이
    읽기 전용 속성이라 AttributeError 로 죽고 있었다.

    회원 생성은 홈페이지에서 한다. 이 엔드포인트는 **이미 있는 회원**을 찾아
    추천 설정(reco_member_settings)만 만들어 준다.
    """
    data = request.get_json() or {}
    email = (data.get("email") or "").strip()
    name = (data.get("name") or "").strip()

    if not email and not name:
        return jsonify({"error": "email 또는 name 이 필요합니다."}), 400

    q = Researcher.query
    r = q.filter_by(email=email).first() if email else None
    if r is None and name:
        r = q.filter(
            db.or_(Researcher.name_ko == name, Researcher.name_en == name)
        ).first()

    if r is None:
        return jsonify({
            "error": "홈페이지에 등록된 회원이 아닙니다. "
                     "회원 추가는 연구실 홈페이지에서 진행해 주세요.",
            "searched": {"email": email or None, "name": name or None},
        }), 404

    from app.config import Config

    created = r._settings is None
    st = r.ensure_settings()
    if created:
        st.core_percent = Config.DEFAULT_CORE_PERCENT
        st.related_percent = Config.DEFAULT_RELATED_PERCENT
        st.reference_percent = Config.DEFAULT_REFERENCE_PERCENT
        st.target_year_range = Config.DEFAULT_TARGET_YEAR_RANGE
    st.is_active = True
    db.session.commit()

    logger.info(
        "Researcher %d (%s) 추천 대상 %s", r.id, r.name,
        "등록" if created else "재활성화",
    )
    return jsonify({
        "id": r.id, "name": r.name, "is_active": True, "created": created,
    }), 201 if created else 200


@litreview_bp.route("/api/researchers/<int:rid>", methods=["GET"])
def get_researcher(rid: int):
    """연구원 상세."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404
    return researcher_detail(rid)


@litreview_bp.route("/api/researchers/<int:rid>", methods=["PUT"])
def update_researcher(rid: int):
    """추천 설정 수정.

    이전에는 name/email/scopus_id/updated_at 를 members 에 직접 썼다. members 는
    홈페이지 소유라 이 앱이 고치면 안 되고, name 은 읽기 전용 속성이라 일부는
    AttributeError 로 죽고 있었다.

    회원 정보(이름·이메일·Scopus ID)는 홈페이지에서 수정한다.
    여기서는 reco_member_settings 의 추천 설정만 다룬다.
    """
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json() or {}

    HOMEPAGE_FIELDS = {"name", "email", "scopus_id", "orcid", "research_description"}
    rejected = HOMEPAGE_FIELDS & set(data)
    if rejected:
        return jsonify({
            "error": "회원 정보는 연구실 홈페이지에서 수정해 주세요.",
            "rejected_fields": sorted(rejected),
        }), 400

    SETTING_FIELDS = (
        "researcher_type", "core_percent", "related_percent", "reference_percent",
        "target_year_range", "max_per_grade", "email_cycle_weeks", "is_active",
    )
    st = r.ensure_settings()
    updated = []
    for field in SETTING_FIELDS:
        if field in data:
            setattr(st, field, data[field])
            updated.append(field)

    if not updated:
        return jsonify({
            "error": "수정할 설정이 없습니다.",
            "allowed_fields": list(SETTING_FIELDS),
        }), 400

    st.updated_at = datetime.utcnow()
    db.session.commit()
    logger.info("Researcher %d 설정 수정: %s", rid, ", ".join(updated))
    return jsonify({"id": r.id, "updated": updated})


@litreview_bp.route("/api/researchers/<int:rid>", methods=["DELETE"])
def deactivate_researcher(rid: int):
    """연구원을 추천 대상에서 제외한다 (비활성화).

    이전에는 db.session.delete(r) 로 **홈페이지 members 행을 삭제**했다.
    Researcher.__tablename__ 이 "members" 이므로 연구실 홈페이지의 회원 데이터가
    지워지고, cascade 로 소속·논문저자 연결까지 연쇄 삭제되는 동작이었다.
    인증도 없었다.

    회원 삭제는 홈페이지 소관이다. 여기서는 추천 설정만 끈다.
    추천 이력(reco_recommendations 등)은 보존한다 — 다시 켜면 그대로 이어진다.
    """
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    st = r.ensure_settings()
    st.is_active = False
    db.session.commit()

    logger.info("Researcher %d (%s) 추천 대상에서 비활성화", rid, r.name)
    return jsonify({
        "id": rid,
        "name": r.name,
        "is_active": False,
        "note": "추천 대상에서 제외했습니다. 회원 정보 자체는 홈페이지에서 관리합니다.",
    })


@litreview_bp.route("/api/researchers/<int:rid>/fetch", methods=["POST"])
def fetch_researcher_papers(rid: int):
    """Scopus에서 논문 가져오기."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404
    if not r.scopus_id:
        return jsonify({"error": "No scopus_id"}), 400

    from app import socketio
    from app.litreview.profile.scopus_fetcher import fetch_researcher_publications

    def progress(done, total):
        socketio.emit(
            "scopus.collect.progress",
            {"researcher_id": rid, "done": done, "total": total},
            to=f"researcher:{rid}",
        )

    socketio.emit(
        "scopus.collect.start",
        {"researcher_id": rid},
        to=f"researcher:{rid}",
    )

    try:
        result = fetch_researcher_publications(
            r.scopus_id, rid, progress_callback=progress
        )
        socketio.emit(
            "scopus.collect.done",
            {"researcher_id": rid, **result},
            to=f"researcher:{rid}",
        )

        # 임베딩 자동 실행
        from app.litreview.recommendation.embedder import Embedder

        embedder = Embedder()
        embedded = embedder.embed_paper_abstracts(
            table="reference_papers", researcher_id=rid
        )
        result["embedded"] = embedded

        # 유형별 자동 주제 생성
        if result["researcher_type"] == "A":
            # 10편+ → 클러스터링 → 클러스터별 자동 주제
            from app.litreview.profile.cluster_analyzer import analyze_clusters

            cluster_result = analyze_clusters(rid)
            result["clusters"] = cluster_result
        elif result["researcher_type"] == "B":
            # 2~29편 → B유형 자동 주제 1개
            from app.litreview.profile.topic_manager import create_auto_topic_type_b

            try:
                b_topic = create_auto_topic_type_b(rid)
                result["auto_topic"] = b_topic
            except Exception:
                logger.exception("Auto topic creation failed for B-type researcher %d", rid)

        # 본인 게재지를 수집 타겟으로 동기화.
        # 참고문헌 역추적은 폐지했다 — 인용한 저널은 이미 아는 저널이라
        # 발굴 가치가 없고, 자동 등록이 타겟 목록을 오염시켰다.
        from app.litreview.journal.published_sync import sync_published_journals

        sync_result = sync_published_journals(rid)
        result["published_journals"] = sync_result["added"]

        return jsonify(result), 200

    except Exception as e:
        logger.exception("Fetch failed for researcher %d", rid)
        socketio.emit(
            "scopus.collect.done",
            {"researcher_id": rid, "error": str(e)},
            to=f"researcher:{rid}",
        )
        return jsonify({"error": str(e)}), 500


@litreview_bp.route("/api/researchers/<int:rid>/cluster", methods=["POST"])
def run_clustering(rid: int):
    """클러스터링 실행 (10편+) → 자동 주제 생성."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    from app import socketio
    from app.litreview.profile.cluster_analyzer import analyze_clusters

    socketio.emit("cluster.start", {"researcher_id": rid}, to=f"researcher:{rid}")

    try:
        result = analyze_clusters(rid)
        socketio.emit(
            "cluster.done",
            {"researcher_id": rid, "k": result.get("cluster_count"), "silhouette": result.get("silhouette")},
            to=f"researcher:{rid}",
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("Clustering failed for researcher %d", rid)
        return jsonify({"error": str(e)}), 500


# =========================================================================
# 프로필 관리
# =========================================================================


@litreview_bp.route("/api/researchers/<int:rid>/profile", methods=["GET"])
def get_profile(rid: int):
    """프로필 조회 (자동 집계 + 수동 + 주제)."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    kw_freq = (
        db.session.query(PaperKeyword.keyword, func.count(PaperKeyword.id))
        .join(ReferencePaper, PaperKeyword.paper_id == ReferencePaper.publication_id)
        .filter(ReferencePaper.researcher_id == rid)
        .group_by(PaperKeyword.keyword)
        .order_by(func.count(PaperKeyword.id).desc())
        .limit(20)
        .all()
    )

    custom_kws = CustomKeyword.query.filter_by(researcher_id=rid).all()
    custom_jrns = CustomJournal.query.filter_by(researcher_id=rid).all()
    clusters = ResearchCluster.query.filter_by(researcher_id=rid).all()
    topics = ResearchTopic.query.filter_by(researcher_id=rid).order_by(ResearchTopic.sort_order).all()

    return jsonify({
        "researcher_type": r.researcher_type,
        "research_description": r.research_description,
        "keyword_frequency": [{"keyword": kw, "count": cnt} for kw, cnt in kw_freq],
        "custom_keywords": [{"id": k.id, "keyword": k.keyword} for k in custom_kws],
        "custom_journals": [
            {"id": j.id, "journal_name": j.journal_name, "added_by": j.added_by}
            for j in custom_jrns
        ],
        "clusters": [
            {"id": c.id, "label": c.cluster_label, "paper_count": c.paper_count}
            for c in clusters
        ],
        "topics": [
            {
                "id": t.id,
                "name": t.name,
                "source_type": t.source_type,
                "keywords": t.keywords or [],
                "has_vector": t.representative_vector is not None,
            }
            for t in topics
        ],
    })


@litreview_bp.route("/api/researchers/<int:rid>/profile", methods=["PUT"])
def update_profile(rid: int):
    """연구 설명 수정."""
    from app.litreview.profile.profile_manager import ProfileManager

    data = request.get_json()
    try:
        result = ProfileManager.update_research_description(rid, data.get("research_description", ""))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@litreview_bp.route("/api/researchers/<int:rid>/keywords", methods=["POST"])
def add_keyword(rid: int):
    """관심 키워드 추가."""
    from app.litreview.profile.profile_manager import ProfileManager

    data = request.get_json()
    try:
        result = ProfileManager.add_custom_keyword(rid, data.get("keyword", ""))
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@litreview_bp.route("/api/researchers/<int:rid>/keywords/<int:kid>", methods=["DELETE"])
def delete_keyword(rid: int, kid: int):
    """관심 키워드 삭제."""
    from app.litreview.profile.profile_manager import ProfileManager

    deleted = ProfileManager.delete_custom_keyword(kid)
    if not deleted:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"deleted": True})


@litreview_bp.route("/api/researchers/<int:rid>/journals", methods=["POST"])
def add_journal(rid: int):
    """관심 저널 추가."""
    from app.litreview.profile.profile_manager import ProfileManager

    data = request.get_json()
    try:
        result = ProfileManager.add_custom_journal(
            rid,
            data.get("journal_name", ""),
            scopus_source_id=data.get("scopus_source_id"),
            added_by=data.get("added_by", "user"),
        )
        from app.litreview.journal.journal_analyzer import _ensure_target_journal
        _ensure_target_journal(rid, data.get("journal_name", ""), "manual")
        db.session.commit()

        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@litreview_bp.route("/api/researchers/<int:rid>/journals/<int:jid>", methods=["DELETE"])
def delete_journal(rid: int, jid: int):
    """관심 저널 삭제."""
    from app.litreview.profile.profile_manager import ProfileManager

    deleted = ProfileManager.delete_custom_journal(jid)
    if not deleted:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"deleted": True})


@litreview_bp.route("/api/researchers/<int:rid>/settings", methods=["PUT"])
def update_settings(rid: int):
    """추천 설정 변경."""
    from app.litreview.profile.profile_manager import ProfileManager

    data = request.get_json()
    try:
        result = ProfileManager.update_recommendation_settings(rid, data)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# =========================================================================
# 추천
# =========================================================================


@litreview_bp.route("/api/researchers/<int:rid>/recommendations", methods=["GET"])
def get_recommendations(rid: int):
    """추천 논문 조회 (등급/주제 필터)."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    grade = request.args.get("grade")
    topic_id = request.args.get("topic_id", type=int)

    query = PaperRecommendation.query.filter_by(researcher_id=rid)
    if grade:
        query = query.filter_by(grade=grade)
    if topic_id:
        query = query.filter_by(topic_id=topic_id)

    recs = query.order_by(PaperRecommendation.similarity_score.desc()).all()

    return jsonify([
        {
            "id": rec.id,
            "paper_id": rec.paper_id,
            "title": rec.paper.title if rec.paper else None,
            "journal": rec.paper.journal if rec.paper else None,
            "year": rec.paper.year if rec.paper else None,
            "doi": rec.paper.doi if rec.paper else None,
            "grade": rec.grade,
            "topic_id": rec.topic_id,
            "topic_name": rec.topic.name if rec.topic else None,
            "similarity_score": rec.similarity_score,
            "percentile_rank": rec.percentile_rank,
            "grade_reason": rec.grade_reason,
            "recommendation_reason": rec.recommendation_reason,
            "summary_source": rec.summary_source,
            "summary_core_topic": rec.summary_core_topic,
            "summary_purpose": rec.summary_purpose,
            "summary_method": rec.summary_method,
            "summary_results": rec.summary_results,
            "is_saved": rec.is_saved,
            "is_read": rec.is_read,
            "user_feedback": rec.user_feedback,
            "created_at": str(rec.created_at),
        }
        for rec in recs
    ])


@litreview_bp.route("/api/researchers/<int:rid>/useful-recommendations", methods=["GET"])
def get_useful_recommendations(rid: int):
    """useful 별도 트랙 추천 — useful 논문별로 그룹핑된 결과 (1대1 매칭).

    Query: ?threshold=0.6 (cosine 임계치)
    """
    if not db.session.get(Researcher, rid):
        return jsonify({"error": "Not found"}), 404

    threshold = request.args.get("threshold", default=0.6, type=float)
    from app.litreview.feedback.useful_track import calculate_useful_recommendations
    return jsonify(calculate_useful_recommendations(rid, threshold=threshold))


@litreview_bp.route("/api/recommendations/<int:rec_id>/save", methods=["POST"])
def save_recommendation(rec_id: int):
    """논문 저장 (북마크)."""
    rec = db.session.get(PaperRecommendation, rec_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    rec.is_saved = True
    rec.is_read = True
    db.session.commit()
    return jsonify({"saved": True})


@litreview_bp.route("/api/recommendations/<int:rec_id>/feedback", methods=["POST"])
def submit_feedback(rec_id: int):
    """피드백 (useful / not_relevant)."""
    rec = db.session.get(PaperRecommendation, rec_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json()
    feedback = data.get("feedback")
    if feedback not in ("useful", "not_relevant"):
        return jsonify({"error": "feedback must be 'useful' or 'not_relevant'"}), 400

    rec.user_feedback = feedback
    rec.is_read = True
    db.session.commit()

    if feedback == "useful" and rec.paper:
        from app.litreview.journal.journal_analyzer import add_feedback_journal
        add_feedback_journal(rec.researcher_id, rec.paper.journal)

    return jsonify({"feedback": feedback})


# =========================================================================
# 저널
# =========================================================================


@litreview_bp.route("/api/researchers/<int:rid>/journals/target", methods=["GET"])
def get_target_journals(rid: int):
    """타겟 저널 목록."""
    journals = TargetJournal.query.filter_by(
        researcher_id=rid
    ).order_by(TargetJournal.created_at.desc()).all()

    return jsonify([
        {
            "id": j.id,
            "journal_name": j.journal_name,
            "issn": j.issn,
            "scopus_source_id": j.scopus_source_id,
            "source_type": j.source_type,
            "is_active": j.is_active,
            "last_checked": str(j.last_checked) if j.last_checked else None,
        }
        for j in journals
    ])


@litreview_bp.route("/api/journals/<int:jid>/toggle", methods=["POST"])
def toggle_journal(jid: int):
    """저널 모니터링 활성/비활성 토글."""
    j = db.session.get(TargetJournal, jid)
    if not j:
        return jsonify({"error": "Not found"}), 404

    j.is_active = not j.is_active
    db.session.commit()
    return jsonify({"id": j.id, "is_active": j.is_active})


@litreview_bp.route("/api/journals/discovery", methods=["GET"])
def get_discovery_candidates():
    """저널 탐색 후보 조회."""
    rid = request.args.get("researcher_id", type=int)
    if not rid:
        return jsonify({"error": "researcher_id required"}), 400

    # 월간 키워드 탐색은 폐지하고 저널 발굴 파이프라인으로 대체했다.
    # 이 엔드포인트는 가장 최근 실행분의 결과를 돌려준다.
    latest = (
        JournalRecommendation.query
        .filter_by(researcher_id=rid)
        .order_by(JournalRecommendation.created_at.desc())
        .first()
    )
    if not latest:
        return jsonify([])

    rows = (
        JournalRecommendation.query
        .filter_by(researcher_id=rid, run_id=latest.run_id)
        .order_by(JournalRecommendation.score.desc())
        .all()
    )
    return jsonify([_journal_rec_dict(r) for r in rows])


def _journal_rec_dict(r) -> dict:
    """저널 추천 1건 → JSON. 지표 원값을 그대로 노출해 화면에서 근거를 보여준다."""
    return {
        "id": r.id,
        "researcher_id": r.researcher_id,
        "topic_id": r.topic_id,
        "topic_name": r.topic.name if r.topic else None,
        "run_id": r.run_id,
        "journal_name": r.journal_name,
        "scopus_source_id": r.scopus_source_id,
        "issn": r.issn,
        "related_paper_count": r.related_paper_count,
        "avg_similarity": r.avg_similarity,
        "growth_rate": r.growth_rate,
        "familiarity": r.familiarity,
        "novelty": r.novelty,
        "citescore": r.citescore,
        "quality": r.quality,
        "score": r.score,
        "rank": r.rank,
        "evidence": r.evidence or [],
        "status": r.status,
        "created_at": str(r.created_at) if r.created_at else None,
    }


@litreview_bp.route("/api/researchers/<int:rid>/journal-recommendations", methods=["GET"])
def list_journal_recommendations(rid: int):
    """저널 발굴 결과 조회.

    쿼리 파라미터:
        run_id  특정 회차만. 없으면 가장 최근 회차.
        status  쉼표 구분 필터 (기본: new,seen)
        topic_id 특정 주제만
    """
    q = JournalRecommendation.query.filter_by(researcher_id=rid)

    run_id = request.args.get("run_id")
    if not run_id:
        latest = (
            JournalRecommendation.query.filter_by(researcher_id=rid)
            .order_by(JournalRecommendation.created_at.desc())
            .first()
        )
        if not latest:
            return jsonify({"run_id": None, "journals": []})
        run_id = latest.run_id
    q = q.filter_by(run_id=run_id)

    topic_id = request.args.get("topic_id", type=int)
    if topic_id:
        q = q.filter_by(topic_id=topic_id)

    statuses = request.args.get("status", "new,seen")
    if statuses != "all":
        q = q.filter(JournalRecommendation.status.in_(statuses.split(",")))

    rows = q.order_by(JournalRecommendation.score.desc()).all()
    return jsonify({
        "run_id": run_id,
        "count": len(rows),
        "journals": [_journal_rec_dict(r) for r in rows],
    })


@litreview_bp.route(
    "/api/journal-recommendations/<int:jrid>/subscribe", methods=["POST"]
)
def subscribe_journal_recommendation(jrid: int):
    """추천 저널을 수집 타겟으로 편입 — 저널 발굴과 논문 추천을 잇는 통로."""
    from app.litreview.journal.journal_recommender import subscribe_journal

    try:
        return jsonify(subscribe_journal(jrid))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@litreview_bp.route(
    "/api/journal-recommendations/<int:jrid>/dismiss", methods=["POST"]
)
def dismiss_journal_recommendation(jrid: int):
    """추천 저널 기각. 다음 회차에서 다시 뜨지 않게 하려면 이 상태를 참조한다."""
    rec = db.session.get(JournalRecommendation, jrid)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    rec.status = "dismissed"
    db.session.commit()
    return jsonify({"id": rec.id, "status": rec.status})


@litreview_bp.route("/api/journal-discovery/run", methods=["POST"])
def run_journal_discovery_api():
    """저널 발굴 수동 실행. body: {researcher_ids?: [], topic_ids?: []}"""
    from app.litreview.journal.journal_recommender import run_journal_discovery

    data = request.get_json(silent=True) or {}
    try:
        result = run_journal_discovery(
            researcher_ids=data.get("researcher_ids"),
            topic_ids=data.get("topic_ids"),
            apply_quality_gate=data.get("apply_quality_gate", True),
        )
        status = 200 if result["topics_processed"] else 500
        return jsonify(result), status
    except Exception as e:
        logger.exception("Journal discovery run failed")
        return jsonify({"error": str(e)}), 500


@litreview_bp.route("/api/researchers/<int:rid>/journals/sync-published", methods=["POST"])
def sync_published_journals_api(rid: int):
    """본인 게재지를 수집 타겟으로 동기화."""
    from app.litreview.journal.published_sync import sync_published_journals

    try:
        return jsonify(sync_published_journals(rid))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


# =========================================================================
# 시스템
# =========================================================================


@litreview_bp.route("/api/scheduler/status", methods=["GET"])
def scheduler_status():
    """스케줄러 상태."""
    from app.litreview.scheduler.jobs import scheduler

    if not scheduler or not scheduler.running:
        return jsonify({"running": False, "jobs": []})

    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })

    return jsonify({"running": True, "jobs": jobs})


@litreview_bp.route("/api/scheduler/run/<job_type>", methods=["POST"])
def run_batch_manually(job_type: str):
    """수동 배치 실행."""
    from app.litreview.scheduler.jobs import (
        monthly_journal_discovery,
        weekly_email_notification,
        weekly_journal_monitoring,
        weekly_paper_pipeline,
    )

    job_map = {
        "weekly_paper_pipeline": weekly_paper_pipeline,
        "weekly_journal_monitoring": weekly_journal_monitoring,
        "weekly_email_notification": weekly_email_notification,
        "monthly_journal_discovery": monthly_journal_discovery,
    }

    job_fn = job_map.get(job_type)
    if not job_fn:
        return jsonify({"error": f"Unknown job: {job_type}", "available": list(job_map.keys())}), 400

    from app import socketio

    def _emit_progress(step: str, message: str) -> None:
        # 통합 배포(Web_server.py)에서는 app.socketio가 init_app 되지 않아
        # emit이 실패할 수 있음 — 진행 알림 실패가 배치 실행을 막으면 안 됨.
        try:
            socketio.emit("batch.progress", {"job": job_type, "step": step, "message": message}, to="admin")
        except Exception:
            logger.warning("batch.progress emit failed (job=%s, step=%s)", job_type, step)

    _emit_progress("started", f"{job_type} 수동 실행")

    try:
        job_fn()
        _emit_progress("completed", f"{job_type} 완료")
        return jsonify({"job_type": job_type, "status": "completed"})
    except Exception as e:
        logger.exception("Manual batch run failed: %s", job_type)
        return jsonify({"error": str(e)}), 500


@litreview_bp.route("/api/batch-logs", methods=["GET"])
def get_batch_logs():
    """배치 실행 로그."""
    limit = request.args.get("limit", 20, type=int)
    logs = BatchLog.query.order_by(BatchLog.started_at.desc()).limit(limit).all()

    return jsonify([
        {
            "id": l.id,
            "job_type": l.job_type,
            "status": l.status,
            "details": l.details,
            "started_at": str(l.started_at),
            "completed_at": str(l.completed_at) if l.completed_at else None,
        }
        for l in logs
    ])


# =========================================================================
# 논문 수동 추가
# =========================================================================


@litreview_bp.route("/api/researchers/<int:rid>/papers", methods=["POST"])
def add_reference_paper(rid: int):
    """연구원의 새 논문 수동 추가 → 임베딩 → type 전환 체크."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json()
    if not data.get("title"):
        return jsonify({"error": "title required"}), 400

    paper = ReferencePaper(
        researcher_id=rid,
        scopus_id=data.get("scopus_id"),
        title=data["title"],
        authors=data.get("authors"),
        journal=data.get("journal"),
        year=data.get("year"),
        abstract=data.get("abstract"),
        doi=data.get("doi"),
    )
    db.session.add(paper)
    db.session.commit()

    if paper.abstract:
        from app.litreview.recommendation.embedder import Embedder
        embedder = Embedder()
        embedder.embed_paper_abstracts(
            paper_ids=[paper.id], table="reference_papers"
        )

    count = ReferencePaper.query.filter_by(researcher_id=rid).count()
    new_type = "A" if count >= 10 else ("B" if count >= 2 else "C")
    old_type = r.researcher_type

    if new_type != old_type:
        r.researcher_type = new_type
        r.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info("Researcher %d type changed: %s → %s", rid, old_type, new_type)

        if new_type == "A" and old_type != "A":
            from app.litreview.profile.cluster_analyzer import analyze_clusters
            try:
                analyze_clusters(rid)
            except Exception:
                logger.exception("Auto-clustering failed for researcher %d", rid)
        elif new_type == "B":
            from app.litreview.profile.topic_manager import create_auto_topic_type_b
            try:
                create_auto_topic_type_b(rid)
            except Exception:
                logger.exception("Auto topic creation failed for B-type researcher %d", rid)

    return jsonify({
        "id": paper.id,
        "researcher_type": r.researcher_type,
        "paper_count": count,
    }), 201


# =========================================================================
# 관리자
# =========================================================================


@litreview_bp.route("/api/admin/default-journals", methods=["POST"])
def set_admin_default_journals():
    """관리자가 C유형 연구원에게 기본 저널 설정."""
    data = request.get_json()
    researcher_id = data.get("researcher_id")
    journal_names = data.get("journals", [])

    r = db.session.get(Researcher, researcher_id)
    if not r:
        return jsonify({"error": "Researcher not found"}), 404

    from app.litreview.profile.profile_manager import ProfileManager

    added = []
    for jname in journal_names:
        result = ProfileManager.add_custom_journal(
            researcher_id, jname, added_by="admin"
        )
        added.append(result)

    return jsonify({"added": added})
