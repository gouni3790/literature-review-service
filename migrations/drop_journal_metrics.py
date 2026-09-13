"""저널 지표(JCR/JIF) 스캐폴딩 제거.

배경
----
Web of Science/JCR 데이터를 수집해 Q1 저널만 추천하려던 계획이 취소됐다.
JIF·JCI·분위를 채울 데이터 출처를 쓰지 않기로 했으므로, 그 데이터를 담으려고
만든 스키마는 채워질 일이 없다. 죽은 스키마를 남기면 나중에 읽는 사람이
"여기 값이 왜 다 NULL인가"를 다시 조사하게 되므로 지운다.

지우는 것
    reco_journal_metrics                     테이블 통째로 (0행)
    reco_journal_recommendations 의 6개 컬럼  quartile, jif, jci,
                                             metric_source, metric_category,
                                             citescore_percentile

남기는 것
    reco_journal_recommendations.citescore / quality
        → 점수 공식의 QualityGate 항이 쓰는 값으로, Q1 필터와 다른 것이다.
          (Score = Base × Novelty × QualityGate — 하위 25% 컷)

두 테이블 모두 0행이라 데이터 손실이 없다. SQLite 는 DROP COLUMN 지원이
제한적이라 테이블을 다시 만드는 방식으로 처리한다.

    python migrations/drop_journal_metrics.py --dry-run
    python migrations/drop_journal_metrics.py
"""

import argparse
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

DROP_COLUMNS = (
    "quartile", "jif", "jci",
    "metric_source", "metric_category", "citescore_percentile",
)

# add_journal_recommendations.py 와 같은 정의 (제거 대상 컬럼 없음)
REC_DDL = """
CREATE TABLE reco_journal_recommendations (
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

REC_INDEXES = (
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
        f"bist.db.before_drop_metrics.{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    src, dst = sqlite3.connect(MAIN_DB), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="저널 지표 스캐폴딩 제거")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(MAIN_DB):
        print(f"오류: {MAIN_DB} 없음", file=sys.stderr)
        return 1

    conn = sqlite3.connect(MAIN_DB)
    try:
        print("=" * 62)
        print("저널 지표 스캐폴딩 제거" + ("  [DRY RUN]" if args.dry_run else ""))
        print("=" * 62)

        # --- 안전 확인: 데이터가 있으면 중단 ---
        print("\n[1] 데이터 확인")
        for t in ("reco_journal_metrics", "reco_journal_recommendations"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if exists else -1
            print(f"      {t:34s} {'없음' if n < 0 else str(n) + '행'}")
            if n > 0:
                print(
                    f"\n중단: {t} 에 {n}행이 있습니다. 데이터를 잃지 않도록 "
                    "수동 확인이 필요합니다.",
                    file=sys.stderr,
                )
                return 2

        rec_cols = [
            r[1] for r in conn.execute(
                "PRAGMA table_info(reco_journal_recommendations)"
            )
        ]
        to_drop = [c for c in DROP_COLUMNS if c in rec_cols]
        print(f"\n[2] 제거할 컬럼: {', '.join(to_drop) or '없음'}")

        if args.dry_run:
            print("\n[3] reco_journal_metrics DROP (예정)")
            print("\nDRY RUN 종료 — 변경 없음.")
            return 0

        print(f"\n[3] 백업 -> {backup()}")

        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")

        conn.execute("DROP TABLE IF EXISTS reco_journal_metrics")
        print("      reco_journal_metrics DROP 완료")

        if to_drop:
            conn.execute(
                "ALTER TABLE reco_journal_recommendations RENAME TO _jrec_old"
            )
            conn.execute(REC_DDL)
            conn.execute("DROP TABLE _jrec_old")
            for sql in REC_INDEXES:
                conn.execute(sql)
            print(f"      reco_journal_recommendations 재작성 ({len(to_drop)}개 컬럼 제거)")

        conn.commit()

        cols = [
            r[1] for r in conn.execute(
                "PRAGMA table_info(reco_journal_recommendations)"
            )
        ]
        left = [c for c in DROP_COLUMNS if c in cols]
        print(f"\n[4] 결과 — 컬럼 {len(cols)}개, 잔존 제거대상 {len(left)}개")
        print(f"      {', '.join(cols)}")
        print("\n완료.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
