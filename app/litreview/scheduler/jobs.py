"""APScheduler 배치 작업 4개. persistent jobstore (SQLAlchemy).

주제 기반 파이프라인: 각 주제별 독립 수집 → 임베딩 → 유사도 → 등급 → core 요약.
"""

import logging
from datetime import datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from flask import has_app_context

from app import db
from app.models import (
    BatchLog,
    CollectedPaper,
    PaperRecommendation,
    Researcher,
    ResearchTopic,
)

logger = logging.getLogger(__name__)

scheduler: BackgroundScheduler | None = None
_scheduler_app = None


def init_scheduler(app):
    """스케줄러 초기화 + 주간 추천 준비/발송 배치 등록."""
    global scheduler, _scheduler_app
    _scheduler_app = app

    # jobstore 는 홈페이지와 공유하는 bist.db 가 아니라 수집물 DB에 둔다.
    # apscheduler_jobs 는 잡이 실행/재예약될 때마다 갱신되므로 공유 파일에
    # 불필요한 쓰기·락을 만들지 않기 위함이다.
    from app import resolve_papers_db_path

    papers_path = resolve_papers_db_path(app).replace("\\", "/")
    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{papers_path}")}

    tz = app.config.get("SCHEDULER_TIMEZONE", "Asia/Seoul")

    scheduler = BackgroundScheduler(
        jobstores=jobstores,
        timezone=tz,
        # 서버 부하로 스케줄러가 정각에서 수 초 밀리면 기본 유예(1초)로는
        # 잡이 통째로 스킵됨 — 재시작 직후 만회 실행까지 고려해 1시간 유예.
        job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    )

    scheduler.add_job(
        weekly_publication_sync,
        "cron",
        day_of_week="sun",
        hour=9,
        minute=0,
        id="weekly_publication_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        weekly_paper_pipeline,
        "cron",
        day_of_week="sun",
        hour=10,
        minute=0,
        id="weekly_paper_pipeline",
        replace_existing=True,
    )
    scheduler.add_job(
        weekly_journal_monitoring,
        "cron",
        day_of_week="sun",
        hour=12,
        minute=0,
        id="weekly_journal_monitoring",
        replace_existing=True,
    )
    scheduler.add_job(
        weekly_email_notification,
        "cron",
        day_of_week="mon",
        hour=10,
        minute=0,
        id="weekly_email_notification",
        replace_existing=True,
    )
    scheduler.add_job(
        monthly_journal_discovery,
        "cron",
        day=1,
        hour=3,
        minute=0,
        id="monthly_journal_discovery",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started with weekly recommendation jobs (tz=%s)", tz)
    return scheduler


def _run_in_app_context(func):
    if has_app_context() or _scheduler_app is None:
        return func()
    with _scheduler_app.app_context():
        return func()


def _log_batch(job_type: str, status: str, details: dict | None = None) -> BatchLog:
    log = BatchLog(
        job_type=job_type,
        status=status,
        details=details,
        started_at=datetime.utcnow(),
    )
    db.session.add(log)
    db.session.commit()
    return log


def _complete_batch(log: BatchLog, status: str, details: dict | None = None):
    log.status = status
    log.completed_at = datetime.utcnow()
    if details:
        log.details = {**(log.details or {}), **details}
    db.session.commit()


def weekly_publication_sync():
    """일요일 09:00 — 전체 연구원 출판물 Scopus 동기화.

    scopus_id가 등록된 연구원마다 신규 논문 upsert + 기존 논문 인용수 갱신.
    BIST paper 대시보드('lab-stats')가 이 데이터를 읽으므로, 이 잡이
    돌아야 신규 투고 논문이 화면에 반영된다. 실패 시 관리자 이메일 알림.
    """
    if not has_app_context() and _scheduler_app is not None:
        return _run_in_app_context(weekly_publication_sync)

    from app.litreview.notification.email_sender import send_admin_alert
    from app.litreview.profile.scopus_fetcher import sync_researcher_publications

    log = _log_batch("publication_sync", "running")
    results = {"researchers": [], "total_new": 0, "total_updated": 0}
    errors = []

    researchers = Researcher.query.filter(
        Researcher.scopus_id.isnot(None), Researcher.scopus_id != ""
    ).all()

    for r in researchers:
        try:
            stats = sync_researcher_publications(r.id)
            results["researchers"].append({
                "id": r.id,
                "name": r.name,
                **stats,
            })
            results["total_new"] += stats["new_papers"]
            results["total_updated"] += stats["updated_citations"]
        except Exception as e:
            logger.exception("Publication sync failed for researcher %d", r.id)
            db.session.rollback()
            errors.append(f"{r.name} (id={r.id}): {e}")
            results["researchers"].append({
                "id": r.id,
                "name": r.name,
                "error": str(e),
            })

    if errors:
        results["errors"] = errors
        _complete_batch(log, "failed" if len(errors) == len(researchers) else "partial", results)
        send_admin_alert(
            "출판물 동기화 오류",
            f"연구원 {len(researchers)}명 중 {len(errors)}명 동기화 실패:\n\n"
            + "\n".join(errors),
        )
    else:
        _complete_batch(log, "completed", results)

    logger.info(
        "Publication sync done: %d new, %d citation updates, %d errors",
        results["total_new"],
        results["total_updated"],
        len(errors),
    )
    return results


def weekly_paper_pipeline():
    """일요일 10:00 — 전체 연구원 주제별 추천 준비 파이프라인.

    각 주제별: 쿼리 → 수집 → 임베딩 → 유사도(주제 대표 벡터 vs 수집 논문) → 등급 → core 요약
    """
    if not has_app_context() and _scheduler_app is not None:
        return _run_in_app_context(weekly_paper_pipeline)

    from app.litreview.collection.fulltext_downloader import download_fulltext
    from app.litreview.collection.paper_collector import collect_papers
    from app.litreview.collection.query_builder import build_queries
    from app.litreview.profile.topic_manager import create_auto_topic_type_b
    from app.litreview.recommendation.embedder import Embedder
    from app.litreview.recommendation.grader import assign_grades
    from app.litreview.recommendation.similarity import calculate_similarity
    from app.litreview.recommendation.summarizer import summarize_core_papers

    log = _log_batch("weekly_paper_pipeline", "running")
    results = {"researchers": []}

    try:
        researchers = Researcher.query.all()
        embedder = Embedder()

        for r in researchers:
            r_result = {"id": r.id, "name": r.name, "topics": []}
            try:
                # 0. B유형: 자동 주제 갱신 (키워드/저널 빈도 변동 반영)
                if r.researcher_type == "B":
                    try:
                        create_auto_topic_type_b(r.id)
                    except Exception:
                        logger.exception(
                            "B-type auto topic refresh failed for researcher %d",
                            r.id,
                        )

                # 1. 주제별 쿼리 생성
                queries = build_queries(r.id)
                r_result["total_queries"] = len(queries)

                if not queries:
                    results["researchers"].append(r_result)
                    continue

                # 2. 논문 수집 (모든 주제의 쿼리를 한꺼번에)
                collect_result = collect_papers(queries, researcher_id=r.id)
                r_result["collected"] = {
                    k: v for k, v in collect_result.items()
                    if k not in ("collected_paper_ids", "new_paper_ids")
                }

                # 이 연구원의 검색으로 매칭해야 할 모든 논문 (신규 + 기존)
                researcher_paper_ids = collect_result.get("collected_paper_ids", [])
                # 이번에 새로 추가된 것만 (임베딩 대상)
                new_ids = collect_result.get("new_paper_ids", [])

                # 2-b. 관심 저자의 신규 논문 (OpenAlex Author ID 기준 추적)
                # 키워드 검색으로는 안 걸리지만 팔로우한 저자가 낸 논문을
                # 같은 유사도·등급 절차에 태우기 위해 후보 풀에 합친다.
                try:
                    from app.litreview.author.service import track_followed_authors

                    author_result = track_followed_authors(r.id)
                    author_paper_ids = author_result["collected_paper_ids"]
                    if author_paper_ids:
                        researcher_paper_ids = list(
                            dict.fromkeys(researcher_paper_ids + author_paper_ids)
                        )
                        new_ids = list(dict.fromkeys(new_ids + author_paper_ids))
                    r_result["followed_authors"] = {
                        "authors": len(author_result["authors"]),
                        "new_papers": sum(
                            a["new_papers"] for a in author_result["authors"]
                        ),
                        "added_to_pool": len(author_paper_ids),
                    }
                except Exception:
                    logger.exception(
                        "Followed-author tracking failed for researcher %d", r.id
                    )
                    db.session.rollback()

                # 3. 신규 논문만 임베딩 (기존 것은 이미 임베딩 되어 있음)
                if new_ids:
                    new_papers_to_embed = (
                        CollectedPaper.query.filter(
                            CollectedPaper.id.in_(new_ids),
                            CollectedPaper.embedding.is_(None),
                            CollectedPaper.abstract.isnot(None),
                        ).all()
                    )
                    embed_ids = [p.id for p in new_papers_to_embed]
                    if embed_ids:
                        embedder.embed_paper_abstracts(
                            paper_ids=embed_ids, table="collected_papers"
                        )

                # 4. 주제별 유사도 + 등급 — 이 연구원의 검색 결과 전체를 매칭 대상으로
                embedded_ids = [
                    p.id
                    for p in CollectedPaper.query.filter(
                        CollectedPaper.id.in_(researcher_paper_ids),
                        CollectedPaper.embedding.isnot(None),
                    ).all()
                ] if researcher_paper_ids else []

                if r.researcher_type == "B":
                    # B유형: 자동 주제(max mode, 벡터 불필요) + 벡터 있는 수동 주제
                    from sqlalchemy import or_
                    topics = ResearchTopic.query.filter_by(
                        researcher_id=r.id
                    ).filter(
                        or_(
                            ResearchTopic.source_type == "auto",
                            ResearchTopic.representative_vector.isnot(None),
                        )
                    ).all()
                else:
                    topics = ResearchTopic.query.filter_by(
                        researcher_id=r.id
                    ).filter(
                        ResearchTopic.representative_vector.isnot(None)
                    ).all()

                for topic in topics:
                    topic_result = {"topic_id": topic.id, "name": topic.name}
                    try:
                        if embedded_ids:
                            sims = calculate_similarity(topic.id, embedded_ids)
                            topic_result["similarities"] = len(sims)

                            if sims:
                                grades = assign_grades(topic.id, sims)
                                topic_result["grades"] = {
                                    g: len([x for x in grades if x["grade"] == g])
                                    for g in ("core", "related", "reference")
                                }

                                # core 전문 다운로드
                                core_paper_ids = [
                                    x["paper_id"]
                                    for x in grades
                                    if x["grade"] == "core"
                                ]
                                if core_paper_ids:
                                    dl_result = download_fulltext(core_paper_ids)
                                    topic_result["fulltext"] = dl_result

                    except Exception:
                        logger.exception(
                            "Topic pipeline failed for topic %d (researcher %d)",
                            topic.id,
                            r.id,
                        )
                        topic_result["error"] = True

                    r_result["topics"].append(topic_result)

                # 5. core 요약 (연구원 단위로 일괄)
                try:
                    summaries = summarize_core_papers(r.id)
                    r_result["summaries"] = len(summaries)
                except Exception:
                    logger.exception("Core summarization failed for researcher %d", r.id)

            except Exception:
                logger.exception("Pipeline failed for researcher %d", r.id)
                r_result["error"] = True

            results["researchers"].append(r_result)

        _complete_batch(log, "completed", results)
        logger.info("weekly_paper_pipeline completed: %d researchers", len(researchers))

    except Exception:
        logger.exception("weekly_paper_pipeline failed")
        _complete_batch(log, "failed", {"error": "see logs"})


def weekly_journal_monitoring():
    """일요일 12:00 — 타겟 저널 신규 논문 모니터링 및 요약 준비."""
    if not has_app_context() and _scheduler_app is not None:
        return _run_in_app_context(weekly_journal_monitoring)

    from app.litreview.journal.journal_monitor import monitor_target_journals
    from app.litreview.recommendation.summarizer import summarize_core_papers

    log = _log_batch("weekly_journal_monitoring", "running")

    try:
        result = monitor_target_journals()

        if result["new_paper_ids"]:
            researchers = Researcher.query.all()
            for r in researchers:
                try:
                    summarize_core_papers(r.id)
                except Exception:
                    logger.exception(
                        "Core summarization failed for researcher %d", r.id
                    )

        _complete_batch(log, "completed", result)
        logger.info("weekly_journal_monitoring completed: %s", result)

    except Exception:
        logger.exception("weekly_journal_monitoring failed")
        _complete_batch(log, "failed", {"error": "see logs"})


EMAIL_EXCLUDED_RESEARCHER_IDS = []


def _email_cycle_due(researcher) -> bool:
    """연구원별 이메일 주기(email_cycle_weeks) 도래 여부.

    주기가 N주면 마지막 발송 후 N주(그레이스 1일)가 지나야 발송.
    발송 이력이 없으면 즉시 발송 대상.
    """
    from app.models import EmailLog

    cycle = researcher.email_cycle_weeks or 1
    if cycle <= 1:
        return True

    last = (
        EmailLog.query.filter_by(researcher_id=researcher.id, status="sent")
        .order_by(EmailLog.sent_at.desc())
        .first()
    )
    if not last or not last.sent_at:
        return True

    # 잡이 매주 월요일 실행되므로 하루 그레이스를 둬서 경계 스킵 방지
    from datetime import timedelta
    return datetime.utcnow() - last.sent_at >= timedelta(weeks=cycle) - timedelta(days=1)


def weekly_email_notification():
    """월요일 10:00 — 전체 연구원에게 미발송 추천 논문 이메일.

    연구원별 email_cycle_weeks 주기를 존중한다 (기본 1 = 매주).
    """
    if not has_app_context() and _scheduler_app is not None:
        return _run_in_app_context(weekly_email_notification)

    from app.litreview.notification.email_sender import send_recommendation_email

    log = _log_batch("weekly_email_notification", "running")
    sent_count = 0
    failed_count = 0
    skipped_cycle = 0

    try:
        researchers = Researcher.query.filter(
            ~Researcher.id.in_(EMAIL_EXCLUDED_RESEARCHER_IDS)
        ).all()

        for r in researchers:
            has_unsent = PaperRecommendation.query.filter_by(
                researcher_id=r.id, is_read=False
            ).first()
            if not has_unsent:
                continue

            if not _email_cycle_due(r):
                skipped_cycle += 1
                continue

            try:
                success = send_recommendation_email(r.id)
                if success:
                    sent_count += 1
                else:
                    failed_count += 1
            except Exception:
                logger.exception("Email failed for researcher %d", r.id)
                failed_count += 1

        _complete_batch(
            log,
            "completed",
            {"sent": sent_count, "failed": failed_count, "skipped_cycle": skipped_cycle},
        )
        logger.info(
            "weekly_email_notification: %d sent, %d failed, %d skipped (cycle)",
            sent_count,
            failed_count,
            skipped_cycle,
        )

    except Exception:
        logger.exception("weekly_email_notification failed")
        _complete_batch(log, "failed", {"error": "see logs"})


def monthly_journal_discovery():
    """매월 1일 03:00 — 저널 발굴 파이프라인.

    이전에는 키워드로 Scopus 를 훑어 후보 저널을 만들고는 **결과를 저장하지
    않고 로그만 남겼다**. 2회 실행에 등록 0건이었다. 이제는 주제별로 관련 논문을
    모아 저널 단위로 점수를 매기고 reco_journal_recommendations 에 저장한다.
    """
    if not has_app_context() and _scheduler_app is not None:
        return _run_in_app_context(monthly_journal_discovery)

    from app.litreview.journal.journal_recommender import run_journal_discovery

    log = _log_batch("monthly_journal_discovery", "running")

    try:
        result = run_journal_discovery()

        # 조용한 실패를 막는다 — 처리된 주제가 없거나 Scopus 가 죽었으면 실패로 기록.
        # 이전 저널 배치가 10주 연속 0건이면서 completed 로 남던 문제의 재발 방지.
        if result.get("aborted") == "scopus_unavailable":
            _complete_batch(log, "failed", result)
            logger.error("monthly_journal_discovery aborted: Scopus unavailable")
            return
        if result["topics_processed"] == 0:
            _complete_batch(log, "failed", result)
            logger.error("monthly_journal_discovery: 처리된 주제 0개")
            return

        _complete_batch(log, "completed", result)
        logger.info(
            "monthly_journal_discovery completed: 주제 %d개, 저널 %d종",
            result["topics_processed"], result["journals_saved"],
        )

    except Exception:
        logger.exception("monthly_journal_discovery failed")
        _complete_batch(log, "failed", {"error": "see logs"})
