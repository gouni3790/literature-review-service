"""DB 테이블 직접 생성 스크립트.

Usage:
    python -m migrations.create_tables
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect

from app import create_app, db


def create_all_tables():
    """전체 테이블 생성 (SQLite)."""
    app = create_app()
    with app.app_context():
        db.create_all()
        print("✓ All tables created")

        inspector = inspect(db.engine)
        tables = sorted(inspector.get_table_names())
        print(f"✓ Tables ({len(tables)}): {', '.join(tables)}")


if __name__ == "__main__":
    create_all_tables()
