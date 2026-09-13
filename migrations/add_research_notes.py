"""연구 소개를 여러 건 저장할 수 있도록 research_notes 테이블 생성.

기존 researchers.research_description(단일 텍스트)에 값이 있으면 노트 1건으로
옮긴 뒤 원래 칸을 비운다. 컬럼 자체는 지우지 않는다 — 구버전 화면
(profile.html, home_v2.html)이 아직 읽고 있어 깨뜨리지 않기 위함이다.

사용:
    python migrations/add_research_notes.py [db_path]
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def migrate(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS research_notes (
            id INTEGER PRIMARY KEY,
            researcher_id INTEGER NOT NULL
                REFERENCES researchers(id) ON DELETE CASCADE,
            title VARCHAR(200),
            content TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_notes_researcher "
        "ON research_notes(researcher_id)"
    )
    print("research_notes 테이블 준비 완료")

    # 기존 단일 필드 → 노트 1건으로 이관
    rows = cur.execute(
        "SELECT id, name, research_description FROM researchers "
        "WHERE research_description IS NOT NULL AND TRIM(research_description) != ''"
    ).fetchall()

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    moved = 0
    for rid, name, desc in rows:
        already = cur.execute(
            "SELECT COUNT(*) FROM research_notes WHERE researcher_id=? AND content=?",
            (rid, desc),
        ).fetchone()[0]
        if already:
            print(f"  건너뜀(이미 이관됨): {name}")
            continue
        cur.execute(
            "INSERT INTO research_notes "
            "(researcher_id, title, content, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (rid, "연구 소개", desc, now, now),
        )
        cur.execute(
            "UPDATE researchers SET research_description = NULL WHERE id = ?", (rid,)
        )
        moved += 1
        print(f"  이관: {name} ({len(desc)}자)")

    print(f"기존 연구 소개 {moved}건을 노트로 옮겼습니다 "
          f"(대상 {len(rows)}명)")

    con.commit()
    con.close()
    print("migration done:", db_path)


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "instance" / "litreview.db"
    migrate(sys.argv[1] if len(sys.argv) > 1 else str(default))
