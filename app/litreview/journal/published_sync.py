"""본인 게재지 → 수집 타겟(reco_target_journals) 동기화.

배경
----
참고문헌 역추적(discover_journals_from_references)을 폐지하면서, 그 함수가
하던 두 가지 중 "본인이 게재한 저널을 타겟으로 넣는" 부분만 남긴 것이다.
인용 저널을 자동 등록하던 부분은 의도적으로 버렸다 — 인용한 저널은 이미
아는 저널이라 발굴 가치가 없고, 자동 등록이 타겟 목록을 오염시켰다.

결정 사항: 수집 타겟은 **본인 게재지 + 사용자 승인분**으로만 채운다.
사용자 승인분은 journal_recommender.subscribe_journal() 이 담당한다.
"""

import logging

from app import db
from app.models import ReferencePaper, Researcher, TargetJournal

logger = logging.getLogger(__name__)

SOURCE_TYPE = "self_published"


def sync_published_journals(researcher_id: int) -> dict:
    """연구원이 게재한 저널을 타겟으로 등록한다.

    이미 같은 (researcher_id, journal_name) 행이 있으면 건드리지 않는다.
    사용자가 비활성화해 둔 저널을 되살리지 않기 위함이다.

    Returns:
        {researcher_id, published, added, existing}
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        raise ValueError(f"Researcher {researcher_id} not found")

    published = {
        j for (j,) in db.session.query(ReferencePaper.journal)
        .filter(
            ReferencePaper.researcher_id == researcher_id,
            ReferencePaper.journal.isnot(None),
            ReferencePaper.journal != "",
        )
        .distinct()
        .all()
    }

    existing = {
        j for (j,) in db.session.query(TargetJournal.journal_name)
        .filter(TargetJournal.researcher_id == researcher_id)
        .all()
    }

    added = 0
    for name in sorted(published - existing):
        db.session.add(
            TargetJournal(
                researcher_id=researcher_id,
                journal_name=name,
                source_type=SOURCE_TYPE,
                is_active=True,
            )
        )
        added += 1

    db.session.commit()
    logger.info(
        "게재지 동기화 r%d: 게재 %d종, 신규 등록 %d종 (기존 %d종)",
        researcher_id, len(published), added, len(existing),
    )
    return {
        "researcher_id": researcher_id,
        "published": len(published),
        "added": added,
        "existing": len(existing),
    }


def sync_all(researcher_ids: list[int] | None = None) -> dict:
    """전체(또는 지정) 연구원의 게재지를 동기화한다."""
    if researcher_ids:
        targets = researcher_ids
    else:
        targets = [
            r for (r,) in db.session.query(ReferencePaper.researcher_id)
            .distinct()
            .all()
        ]

    out = {"researchers": 0, "added": 0, "results": []}
    for rid in sorted(targets):
        try:
            res = sync_published_journals(rid)
            out["researchers"] += 1
            out["added"] += res["added"]
            out["results"].append(res)
        except Exception:
            db.session.rollback()
            logger.exception("게재지 동기화 실패 r%d", rid)
    return out
