"""reference_papers 테이블에 status 컬럼 추가.

status: published(기본, 게재) | submitted(투고완료) | under_review(심사중)

투고 예정/심사 중인 논문을 수동 등록하고 상태를 관리하기 위함.
기존 행(Scopus/DOI로 들어온 게재 논문)은 모두 'published'로 백필.

Idempotent.

Usage:
    python -m migrations.add_paper_status
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

        if "status" in cols:
            print("- status: 이미 존재 -> skip")
        else:
            with db.engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE reference_papers ADD COLUMN status VARCHAR(20)")
                )
                conn.execute(
                    text("UPDATE reference_papers SET status = 'published' WHERE status IS NULL")
                )
            print("+ status VARCHAR(20) 추가 + 기존행 'published' 백필")

        print("[완료] 마이그레이션 완료")


if __name__ == "__main__":
    run()
