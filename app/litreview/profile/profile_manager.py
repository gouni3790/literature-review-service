"""수동 프로필 관리 — 관심 키워드/저널 CRUD, 연구 설명, 추천 설정."""

import logging
from datetime import datetime

from app import db
from app.models import CustomJournal, CustomKeyword, Researcher

logger = logging.getLogger(__name__)

ALLOWED_SETTINGS = {
    "core_percent": float,
    "related_percent": float,
    "reference_percent": float,
    "target_year_range": int,
    "email_cycle_weeks": int,  # 추천 이메일 주기 (주 단위)
}


class ProfileManager:
    """연구원 수동 프로필 CRUD."""

    # ------------------------------------------------------------------
    # 관심 키워드
    # ------------------------------------------------------------------

    @staticmethod
    def get_custom_keywords(researcher_id: int) -> list[dict]:
        """관심 키워드 목록."""
        keywords = CustomKeyword.query.filter_by(
            researcher_id=researcher_id
        ).order_by(CustomKeyword.created_at.desc()).all()
        return [
            {"id": k.id, "keyword": k.keyword, "created_at": str(k.created_at)}
            for k in keywords
        ]

    @staticmethod
    def add_custom_keyword(researcher_id: int, keyword: str) -> dict:
        """관심 키워드 추가. 중복 시 기존 반환."""
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("Keyword cannot be empty")

        existing = CustomKeyword.query.filter_by(
            researcher_id=researcher_id, keyword=keyword
        ).first()
        if existing:
            return {"id": existing.id, "keyword": existing.keyword, "duplicate": True}

        kw = CustomKeyword(researcher_id=researcher_id, keyword=keyword)
        db.session.add(kw)
        db.session.commit()
        logger.info("Added keyword '%s' for researcher %d", keyword, researcher_id)
        return {"id": kw.id, "keyword": kw.keyword, "duplicate": False}

    @staticmethod
    def delete_custom_keyword(keyword_id: int) -> bool:
        """관심 키워드 삭제."""
        kw = db.session.get(CustomKeyword, keyword_id)
        if not kw:
            return False
        db.session.delete(kw)
        db.session.commit()
        return True

    # ------------------------------------------------------------------
    # 관심 저널
    # ------------------------------------------------------------------

    @staticmethod
    def get_custom_journals(researcher_id: int) -> list[dict]:
        """관심 저널 목록."""
        journals = CustomJournal.query.filter_by(
            researcher_id=researcher_id
        ).order_by(CustomJournal.created_at.desc()).all()
        return [
            {
                "id": j.id,
                "journal_name": j.journal_name,
                "scopus_source_id": j.scopus_source_id,
                "added_by": j.added_by,
                "created_at": str(j.created_at),
            }
            for j in journals
        ]

    @staticmethod
    def add_custom_journal(
        researcher_id: int,
        journal_name: str,
        scopus_source_id: str | None = None,
        added_by: str = "user",
    ) -> dict:
        """관심 저널 추가. 중복 시 기존 반환."""
        journal_name = journal_name.strip()
        if not journal_name:
            raise ValueError("Journal name cannot be empty")
        if added_by not in ("user", "admin"):
            raise ValueError("added_by must be 'user' or 'admin'")

        existing = CustomJournal.query.filter_by(
            researcher_id=researcher_id, journal_name=journal_name
        ).first()
        if existing:
            return {
                "id": existing.id,
                "journal_name": existing.journal_name,
                "duplicate": True,
            }

        journal = CustomJournal(
            researcher_id=researcher_id,
            journal_name=journal_name,
            scopus_source_id=scopus_source_id,
            added_by=added_by,
        )
        db.session.add(journal)
        db.session.commit()
        logger.info(
            "Added journal '%s' for researcher %d (by %s)",
            journal_name,
            researcher_id,
            added_by,
        )
        return {
            "id": journal.id,
            "journal_name": journal.journal_name,
            "duplicate": False,
        }

    @staticmethod
    def delete_custom_journal(journal_id: int) -> bool:
        """관심 저널 삭제."""
        journal = db.session.get(CustomJournal, journal_id)
        if not journal:
            return False
        db.session.delete(journal)
        db.session.commit()
        return True

    # ------------------------------------------------------------------
    # 연구 설명
    # ------------------------------------------------------------------

    @staticmethod
    def update_research_description(
        researcher_id: int, description: str
    ) -> dict:
        """연구 관심 분야 자유 서술 수정."""
        researcher = db.session.get(Researcher, researcher_id)
        if not researcher:
            raise ValueError(f"Researcher {researcher_id} not found")

        researcher.research_description = description.strip()
        researcher.updated_at = datetime.utcnow()
        db.session.commit()
        return {
            "id": researcher.id,
            "research_description": researcher.research_description,
        }

    # ------------------------------------------------------------------
    # 추천 설정
    # ------------------------------------------------------------------

    @staticmethod
    def update_recommendation_settings(
        researcher_id: int, settings: dict
    ) -> dict:
        """추천 설정 변경. 허용된 필드만 업데이트."""
        researcher = db.session.get(Researcher, researcher_id)
        if not researcher:
            raise ValueError(f"Researcher {researcher_id} not found")

        updated = {}
        for key, cast in ALLOWED_SETTINGS.items():
            if key in settings:
                value = cast(settings[key])
                setattr(researcher, key, value)
                updated[key] = value

        if updated:
            researcher.updated_at = datetime.utcnow()
            db.session.commit()
            logger.info(
                "Updated settings for researcher %d: %s",
                researcher_id,
                updated,
            )

        return {
            "id": researcher.id,
            "updated_fields": updated,
            "current_settings": {
                k: getattr(researcher, k) for k in ALLOWED_SETTINGS
            },
        }
