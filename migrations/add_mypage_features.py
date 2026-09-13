"""마이페이지 기능 마이그레이션.

1. researchers.email_cycle_weeks 컬럼 추가 (추천 이메일 주기, 기본 1주)
2. interested_authors 테이블 생성 (관심 저자 등록)

사용:
    python migrations/add_mypage_features.py [db_path]
    (기본 db_path: instance/litreview.db)
"""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # 1. email_cycle_weeks 컬럼
    cols = [r[1] for r in cur.execute("PRAGMA table_info(researchers)")]
    if "email_cycle_weeks" not in cols:
        cur.execute(
            "ALTER TABLE researchers ADD COLUMN email_cycle_weeks INTEGER DEFAULT 1"
        )
        print("added researchers.email_cycle_weeks")
    else:
        print("researchers.email_cycle_weeks already exists")

    # 2. interested_authors 테이블
    cur.execute("""
        CREATE TABLE IF NOT EXISTS interested_authors (
            id INTEGER PRIMARY KEY,
            researcher_id INTEGER NOT NULL
                REFERENCES researchers(id) ON DELETE CASCADE,
            author_name VARCHAR(300) NOT NULL,
            scopus_author_id VARCHAR(50),
            affiliation VARCHAR(300),
            note TEXT,
            created_at DATETIME,
            CONSTRAINT uq_interested_author UNIQUE (researcher_id, author_name)
        )
    """)
    print("interested_authors table ready")

    con.commit()
    con.close()
    print("migration done:", db_path)


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "instance" / "litreview.db"
    migrate(sys.argv[1] if len(sys.argv) > 1 else str(default))
