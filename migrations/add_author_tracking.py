"""관심 저자 추적 테이블 생성 (OpenAlex Author ID 기준).

- authors        : 저자 마스터. openalex_author_id가 실질 식별자.
- author_follows : 연구원 ↔ 저자 관계 (user_id | author_id | followed_at)
- author_papers  : Author ID로 추적해 발견한 논문

기존 interested_authors 테이블은 이름만으로 저장하던 초기 버전이라 대체된다.
데이터가 비어 있을 때만 삭제하고, 행이 하나라도 있으면 남겨 둔 뒤 알린다.

사용:
    python migrations/add_author_tracking.py [db_path]
"""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS authors (
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
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_authors_normalized_name ON authors(normalized_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_authors_orcid ON authors(orcid)")
    print("authors 테이블 준비 완료")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS author_follows (
            id INTEGER PRIMARY KEY,
            researcher_id INTEGER NOT NULL
                REFERENCES researchers(id) ON DELETE CASCADE,
            author_id INTEGER NOT NULL
                REFERENCES authors(id) ON DELETE CASCADE,
            followed_at DATETIME,
            source VARCHAR(20) DEFAULT 'name_search',
            note TEXT,
            CONSTRAINT uq_author_follow UNIQUE (researcher_id, author_id)
        )
    """)
    print("author_follows 테이블 준비 완료")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS author_papers (
            id INTEGER PRIMARY KEY,
            author_id INTEGER NOT NULL
                REFERENCES authors(id) ON DELETE CASCADE,
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
            CONSTRAINT uq_author_work UNIQUE (author_id, openalex_work_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_author_papers_doi ON author_papers(doi)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_author_papers_work ON author_papers(openalex_work_id)")
    print("author_papers 테이블 준비 완료")

    # 구버전 interested_authors 정리 (비어 있을 때만)
    exists = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='interested_authors'"
    ).fetchone()
    if exists:
        n = cur.execute("SELECT COUNT(*) FROM interested_authors").fetchone()[0]
        if n == 0:
            cur.execute("DROP TABLE interested_authors")
            print("구버전 interested_authors 삭제 (행 0개)")
        else:
            print(
                f"주의: interested_authors에 {n}행이 남아 있어 삭제하지 않았습니다. "
                "내용을 확인한 뒤 수동으로 정리하세요."
            )
    else:
        print("interested_authors 테이블 없음 (정리 불필요)")

    con.commit()
    con.close()
    print("migration done:", db_path)


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "instance" / "litreview.db"
    migrate(sys.argv[1] if len(sys.argv) > 1 else str(default))
