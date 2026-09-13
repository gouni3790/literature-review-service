"""paper_references 스키마 변경:
   - paper_id: NOT NULL → NULL 허용
   - interest_paper_id: 신규 컬럼 (FK to interest_papers.id)

SQLite는 ALTER COLUMN을 지원하지 않으므로 테이블 재생성 방식 사용.

Idempotent: 이미 변경되어 있으면 skip.

Usage:
    python -m migrations.add_interest_paper_id_to_paper_references
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text

from app import create_app, db


def _is_already_migrated(conn) -> bool:
    """interest_paper_id 컬럼이 있고 paper_id가 nullable이면 이미 마이그레이션됨."""
    cols = conn.execute(text("PRAGMA table_info(paper_references)")).fetchall()
    has_interest_col = any(c[1] == "interest_paper_id" for c in cols)
    paper_id_col = next((c for c in cols if c[1] == "paper_id"), None)
    paper_id_nullable = paper_id_col is not None and paper_id_col[3] == 0
    return has_interest_col and paper_id_nullable


def run() -> None:
    app = create_app()
    with app.app_context():
        with db.engine.begin() as conn:
            if _is_already_migrated(conn):
                print("· 이미 마이그레이션됨 → skip")
                return

            row_count_before = conn.execute(
                text("SELECT COUNT(*) FROM paper_references")
            ).scalar()
            print(f"· 시작: paper_references 행 수 = {row_count_before}")

            # SQLite 안전 모드: FK 잠시 끔
            conn.execute(text("PRAGMA foreign_keys=OFF"))

            # 1. 기존 인덱스 삭제 (재생성을 위해)
            indexes = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='paper_references' AND name NOT LIKE 'sqlite_%'"
                )
            ).fetchall()
            for (idx_name,) in indexes:
                conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))
                print(f"  - drop index {idx_name}")

            # 2. 기존 테이블을 임시 이름으로 변경
            conn.execute(text("ALTER TABLE paper_references RENAME TO _paper_references_old"))
            print("  - rename paper_references → _paper_references_old")

            # 3. 새 스키마로 테이블 생성
            conn.execute(
                text(
                    """
                CREATE TABLE paper_references (
                    id INTEGER NOT NULL,
                    paper_id INTEGER,
                    interest_paper_id INTEGER,
                    ref_scopus_id VARCHAR(50),
                    ref_title TEXT,
                    ref_authors TEXT,
                    ref_year INTEGER,
                    ref_journal VARCHAR(500),
                    ref_journal_scopus_id VARCHAR(50),
                    ref_doi VARCHAR(200),
                    created_at DATETIME,
                    PRIMARY KEY (id),
                    FOREIGN KEY(paper_id) REFERENCES reference_papers (id) ON DELETE CASCADE,
                    FOREIGN KEY(interest_paper_id) REFERENCES interest_papers (id) ON DELETE CASCADE
                )
                """
                )
            )
            print("  - create new paper_references (paper_id NULL 허용 + interest_paper_id 추가)")

            # 4. 기존 데이터 복사 (interest_paper_id는 NULL)
            conn.execute(
                text(
                    """
                INSERT INTO paper_references
                    (id, paper_id, interest_paper_id, ref_scopus_id, ref_title,
                     ref_authors, ref_year, ref_journal, ref_journal_scopus_id,
                     ref_doi, created_at)
                SELECT id, paper_id, NULL, ref_scopus_id, ref_title,
                       ref_authors, ref_year, ref_journal, ref_journal_scopus_id,
                       ref_doi, created_at
                FROM _paper_references_old
                """
                )
            )
            row_count_after = conn.execute(
                text("SELECT COUNT(*) FROM paper_references")
            ).scalar()
            print(f"  - copy: {row_count_after} rows")

            assert row_count_before == row_count_after, "행 수 불일치!"

            # 5. 임시 테이블 삭제
            conn.execute(text("DROP TABLE _paper_references_old"))
            print("  - drop _paper_references_old")

            # 6. 인덱스 재생성
            conn.execute(
                text(
                    "CREATE INDEX ix_paper_references_ref_scopus_id "
                    "ON paper_references (ref_scopus_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX ix_paper_references_ref_journal_scopus_id "
                    "ON paper_references (ref_journal_scopus_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX ix_paper_references_interest_paper_id "
                    "ON paper_references (interest_paper_id)"
                )
            )
            print("  - recreate 3 indexes (ref_scopus_id, ref_journal_scopus_id, interest_paper_id)")

            conn.execute(text("PRAGMA foreign_keys=ON"))

        # 검증
        with app.app_context():
            cols = db.session.execute(
                text("PRAGMA table_info(paper_references)")
            ).fetchall()
            print(f"\n현재 paper_references 컬럼:")
            for c in cols:
                nn = "NOT NULL" if c[3] else "NULL"
                print(f"  {c[1]:30s} {c[2]:15s} {nn}")

        print("\n✓ 마이그레이션 완료")


if __name__ == "__main__":
    run()
