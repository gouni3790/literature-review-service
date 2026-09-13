"""SQLAlchemy 모델 — 주제 기반 통합 구조."""

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
# §3.1 연구원
# ---------------------------------------------------------------------------


class Researcher(db.Model):
    """연구원 프로필 + 추천 설정."""

    __tablename__ = "researchers"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=False, unique=True)
    scopus_id = Column(String(50))
    research_description = Column(Text)  # C유형 기본 설명

    # 추천 설정 (연구원별 튜닝 가능)
    core_percent = Column(Float, default=5)
    related_percent = Column(Float, default=15)
    reference_percent = Column(Float, default=25)
    target_year_range = Column(Integer, default=3)
    max_per_grade = Column(Integer)  # NULL=제한없음, 값 있으면 등급별 상한 (예: 20)
    email_cycle_weeks = Column(Integer, default=1)  # 추천 이메일 주기 (주 단위, 1=매주)

    researcher_type = Column(String(5), default="C")
    password_hash = Column(String(256))
    group = Column(String(64))  # HVAC, UBI, AI agent, IEI

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
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


# ---------------------------------------------------------------------------
# §3.2 기존 논문 (연구자 프로필의 핵심)
# ---------------------------------------------------------------------------


class ReferencePaper(db.Model):
    """연구원의 기존 논문. 임베딩 + 클러스터 소속."""

    __tablename__ = "reference_papers"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    scopus_id = Column(String(50))
    title = Column(Text, nullable=False)
    authors = Column(Text)
    journal = Column(String(500))
    classification = Column(String(20))  # SCIE | KCI | null (수동 등록 시 지정)
    volume = Column(String(50))
    publisher = Column(String(300))
    year = Column(Integer)
    abstract = Column(Text)
    doi = Column(String(200))
    cited_by = Column(Integer, default=0)
    source = Column(String(20), default="scopus")  # scopus | manual
    status = Column(String(20), default="published")  # published | submitted | under_review
    embedding = Column(VectorType())
    cluster_id = Column(Integer)
    is_representative = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    researcher = relationship("Researcher", back_populates="reference_papers")
    keywords = relationship(
        "PaperKeyword", back_populates="paper", cascade="all, delete-orphan"
    )
    references = relationship(
        "PaperReference", back_populates="paper", cascade="all, delete-orphan"
    )


class PaperKeyword(db.Model):
    """저자 키워드."""

    __tablename__ = "paper_keywords"

    id = Column(Integer, primary_key=True)
    paper_id = Column(
        Integer, ForeignKey("reference_papers.id", ondelete="CASCADE"), nullable=False
    )
    keyword = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    paper = relationship("ReferencePaper", back_populates="keywords")


class InterestPaper(db.Model):
    """사용자가 useful 피드백한 논문 (별도 추천 트랙용 시드).

    useful 클릭 시 Scopus default view로 메타데이터 보강 + OpenAI로 임베딩 생성.
    추천 점수 계산은 useful별 1대1 매칭 (트랙 분리).
    """

    __tablename__ = "interest_papers"
    __table_args__ = (
        UniqueConstraint(
            "researcher_id", "scopus_id", name="uq_interest_researcher_scopus"
        ),
    )

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    scopus_id = Column(String(50), index=True)
    title = Column(Text)
    authors = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    doi = Column(String(200))
    abstract = Column(Text)
    keywords = Column(JSON)  # ["Stack effect", "High-rise buildings", ...]
    ref_journals = Column(JSON)  # 보류 (paper_references와 중복)
    embedding = Column(VectorType())
    created_at = Column(DateTime, default=datetime.utcnow)  # = useful 반응 시간

    researcher = relationship("Researcher")


class PaperReference(db.Model):
    """참고문헌 — 공통 참고문헌 매칭 + 저널 역추적용.

    출처는 paper_id (본인 논문) 또는 interest_paper_id (useful 논문) 중 하나만 채워짐.
    """

    __tablename__ = "paper_references"

    id = Column(Integer, primary_key=True)
    paper_id = Column(
        Integer, ForeignKey("reference_papers.id", ondelete="CASCADE"), nullable=True
    )
    interest_paper_id = Column(
        Integer, ForeignKey("interest_papers.id", ondelete="CASCADE"), nullable=True,
        index=True,
    )
    ref_scopus_id = Column(String(50), index=True)  # 공통 ref 매칭의 핵심 키
    ref_title = Column(Text)
    ref_authors = Column(Text)
    ref_year = Column(Integer)
    ref_journal = Column(String(500))
    ref_journal_scopus_id = Column(String(50), index=True)  # 저널 표기 흔들림 방지
    ref_doi = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    paper = relationship("ReferencePaper", back_populates="references")
    interest_paper = relationship("InterestPaper", backref="references")


# ---------------------------------------------------------------------------
# §3.3 클러스터 (10편+ 연구원만)
# ---------------------------------------------------------------------------


class ResearchCluster(db.Model):
    """연구 클러스터. centroid 벡터 포함."""

    __tablename__ = "research_clusters"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    cluster_label = Column(Integer, nullable=False)
    paper_count = Column(Integer)
    centroid = Column(VectorType())
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="clusters")


# ---------------------------------------------------------------------------
# §3.3b 연구 주제 (자동 + 수동 통합)
# ---------------------------------------------------------------------------


class ResearchTopic(db.Model):
    """연구 주제. 파이프라인의 단위. 자동(클러스터) 또는 수동(사용자 정의).

    자동 주제: 클러스터링 결과에서 생성, source_type='auto'
    수동 주제: 사용자 입력 (키워드, DOI, 설명), source_type='manual'
    """

    __tablename__ = "research_topics"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(300), nullable=False)
    source_type = Column(String(20), nullable=False, default="manual")  # 'auto' | 'manual'
    description = Column(Text)  # 주제 설명 (수동만)
    keywords = Column(JSON)  # 키워드 배열 ["kw1", "kw2", ...]
    # 사용자가 키워드를 직접 편집했으면 True → 자동 갱신이 덮어쓰지 않는다.
    # (B유형 자동 주제는 매주 삭제 후 재생성되므로 이 플래그가 없으면 편집분이 유실됨)
    keywords_locked = Column(Boolean, default=False)
    representative_vector = Column(VectorType())  # 주제 대표 벡터 (1536d)
    cluster_label = Column(Integer)  # 클러스터 번호 (자동만)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    researcher = relationship("Researcher", back_populates="research_topics")
    reference_papers = relationship(
        "TopicReferencePaper", back_populates="topic", cascade="all, delete-orphan"
    )
    recommendations = relationship(
        "PaperRecommendation", back_populates="topic", cascade="all, delete-orphan"
    )


class TopicReferencePaper(db.Model):
    """주제별 레퍼런스 논문. 수동 주제의 DOI로 수집되거나, 자동 주제의 대표 논문."""

    __tablename__ = "topic_reference_papers"

    id = Column(Integer, primary_key=True)
    topic_id = Column(
        Integer, ForeignKey("research_topics.id", ondelete="CASCADE"), nullable=False
    )
    scopus_id = Column(String(50))
    title = Column(Text)
    authors = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    doi = Column(String(200))
    abstract = Column(Text)
    ref_journals = Column(JSON)  # 참고문헌 저널 목록
    created_at = Column(DateTime, default=datetime.utcnow)

    topic = relationship("ResearchTopic", back_populates="reference_papers")


# ---------------------------------------------------------------------------
# §3.4 수동 프로필
# ---------------------------------------------------------------------------


class CustomKeyword(db.Model):
    """사용자가 수동 추가한 관심 키워드."""

    __tablename__ = "custom_keywords"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    keyword = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="custom_keywords")


class ResearchNote(db.Model):
    """연구원이 직접 쓴 연구 소개. 관심 주제가 여러 개면 여러 건 작성한다.

    Researcher.research_description(단일 필드)를 대체한다. 이 글들은 core 등급
    논문의 '추천 이유'를 Claude가 쓸 때 연구원 프로필로 전달되며, Scopus 검색이나
    유사도 순위에는 관여하지 않는다 (그건 ResearchTopic이 담당).
    """

    __tablename__ = "research_notes"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    title = Column(String(200))  # 없으면 본문 앞부분을 제목처럼 쓴다
    content = Column(Text, nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="research_notes")


class Author(db.Model):
    """학술 저자 — 이름이 아니라 외부 고유 식별자로 관리한다.

    동명이인이 흔하고(예: 'Sungmin Yoon' OpenAlex 기준 33명) 한 사람도 논문마다
    표기가 달라지므로(Sungmin Yoon / Sung-Min Yoon / S. Yoon / Yoon, S.)
    openalex_author_id를 실질적 식별자로 쓴다. display_name은 표시용일 뿐이다.
    """

    __tablename__ = "authors"

    id = Column(Integer, primary_key=True)  # internal_author_id
    display_name = Column(String(300), nullable=False)
    # 소문자화 + 구두점 제거한 형태. 식별자가 아니라 로컬 검색 보조용.
    normalized_name = Column(String(300), index=True)
    # 외부 식별자 — OpenAlex를 1차 기준으로 삼는다 ("A5045676373")
    openalex_author_id = Column(String(60), unique=True, index=True)
    orcid = Column(String(64), index=True)
    semantic_scholar_author_id = Column(String(64))
    scopus_author_id = Column(String(50))

    affiliation = Column(String(500))  # 현재/주요 소속
    research_topics = Column(JSON)  # ["Building Energy and Comfort Optimization", ...]
    representative_papers = Column(JSON)  # [{title, year, doi, cited_by, openalex_id}]
    name_alternatives = Column(JSON)  # ["S. Yoon", "Yoon, S.", ...] 표기 흔들림 기록
    works_count = Column(Integer)
    cited_by_count = Column(Integer)

    # 신규 논문 확인 시각 — 다음 추적 때 이 시점 이후만 조회한다.
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
    """연구원 ↔ 관심 저자 관계 (user_id | author_id | followed_at)."""

    __tablename__ = "author_follows"
    __table_args__ = (
        UniqueConstraint("researcher_id", "author_id", name="uq_author_follow"),
    )

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    author_id = Column(
        Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False
    )
    followed_at = Column(DateTime, default=datetime.utcnow)
    # 어느 경로로 등록했는지: name_search | paper_detail | orcid
    source = Column(String(20), default="name_search")
    note = Column(Text)

    researcher = relationship("Researcher", back_populates="author_follows")
    author = relationship("Author", back_populates="follows")


class AuthorPaper(db.Model):
    """관심 저자의 논문 — Author ID 기준으로 추적해 발견한 것.

    collected_paper_id가 채워지면 그 논문은 추천 파이프라인(임베딩→유사도→등급)에
    편입된 상태다.
    """

    __tablename__ = "author_papers"
    __table_args__ = (
        UniqueConstraint("author_id", "openalex_work_id", name="uq_author_work"),
    )

    id = Column(Integer, primary_key=True)
    author_id = Column(
        Integer, ForeignKey("authors.id", ondelete="CASCADE"), nullable=False
    )
    openalex_work_id = Column(String(60), index=True)
    doi = Column(String(200), index=True)
    title = Column(Text)
    journal = Column(String(500))
    year = Column(Integer)
    publication_date = Column(String(20))
    abstract = Column(Text)
    cited_by_count = Column(Integer)
    # 추천 파이프라인으로 넘긴 collected_papers.id
    collected_paper_id = Column(Integer)
    first_seen_at = Column(DateTime, default=datetime.utcnow)

    author = relationship("Author", back_populates="papers")


class CustomJournal(db.Model):
    """사용자/관리자가 수동 추가한 관심 저널."""

    __tablename__ = "custom_journals"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    journal_name = Column(String(500), nullable=False)
    scopus_source_id = Column(String(50))
    added_by = Column(String(20), default="user")
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="custom_journals")


# ---------------------------------------------------------------------------
# §3.5 수집 논문 및 추천
# ---------------------------------------------------------------------------


class CollectedPaper(db.Model):
    """Scopus 검색으로 수집된 신규 논문."""

    __tablename__ = "collected_papers"

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

    __tablename__ = "paper_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "paper_id", "researcher_id", "topic_id",
            name="uq_paper_researcher_topic",
        ),
    )

    id = Column(Integer, primary_key=True)
    paper_id = Column(
        Integer, ForeignKey("collected_papers.id", ondelete="CASCADE"), nullable=False
    )
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    topic_id = Column(
        Integer, ForeignKey("research_topics.id", ondelete="SET NULL")
    )

    # 단일 유사도 점수
    similarity_score = Column(Float)  # 주제 대표 벡터와의 코사인 유사도
    percentile_rank = Column(Float)  # 해당 주제 수집 논문 중 백분위 (0~1)

    similarity_details = Column(JSON)  # 상세 정보 (주제명, 대표 벡터 출처 등)

    # 등급
    grade = Column(String(20), nullable=False)
    grade_reason = Column(Text)

    # 에이전트 요약 (core 등급만)
    summary_core_topic = Column(Text)
    summary_purpose = Column(Text)
    summary_method = Column(Text)
    summary_results = Column(Text)
    summary_limitations = Column(Text)
    summary_future = Column(Text)
    recommendation_reason = Column(Text)

    # 사용자 피드백
    is_saved = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)
    user_feedback = Column(String(50))

    created_at = Column(DateTime, default=datetime.utcnow)

    paper = relationship("CollectedPaper", back_populates="recommendations")
    researcher = relationship("Researcher", back_populates="recommendations")
    topic = relationship("ResearchTopic", back_populates="recommendations")


# ---------------------------------------------------------------------------
# §3.6 저널 관리
# ---------------------------------------------------------------------------


class TargetJournal(db.Model):
    """모니터링 대상 저널."""

    __tablename__ = "target_journals"

    id = Column(Integer, primary_key=True)
    journal_name = Column(String(500), nullable=False)
    issn = Column(String(20))
    scopus_source_id = Column(String(50))
    source_type = Column(String(30), nullable=False)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    is_active = Column(Boolean, default=True)
    last_checked = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="target_journals")


# ---------------------------------------------------------------------------
# §3.7 시스템 관리
# ---------------------------------------------------------------------------


class SearchQuery(db.Model):
    """실행된 검색 쿼리 로그."""

    __tablename__ = "search_queries"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    query_string = Column(Text, nullable=False)
    topic_id = Column(Integer)  # 어떤 주제에서 생성된 쿼리인지
    last_executed = Column(DateTime)
    result_count = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    researcher = relationship("Researcher", back_populates="search_queries")


class BatchLog(db.Model):
    """배치 작업 실행 로그."""

    __tablename__ = "batch_logs"

    id = Column(Integer, primary_key=True)
    job_type = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False)
    details = Column(JSON)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


class EmailLog(db.Model):
    """이메일 발송 로그."""

    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True)
    researcher_id = Column(
        Integer, ForeignKey("researchers.id", ondelete="CASCADE"), nullable=False
    )
    subject = Column(String(500))
    paper_count = Column(Integer)
    sent_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default="sent")

    researcher = relationship("Researcher", back_populates="email_logs")
