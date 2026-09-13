"""reco_recommendations 에 summary_source 컬럼 추가.

core 등급이어도 Elsevier 구독 저널이 아니면 전문을 받지 못한다(운영 실측으로
요약 대기 213편 중 전문 확보 13편, 6%). 전문 기반 요약과 초록 기반 요약이
구분되지 않으면 연구원이 요약의 신뢰 수준을 판단할 수 없다.

    fulltext  전문을 읽고 작성
    abstract  초록만 읽고 작성
    NULL      아직 요약되지 않음 (또는 이 컬럼 도입 이전 요약)

기존 행은 NULL 로 남긴다. 소급 판정하려면 reco_collected_papers.full_text 가
있는지 봐야 하는데, 요약 시점과 지금의 full_text 상태가 다를 수 있어
추측으로 채우지 않는다.

    python migrations/add_summary_source.py
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


def backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(
        BACKUP_DIR,
        f"bist.db.before_summary_source.{datetime.now().strftime('%Y%m%d_%H%M%S')}",
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

    conn = sqlite3.connect(MAIN_DB)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reco_recommendations)")}
        if "summary_source" in cols:
            n = conn.execute(
                "SELECT COUNT(*) FROM reco_recommendations"
                " WHERE summary_source IS NOT NULL"
            ).fetchone()[0]
            print(f"이미 존재 — summary_source 채워진 행 {n}개")
            return 0

        print(f"백업 -> {backup()}")
        conn.execute(
            "ALTER TABLE reco_recommendations ADD COLUMN summary_source VARCHAR(20)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_reco_reco_summary_source"
            " ON reco_recommendations(summary_source)"
        )
        conn.commit()
        print("summary_source 컬럼 추가 완료 (기존 행은 NULL)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
