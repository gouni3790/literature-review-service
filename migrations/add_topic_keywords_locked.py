"""research_topics.keywords_locked 컬럼 추가.

사용자가 마이페이지에서 주제 키워드를 직접 편집하면 이 값이 1이 되고,
B유형 자동 주제 갱신(create_auto_topic_type_b)이 해당 주제를 건드리지 않는다.
이 컬럼이 없으면 사용자가 고친 키워드가 매주 일요일 배치에서 유실된다.

사용:
    python migrations/add_topic_keywords_locked.py [db_path]
    (기본 db_path: instance/litreview.db)
"""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cols = [r[1] for r in cur.execute("PRAGMA table_info(research_topics)")]
    if "keywords_locked" not in cols:
        cur.execute(
            "ALTER TABLE research_topics "
            "ADD COLUMN keywords_locked BOOLEAN DEFAULT 0"
        )
        print("added research_topics.keywords_locked")
    else:
        print("research_topics.keywords_locked already exists")

    # 기존 행의 NULL을 0으로 정규화
    cur.execute(
        "UPDATE research_topics SET keywords_locked = 0 WHERE keywords_locked IS NULL"
    )
    print(f"normalized {cur.rowcount} existing rows to 0")

    con.commit()
    con.close()
    print("migration done:", db_path)


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "instance" / "litreview.db"
    migrate(sys.argv[1] if len(sys.argv) > 1 else str(default))
