"""구 DB(litreview.db)의 참고문헌을 reco_papers.db 로 이관한다.

왜 필요한가
-----------
저널 발굴 파이프라인의 familiarity(연구실이 이미 아는 저널) 계산은 참고문헌
저널 빈도에 의존한다. 구 DB에는 7,211건(717종)이 쌓여 있는데 bist.db 전환 때
이관되지 않아 현재 reco_paper_references 가 0행이다. 이 상태로는 familiarity 가
전부 0이 되어 Novelty 게이트가 아무 일도 하지 않는다.

키 재매핑
---------
구 paper_references.paper_id 는 구 reference_papers.id 를 가리킨다. 새 스키마의
reco_paper_references.publication_id 는 publications.id 를 가리키므로,
scopus_id → doi → 제목 순으로 대조해 새 publication id 로 바꿔 넣는다.
(실측: 195행 중 190행이 scopus_id 로 매칭, 참고문헌 7,091건 이관 가능)

interest_paper_id 는 이관하지 않는다. 구 interest_papers(3행)를 함께 옮기지
않는 한 가리킬 대상이 없고, 저널 발굴에는 쓰이지 않는다.

실행
----
    python migrations/import_paper_references.py --dry-run
    python migrations/import_paper_references.py
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
INSTANCE = os.path.join(BASE, "instance")
OLD_DB = os.path.join(INSTANCE, "litreview.db")
MAIN_DB = os.path.join(INSTANCE, "bist.db")
PAPERS_DB = os.path.join(INSTANCE, "reco_papers.db")
BACKUP_DIR = os.path.join(INSTANCE, "backup")

COLS = (
    "publication_id", "ref_scopus_id", "ref_title", "ref_authors",
    "ref_year", "ref_journal", "ref_journal_scopus_id", "ref_doi", "created_at",
)


def backup(path: str, tag: str) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{tag}.{stamp}")
    src, dst = sqlite3.connect(path), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def build_mapping(old: sqlite3.Connection, main: sqlite3.Connection) -> dict[int, int]:
    """구 reference_papers.id -> 신 publications.id 대응표."""
    by_scopus = {
        r[0]: r[1]
        for r in main.execute(
            "SELECT scopus_id, id FROM publications"
            " WHERE scopus_id IS NOT NULL AND scopus_id <> ''"
        )
    }
    by_doi = {
        (r[0] or "").lower(): r[1]
        for r in main.execute(
            "SELECT doi, id FROM publications WHERE doi IS NOT NULL AND doi <> ''"
        )
    }
    by_title = {
        r[0]: r[1]
        for r in main.execute("SELECT lower(trim(title)), id FROM publications")
    }

    mapping: dict[int, int] = {}
    stats = {"scopus": 0, "doi": 0, "title": 0, "miss": 0}
    for rid, sid, doi, title in old.execute(
        "SELECT id, scopus_id, doi, lower(trim(title)) FROM reference_papers"
    ):
        if sid and sid in by_scopus:
            mapping[rid] = by_scopus[sid]; stats["scopus"] += 1
        elif doi and doi.lower() in by_doi:
            mapping[rid] = by_doi[doi.lower()]; stats["doi"] += 1
        elif title and title in by_title:
            mapping[rid] = by_title[title]; stats["title"] += 1
        else:
            stats["miss"] += 1

    print(
        f"      매칭 {len(mapping)}행 "
        f"(scopus {stats['scopus']}, doi {stats['doi']}, 제목 {stats['title']}) "
        f"/ 미매칭 {stats['miss']}행"
    )
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser(description="구 DB 참고문헌 이관")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for p in (OLD_DB, MAIN_DB, PAPERS_DB):
        if not os.path.exists(p):
            print(f"오류: {p} 없음", file=sys.stderr)
            return 1

    print("=" * 66)
    print("참고문헌 이관" + ("  [DRY RUN]" if args.dry_run else ""))
    print("=" * 66)

    old = sqlite3.connect(OLD_DB)
    main_db = sqlite3.connect(MAIN_DB)
    papers = sqlite3.connect(PAPERS_DB)

    try:
        existing = papers.execute(
            "SELECT COUNT(*) FROM reco_paper_references"
        ).fetchone()[0]
        print(f"\n[1] 대상 테이블 현재 {existing}행")
        if existing > 0:
            print("      이미 데이터가 있어 중단합니다. 중복 이관을 막기 위함입니다.")
            return 2

        print("\n[2] 논문 키 재매핑 (구 reference_papers -> publications)")
        mapping = build_mapping(old, main_db)
        if not mapping:
            print("      매칭 0행 — 중단", file=sys.stderr)
            return 3

        print("\n[3] 참고문헌 읽기")
        rows = old.execute(
            "SELECT paper_id, ref_scopus_id, ref_title, ref_authors, ref_year,"
            "       ref_journal, ref_journal_scopus_id, ref_doi, created_at"
            "  FROM paper_references WHERE paper_id IS NOT NULL"
        ).fetchall()
        payload = [
            (mapping[r[0]],) + tuple(r[1:]) for r in rows if r[0] in mapping
        ]
        skipped = len(rows) - len(payload)
        print(f"      전체 {len(rows)}건 중 이관 대상 {len(payload)}건 (건너뜀 {skipped}건)")

        journals = {r[5] for r in rows if r[5]}
        print(f"      고유 저널 {len(journals)}종")

        if args.dry_run:
            print("\nDRY RUN 종료 — 변경 없음.")
            return 0

        print(f"\n[4] 백업 -> {backup(PAPERS_DB, 'before_refs')}")

        print("\n[5] 삽입")
        collist = ", ".join(COLS)
        ph = ", ".join("?" * len(COLS))
        papers.executemany(
            f"INSERT INTO reco_paper_references ({collist}) VALUES ({ph})", payload
        )
        papers.commit()

        n = papers.execute("SELECT COUNT(*) FROM reco_paper_references").fetchone()[0]
        j = papers.execute(
            "SELECT COUNT(DISTINCT ref_journal) FROM reco_paper_references"
            " WHERE ref_journal IS NOT NULL AND ref_journal <> ''"
        ).fetchone()[0]
        print(f"      완료 — {n}행, 고유 저널 {j}종")

        print("\n[6] familiarity 상위 10 (연구실이 이미 아는 저널)")
        for name, cnt in papers.execute(
            "SELECT ref_journal, COUNT(*) c FROM reco_paper_references"
            " WHERE ref_journal IS NOT NULL AND ref_journal <> ''"
            " GROUP BY ref_journal ORDER BY c DESC LIMIT 10"
        ):
            print(f"      {name[:52]:54s} {cnt:5d}")

        mb = os.path.getsize(PAPERS_DB) / 1024 / 1024
        print(f"\n[7] reco_papers.db {mb:.2f} MB")
        print("\n완료.")
        return 0
    finally:
        papers.close()
        main_db.close()
        old.close()


if __name__ == "__main__":
    sys.exit(main())
