"""연구원별 본인 논문 테이블(reco_member_papers) 생성 + publications에서 채우기.

bist.db는 논문을 publications 1건 ↔ publication_authors N명으로 정규화해 두었다.
반면 추천 코드는 '연구원별 본인 논문 목록'을 한 테이블에서 읽도록 작성돼 있고,
클러스터 번호(cluster_id)와 대표 논문 여부(is_representative)는 연구원마다 다른 값이라
논문 쪽에 둘 수 없다.

그래서 (member_id, publication_id) 조합을 행으로 갖는 파생 테이블을 만든다.
임베딩은 논문 고유값이라 reco_publication_meta에서 한 번만 계산해 여기로 복사한다
(같은 논문을 공저자 수만큼 다시 임베딩하면 돈이 낭비된다).

publications가 갱신되면 이 스크립트를 다시 돌리거나 주간 배치가 호출한다.

사용:
    python migrations/bist_member_papers.py [db_path]
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS reco_member_papers (
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    scopus_id VARCHAR(50),
    title TEXT NOT NULL,
    authors TEXT,
    journal VARCHAR(500),
    classification VARCHAR(20),
    volume VARCHAR(50),
    publisher VARCHAR(300),
    year INTEGER,
    abstract TEXT,
    doi VARCHAR(200),
    cited_by INTEGER DEFAULT 0,
    source VARCHAR(20) DEFAULT 'bist_homepage',
    status VARCHAR(20) DEFAULT 'published',
    is_first BOOLEAN DEFAULT 0,
    is_corresponding BOOLEAN DEFAULT 0,
    author_position INTEGER,
    embedding TEXT,
    cluster_id INTEGER,
    is_representative BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME,
    CONSTRAINT uq_reco_member_publication UNIQUE (member_id, publication_id)
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_reco_mp_member ON reco_member_papers(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_mp_pub ON reco_member_papers(publication_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_mp_cluster ON reco_member_papers(cluster_id)",
]

SELECT_SOURCE = """
    SELECT pa.member_id, pa.publication_id, pa.position, pa.is_first,
           pa.is_corresponding,
           p.scopus_id, p.title, p.venue AS journal, p.volume, p.publisher,
           p.year, p.abstract, p.doi, p.classification, p.cited_by, p.status,
           (SELECT GROUP_CONCAT(COALESCE(x.raw_name, m2.name_en, m2.name_ko), '; ')
              FROM publication_authors x
              LEFT JOIN members m2 ON m2.id = x.member_id
             WHERE x.publication_id = p.id) AS authors
      FROM publication_authors pa
      JOIN publications p ON p.id = pa.publication_id
     WHERE pa.member_id IS NOT NULL
     ORDER BY pa.member_id, p.year DESC
"""

UPDATE_SQL = """
    UPDATE reco_member_papers SET
      scopus_id=?, title=?, authors=?, journal=?, classification=?,
      volume=?, publisher=?, year=?, abstract=?, doi=?,
      cited_by=?, status=?, is_first=?, is_corresponding=?, author_position=?
    WHERE id=?
"""

INSERT_SQL = """
    INSERT INTO reco_member_papers
      (member_id, publication_id, scopus_id, title, authors, journal,
       classification, volume, publisher, year, abstract, doi,
       cited_by, status, is_first, is_corresponding, author_position,
       source, is_representative, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            'bist_homepage', 0, ?)
"""

# 홈페이지에서 저자 연결이 끊긴 행 정리
PRUNE_SQL = """
    DELETE FROM reco_member_papers
     WHERE NOT EXISTS (
        SELECT 1 FROM publication_authors pa
         WHERE pa.member_id = reco_member_papers.member_id
           AND pa.publication_id = reco_member_papers.publication_id
     )
"""

# 이미 계산된 논문 임베딩 복사 (중복 임베딩 방지)
COPY_EMB_SQL = """
    UPDATE reco_member_papers SET embedding = (
        SELECT m.embedding FROM reco_publication_meta m
         WHERE m.publication_id = reco_member_papers.publication_id
    )
    WHERE embedding IS NULL
      AND EXISTS (
        SELECT 1 FROM reco_publication_meta m
         WHERE m.publication_id = reco_member_papers.publication_id
           AND m.embedding IS NOT NULL
      )
"""


def refresh(db_path: str, quiet: bool = False) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    if not cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='publications'"
    ).fetchone():
        raise SystemExit("중단: publications 테이블이 없습니다 (홈페이지 DB가 아님)")

    cur.execute(DDL)
    for sql in INDEXES:
        cur.execute(sql)

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    rows = cur.execute(SELECT_SOURCE).fetchall()

    inserted = updated = 0
    for r in rows:
        exists = cur.execute(
            "SELECT id FROM reco_member_papers "
            "WHERE member_id=? AND publication_id=?",
            (r["member_id"], r["publication_id"]),
        ).fetchone()

        vals = (
            r["scopus_id"], r["title"], r["authors"], r["journal"],
            r["classification"], r["volume"], r["publisher"], r["year"],
            r["abstract"], r["doi"], r["cited_by"] or 0,
            r["status"] or "published",
            1 if r["is_first"] else 0,
            1 if r["is_corresponding"] else 0,
            r["position"],
        )
        if exists:
            # 클러스터/임베딩은 유지하고 서지 정보만 갱신
            cur.execute(UPDATE_SQL, (*vals, exists["id"]))
            updated += 1
        else:
            cur.execute(INSERT_SQL, (r["member_id"], r["publication_id"], *vals, now))
            inserted += 1

    cur.execute(PRUNE_SQL)
    removed = cur.rowcount
    cur.execute(COPY_EMB_SQL)
    copied = cur.rowcount

    con.commit()

    total = cur.execute("SELECT COUNT(*) FROM reco_member_papers").fetchone()[0]
    people = cur.execute(
        "SELECT COUNT(DISTINCT member_id) FROM reco_member_papers"
    ).fetchone()[0]
    stats = {
        "inserted": inserted, "updated": updated, "removed": removed,
        "embedding_copied": copied, "total": total, "members": people,
    }

    if not quiet:
        print("reco_member_papers 테이블 준비 완료")
        print(f"신규 {inserted}행 · 갱신 {updated}행 · 삭제 {removed}행 "
              f"· 임베딩 복사 {copied}행")
        print(f"현재 {total}행 / 연구원 {people}명")
        print("\n연구원별 논문 수:")
        for r in cur.execute(
            "SELECT m.name_ko, COUNT(*) c, "
            "SUM(CASE WHEN mp.abstract IS NOT NULL AND TRIM(mp.abstract)<>'' "
            "         THEN 1 ELSE 0 END) a "
            "FROM reco_member_papers mp JOIN members m ON m.id = mp.member_id "
            "GROUP BY mp.member_id ORDER BY c DESC"
        ):
            print(f"  {r['name_ko']:<10} {r['c']:>3}편 (초록 {r['a']}편)")

    con.close()
    return stats


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "instance" / "bist.db"
    path = sys.argv[1] if len(sys.argv) > 1 else str(default)
    refresh(path)
    print("\ndone:", path)
