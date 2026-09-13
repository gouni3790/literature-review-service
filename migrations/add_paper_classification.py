"""reference_papers 테이블에 classification 컬럼 추가.

classification: SCIE | KCI | null

수동 등록 논문에서 학술지 분류(SCIE/KCI)를 지정하기 위함.
Scopus 자동 수집 논문은 null(결과 목록에서 'Scopus'로 표시).

Idempotent.

Usage:
    python -m migrations.add_paper_classification
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text

from app import create_app, db


def run() -> None:
    app = create_app()
    with app.app_context():
        cols = {c["name"] for c in inspect(db.engine).get_columns("reference_papers")}
        if "classification" in cols:
            print("- classification: 이미 존재 -> skip")
        else:
            with db.engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE reference_papers ADD COLUMN classification VARCHAR(20)")
                )
            print("+ classification VARCHAR(20) 추가")
        print("[완료] 마이그레이션 완료")


if __name__ == "__main__":
    run()
