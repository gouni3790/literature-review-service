"""reco_api_usage 테이블 생성 — API 토큰 사용량·비용 기록.

    python migrations/add_api_usage.py
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
CREATE TABLE IF NOT EXISTS reco_api_usage (
    id                INTEGER PRIMARY KEY,
    occurred_at       DATETIME NOT NULL,

    provider          VARCHAR(20) NOT NULL,
    model             VARCHAR(80) NOT NULL,
    operation         VARCHAR(40) NOT NULL,

    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,

    usd_cost          FLOAT,
    usd_krw_rate      FLOAT,
    rate_date         DATE,
    rate_source       VARCHAR(30),
    krw_cost          FLOAT,

    -- member_id 는 파일 내부 FK 라 정상 동작한다
    member_id         INTEGER REFERENCES members(id) ON DELETE SET NULL,
    topic_id          INTEGER,
    note              VARCHAR(300)
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_reco_usage_time ON reco_api_usage(occurred_at)",
    "CREATE INDEX IF NOT EXISTS ix_reco_usage_op ON reco_api_usage(operation)",
    "CREATE INDEX IF NOT EXISTS ix_reco_usage_model ON reco_api_usage(model)",
    "CREATE INDEX IF NOT EXISTS ix_reco_usage_provider ON reco_api_usage(provider)",
    "CREATE INDEX IF NOT EXISTS ix_reco_usage_member ON reco_api_usage(member_id)",
)


def backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(
        BACKUP_DIR,
        f"bist.db.before_usage.{datetime.now().strftime('%Y%m%d_%H%M%S')}",
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

    print("reco_api_usage 생성")
    conn = sqlite3.connect(MAIN_DB)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reco_api_usage'"
        ).fetchone()
        if exists:
            n = conn.execute("SELECT COUNT(*) FROM reco_api_usage").fetchone()[0]
            print(f"   이미 존재 — {n}행")
        else:
            print(f"   백업 -> {backup()}")

        conn.execute(DDL)
        for sql in INDEXES:
            conn.execute(sql)
        conn.commit()

        cols = [r[1] for r in conn.execute("PRAGMA table_info(reco_api_usage)")]
        print(f"   완료 — 컬럼 {len(cols)}개")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
