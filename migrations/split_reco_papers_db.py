"""추천 수집물 테이블을 bist.db 에서 reco_papers.db 로 분리한다.

배경
----
bist.db 는 홈페이지와 공유하는 파일이라 작게 유지해야 한다. 그런데 추천
파이프라인이 쌓는 수집 논문(초록·전문·1536차원 임베딩)은 구 DB(litreview.db)
실측 기준 논문 1건당 약 12KB, 5,274건에 66MB 였다. 이 부피가 공유 파일로
들어가면 안 되므로 수집물 계열 6개 테이블만 별도 파일로 뺀다.

경계
----
bist.db (남는 쪽 — 사람·설정·판단 결과, 오래 보존)
    members / publications / ... (홈페이지 소유)
    reco_member_settings, reco_topics, reco_topic_papers, reco_clusters,
    reco_notes, reco_custom_keywords, reco_custom_journals,
    reco_target_journals, reco_authors, reco_author_follows,
    reco_recommendations, reco_member_papers, reco_publication_meta,
    reco_publication_keywords, reco_email_logs

reco_papers.db (나가는 쪽 — 부피 큰 수집물, 재수집 가능)
    reco_collected_papers, reco_author_papers, reco_interest_papers,
    reco_paper_references, reco_batch_logs, reco_search_queries

조인
----
런타임에는 ATTACH DATABASE 로 두 파일을 한 커넥션에 붙이므로
`FROM reco_recommendations r JOIN papers.reco_collected_papers p` 형태의
SQL 조인이 그대로 동작한다. 앱 코드의 조인문은 수정하지 않는다.
(app/__init__.py 의 _init_attached_papers_db 참고)

외래키
------
파일이 갈리면서 파일 경계를 넘게 되는 FK 제약은 물리 스키마에서 제거한다.
SQLite 는 ATTACH 된 다른 파일의 테이블을 REFERENCES 대상으로 삼을 수 없기
때문이다. 참조 관계 자체는 ORM(app/models.py)에 그대로 남아 있고, 이 앱은
PRAGMA foreign_keys 를 켜지 않으므로(SQLite 기본값 OFF) 동작에 차이는 없다.
제거되는 제약:
    reco_interest_papers.member_id      -> members(id)
    reco_search_queries.member_id       -> members(id)
    reco_paper_references.publication_id-> publications(id)
    reco_author_papers.author_id        -> reco_authors(id)
    reco_recommendations.paper_id       -> reco_collected_papers(id)

실행
----
    python migrations/split_reco_papers_db.py            # 실행
    python migrations/split_reco_papers_db.py --dry-run  # 계획만 출력
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# 윈도우 기본 콘솔(cp949)에서 em dash 같은 문자에 UnicodeEncodeError 가 나므로
# 출력 스트림을 UTF-8 로 바꾼다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
MAIN_DB = os.path.join(INSTANCE_DIR, "bist.db")
PAPERS_DB = os.path.join(INSTANCE_DIR, "reco_papers.db")
BACKUP_DIR = os.path.join(INSTANCE_DIR, "backup")

# 이동 대상 테이블 — 순서는 의미 없음 (파일 경계 FK 를 모두 뗐으므로)
MOVE_TABLES = (
    "reco_collected_papers",
    "reco_author_papers",
    "reco_interest_papers",
    "reco_paper_references",
    "reco_batch_logs",
    "reco_search_queries",
)

# reco_papers.db 에 새로 만들 스키마.
# bist.db 의 기존 DDL 과 컬럼 구성이 동일하며, 파일 경계를 넘는 REFERENCES 절만
# 뺐다 (원래 대상은 주석으로 남겨 둔다).
PAPERS_SCHEMA_SQL = {
    "reco_collected_papers": """
        CREATE TABLE IF NOT EXISTS reco_collected_papers (
            id INTEGER PRIMARY KEY,
            scopus_id VARCHAR(50) UNIQUE,
            title TEXT NOT NULL,
            authors TEXT,
            journal VARCHAR(500),
            year INTEGER,
            abstract TEXT,
            doi VARCHAR(200),
            full_text TEXT,
            embedding TEXT,
            source_query TEXT,
            collection_date DATE,
            created_at DATETIME
        )
    """,
    "reco_author_papers": """
        CREATE TABLE IF NOT EXISTS reco_author_papers (
            id INTEGER PRIMARY KEY,
            -- author_id -> main.reco_authors(id) : 파일 경계라 FK 제약 없음
            author_id INTEGER NOT NULL,
            openalex_work_id VARCHAR(60),
            doi VARCHAR(200),
            title TEXT,
            journal VARCHAR(500),
            year INTEGER,
            publication_date VARCHAR(20),
            abstract TEXT,
            cited_by_count INTEGER,
            collected_paper_id INTEGER,
            first_seen_at DATETIME,
            CONSTRAINT uq_reco_author_work UNIQUE (author_id, openalex_work_id)
        )
    """,
    "reco_interest_papers": """
        CREATE TABLE IF NOT EXISTS reco_interest_papers (
            id INTEGER PRIMARY KEY,
            -- member_id -> main.members(id) : 파일 경계라 FK 제약 없음
            member_id INTEGER NOT NULL,
            scopus_id VARCHAR(50),
            title TEXT,
            authors TEXT,
            journal VARCHAR(500),
            year INTEGER,
            doi VARCHAR(200),
            abstract TEXT,
            keywords TEXT,
            ref_journals TEXT,
            embedding TEXT,
            created_at DATETIME,
            CONSTRAINT uq_reco_interest_member_scopus UNIQUE (member_id, scopus_id)
        )
    """,
    "reco_paper_references": """
        CREATE TABLE IF NOT EXISTS reco_paper_references (
            id INTEGER PRIMARY KEY,
            -- publication_id -> main.publications(id) : 파일 경계라 FK 제약 없음
            publication_id INTEGER,
            interest_paper_id INTEGER
                REFERENCES reco_interest_papers(id) ON DELETE CASCADE,
            ref_scopus_id VARCHAR(50),
            ref_title TEXT,
            ref_authors TEXT,
            ref_year INTEGER,
            ref_journal VARCHAR(500),
            ref_journal_scopus_id VARCHAR(50),
            ref_doi VARCHAR(200),
            created_at DATETIME
        )
    """,
    "reco_batch_logs": """
        CREATE TABLE IF NOT EXISTS reco_batch_logs (
            id INTEGER PRIMARY KEY,
            job_type VARCHAR(100) NOT NULL,
            status VARCHAR(50) NOT NULL,
            details TEXT,
            started_at DATETIME,
            completed_at DATETIME
        )
    """,
    "reco_search_queries": """
        CREATE TABLE IF NOT EXISTS reco_search_queries (
            id INTEGER PRIMARY KEY,
            -- member_id -> main.members(id) : 파일 경계라 FK 제약 없음
            member_id INTEGER NOT NULL,
            query_string TEXT NOT NULL,
            topic_id INTEGER,
            last_executed DATETIME,
            result_count INTEGER,
            created_at DATETIME
        )
    """,
}

PAPERS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_reco_authorpapers_doi"
    " ON reco_author_papers(doi)",
    "CREATE INDEX IF NOT EXISTS ix_reco_interest_scopus"
    " ON reco_interest_papers(scopus_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_paperrefs_interest"
    " ON reco_paper_references(interest_paper_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_paperrefs_refscopus"
    " ON reco_paper_references(ref_scopus_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_paperrefs_refjournal_scopus"
    " ON reco_paper_references(ref_journal_scopus_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_collected_scopus"
    " ON reco_collected_papers(scopus_id)",
)

# bist.db 에 남지만 paper_id FK 대상이 다른 파일로 가버리는 테이블.
# 데이터를 보존한 채 FK 절만 뺀 정의로 재작성한다.
RECOMMENDATIONS_NEW_SQL = """
    CREATE TABLE reco_recommendations (
        id INTEGER PRIMARY KEY,
        -- paper_id -> papers.reco_collected_papers(id) : 파일 경계라 FK 제약 없음
        paper_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
        topic_id INTEGER REFERENCES reco_topics(id) ON DELETE SET NULL,
        similarity_score FLOAT,
        percentile_rank FLOAT,
        similarity_details TEXT,
        grade VARCHAR(20) NOT NULL,
        grade_reason TEXT,
        summary_core_topic TEXT,
        summary_purpose TEXT,
        summary_method TEXT,
        summary_results TEXT,
        summary_limitations TEXT,
        summary_future TEXT,
        recommendation_reason TEXT,
        is_saved BOOLEAN DEFAULT 0,
        is_read BOOLEAN DEFAULT 0,
        user_feedback VARCHAR(50),
        created_at DATETIME,
        CONSTRAINT uq_reco_paper_member_topic
            UNIQUE (paper_id, member_id, topic_id)
    )
"""

RECOMMENDATIONS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_reco_reco_member"
    " ON reco_recommendations(member_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_reco_paper"
    " ON reco_recommendations(paper_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_reco_topic"
    " ON reco_recommendations(topic_id)",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return -1
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def backup_main_db() -> str:
    """SQLite 백업 API 로 bist.db 스냅샷을 뜬다.

    WAL 모드라 파일 복사만으로는 최근 커밋이 누락될 수 있어 backup() 을 쓴다.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"bist.db.before_split.{stamp}")
    src = sqlite3.connect(MAIN_DB)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def create_papers_db(dry_run: bool) -> None:
    """reco_papers.db 파일과 6개 테이블을 만든다."""
    print(f"[2] reco_papers.db 생성: {PAPERS_DB}")
    if dry_run:
        for t in MOVE_TABLES:
            print(f"      CREATE {t}")
        return

    conn = sqlite3.connect(PAPERS_DB)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for table, sql in PAPERS_SCHEMA_SQL.items():
            conn.execute(sql)
            print(f"      CREATE {table}")
        for sql in PAPERS_INDEX_SQL:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def copy_rows(dry_run: bool) -> dict[str, int]:
    """bist.db 의 6개 테이블 데이터를 reco_papers.db 로 복사한다.

    현재는 6개 모두 0행이지만, 나중에 데이터가 쌓인 뒤 실행해도 안전하도록
    일반적인 복사로 구현한다. 대상 테이블에 이미 행이 있으면 건너뛴다.
    """
    print("[3] 데이터 복사")
    moved: dict[str, int] = {}

    main = sqlite3.connect(MAIN_DB)
    papers = sqlite3.connect(PAPERS_DB)
    try:
        for table in MOVE_TABLES:
            src_n = _row_count(main, table)
            if src_n < 0:
                print(f"      {table:26s} bist.db 에 없음 — 건너뜀")
                moved[table] = 0
                continue
            dst_n = _row_count(papers, table)
            if dst_n > 0:
                print(
                    f"      {table:26s} 대상에 이미 {dst_n}행 — 복사 건너뜀"
                )
                moved[table] = dst_n
                continue
            if src_n == 0:
                print(f"      {table:26s} 0행")
                moved[table] = 0
                continue

            cols = _columns(main, table)
            collist = ", ".join(f'"{c}"' for c in cols)
            ph = ", ".join("?" * len(cols))
            if dry_run:
                print(f"      {table:26s} {src_n}행 복사 예정")
                moved[table] = src_n
                continue

            rows = main.execute(f'SELECT {collist} FROM "{table}"').fetchall()
            papers.executemany(
                f'INSERT INTO "{table}" ({collist}) VALUES ({ph})', rows
            )
            papers.commit()
            moved[table] = _row_count(papers, table)
            print(f"      {table:26s} {src_n}행 -> {moved[table]}행")
    finally:
        papers.close()
        main.close()
    return moved


def verify(moved: dict[str, int]) -> bool:
    """원본과 복사본의 행 수가 같은지 확인한다. 다르면 드롭하지 않는다."""
    print("[4] 검증")
    main = sqlite3.connect(MAIN_DB)
    papers = sqlite3.connect(PAPERS_DB)
    ok = True
    try:
        for table in MOVE_TABLES:
            src_n = max(_row_count(main, table), 0)
            dst_n = _row_count(papers, table)
            mark = "OK" if dst_n >= src_n else "불일치"
            if dst_n < src_n:
                ok = False
            print(f"      {table:26s} bist={src_n:6d}  papers={dst_n:6d}  {mark}")
    finally:
        papers.close()
        main.close()
    return ok


def drop_from_main(dry_run: bool) -> None:
    """bist.db 에서 이동 완료된 6개 테이블을 제거한다."""
    print("[5] bist.db 에서 원본 테이블 제거")
    if dry_run:
        for t in MOVE_TABLES:
            print(f"      DROP {t}")
        return

    conn = sqlite3.connect(MAIN_DB)
    try:
        for table in MOVE_TABLES:
            if _table_exists(conn, table):
                conn.execute(f'DROP TABLE "{table}"')
                print(f"      DROP {table}")
            else:
                print(f"      {table} 없음 — 건너뜀")
        conn.commit()
    finally:
        conn.close()


def rebuild_recommendations(dry_run: bool) -> None:
    """reco_recommendations 를 파일 경계 FK 없이 재작성한다 (데이터 보존)."""
    print("[6] reco_recommendations 재작성 (paper_id FK 제거)")
    conn = sqlite3.connect(MAIN_DB)
    try:
        if not _table_exists(conn, "reco_recommendations"):
            print("      테이블 없음 — 건너뜀")
            return

        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='reco_recommendations'"
        ).fetchone()[0]
        if "reco_collected_papers" not in ddl:
            print("      이미 FK 없음 — 건너뜀")
            return

        n = _row_count(conn, "reco_recommendations")
        cols = _columns(conn, "reco_recommendations")
        collist = ", ".join(f'"{c}"' for c in cols)
        print(f"      {n}행 보존하며 재작성")
        if dry_run:
            return

        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE reco_recommendations RENAME TO _reco_reco_old")
        conn.execute(RECOMMENDATIONS_NEW_SQL)
        conn.execute(
            f"INSERT INTO reco_recommendations ({collist})"
            f" SELECT {collist} FROM _reco_reco_old"
        )
        conn.execute("DROP TABLE _reco_reco_old")
        for sql in RECOMMENDATIONS_INDEX_SQL:
            conn.execute(sql)
        conn.commit()
        print(f"      완료 — {_row_count(conn, 'reco_recommendations')}행")
    finally:
        conn.close()


def report_sizes() -> None:
    print("[7] 결과 파일 크기")
    for path in (MAIN_DB, PAPERS_DB):
        if os.path.exists(path):
            mb = os.path.getsize(path) / 1024 / 1024
            print(f"      {os.path.basename(path):20s} {mb:8.2f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="추천 수집물 테이블을 reco_papers.db 로 분리"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="실제 변경 없이 계획만 출력"
    )
    args = parser.parse_args()

    if not os.path.exists(MAIN_DB):
        print(f"오류: {MAIN_DB} 없음", file=sys.stderr)
        return 1

    print("=" * 66)
    print("reco_papers.db 분리" + ("  [DRY RUN]" if args.dry_run else ""))
    print("=" * 66)

    print("[1] bist.db 백업")
    if args.dry_run:
        print(f"      -> {BACKUP_DIR}\\bist.db.before_split.<시각> (예정)")
    else:
        print(f"      -> {backup_main_db()}")

    create_papers_db(args.dry_run)
    moved = copy_rows(args.dry_run)

    if not args.dry_run:
        if not verify(moved):
            print(
                "\n검증 실패 — bist.db 는 그대로 두었습니다. "
                "reco_papers.db 를 지우고 다시 실행하세요.",
                file=sys.stderr,
            )
            return 2
        drop_from_main(args.dry_run)
        rebuild_recommendations(args.dry_run)
        report_sizes()
    else:
        # dry-run 은 파일을 만들지 않으므로 검증 단계는 건너뛴다
        print("[4] 검증 — 실제 실행 시에만 수행")
        drop_from_main(True)
        rebuild_recommendations(True)

    print("\n완료." if not args.dry_run else "\nDRY RUN 종료 — 변경 없음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
