"""홈페이지 DB(bist.db)에 논문 추천용 테이블을 추가한다.

원칙: 홈페이지가 소유한 기존 테이블(members, publications, publication_authors,
serials, teams, ...)은 절대 변경하지 않는다. ALTER도 하지 않는다.
추천에 필요한 것은 전부 reco_ 접두사를 붙인 새 테이블로 만들고,
members/publications와는 1:1 또는 FK로 연결한다.

이렇게 하면 홈페이지 앱이 같은 DB를 계속 써도 영향이 없고,
나중에 추천 기능만 걷어낼 때도 reco_ 테이블만 지우면 된다.

사용:
    python migrations/bist_reco_schema.py [db_path]
    (기본: instance/bist.db)
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 홈페이지 소유 — 이 스크립트가 절대 손대면 안 되는 테이블
PROTECTED = {
    "members", "publications", "publication_authors", "serials",
    "teams", "team_memberships", "member_history", "member_links",
    "news_posts", "news_photos", "newsletters", "projects",
    "project_members", "allowed_emails", "app_state", "audit_entries",
}

DDL = {
    # --- 연구원: members에 붙는 추천 설정 (members는 건드리지 않는다) ---
    "reco_member_settings": """
        CREATE TABLE IF NOT EXISTS reco_member_settings (
            member_id INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
            researcher_type VARCHAR(5) DEFAULT 'C',
            core_percent FLOAT DEFAULT 5,
            related_percent FLOAT DEFAULT 15,
            reference_percent FLOAT DEFAULT 25,
            target_year_range INTEGER DEFAULT 3,
            max_per_grade INTEGER,
            email_cycle_weeks INTEGER DEFAULT 1,
            password_hash VARCHAR(256),
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME
        )""",

    # --- 논문: publications에 붙는 임베딩/클러스터 (publications는 건드리지 않는다) ---
    "reco_publication_meta": """
        CREATE TABLE IF NOT EXISTS reco_publication_meta (
            publication_id INTEGER PRIMARY KEY
                REFERENCES publications(id) ON DELETE CASCADE,
            embedding TEXT,
            cluster_id INTEGER,
            is_representative BOOLEAN NOT NULL DEFAULT 0,
            embedded_at DATETIME
        )""",

    # publications.keywords가 "A, B, C" 한 줄이라 집계가 어려워 행으로 펼친다
    "reco_publication_keywords": """
        CREATE TABLE IF NOT EXISTS reco_publication_keywords (
            id INTEGER PRIMARY KEY,
            publication_id INTEGER NOT NULL
                REFERENCES publications(id) ON DELETE CASCADE,
            keyword VARCHAR(300) NOT NULL
        )""",

    "reco_clusters": """
        CREATE TABLE IF NOT EXISTS reco_clusters (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            cluster_label INTEGER NOT NULL,
            paper_count INTEGER,
            centroid TEXT,
            created_at DATETIME,
            updated_at DATETIME
        )""",

    "reco_topics": """
        CREATE TABLE IF NOT EXISTS reco_topics (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            name VARCHAR(300) NOT NULL,
            source_type VARCHAR(20) NOT NULL DEFAULT 'manual',
            description TEXT,
            keywords TEXT,
            keywords_locked BOOLEAN DEFAULT 0,
            representative_vector TEXT,
            cluster_label INTEGER,
            sort_order INTEGER DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
        )""",

    "reco_topic_papers": """
        CREATE TABLE IF NOT EXISTS reco_topic_papers (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL REFERENCES reco_topics(id) ON DELETE CASCADE,
            scopus_id VARCHAR(50),
            title TEXT,
            authors TEXT,
            journal VARCHAR(500),
            year INTEGER,
            doi VARCHAR(200),
            abstract TEXT,
            ref_journals TEXT,
            created_at DATETIME
        )""",

    "reco_collected_papers": """
        CREATE TABLE IF NOT EXISTS reco_collected_papers (
            id INTEGER PRIMARY KEY,
            scopus_id VARCHAR(50) UNIQUE,
            title TEXT NOT NULL,
            authors TEXT,
            journal VARCHAR(500),
            year INTEGER,
            abstract TEXT,
            doi VARCHAR(200),
            full_text TEXT,
            embedding TEXT,
            source_query TEXT,
            collection_date DATE,
            created_at DATETIME
        )""",

    "reco_recommendations": """
        CREATE TABLE IF NOT EXISTS reco_recommendations (
            id INTEGER PRIMARY KEY,
            paper_id INTEGER NOT NULL
                REFERENCES reco_collected_papers(id) ON DELETE CASCADE,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            topic_id INTEGER REFERENCES reco_topics(id) ON DELETE SET NULL,
            similarity_score FLOAT,
            percentile_rank FLOAT,
            similarity_details TEXT,
            grade VARCHAR(20) NOT NULL,
            grade_reason TEXT,
            summary_core_topic TEXT,
            summary_purpose TEXT,
            summary_method TEXT,
            summary_results TEXT,
            summary_limitations TEXT,
            summary_future TEXT,
            recommendation_reason TEXT,
            is_saved BOOLEAN DEFAULT 0,
            is_read BOOLEAN DEFAULT 0,
            user_feedback VARCHAR(50),
            created_at DATETIME,
            CONSTRAINT uq_reco_paper_member_topic
                UNIQUE (paper_id, member_id, topic_id)
        )""",

    "reco_custom_keywords": """
        CREATE TABLE IF NOT EXISTS reco_custom_keywords (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            keyword VARCHAR(300) NOT NULL,
            created_at DATETIME
        )""",

    "reco_custom_journals": """
        CREATE TABLE IF NOT EXISTS reco_custom_journals (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            journal_name VARCHAR(500) NOT NULL,
            scopus_source_id VARCHAR(50),
            added_by VARCHAR(20) DEFAULT 'user',
            created_at DATETIME
        )""",

    "reco_target_journals": """
        CREATE TABLE IF NOT EXISTS reco_target_journals (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            journal_name VARCHAR(500) NOT NULL,
            issn VARCHAR(20),
            scopus_source_id VARCHAR(50),
            source_type VARCHAR(30) NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            last_checked DATETIME,
            created_at DATETIME
        )""",

    "reco_search_queries": """
        CREATE TABLE IF NOT EXISTS reco_search_queries (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            query_string TEXT NOT NULL,
            topic_id INTEGER,
            last_executed DATETIME,
            result_count INTEGER,
            created_at DATETIME
        )""",

    "reco_batch_logs": """
        CREATE TABLE IF NOT EXISTS reco_batch_logs (
            id INTEGER PRIMARY KEY,
            job_type VARCHAR(100) NOT NULL,
            status VARCHAR(50) NOT NULL,
            details TEXT,
            started_at DATETIME,
            completed_at DATETIME
        )""",

    "reco_email_logs": """
        CREATE TABLE IF NOT EXISTS reco_email_logs (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            subject VARCHAR(500),
            paper_count INTEGER,
            sent_at DATETIME,
            status VARCHAR(50) DEFAULT 'sent'
        )""",

    "reco_interest_papers": """
        CREATE TABLE IF NOT EXISTS reco_interest_papers (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            scopus_id VARCHAR(50),
            title TEXT,
            authors TEXT,
            journal VARCHAR(500),
            year INTEGER,
            doi VARCHAR(200),
            abstract TEXT,
            keywords TEXT,
            ref_journals TEXT,
            embedding TEXT,
            created_at DATETIME,
            CONSTRAINT uq_reco_interest_member_scopus UNIQUE (member_id, scopus_id)
        )""",

    "reco_paper_references": """
        CREATE TABLE IF NOT EXISTS reco_paper_references (
            id INTEGER PRIMARY KEY,
            publication_id INTEGER REFERENCES publications(id) ON DELETE CASCADE,
            interest_paper_id INTEGER
                REFERENCES reco_interest_papers(id) ON DELETE CASCADE,
            ref_scopus_id VARCHAR(50),
            ref_title TEXT,
            ref_authors TEXT,
            ref_year INTEGER,
            ref_journal VARCHAR(500),
            ref_journal_scopus_id VARCHAR(50),
            ref_doi VARCHAR(200),
            created_at DATETIME
        )""",

    # --- 관심 저자 (OpenAlex ID 기준) ---
    "reco_authors": """
        CREATE TABLE IF NOT EXISTS reco_authors (
            id INTEGER PRIMARY KEY,
            display_name VARCHAR(300) NOT NULL,
            normalized_name VARCHAR(300),
            openalex_author_id VARCHAR(60) UNIQUE,
            orcid VARCHAR(64),
            semantic_scholar_author_id VARCHAR(64),
            scopus_author_id VARCHAR(50),
            affiliation VARCHAR(500),
            research_topics TEXT,
            representative_papers TEXT,
            name_alternatives TEXT,
            works_count INTEGER,
            cited_by_count INTEGER,
            last_checked_at DATETIME,
            created_at DATETIME,
            updated_at DATETIME
        )""",

    "reco_author_follows": """
        CREATE TABLE IF NOT EXISTS reco_author_follows (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            author_id INTEGER NOT NULL REFERENCES reco_authors(id) ON DELETE CASCADE,
            followed_at DATETIME,
            source VARCHAR(20) DEFAULT 'name_search',
            note TEXT,
            CONSTRAINT uq_reco_author_follow UNIQUE (member_id, author_id)
        )""",

    "reco_author_papers": """
        CREATE TABLE IF NOT EXISTS reco_author_papers (
            id INTEGER PRIMARY KEY,
            author_id INTEGER NOT NULL REFERENCES reco_authors(id) ON DELETE CASCADE,
            openalex_work_id VARCHAR(60),
            doi VARCHAR(200),
            title TEXT,
            journal VARCHAR(500),
            year INTEGER,
            publication_date VARCHAR(20),
            abstract TEXT,
            cited_by_count INTEGER,
            collected_paper_id INTEGER,
            first_seen_at DATETIME,
            CONSTRAINT uq_reco_author_work UNIQUE (author_id, openalex_work_id)
        )""",

    # --- 연구 소개 (여러 건) ---
    "reco_notes": """
        CREATE TABLE IF NOT EXISTS reco_notes (
            id INTEGER PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            title VARCHAR(200),
            content TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
        )""",
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_reco_pubkw_pub ON reco_publication_keywords(publication_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_pubkw_kw ON reco_publication_keywords(keyword)",
    "CREATE INDEX IF NOT EXISTS ix_reco_topics_member ON reco_topics(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_reco_member ON reco_recommendations(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_reco_paper ON reco_recommendations(paper_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_notes_member ON reco_notes(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_authors_orcid ON reco_authors(orcid)",
    "CREATE INDEX IF NOT EXISTS ix_reco_authorpapers_doi ON reco_author_papers(doi)",
]


def migrate(db_path: str, include_alumni: bool = False) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # --- 안전 확인: 정말 홈페이지 DB인가 ---
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing = {"members", "publications", "publication_authors"} - tables
    if missing:
        print(f"중단: 홈페이지 DB가 아닌 것 같습니다. 없는 테이블: {sorted(missing)}")
        sys.exit(1)

    before = {t for t in tables if t in PROTECTED}
    print(f"대상 DB: {db_path}")
    print(f"보호 대상(변경 안 함) 테이블 {len(before)}개 확인\n")

    # --- 1. 테이블 생성 ---
    created = 0
    for name, sql in DDL.items():
        existed = name in tables
        cur.execute(sql)
        print(f"  {'유지' if existed else '생성'}  {name}")
        if not existed:
            created += 1
    for sql in INDEXES:
        cur.execute(sql)
    print(f"\n테이블 {len(DDL)}개 중 신규 {created}개, 인덱스 {len(INDEXES)}개 준비 완료")

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    # --- 2. 추천 대상 연구원 설정 생성 ---
    where = "" if include_alumni else " WHERE status = 'current'"
    members = cur.execute(
        f"SELECT id, name_ko, status FROM members{where} ORDER BY id"
    ).fetchall()

    added = 0
    for m in members:
        exists = cur.execute(
            "SELECT 1 FROM reco_member_settings WHERE member_id = ?", (m["id"],)
        ).fetchone()
        if exists:
            continue
        # 본인 논문 수로 유형 판정 (A: 10편+, B: 2~9편, C: 0~1편)
        n = cur.execute(
            "SELECT COUNT(*) FROM publication_authors WHERE member_id = ?", (m["id"],)
        ).fetchone()[0]
        rtype = "A" if n >= 10 else ("B" if n >= 2 else "C")
        cur.execute(
            "INSERT INTO reco_member_settings "
            "(member_id, researcher_type, core_percent, related_percent, "
            " reference_percent, target_year_range, email_cycle_weeks, is_active, "
            " created_at, updated_at) "
            "VALUES (?, ?, 5, 15, 25, 3, 1, 1, ?, ?)",
            (m["id"], rtype, now, now),
        )
        added += 1

    print(f"\n추천 대상 설정 {added}건 생성 "
          f"(대상 {len(members)}명, {'alumni 포함' if include_alumni else 'status=current'})")
    for r in cur.execute(
        "SELECT researcher_type, COUNT(*) c FROM reco_member_settings "
        "GROUP BY researcher_type ORDER BY researcher_type"
    ):
        print(f"    {r['researcher_type']}유형 {r['c']}명")

    # --- 3. publications.keywords → 행으로 펼치기 ---
    cur.execute("DELETE FROM reco_publication_keywords")
    pubs = cur.execute(
        "SELECT id, keywords FROM publications "
        "WHERE keywords IS NOT NULL AND TRIM(keywords) != ''"
    ).fetchall()
    kw_rows = 0
    for p in pubs:
        seen = set()
        for raw in str(p["keywords"]).split(","):
            kw = raw.strip()
            if not kw or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            cur.execute(
                "INSERT INTO reco_publication_keywords (publication_id, keyword) "
                "VALUES (?, ?)",
                (p["id"], kw),
            )
            kw_rows += 1
    print(f"\n논문 키워드 정규화: {len(pubs)}편 → {kw_rows}행 "
          f"(고유 {cur.execute('SELECT COUNT(DISTINCT keyword) FROM reco_publication_keywords').fetchone()[0]}개)")

    # --- 4. 임베딩 자리 만들기 (값은 나중에 채움) ---
    cur.execute("""
        INSERT INTO reco_publication_meta (publication_id, is_representative)
        SELECT p.id, 0 FROM publications p
        WHERE p.abstract IS NOT NULL AND TRIM(p.abstract) != ''
          AND NOT EXISTS (
            SELECT 1 FROM reco_publication_meta m WHERE m.publication_id = p.id
          )
    """)
    print(f"임베딩 대상 논문 자리 {cur.rowcount}건 생성 "
          f"(총 {cur.execute('SELECT COUNT(*) FROM reco_publication_meta').fetchone()[0]}건)")

    con.commit()

    # --- 5. 보호 테이블이 그대로인지 확인 ---
    after = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )} & PROTECTED
    print(f"\n보호 테이블 검증: {'이상 없음' if after == before else '변경 감지! ' + str(after ^ before)}")

    con.close()
    print("migration done:", db_path)


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "instance" / "bist.db"
    path = sys.argv[1] if len(sys.argv) > 1 else str(default)
    migrate(path, include_alumni="--include-alumni" in sys.argv)
