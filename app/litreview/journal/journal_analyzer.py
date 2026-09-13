"""useful 피드백 저널 등록.

원래 이 모듈에는 게재/참고문헌/피드백 저널 빈도를 집계해 타겟으로 등록하는
analyze_journals() 와 하위 집계 함수 3개가 있었으나, **호출하는 곳이 하나도
없는 죽은 코드**였고 journal_discovery 와 기능이 중복이었다(같은 일을
published/reference 대 ref_self_published/ref_discovery 라는 다른 source_type
이름으로 수행). 저널 발굴 파이프라인 도입과 함께 제거했다.

남은 것은 사용자가 useful 을 눌렀을 때 그 저널을 타겟에 넣는 경로뿐이다.
useful 은 명시적 사용자 행동이므로 "사용자 승인분"에 해당한다.
"""

import logging

from app import db
from app.models import TargetJournal

logger = logging.getLogger(__name__)


def _ensure_target_journal(
    researcher_id: int, journal_name: str, source_type: str
) -> int:
    """target_journals에 없으면 추가. 추가 시 1 반환, 기존이면 0."""
    existing = TargetJournal.query.filter_by(
        researcher_id=researcher_id,
        journal_name=journal_name,
    ).first()

    if existing:
        return 0

    tj = TargetJournal(
        researcher_id=researcher_id,
        journal_name=journal_name,
        source_type=source_type,
    )
    db.session.add(tj)
    return 1


def add_feedback_journal(researcher_id: int, journal_name: str) -> bool:
    """useful 피드백 시 즉시 저널 추가 (이벤트용)."""
    if not journal_name:
        return False
    added = _ensure_target_journal(researcher_id, journal_name, "feedback")
    if added:
        db.session.commit()
        logger.info("Added feedback journal '%s' for researcher %d", journal_name, researcher_id)
    return bool(added)
