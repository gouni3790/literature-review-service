"""기존 본인 논문 187편의 참고문헌을 일괄 수집해 paper_references에 채워넣는 백필.

특성:
- Idempotent: 이미 paper_references에 행이 있는 논문은 스킵.
- Resumable: 중간에 끊겨도 다시 실행하면 미완료분만 처리.
- 10편마다 commit하여 부분 진행 보존.

Usage:
    python -m migrations.backfill_paper_references
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models import PaperReference, ReferencePaper
from app.litreview.profile.scopus_fetcher import _create_session, _fetch_paper_references


COMMIT_EVERY = 10


def run() -> None:
    app = create_app()
    with app.app_context():
        all_papers = (
            ReferencePaper.query.filter(ReferencePaper.scopus_id.isnot(None))
            .order_by(ReferencePaper.researcher_id, ReferencePaper.id)
            .all()
        )
        print(f"전체 본인 논문 (scopus_id 있는 것): {len(all_papers)}편")

        already = {pid for (pid,) in db.session.query(PaperReference.paper_id).distinct()}
        print(f"이미 refs 적재된 논문: {len(already)}편")

        targets = [p for p in all_papers if p.id not in already]
        print(f"백필 대상: {len(targets)}편\n")

        if not targets:
            print("할 일 없음. 종료.")
            return

        session = _create_session()
        ok = empty = fail = 0
        total_refs_added = 0
        start = time.time()

        for i, paper in enumerate(targets, start=1):
            title_short = (paper.title or "")[:50].replace("\n", " ")
            try:
                refs = _fetch_paper_references(paper.scopus_id, session)
            except Exception as e:
                fail += 1
                print(f"[{i}/{len(targets)}] FAIL  p{paper.id} sid={paper.scopus_id} -- {e}")
                continue

            if not refs:
                empty += 1
                print(f"[{i}/{len(targets)}] EMPTY p{paper.id} {title_short}")
            else:
                added = 0
                for ref in refs:
                    if not ref["ref_scopus_id"]:
                        continue
                    db.session.add(
                        PaperReference(
                            paper_id=paper.id,
                            ref_scopus_id=ref["ref_scopus_id"],
                            ref_title=ref["ref_title"],
                            ref_authors=ref["ref_authors"],
                            ref_year=ref["ref_year"],
                            ref_journal=ref["ref_journal"],
                            ref_doi=ref["ref_doi"],
                        )
                    )
                    added += 1
                total_refs_added += added
                ok += 1
                print(f"[{i}/{len(targets)}] OK    p{paper.id} {title_short} -- +{added} refs")

            if i % COMMIT_EVERY == 0:
                db.session.commit()
                elapsed = time.time() - start
                rate = i / elapsed
                eta = (len(targets) - i) / rate if rate > 0 else 0
                print(f"  ... commit. 경과 {elapsed:.0f}s, ETA {eta:.0f}s, 누적 refs {total_refs_added}")

        db.session.commit()
        elapsed = time.time() - start

        print("\n" + "=" * 60)
        print(f"완료. 소요 {elapsed:.0f}s")
        print(f"  성공: {ok}편 / 빈 응답: {empty}편 / 실패: {fail}편")
        print(f"  추가된 paper_references 행: {total_refs_added}건")
        print("=" * 60)


if __name__ == "__main__":
    run()
