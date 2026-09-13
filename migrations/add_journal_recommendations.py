"""reco_journal_recommendations 테이블 생성.

저널 발굴 파이프라인의 산출물을 담는다. reco_target_journals(수집 타겟)와
역할이 다르며, 사용자가 구독한 것만 그쪽으로 편입된다.

bist.db 에 만든다 — 판단 결과이고 작아서 공유 파일에 두어도 무방하다.
member_id/topic_id FK 도 같은 파일 안이라 정상 동작한다.

    python migrations/add_journal_recommendations.py
"""

import os
import sqlite3
import sys
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_DB = os.path.join(BASE, "instance", "bist.db")
BACKUP_DIR = os.path.join(BASE, "instance", "backup")

DDL = """
CREATE TABLE IF NOT EXISTS reco_journal_recommendations (
    id                  INTEGER PRIMARY KEY,
    member_id           INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    topic_id            INTEGER REFERENCES reco_topics(id) ON DELETE CASCADE,
    run_id              VARCHAR(40) NOT NULL,

    journal_name        VARCHAR(500) NOT NULL,
    scopus_source_id    VARCHAR(50),
    issn                VARCHAR(20),

    related_paper_count INTEGER,
    avg_similarity      FLOAT,
    growth_rate         FLOAT,
    familiarity         FLOAT,
    novelty             FLOAT,
    citescore           FLOAT,
    quality             FLOAT,

    score               FLOAT NOT NULL,
    rank                INTEGER,
    evidence            TEXT,

    status              VARCHAR(20) NOT NULL DEFAULT 'new',
    created_at          DATETIME,

    CONSTRAINT uq_reco_journalrec
        UNIQUE (member_id, topic_id, journal_name, run_id)
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_reco_jrec_member"
    " ON reco_journal_recommendations(member_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_reco_jrec_topic"
    " ON reco_journal_recommendations(topic_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_jrec_run"
    " ON reco_journal_recommendations(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_jrec_score"
    " ON reco_journal_recommendations(member_id, score DESC)",
)


def backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(
        BACKUP_DIR,
        f"bist.db.before_jrec.{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    src, dst = sqlite3.connect(MAIN_DB), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def main() -> int:
    if not os.path.exists(MAIN_DB):
        print(f"오류: {MAIN_DB} 없음", file=sys.stderr)
        return 1

    print("reco_journal_recommendations 생성")
    conn = sqlite3.connect(MAIN_DB)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='reco_journal_recommendations'"
        ).fetchone()
        if exists:
            n = conn.execute(
                "SELECT COUNT(*) FROM reco_journal_recommendations"
            ).fetchone()[0]
            print(f"   이미 존재 — {n}행. 인덱스만 확인합니다.")
        else:
            print(f"   백업 -> {backup()}")

        conn.execute(DDL)
        for sql in INDEXES:
            conn.execute(sql)
        conn.commit()

        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(reco_journal_recommendations)"
        )]
        print(f"   완료 — 컬럼 {len(cols)}개: {', '.join(cols)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
