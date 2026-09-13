"""reference_papers 테이블에 volume, publisher, source 컬럼 추가.

- volume: 저널 권/호 (Scopus prism:volume 또는 Crossref volume)
- publisher: 출판사 (주로 Crossref publisher)
- source: 'scopus'(자동 수집) | 'manual'(DOI 수동 등록)

BIST paper 대시보드의 Pure 스타일 결과 행에 표시.

Idempotent.

Usage:
    python -m migrations.add_paper_volume_publisher
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

        to_add = [
            ("volume", "VARCHAR(50)"),
            ("publisher", "VARCHAR(300)"),
            ("source", "VARCHAR(20)"),
        ]

        with db.engine.begin() as conn:
            for name, ddl in to_add:
                if name in cols:
                    print(f"· {name}: 이미 존재 → skip")
                    continue
                conn.execute(
                    text(f"ALTER TABLE reference_papers ADD COLUMN {name} {ddl}")
                )
                print(f"+ {name} {ddl} 추가")

            # 기존 행: scopus_id 있으면 scopus, 없으면 manual 로 백필
            if "source" not in cols:
                conn.execute(
                    text(
                        "UPDATE reference_papers SET source = "
                        "CASE WHEN scopus_id IS NOT NULL AND scopus_id != '' "
                        "THEN 'scopus' ELSE 'manual' END "
                        "WHERE source IS NULL"
                    )
                )
                print("· source 백필 완료 (scopus_id 기준)")

        print("[완료] 마이그레이션 완료")


if __name__ == "__main__":
    run()
