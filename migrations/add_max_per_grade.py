"""researchers 테이블에 max_per_grade 컬럼 추가.

NULL = 등급별 상한 없음 (기본). 값 있으면 grader가 그 수치를 cap으로 적용.

Idempotent.

Usage:
    python -m migrations.add_max_per_grade
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text

from app import create_app, db


def run() -> None:
    app = create_app()
    with app.app_context():
        cols = {c["name"] for c in inspect(db.engine).get_columns("researchers")}
        if "max_per_grade" in cols:
            print("· max_per_grade: 이미 존재 → skip")
            return

        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE researchers ADD COLUMN max_per_grade INTEGER"))
            print("+ max_per_grade INTEGER 추가")

        print("✓ 마이그레이션 완료")


if __name__ == "__main__":
    run()
