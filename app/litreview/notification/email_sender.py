"""Flask-Mail HTML 이메일 발송. 등급별 내용 차등 구성."""

from html import escape
import logging
from datetime import datetime

from flask import current_app
from flask_mail import Message

from app import db, mail
from app.models import CollectedPaper, EmailLog, PaperRecommendation, Researcher

logger = logging.getLogger(__name__)


def send_admin_alert(subject: str, body: str) -> bool:
    """관리자에게 배치 실패 등 시스템 알림 발송.

    수신자: ADMIN_EMAIL (미설정 시 MAIL_USERNAME).

    Returns:
        발송 성공 여부.
    """
    recipient = (
        current_app.config.get("ADMIN_EMAIL")
        or current_app.config.get("MAIL_USERNAME")
    )
    if not recipient:
        logger.warning("No ADMIN_EMAIL/MAIL_USERNAME configured; alert skipped")
        return False

    try:
        msg = Message(
            subject=f"[Literature Review] {subject}",
            sender=current_app.config.get("MAIL_USERNAME"),
            recipients=[recipient],
            html=(
                "<div style='font-family: Arial, sans-serif;'>"
                f"<h3>{escape(subject)}</h3>"
                f"<pre style='background:#f7fafc;padding:12px;border-radius:6px;'>"
                f"{escape(body)}</pre>"
                f"<p style='color:#718096;font-size:12px;'>"
                f"발송 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
                "</div>"
            ),
        )
        mail.send(msg)
        logger.info("Admin alert sent to %s: %s", recipient, subject)
        return True
    except Exception:
        logger.exception("Admin alert send failed: %s", subject)
        return False


def _build_html_email(
    researcher: Researcher,
    core_recs: list[PaperRecommendation],
    related_recs: list[PaperRecommendation],
    reference_recs: list[PaperRecommendation],
) -> str:
    """등급별 HTML 이메일 본문 구성."""
    sections = []

    sections.append(f"""
    <div style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
    <h2 style="color: #1a365d;">📚 Literature Review — 신규 추천 논문</h2>
    <p>안녕하세요, {researcher.name}님. 새로운 추천 논문이 있습니다.</p>
    <hr style="border: 1px solid #e2e8f0;">
    """)

    # Core
    if core_recs:
        sections.append('<h3 style="color: #c53030;">🔴 핵심 추천 (Core)</h3>')
        for rec in core_recs:
            paper = rec.paper
            doi_link = f"https://doi.org/{paper.doi}" if paper.doi else "#"
            sections.append(f"""
            <div style="background: #fff5f5; border-left: 4px solid #c53030; padding: 12px; margin: 8px 0;">
                <strong><a href="{doi_link}" style="color: #1a365d;">{paper.title}</a></strong>
                <br><small>{paper.journal} ({paper.year}) — similarity: {rec.similarity_score:.3f}</small>
            """)
            if rec.recommendation_reason:
                sections.append(f'<p style="color: #4a5568;"><em>추천 이유: {rec.recommendation_reason}</em></p>')
            if rec.summary_core_topic:
                sections.append(f"""
                <details><summary style="cursor:pointer; color:#2b6cb0;">요약 보기</summary>
                <ul style="color: #4a5568;">
                    <li><strong>핵심 주제:</strong> {rec.summary_core_topic}</li>
                    <li><strong>목적:</strong> {rec.summary_purpose or '-'}</li>
                    <li><strong>방법:</strong> {rec.summary_method or '-'}</li>
                    <li><strong>결과:</strong> {rec.summary_results or '-'}</li>
                </ul>
                </details>""")
            sections.append("</div>")

    # Related
    if related_recs:
        sections.append('<h3 style="color: #d69e2e;">🟡 관련 논문 (Related)</h3>')
        sections.append('<ul style="line-height: 1.6;">')
        for rec in related_recs:
            paper = rec.paper
            doi_link = f"https://doi.org/{paper.doi}" if paper.doi else "#"
            sim = f"{rec.similarity_score:.3f}" if rec.similarity_score is not None else "-"
            sections.append(
                f'<li><a href="{doi_link}" style="color:#1a365d;">{paper.title}</a><br>'
                f'<small style="color:#4a5568;">{paper.journal} ({paper.year}) — '
                f'similarity: {sim}</small></li>'
            )
        sections.append('</ul>')

    # Reference
    if reference_recs:
        sections.append('<h3 style="color: #718096;">⚪ 참고 논문 (Reference)</h3>')
        sections.append('<ul style="color: #4a5568; line-height: 1.6;">')
        for rec in reference_recs:
            paper = rec.paper
            doi_link = f"https://doi.org/{paper.doi}" if paper.doi else "#"
            sim = f"{rec.similarity_score:.3f}" if rec.similarity_score is not None else "-"
            sections.append(
                f'<li><a href="{doi_link}" style="color:#4a5568;">{paper.title}</a><br>'
                f'<small style="color:#718096;">{paper.journal} ({paper.year}) — '
                f'similarity: {sim}</small></li>'
            )
        sections.append('</ul>')

    sections.append("""
    <hr style="border: 1px solid #e2e8f0;">
    <p style="color: #a0aec0; font-size: 12px;">
        Literature Review Assistant — 자동 발송 이메일입니다.
    </p>
    </div>""")

    return "\n".join(sections)


def send_recommendation_email(
    researcher_id: int,
    recommendation_ids: list[int] | None = None,
) -> bool:
    """추천 논문 이메일 발송.

    Args:
        researcher_id: researchers.id
        recommendation_ids: 특정 추천만. None이면 미발송(is_read=False) 전체.

    Returns:
        발송 성공 여부.
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher or not researcher.email:
        logger.warning("Researcher %d not found or no email", researcher_id)
        return False

    query = PaperRecommendation.query.filter_by(researcher_id=researcher_id)
    if recommendation_ids:
        query = query.filter(PaperRecommendation.id.in_(recommendation_ids))
    else:
        query = query.filter_by(is_read=False)

    recs = query.order_by(PaperRecommendation.similarity_score.desc()).all()
    if not recs:
        logger.info("No recommendations to send for researcher %d", researcher_id)
        return False
    sent_recs = list(recs)

    # paper_id 단위 중복 제거: 같은 논문이 여러 주제에 추천되면
    # 가장 높은 등급(core > related > reference) + 최고 유사도 1건만 사용
    grade_priority = {"core": 0, "related": 1, "reference": 2}
    deduped: dict[int, PaperRecommendation] = {}
    for r in recs:
        existing = deduped.get(r.paper_id)
        if existing is None:
            deduped[r.paper_id] = r
            continue
        # 더 높은 등급이면 교체
        if grade_priority[r.grade] < grade_priority[existing.grade]:
            deduped[r.paper_id] = r
        # 같은 등급이면 유사도 높은 것
        elif r.grade == existing.grade and (r.similarity_score or 0) > (existing.similarity_score or 0):
            deduped[r.paper_id] = r

    recs = sorted(deduped.values(), key=lambda x: -(x.similarity_score or 0))

    # 등급별 상위 N편으로 제한 (메일 크기 통제)
    MAX_PER_GRADE = 10
    core_recs = [r for r in recs if r.grade == "core"][:MAX_PER_GRADE]
    related_recs = [r for r in recs if r.grade == "related"][:MAX_PER_GRADE]
    reference_recs = [r for r in recs if r.grade == "reference"][:MAX_PER_GRADE]

    subject = (
        f"[Literature Review] 신규 추천 논문 {len(recs)}편 "
        f"(Core {len(core_recs)}, Related {len(related_recs)}, "
        f"Reference {len(reference_recs)})"
    )

    html = _build_html_email(researcher, core_recs, related_recs, reference_recs)

    try:
        msg = Message(
            subject=subject,
            recipients=[researcher.email],
            html=html,
            sender=("BIST Literature Review", current_app.config.get("MAIL_USERNAME")),
        )
        mail.send(msg)
    except Exception:
        logger.exception("Email send failed for researcher %d", researcher_id)

        log = EmailLog(
            researcher_id=researcher_id,
            subject=subject,
            paper_count=len(recs),
            status="failed",
        )
        db.session.add(log)
        db.session.commit()
        return False

    # 발송 성공 로그
    log = EmailLog(
        researcher_id=researcher_id,
        subject=subject,
        paper_count=len(recs),
        status="sent",
    )
    db.session.add(log)
    for rec in sent_recs:
        rec.is_read = True
    db.session.commit()

    logger.info(
        "Email sent to %s: %d papers (%d core, %d related, %d reference)",
        researcher.email,
        len(recs),
        len(core_recs),
        len(related_recs),
        len(reference_recs),
    )
    return True


def send_shared_recommendation_email(
    recommendation_id: int,
    target_researcher_id: int,
    shared_by_researcher_id: int | None = None,
) -> bool:
    """추천 논문 1건을 다른 연구원에게 공유 메일로 발송."""
    rec = db.session.get(PaperRecommendation, recommendation_id)
    target = db.session.get(Researcher, target_researcher_id)
    shared_by = (
        db.session.get(Researcher, shared_by_researcher_id)
        if shared_by_researcher_id
        else None
    )

    if not rec or not rec.paper:
        logger.warning("Recommendation %s not found for sharing", recommendation_id)
        return False
    if not target or not target.email:
        logger.warning("Target researcher %s not found or no email", target_researcher_id)
        return False

    paper = rec.paper
    sharer_name = shared_by.name if shared_by else (rec.researcher.name if rec.researcher else "BIST Lab")
    doi_link = f"https://doi.org/{paper.doi}" if paper.doi else ""
    score = f"{(rec.similarity_score or 0) * 100:.1f}%"
    topic_name = rec.topic.name if rec.topic else ""

    summary_rows = [
        ("핵심 주제", rec.summary_core_topic),
        ("연구 목적", rec.summary_purpose),
        ("방법론", rec.summary_method),
        ("결과", rec.summary_results),
        ("한계", rec.summary_limitations),
        ("향후 연구", rec.summary_future),
    ]
    summary_html = "\n".join(
        f"<tr><th style='text-align:left;color:#718096;padding:6px 10px;width:100px;'>{escape(label)}</th>"
        f"<td style='padding:6px 10px;color:#2d3748;'>{escape(value)}</td></tr>"
        for label, value in summary_rows
        if value
    )

    reason_html = (
        f"<div style='margin-top:14px;padding:12px 14px;background:#f0fff4;border-left:4px solid #38a169;'>"
        f"<strong>추천 이유</strong><br>{escape(rec.recommendation_reason or rec.grade_reason or '')}</div>"
        if (rec.recommendation_reason or rec.grade_reason)
        else ""
    )

    abstract_html = (
        f"<div style='margin-top:14px;color:#4a5568;line-height:1.6;'><strong>초록 발췌</strong><br>{escape(paper.abstract)}</div>"
        if paper.abstract
        else ""
    )

    title_html = (
        f"<a href='{escape(doi_link)}' style='color:#1a365d;text-decoration:none;'>{escape(paper.title)}</a>"
        if doi_link
        else escape(paper.title)
    )
    subject = f"[BIST Literature Review] {sharer_name}님이 추천 논문을 공유했습니다"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto;color:#1a202c;">
      <h2 style="color:#1a365d;">추천 논문 공유</h2>
      <p>{escape(sharer_name)}님이 {escape(target.name)}님께 추천 논문을 공유했습니다.</p>
      <div style="border:1px solid #e2e8f0;border-radius:10px;padding:18px;margin-top:16px;">
        <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#718096;">
          {escape((rec.grade or 'reference').upper())} · 유사도 {escape(score)}
        </div>
        <h3 style="margin:8px 0 6px;line-height:1.35;">{title_html}</h3>
        <div style="color:#4a5568;font-size:13px;">
          {escape(paper.authors or '')}<br>
          <em>{escape(paper.journal or '')}</em> {escape(str(paper.year or ''))}
          {f" · 주제: {escape(topic_name)}" if topic_name else ""}
        </div>
        {f"<table style='width:100%;border-collapse:collapse;margin-top:14px;background:#f7fafc;border-radius:8px;overflow:hidden;'>{summary_html}</table>" if summary_html else ""}
        {reason_html}
        {abstract_html}
      </div>
      <p style="margin-top:18px;color:#a0aec0;font-size:12px;">BIST Literature Review Agent</p>
    </div>
    """

    try:
        msg = Message(
            subject=subject,
            recipients=[target.email],
            html=html,
            sender=("BIST Literature Review", current_app.config.get("MAIL_USERNAME")),
        )
        mail.send(msg)
    except Exception:
        logger.exception(
            "Shared recommendation email failed: rec=%d target=%d",
            recommendation_id,
            target_researcher_id,
        )
        return False

    log = EmailLog(
        researcher_id=target_researcher_id,
        subject=subject,
        paper_count=1,
        status="shared",
    )
    db.session.add(log)
    db.session.commit()
    logger.info(
        "Shared recommendation %d sent to researcher %d",
        recommendation_id,
        target_researcher_id,
    )
    return True
