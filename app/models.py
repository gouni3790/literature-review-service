"""SQLAlchemy 모델 — BIST 홈페이지 DB(bist.db) 기반.

설계 원칙
---------
홈페이지가 소유한 테이블(members, publications, publication_authors, serials,
teams, ...)은 **읽기 전용**으로 다룬다. 컬럼 추가/변경을 하지 않고, 추천에 필요한
값은 전부 reco_ 접두사 테이블에 담아 1:1 또는 FK로 붙인다. 그래서 홈페이지 앱이
같은 DB를 계속 써도 서로 간섭하지 않는다.

기존 추천 코드와의 호환
----------------------
코드 전반이 `Researcher`, `ReferencePaper`, `researcher_id` 같은 이름을 쓰고 있어
클래스명과 속성명은 그대로 두고, 실제 테이블/컬럼만 갈아끼웠다.
  - Column("member_id", ...) 처럼 **속성명은 researcher_id, DB 컬럼은 member_id**
  - Researcher.name 은 members.name_ko/name_en 을 합쳐 만든 읽기 전용 속성
  - 추천 설정(등급 비율·유형·비밀번호)은 reco_member_settings 에 두고 속성으로 중계

이전 스키마는 app/models_litreview_backup.py 에 남겨 두었다.
"""

import json
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from app import db

# ---------------------------------------------------------------------------
# 파일 분리 — 수집물은 reco_papers.db 에 있다
# ---------------------------------------------------------------------------
# bist.db 는 홈페이지와 공유하는 파일이라 작게 유지해야 하는데, 추천이 수집하는
# 논문(초록·전문·1536차원 임베딩)은 건당 약 12KB 라 금세 수십 MB가 된다. 그래서
# 수집물 계열 6개 테이블만 reco_papers.db 로 빼고, 런타임에는 ATTACH 로 같은
# 커넥션에 붙인다 (app/__init__.py 의 _init_attached_papers_db).
#
# 그 결과 이 모듈에서 schema=PAPERS_SCHEMA 가 붙은 모델은 물리적으로 다른 파일에
# 있지만, SQL 조인·서브쿼리는 평소와 똑같이 쓸 수 있다.
#
# 파일 경계를 넘는 FK 는 SQLite 가 지원하지 않으므로 물리 스키마에는 제약이 없다.
# 아래 ForeignKey 선언은 ORM 의 조인 조건 추론용으로만 쓰인다.
PAPERS_SCHEMA = "papers"


class VectorType(TypeDecorator):
    """1536-dim float vector stored as JSON text. SQLite-compatible."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(value if isinstance(value, list) else list(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(value)


# ---------------------------------------------------------------------------
# 홈페이지 소유 테이블 (읽기 전용으로만 다룬다)
# ---------------------------------------------------------------------------


class Team(db.Model):
    """연구실 팀. 홈페이지 소유."""

    __tablename__ = "teams"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    slug = Column(String(160))


class TeamMembership(db.Model):
    """팀 소속. 홈페이지 소유."""

    __tablename__ = "team_memberships"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    role = Column(String(20), nullable=False, default="member")

    team = relationship("Team", lazy="joined")


class Publication(db.Model):
    """논문 원본. 홈페이지 소유 — 절대 수정하지 않는다.

    추천용 임베딩·클러스터는 PublicationMeta(reco_publication_meta)와
    ReferencePaper(reco_member_papers)에 둔다.
    """

    __tablename__ = "publications"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False)
    kind = Column(String(20))
    status = Column(String(20))
    classification = Column(String(10))
    venue = Column(String(300))  # = 저널명
    publisher = Column(String(200))
    volume = Column(String(40))
    issue = Column(String(40))
    pages = Column(String(60))
    year = Column(Integer)
    published_date = Column(String(10))
    doi = Column(String(200))
    url = Column(String(500))
    abstract = Column(Text)
    keywords = Column(Text)  # "A, B, C" 한 줄 — 집계는 PaperKeyword를 쓴다
    source = Column(String(10))
    scopus_id = Column(String(50))
    cited_by = Column(Integer)
    confirmed = Column(Boolean)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    @property
    def journal(self):
        """추천 코드가 journal 이라는 이름으로 읽으므로 맞춰 준다."""
        return self.venue


class PublicationAuthor(db.Model):
    """논문 저자 (구성원 연결 + 외부 저자 이름). 홈페이지 소유."""

    __tablename__ = "publication_authors"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publications.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"))
    raw_name = Column(String(150))
    position = Column(Integer, nullable=False)
    is_first = Column(Boolean, nullable=False)
    is_corresponding = Column(Boolean, nullable=False)

    publication = relationship("Publication", lazy="joined")


# ---------------------------------------------------------------------------
# 연구원 = 홈페이지 members + 추천 설정(reco_member_settings)
# ---------------------------------------------------------------------------


class ResearcherSettings(db.Model):
    """members 에 붙는 추천 설정. members 자체는 건드리지 않기 위해 분리했다."""

    __tablename__ = "reco_member_settings"

    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        primary_key=True,
    )
    researcher_type = Column(String(5), default="C")
    core_percent = Column(Float, default=5)
    related_percent = Column(Float, default=15)
    reference_percent = Column(Float, default=25)
    target_year_range = Column(Integer, default=3)
    max_per_grade = Column(Integer)
    email_cycle_weeks = Column(Integer, default=1)
    password_hash = Column(String(256))
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="_settings")


class Researcher(db.Model):
    """연구원 = 홈페이지 members 행.

    홈페이지가 소유한 테이블이라 이 앱에서 INSERT/DELETE 하지 않는다.
    추천 관련 값은 _settings(reco_member_settings)로 중계되며, 아래 속성들은
    기존 코드가 쓰던 이름(researcher_type, core_percent, name, group ...)을
    그대로 유지하기 위한 브리지다.
    """

    __tablename__ = "members"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)
    name_ko = Column(String(120), nullable=False)
    name_en = Column(String(120))
    role = Column(String(60))
    email = Column(String(200))
    status = Column(String(20))  # current | alumni
    scopus_id = Column(String(30))
    orcid = Column(String(19))
    research_interests_en = Column(Text)
    department_ko = Column(String(200))
    department_en = Column(String(200))
    photo_filename = Column(String(200))
    login_email = Column(String(200))
    is_admin = Column(Boolean, default=False)
    roster_order = Column(Integer, default=999)
    language = Column(String(5), default="ko")
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    # --- 추천 설정 (1:1) ---
    _settings = relationship(
        "ResearcherSettings",
        back_populates="researcher",
        uselist=False,
        lazy="joined",
        cascade="all, delete-orphan",
    )

    # --- 추천 데이터 ---
    reference_papers = relationship(
        "ReferencePaper", back_populates="researcher", cascade="all, delete-orphan"
    )
    clusters = relationship(
        "ResearchCluster", back_populates="researcher", cascade="all, delete-orphan"
    )
    custom_keywords = relationship(
        "CustomKeyword", back_populates="researcher", cascade="all, delete-orphan"
    )
    custom_journals = relationship(
        "CustomJournal", back_populates="researcher", cascade="all, delete-orphan"
    )
    research_topics = relationship(
        "ResearchTopic", back_populates="researcher", cascade="all, delete-orphan"
    )
    recommendations = relationship(
        "PaperRecommendation", back_populates="researcher", cascade="all, delete-orphan"
    )
    target_journals = relationship(
        "TargetJournal", back_populates="researcher", cascade="all, delete-orphan"
    )
    search_queries = relationship(
        "SearchQuery", back_populates="researcher", cascade="all, delete-orphan"
    )
    email_logs = relationship(
        "EmailLog", back_populates="researcher", cascade="all, delete-orphan"
    )
    author_follows = relationship(
        "AuthorFollow", back_populates="researcher", cascade="all, delete-orphan"
    )
    research_notes = relationship(
        "ResearchNote", back_populates="researcher", cascade="all, delete-orphan"
    )

    # --- 표시용 이름 ---
    @property
    def name(self) -> str:
        """기존 코드가 기대하는 '홍길동 (gildong hong)' 형태.

        화면 코드가 '(' 앞부분만 잘라 한글 이름을 쓰는 곳이 많아 형식을 맞춘다.
        """
        if self.name_en and self.name_en.strip():
            return f"{self.name_ko} ({self.name_en.strip()})"
        return self.name_ko or ""

    @property
    def group(self) -> str:
        """소속 팀 이름. 홈페이지 teams/team_memberships에서 읽는다."""
        tm = (
            TeamMembership.query.filter_by(member_id=self.id)
            .join(Team)
            .order_by(Team.sort_order)
            .first()
        )
        return tm.team.name if tm and tm.team else ""

    @property
    def research_description(self) -> str:
        """홈페이지가 관리하는 연구 관심 분야 (읽기 전용).

        사용자가 직접 쓰는 연구 소개는 ResearchNote(reco_notes)에 있고,
        summarizer는 노트를 먼저 보고 없을 때 이 값으로 폴백한다.
        """
        return self.research_interests_en or ""

    def ensure_settings(self) -> "ResearcherSettings":
        """설정 행이 없으면 만든다 (신규 구성원이 추천 대상이 될 때)."""
        if self._settings is None:
            self._settings = ResearcherSettings(researcher_id=self.id)
            db.session.add(self._settings)
        return self._settings

    @property
    def settings(self) -> "ResearcherSettings":
        return self.ensure_settings()

    @property
    def is_reco_active(self) -> bool:
        s = self._settings
        return bool(s.is_active) if s else False


def _settings_bridge(field: str, default=None):
    """reco_member_settings 의 값을 Researcher 속성처럼 쓰게 해 주는 브리지.

    기존 코드가 `researcher.core_percent`, `researcher.researcher_type = "A"` 처럼
    직접 읽고 쓰기 때문에 게터/세터를 모두 제공한다.
    """

    def getter(self):
        s = self._settings
        if s is None:
            return default
        v = getattr(s, field)
        return default if v is None else v

    def setter(self, value):
        s = self.ensure_settings()
        setattr(s, field, value)
        s.updated_at = datetime.utcnow()

    return property(getter, setter)


for _field, _default in (
    ("researcher_type", "C"),
    ("core_percent", 5.0),
    ("related_percent", 15.0),
    ("reference_percent", 25.0),
    ("target_year_range", 3),
    ("max_per_grade", None),
    ("email_cycle_weeks", 1),
    ("password_hash", None),
):
    setattr(Researcher, _field, _settings_bridge(_field, _default))


# ---------------------------------------------------------------------------
# 본인 논문 — publications × publication_authors 파생 (reco_member_papers)
# ---------------------------------------------------------------------------


class PublicationMeta(db.Model):
    """논문 단위 임베딩. 같은 논문을 공저자마다 다시 임베딩하지 않기 위한 캐시."""

    __tablename__ = "reco_publication_meta"

    publication_id = Column(
        Integer, ForeignKey("publications.id", ondelete="CASCADE"), primary_key=True
    )
    embedding = Column(VectorType())
    cluster_id = Column(Integer)  # 사용하지 않음 (클러스터는 연구원별)
    is_representative = Column(Boolean, nullable=False, default=False)
    embedded_at = Column(DateTime)


class ReferencePaper(db.Model):
    """연구원의 본인 논문. publications를 연구원별로 펼친 파생 테이블.

    클러스터 번호와 대표 논문 여부는 연구원마다 다르므로 여기에 둔다.
    서지 정보는 migrations/bist_member_papers.py 가 publications에서 다시 채운다.
    """

    __tablename__ = "reco_member_papers"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "publication_id", name="uq_reco_member_publication"
        ),
    )

    id = Column(Integer, primary_key=True)
    # 속성명은 기존 코드 호환을 위해 researcher_id, 실제 컬럼은 member_id
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    publication_id = Column(
        Integer, ForeignKey("publications.id", ondelete="CASCADE"), nullable=False
    )
    scopus_id = Column(String(50))
    title = Column(Text, nullable=False)
    authors = Column(Text)
    journal = Column(String(500))
    classification = Column(String(20))
    volume = Column(String(50))
    publisher = Column(String(300))
    year = Column(Integer)
    abstract = Column(Text)
    doi = Column(String(200))
    cited_by = Column(Integer, default=0)
    source = Column(String(20), default="bist_homepage")
    status = Column(String(20), default="published")
    is_first = Column(Boolean, default=False)
    is_corresponding = Column(Boolean, default=False)
    author_position = Column(Integer)
    embedding = Column(VectorType())
    cluster_id = Column(Integer)
    is_representative = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="reference_papers")
    publication = relationship("Publication", lazy="joined")

    # 키워드는 논문 단위라 publication_id 로 이어 준다
    keywords = relationship(
        "PaperKeyword",
        primaryjoin="foreign(PaperKeyword.paper_id) == ReferencePaper.publication_id",
        viewonly=True,
    )
    references = relationship(
        "PaperReference",
        primaryjoin="foreign(PaperReference.paper_id) == ReferencePaper.publication_id",
        viewonly=True,
    )


class PaperKeyword(db.Model):
    """논문 키워드. publications.keywords("A, B, C")를 행으로 펼친 것.

    paper_id 는 publications.id 를 가리킨다 (ReferencePaper.id 가 아니다).
    """

    __tablename__ = "reco_publication_keywords"

    id = Column(Integer, primary_key=True)
    paper_id = Column(
        "publication_id", Integer,
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=False,
    )
    keyword = Column(String(300), nullable=False)


# ---------------------------------------------------------------------------
# 관심 논문 (useful 트랙)
# ---------------------------------------------------------------------------


class InterestPaper(db.Model):
    """사용자가 useful 피드백한 논문 (별도 추천 트랙용 시드).

    reco_papers.db 에 있다 (초록·임베딩 보유).
    """

    __tablename__ = "reco_interest_papers"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "scopus_id", name="uq_reco_interest_member_scopus"
        ),
        {"schema": PAPERS_SCHEMA},
    )

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    scopus_id = Column(String(50), index=True)
    title = Column(Text)
    authors = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    doi = Column(String(200))
    abstract = Column(Text)
    keywords = Column(JSON)
    ref_journals = Column(JSON)
    embedding = Column(VectorType())
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher")


class PaperReference(db.Model):
    """참고문헌 — 공통 참고문헌 매칭 + 저널 역추적용.

    reco_papers.db 에 있다 (논문당 수십 건씩 쌓여 행 수가 가장 빨리 는다).
    """

    __tablename__ = "reco_paper_references"
    __table_args__ = {"schema": PAPERS_SCHEMA}

    id = Column(Integer, primary_key=True)
    # 속성명은 paper_id 유지, 실제로는 publications.id 를 가리킨다
    paper_id = Column(
        "publication_id", Integer,
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=True,
    )
    interest_paper_id = Column(
        Integer,
        ForeignKey(f"{PAPERS_SCHEMA}.reco_interest_papers.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    ref_scopus_id = Column(String(50), index=True)
    ref_title = Column(Text)
    ref_authors = Column(Text)
    ref_year = Column(Integer)
    ref_journal = Column(String(500))
    ref_journal_scopus_id = Column(String(50), index=True)
    ref_doi = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    interest_paper = relationship("InterestPaper", backref="references")


# ---------------------------------------------------------------------------
# 클러스터 / 연구 주제
# ---------------------------------------------------------------------------


class ResearchCluster(db.Model):
    """연구 클러스터. centroid 벡터 포함."""

    __tablename__ = "reco_clusters"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    cluster_label = Column(Integer, nullable=False)
    paper_count = Column(Integer)
    centroid = Column(VectorType())
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="clusters")


class ResearchTopic(db.Model):
    """연구 주제. 파이프라인의 단위. 자동(클러스터) 또는 수동(사용자 정의)."""

    __tablename__ = "reco_topics"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(300), nullable=False)
    source_type = Column(String(20), nullable=False, default="manual")
    description = Column(Text)
    keywords = Column(JSON)
    keywords_locked = Column(Boolean, default=False)
    representative_vector = Column(VectorType())
    cluster_label = Column(Integer)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="research_topics")

    # 주제에 딸린 레퍼런스 논문은 주제와 함께 사라지는 게 맞다 (FK 도 CASCADE).
    reference_papers = relationship(
        "TopicReferencePaper", back_populates="topic", cascade="all, delete-orphan"
    )

    # 추천은 주제가 사라져도 **보존한다**. topic_id 만 NULL 이 된다.
    #
    # 원래 여기에 cascade="all, delete-orphan" 이 걸려 있어서, 주제를 지우면
    # 그 주제의 추천 이력 + LLM 요약 + 사용자 피드백(저장·useful)까지 함께
    # 삭제됐다. reco_recommendations.topic_id 의 FK 는 ondelete="SET NULL" 로
    # "추천은 남긴다"고 선언돼 있었는데 ORM cascade 가 그 의도를 덮고 있었다.
    #
    # 특히 B유형 자동 주제는 매주 삭제·재생성되므로 이력이 매주 초기화되고,
    # 같은 논문의 요약 비용을 반복 지불하게 된다.
    #
    # cascade 를 기본값(save-update, merge)으로 두면 부모 삭제 시 SQLAlchemy 가
    # 자식의 FK 를 NULL 로 만든다 — FK 선언과 동작이 일치한다.
    # (SQLite 는 PRAGMA foreign_keys 가 꺼져 있어 DB 레벨 SET NULL 은 동작하지
    #  않으므로, ORM 이 처리해 주는 이 경로가 실질적인 보장이다)
    recommendations = relationship(
        "PaperRecommendation", back_populates="topic"
    )


class TopicReferencePaper(db.Model):
    """주제별 레퍼런스 논문 (수동 주제의 DOI 수집분 또는 자동 주제의 대표 논문)."""

    __tablename__ = "reco_topic_papers"

    id = Column(Integer, primary_key=True)
    topic_id = Column(
        Integer, ForeignKey("reco_topics.id", ondelete="CASCADE"), nullable=False
    )
    scopus_id = Column(String(50))
    title = Column(Text)
    authors = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    doi = Column(String(200))
    abstract = Column(Text)
    ref_journals = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    topic = relationship("ResearchTopic", back_populates="reference_papers")


# ---------------------------------------------------------------------------
# 수동 프로필 (키워드 / 저널 / 연구 소개)
# ---------------------------------------------------------------------------


class CustomKeyword(db.Model):
    """사용자가 수동 추가한 관심 키워드 (월간 저널 탐색에 쓰임)."""

    __tablename__ = "reco_custom_keywords"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    keyword = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="custom_keywords")


class CustomJournal(db.Model):
    """사용자/관리자가 수동 추가한 관심 저널."""

    __tablename__ = "reco_custom_journals"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    journal_name = Column(String(500), nullable=False)
    scopus_source_id = Column(String(50))
    added_by = Column(String(20), default="user")
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="custom_journals")


class ResearchNote(db.Model):
    """연구원이 직접 쓴 연구 소개. 관심 주제가 여러 개면 여러 건 작성한다."""

    __tablename__ = "reco_notes"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(200))
    content = Column(Text, nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="research_notes")


# ---------------------------------------------------------------------------
# 관심 저자 (OpenAlex Author ID 기준)
# ---------------------------------------------------------------------------


class Author(db.Model):
    """학술 저자 — 이름이 아니라 OpenAlex Author ID로 식별한다."""

    __tablename__ = "reco_authors"

    id = Column(Integer, primary_key=True)
    display_name = Column(String(300), nullable=False)
    normalized_name = Column(String(300), index=True)
    openalex_author_id = Column(String(60), unique=True, index=True)
    orcid = Column(String(64), index=True)
    semantic_scholar_author_id = Column(String(64))
    scopus_author_id = Column(String(50))
    affiliation = Column(String(500))
    research_topics = Column(JSON)
    representative_papers = Column(JSON)
    name_alternatives = Column(JSON)
    works_count = Column(Integer)
    cited_by_count = Column(Integer)
    last_checked_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    follows = relationship(
        "AuthorFollow", back_populates="author", cascade="all, delete-orphan"
    )
    papers = relationship(
        "AuthorPaper", back_populates="author", cascade="all, delete-orphan"
    )


class AuthorFollow(db.Model):
    """연구원 ↔ 관심 저자 관계."""

    __tablename__ = "reco_author_follows"
    __table_args__ = (
        UniqueConstraint("member_id", "author_id", name="uq_reco_author_follow"),
    )

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_id = Column(
        Integer, ForeignKey("reco_authors.id", ondelete="CASCADE"), nullable=False
    )
    followed_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String(20), default="name_search")
    note = Column(Text)

    researcher = relationship("Researcher", back_populates="author_follows")
    author = relationship("Author", back_populates="follows")


class AuthorPaper(db.Model):
    """관심 저자의 논문 — Author ID 기준으로 추적해 발견한 것.

    reco_papers.db 에 있다 (저자 1명당 수백 편까지 늘 수 있는 수집물).
    """

    __tablename__ = "reco_author_papers"
    __table_args__ = (
        UniqueConstraint("author_id", "openalex_work_id", name="uq_reco_author_work"),
        {"schema": PAPERS_SCHEMA},
    )

    id = Column(Integer, primary_key=True)
    author_id = Column(
        Integer, ForeignKey("reco_authors.id", ondelete="CASCADE"), nullable=False
    )
    openalex_work_id = Column(String(60), index=True)
    doi = Column(String(200), index=True)
    title = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    publication_date = Column(String(20))
    abstract = Column(Text)
    cited_by_count = Column(Integer)
    collected_paper_id = Column(Integer)
    first_seen_at = Column(DateTime, default=datetime.utcnow)

    author = relationship("Author", back_populates="papers")


# ---------------------------------------------------------------------------
# 수집 논문 및 추천
# ---------------------------------------------------------------------------


class CollectedPaper(db.Model):
    """Scopus/OpenAlex 검색으로 수집된 신규 논문 (추천 후보).

    reco_papers.db 에 있다. 이 테이블이 분리의 주된 이유 — 구 DB 실측으로
    5,274건에 66MB(건당 약 12KB)였고, 초록·전문·임베딩이 전부 여기 들어간다.
    """

    __tablename__ = "reco_collected_papers"
    __table_args__ = {"schema": PAPERS_SCHEMA}

    id = Column(Integer, primary_key=True)
    scopus_id = Column(String(50), unique=True)
    title = Column(Text, nullable=False)
    authors = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    abstract = Column(Text)
    doi = Column(String(200))
    full_text = Column(Text)
    embedding = Column(VectorType())
    source_query = Column(Text)
    collection_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.utcnow)

    recommendations = relationship(
        "PaperRecommendation", back_populates="paper", cascade="all, delete-orphan"
    )


class PaperRecommendation(db.Model):
    """논문 추천 결과 — 주제별 단일 유사도, 등급, 요약, 피드백."""

    __tablename__ = "reco_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "paper_id", "member_id", "topic_id",
            name="uq_reco_paper_member_topic",
        ),
    )

    id = Column(Integer, primary_key=True)
    # 추천 결과는 bist.db 에 남지만 논문 본체는 reco_papers.db 에 있다.
    # 파일 경계라 물리 FK 제약은 없고, 아래 선언은 ORM 조인 조건용이다.
    paper_id = Column(
        Integer,
        ForeignKey(f"{PAPERS_SCHEMA}.reco_collected_papers.id", ondelete="CASCADE"),
        nullable=False,
    )
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    topic_id = Column(Integer, ForeignKey("reco_topics.id", ondelete="SET NULL"))

    similarity_score = Column(Float)
    percentile_rank = Column(Float)
    similarity_details = Column(JSON)

    grade = Column(String(20), nullable=False)
    grade_reason = Column(Text)

    # 요약이 무엇을 읽고 쓰였는지. fulltext | abstract | None(미요약)
    #
    # core 등급이어도 Elsevier 구독 저널이 아니면 전문을 받지 못한다
    # (운영 실측: 요약 대기 213편 중 전문 확보는 13편, 6%). 전문 기반 요약과
    # 초록 기반 요약이 화면·메일에서 구분되지 않으면 연구원이 요약의 신뢰
    # 수준을 판단할 수 없다.
    summary_source = Column(String(20))

    summary_core_topic = Column(Text)
    summary_purpose = Column(Text)
    summary_method = Column(Text)
    summary_results = Column(Text)
    summary_limitations = Column(Text)
    summary_future = Column(Text)
    recommendation_reason = Column(Text)

    is_saved = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)
    user_feedback = Column(String(50))

    created_at = Column(DateTime, default=datetime.utcnow)

    paper = relationship("CollectedPaper", back_populates="recommendations")
    researcher = relationship("Researcher", back_populates="recommendations")
    topic = relationship("ResearchTopic", back_populates="recommendations")


# ---------------------------------------------------------------------------
# 저널 관리 / 시스템
# ---------------------------------------------------------------------------


class TargetJournal(db.Model):
    """모니터링 대상 저널."""

    __tablename__ = "reco_target_journals"

    id = Column(Integer, primary_key=True)
    journal_name = Column(String(500), nullable=False)
    issn = Column(String(20))
    scopus_source_id = Column(String(50))
    source_type = Column(String(30), nullable=False)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active = Column(Boolean, default=True)
    last_checked = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="target_journals")


class SearchQuery(db.Model):
    """실행된 검색 쿼리 로그. reco_papers.db 에 있다 (수집 실행 기록)."""

    __tablename__ = "reco_search_queries"
    __table_args__ = {"schema": PAPERS_SCHEMA}

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    query_string = Column(Text, nullable=False)
    topic_id = Column(Integer)
    last_executed = Column(DateTime)
    result_count = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="search_queries")


class BatchLog(db.Model):
    """배치 작업 실행 로그. reco_papers.db 에 있다 (배치마다 계속 늘어난다)."""

    __tablename__ = "reco_batch_logs"
    __table_args__ = {"schema": PAPERS_SCHEMA}

    id = Column(Integer, primary_key=True)
    job_type = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False)
    details = Column(JSON)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


class EmailLog(db.Model):
    """이메일 발송 로그."""

    __tablename__ = "reco_email_logs"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject = Column(String(500))
    paper_count = Column(Integer)
    sent_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default="sent")

    researcher = relationship("Researcher", back_populates="email_logs")


class JournalRecommendation(db.Model):
    """저널 발굴 결과 — 연구원에게 "이 저널을 보세요"로 제시할 산출물.

    reco_target_journals(수집 타겟)와 역할이 다르다. 이쪽은 파이프라인이 만들어
    보여주는 추천이고, 사용자가 구독(status='subscribed')하면 그때 타겟으로
    편입된다. 저널 발굴과 논문 추천을 잇는 유일한 통로가 이 승인 단계다.

    지표 원값(related_paper_count ~ quality)을 전부 남기는 이유는 가중치를
    바꿨을 때 Scopus 재검색 없이 재계산하기 위함이다. 가중치는 첫 실행 후
    실측으로 조정해야 하므로 이게 중요하다.

    bist.db 에 둔다 — 판단 결과이고 작고 오래 보존해야 한다.
    """

    __tablename__ = "reco_journal_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "topic_id", "journal_name", "run_id",
            name="uq_reco_journalrec",
        ),
    )

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    topic_id = Column(
        Integer, ForeignKey("reco_topics.id", ondelete="CASCADE"), index=True
    )
    run_id = Column(String(40), nullable=False, index=True)

    journal_name = Column(String(500), nullable=False)
    scopus_source_id = Column(String(50))
    issn = Column(String(20))

    # --- 지표 원값 (재계산용) ---
    related_paper_count = Column(Integer)
    avg_similarity = Column(Float)      # 베이지안 축소 적용 후
    growth_rate = Column(Float)         # 최근 2년 / 이전 3년
    familiarity = Column(Float)         # 0~1, 연구실 단위
    novelty = Column(Float)             # 1 - familiarity
    citescore = Column(Float)
    quality = Column(Float)             # QualityGate 값 (미적용 시 1.0)

    score = Column(Float, nullable=False)
    rank = Column(Integer)
    evidence = Column(JSON)             # [{title, doi, scopus_id, year, similarity}]

    # new | seen | subscribed | dismissed
    status = Column(String(20), nullable=False, default="new")
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher")
    topic = relationship("ResearchTopic")


class ApiUsage(db.Model):
    """외부 API 호출 1건의 토큰 사용량과 비용.

    임베딩(OpenAI)과 요약(Anthropic) 호출마다 한 행이 쌓인다.

    원화 기록
    --------
    usd_cost 는 호출 시점 단가표로 계산하고, krw_cost 는 **그 시점의 환율**을
    곱해 함께 저장한다. 환율(usd_krw_rate)도 행에 남기므로 나중에 환율이
    변해도 과거 기록이 소급되지 않는다.

    주의: 여기 환율은 시장 기준환율(ECB)이지 카드사가 실제 청구한 환율이
    아니다. 실제 청구액은 카드사 환율 + 해외결제 수수료만큼 조금 더 높다.

    bist.db 에 둔다 — 작고 오래 보존해야 하는 운영 기록이다.
    """

    __tablename__ = "reco_api_usage"

    id = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    provider = Column(String(20), nullable=False, index=True)   # openai | anthropic
    model = Column(String(80), nullable=False, index=True)
    operation = Column(String(40), nullable=False, index=True)  # embedding | summary

    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)

    usd_cost = Column(Float)            # 단가를 모르면 NULL
    usd_krw_rate = Column(Float)        # 호출 시점 환율
    rate_date = Column(Date)            # 환율 기준일
    rate_source = Column(String(30))    # frankfurter | open.er-api
    krw_cost = Column(Float)

    researcher_id = Column(
        "member_id", Integer, ForeignKey("members.id", ondelete="SET NULL"), index=True
    )
    topic_id = Column(Integer)
    note = Column(String(300))


# ---------------------------------------------------------------------------
# 홈페이지 소유 테이블 쓰기 차단 (마지막 방어선)
# ---------------------------------------------------------------------------
# bist.db 는 연구실 홈페이지와 공유하는 파일이다. 이 앱이 members·publications
# 같은 홈페이지 테이블에 INSERT/DELETE 하면 추천 시스템의 버그가 아니라
# **홈페이지 회원 데이터 사고**가 된다.
#
# 위 모델들의 docstring 이 "INSERT/DELETE 하지 않는다"고 적어 두었지만, 실제로
# 그렇게 하는 코드가 있었다 (delete_researcher, create_researcher, /auth/register).
# 주석은 강제력이 없으므로 ORM 이벤트로 DB 도달 자체를 막는다.
#
# 정당하게 써야 할 때(예: 향후 홈페이지 동기화 기능)는 allow_homepage_write()
# 컨텍스트 안에서 수행한다. 그래야 "왜 여기서 쓰는가"가 코드에 드러난다.
#
# NOTE: 마이그레이션 스크립트는 ORM 이 아니라 raw sqlite3 를 쓰므로 영향받지 않는다.

import threading
from contextlib import contextmanager

from sqlalchemy import event as _sa_event


class HomepageTableWriteError(RuntimeError):
    """홈페이지 소유 테이블에 INSERT/DELETE 를 시도했을 때."""


# 홈페이지가 소유하며 이 앱은 읽기만 하는 모델
HOMEPAGE_OWNED_MODELS = (Team, TeamMembership, Publication, PublicationAuthor, Researcher)

_write_allowed = threading.local()


@contextmanager
def allow_homepage_write(reason: str):
    """홈페이지 테이블 쓰기를 이 블록 안에서만 허용한다.

    Args:
        reason: 왜 필요한지. 로그에 남는다.

    사용 예:
        with allow_homepage_write("홈페이지 회원 동기화"):
            db.session.add(member)
    """
    prev = getattr(_write_allowed, "on", False)
    _write_allowed.on = True
    logger = __import__("logging").getLogger(__name__)
    logger.warning("홈페이지 테이블 쓰기 허용: %s", reason)
    try:
        yield
    finally:
        _write_allowed.on = prev


def _changed_columns(mapper, target) -> list[str]:
    """실제로 값이 바뀐 매핑 컬럼 이름."""
    from sqlalchemy import inspect as _sa_inspect

    state = _sa_inspect(target)
    changed = []
    for attr in mapper.column_attrs:
        try:
            if state.attrs[attr.key].history.has_changes():
                changed.append(attr.key)
        except KeyError:
            continue
    return changed


def _guard(op: str):
    def handler(mapper, _connection, target):
        if getattr(_write_allowed, "on", False):
            return

        detail = ""
        if op == "UPDATE":
            # 관계만 건드려도 부모가 dirty 가 되어 before_update 가 발생한다.
            # 예: Researcher.ensure_settings() 가 _settings 를 할당하면 members
            # 컬럼은 그대로인데 이 이벤트가 뜬다. 실제 컬럼 변경이 없으면 통과.
            changed = _changed_columns(mapper, target)
            if not changed:
                return
            detail = f" (변경된 컬럼: {', '.join(changed)})"

        raise HomepageTableWriteError(
            f"{type(target).__name__}({target.__tablename__}) 에 {op} 를 "
            f"시도했습니다{detail}. "
            f"이 테이블은 연구실 홈페이지가 소유하며 추천 시스템은 읽기만 합니다. "
            f"연구원 추가/삭제는 홈페이지에서 하고, 추천 관련 설정은 "
            f"reco_member_settings(ResearcherSettings)를 쓰세요. "
            f"정말 필요하면 allow_homepage_write() 컨텍스트를 쓰십시오."
        )
    return handler


for _model in HOMEPAGE_OWNED_MODELS:
    _sa_event.listen(_model, "before_insert", _guard("INSERT"))
    _sa_event.listen(_model, "before_delete", _guard("DELETE"))
    # UPDATE 도 막는다. 검토 의견은 INSERT/DELETE 만 지적했지만, 실제로
    # update_researcher() 가 members.email / scopus_id / updated_at 에 쓰고
    # 있었다. 홈페이지 회원 정보를 추천 시스템이 고치면 안 된다.
    # (SQLAlchemy 는 실제로 변경된 객체에만 before_update 를 발생시키므로,
    #  단순 조회나 변경 없는 flush 는 영향을 받지 않는다)
    _sa_event.listen(_model, "before_update", _guard("UPDATE"))
