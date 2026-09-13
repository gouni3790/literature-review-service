"""paper_references 테이블에 공통 ref 매칭/저널 추적용 컬럼 추가.

추가 컬럼:
    ref_scopus_id            VARCHAR(50)   indexed
    ref_authors              TEXT
    ref_year                 INTEGER
    ref_journal_scopus_id    VARCHAR(50)   indexed

Idempotent: 이미 있는 컬럼/인덱스는 건너뜀.

Usage:
    python -m migrations.add_paper_references_columns
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text

from app import create_app, db


COLUMNS = [
    ("ref_scopus_id", "VARCHAR(50)"),
    ("ref_authors", "TEXT"),
    ("ref_year", "INTEGER"),
    ("ref_journal_scopus_id", "VARCHAR(50)"),
]

INDEXES = [
    ("ix_paper_references_ref_scopus_id", "ref_scopus_id"),
    ("ix_paper_references_ref_journal_scopus_id", "ref_journal_scopus_id"),
]


def run() -> None:
    app = create_app()
    with app.app_context():
        inspector = inspect(db.engine)
        existing_cols = {c["name"] for c in inspector.get_columns("paper_references")}
        existing_idx = {i["name"] for i in inspector.get_indexes("paper_references")}

        with db.engine.begin() as conn:
            for col, sqltype in COLUMNS:
                if col in existing_cols:
                    print(f"  · {col}: 이미 존재 → skip")
                    continue
                conn.execute(text(f"ALTER TABLE paper_references ADD COLUMN {col} {sqltype}"))
                print(f"  + {col} {sqltype} 추가")

            for idx_name, col in INDEXES:
                if idx_name in existing_idx:
                    print(f"  · {idx_name}: 이미 존재 → skip")
                    continue
                conn.execute(text(f"CREATE INDEX {idx_name} ON paper_references ({col})"))
                print(f"  + index {idx_name}({col}) 생성")

        print("✓ 마이그레이션 완료")

        inspector = inspect(db.engine)
        cols = inspector.get_columns("paper_references")
        print(f"\n현재 paper_references 컬럼 ({len(cols)}개):")
        for c in cols:
            print(f"  {c['name']:30s} {c['type']}")


if __name__ == "__main__":
    run()
