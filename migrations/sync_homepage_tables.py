"""홈페이지 DB에서 홈페이지 소유 테이블만 가져와 로컬 bist.db 를 갱신한다.

배경
----
홈페이지(lab.bistskku.com)는 Vultr 서버 /srv/bist 에서 돌고, 추천 시스템은
BIST 워크스테이션에서 돈다. 둘은 **다른 장비**라 SQLite 파일 하나를 같이 쓸 수
없다. 그래서 홈페이지 데이터를 주기적으로 단방향 복제한다.

이 방향이 안전한 이유는 app/models.py 가 홈페이지 테이블을 **읽기 전용**으로만
다루기 때문이다(모델 이벤트로 INSERT/UPDATE/DELETE 를 실제로 막아 두었다).
추천 시스템이 홈페이지 테이블에 쓰지 않으므로, 통째로 덮어써도 잃을 것이 없다.

    /srv/bist/bist.db  ──(scp)──▶  로컬 사본  ──(이 스크립트)──▶  instance/bist.db
       홈페이지 소유                                                 reco_* 는 보존

무엇을 덮어쓰고 무엇을 지키는가
------------------------------
덮어씀 : 아래 HOMEPAGE_TABLES (migrations/bist_reco_schema.py 의 PROTECTED 와 동일)
지킴   : reco_ 로 시작하는 모든 테이블 — 추천 설정·주제·추천 이력·요약·피드백

스키마가 바뀌어도 따라간다
--------------------------
홈페이지가 컬럼을 추가해도 되도록, 각 테이블을 **원본의 DDL 로 다시 만들고**
행을 복사한다. 컬럼을 일일이 맞출 필요가 없다.

먼저 파일을 받아온다
--------------------
    scp root@158.247.199.32:/srv/bist/bist.db  C:\\temp\\bist_from_server.db

WAL 모드면 -wal 파일에 최근 커밋이 남아 있을 수 있다. 서버에서 체크포인트를
한 뒤 받는 편이 확실하다.

    ssh root@158.247.199.32 "sqlite3 /srv/bist/bist.db 'PRAGMA wal_checkpoint(TRUNCATE);'"

사용
----
    python migrations/sync_homepage_tables.py <원본.db> --dry-run
    python migrations/sync_homepage_tables.py <원본.db>
    python migrations/sync_homepage_tables.py <원본.db> --prune-orphans
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
TARGET_DB = os.path.join(BASE, "instance", "bist.db")
BACKUP_DIR = os.path.join(BASE, "instance", "backup")

# 홈페이지가 소유하는 테이블. bist_reco_schema.py 의 PROTECTED 와 같아야 한다.
HOMEPAGE_TABLES = (
    "members", "publications", "publication_authors", "serials",
    "teams", "team_memberships", "member_history", "member_links",
    "news_posts", "news_photos", "newsletters", "projects",
    "project_members", "allowed_emails", "app_state", "audit_entries",
)

# 추천 시스템이 실제로 읽는 것 — 이게 비면 파이프라인이 동작하지 않는다
CRITICAL_TABLES = (
    "members", "publications", "publication_authors", "teams", "team_memberships",
)

# 핵심 테이블이 이 비율 넘게 줄면 원본을 의심하고 중단한다.
# 홈페이지에서 구성원이 몇 명 빠지는 정상 변동은 통과시키되, 개발용 빈 DB 를
# 잘못 지정한 경우는 잡아내는 수준.
SHRINK_TOLERANCE = 0.30

BAD_MARK = "[중단]"

# 홈페이지 행을 참조하는 reco_ 테이블 (고아 검사용)
# (reco 테이블, 참조 컬럼, 대상 테이블)
REFERENCES = (
    ("reco_member_settings", "member_id", "members"),
    ("reco_member_papers", "member_id", "members"),
    ("reco_member_papers", "publication_id", "publications"),
    ("reco_topics", "member_id", "members"),
    ("reco_clusters", "member_id", "members"),
    ("reco_notes", "member_id", "members"),
    ("reco_custom_keywords", "member_id", "members"),
    ("reco_custom_journals", "member_id", "members"),
    ("reco_target_journals", "member_id", "members"),
    ("reco_author_follows", "member_id", "members"),
    ("reco_email_logs", "member_id", "members"),
    ("reco_recommendations", "member_id", "members"),
    ("reco_publication_meta", "publication_id", "publications"),
    ("reco_publication_keywords", "publication_id", "publications"),
)


def tables_of(conn) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def row_count(conn, table) -> int:
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return -1


def backup_target() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(
        BACKUP_DIR,
        f"bist.db.before_sync.{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    src, dst = sqlite3.connect(TARGET_DB), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def check_orphans(conn) -> list[tuple[str, str, str, int]]:
    """홈페이지 행이 사라져 갈 곳을 잃은 reco_ 행을 센다."""
    present = tables_of(conn)
    out = []
    for reco, col, target in REFERENCES:
        if reco not in present or target not in present:
            continue
        try:
            n = conn.execute(
                f'SELECT COUNT(*) FROM "{reco}" r'
                f' WHERE r."{col}" IS NOT NULL'
                f'   AND NOT EXISTS (SELECT 1 FROM "{target}" t WHERE t.id = r."{col}")'
            ).fetchone()[0]
        except sqlite3.Error:
            continue
        if n:
            out.append((reco, col, target, n))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="홈페이지 DB → 로컬 bist.db 단방향 복제 (reco_* 보존)"
    )
    ap.add_argument("source", help="홈페이지 bist.db 사본 경로 (scp 로 받아온 것)")
    ap.add_argument("--dry-run", action="store_true", help="변경 없이 비교만")
    ap.add_argument(
        "--prune-orphans", action="store_true",
        help="홈페이지에서 사라진 행을 참조하는 reco_ 행을 삭제 (기본은 보고만)",
    )
    ap.add_argument("--target", default=TARGET_DB, help="갱신할 DB (기본 instance/bist.db)")
    ap.add_argument(
        "--force", action="store_true",
        help="핵심 테이블이 크게 줄어도 진행 (원본이 맞다고 확신할 때만)",
    )
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(f"오류: 원본이 없습니다 — {args.source}", file=sys.stderr)
        return 1
    if not os.path.exists(args.target):
        print(f"오류: 대상이 없습니다 — {args.target}", file=sys.stderr)
        return 1
    if os.path.abspath(args.source) == os.path.abspath(args.target):
        print("오류: 원본과 대상이 같은 파일입니다", file=sys.stderr)
        return 1

    print("=" * 72)
    print("홈페이지 테이블 복제" + ("  [DRY RUN]" if args.dry_run else ""))
    print("=" * 72)
    print(f"원본: {args.source}")
    print(f"대상: {args.target}")

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    dst = sqlite3.connect(args.target)

    try:
        src_tables = tables_of(src)
        dst_tables = tables_of(dst)

        # --- 원본이 홈페이지 DB 가 맞는지 확인 ---
        missing_critical = [t for t in CRITICAL_TABLES if t not in src_tables]
        if missing_critical:
            print(
                f"\n중단: 원본에 {', '.join(missing_critical)} 이(가) 없습니다. "
                "홈페이지 DB 가 맞는지 확인하세요.",
                file=sys.stderr,
            )
            return 2

        if any(t.startswith("reco_") for t in src_tables):
            print(
                "\n[!] 원본에 reco_ 테이블이 있습니다. 이 스크립트는 홈페이지 "
                "테이블만 가져오므로 원본의 reco_ 는 무시됩니다."
            )

        # --- 비교 ---
        print("\n[1] 비교 (원본 → 대상)")
        print(f"      {'테이블':26s} {'원본':>8s} {'현재':>8s}   변화")
        plan = []
        for t in HOMEPAGE_TABLES:
            if t not in src_tables:
                print(f"      {t:26s} {'없음':>8s} {row_count(dst, t) if t in dst_tables else '-':>8} "
                      "  원본에 없어 건너뜀")
                continue
            s_n = row_count(src, t)
            d_n = row_count(dst, t) if t in dst_tables else -1
            delta = "신규" if d_n < 0 else ("동일" if s_n == d_n else f"{d_n} → {s_n}")
            print(f"      {t:26s} {s_n:8d} {d_n if d_n >= 0 else '없음':>8}   {delta}")
            plan.append(t)

        preserved = sorted(t for t in dst_tables if t.startswith("reco_"))
        print(f"\n[2] 보존되는 reco_ 테이블 {len(preserved)}개")
        total_reco = sum(max(row_count(dst, t), 0) for t in preserved)
        print(f"      총 {total_reco}행 — 이 스크립트는 건드리지 않습니다")

        # --- 안전장치: 엉뚱한 원본으로 데이터를 날리지 않게 ---
        #
        # 개발용 bist.db 를 원본으로 잘못 지정하는 사고가 실제로 가능하다.
        # LabHomePage 프로젝트 폴더의 bist.db 는 publications 가 0행인 로컬
        # 개발본이라, 그걸로 덮으면 추천 시스템의 입력이 통째로 사라진다.
        # 핵심 테이블이 크게 줄어드는 경우 기본적으로 거부한다.
        print("\n[2-1] 안전 점검 (핵심 테이블 급감 여부)")
        danger = []
        for t in CRITICAL_TABLES:
            if t not in src_tables:
                continue
            s_n, d_n = row_count(src, t), row_count(dst, t) if t in dst_tables else 0
            if d_n > 0 and s_n < d_n * (1 - SHRINK_TOLERANCE):
                danger.append((t, d_n, s_n))

        if danger:
            print(f"      {BAD_MARK} 핵심 테이블이 크게 줄어듭니다")
            for t, d_n, s_n in danger:
                pct = (1 - s_n / d_n) * 100
                print(f"        {t:24s} {d_n} → {s_n}  ({pct:.0f}% 감소)")
            print(
                "\n      원본이 홈페이지 **운영** DB 가 맞는지 확인하세요.\n"
                "      개발용 사본(예: LabHomePage 프로젝트 폴더의 bist.db)은\n"
                "      publications 가 비어 있어 추천 입력이 사라집니다.\n"
                "      운영 DB 는 /srv/bist/bist.db 입니다.\n"
                "        scp root@158.247.199.32:/srv/bist/bist.db <로컬경로>"
            )
            if not args.force:
                print("\n중단했습니다. 의도한 것이라면 --force 를 붙이세요.",
                      file=sys.stderr)
                return 4
            print("      --force 지정됨 — 그대로 진행합니다")
        else:
            print("      이상 없음")

        if args.dry_run:
            print("\n[3] 고아 예상 — 실제 실행 후에만 정확히 셀 수 있습니다")
            print("\nDRY RUN 종료 — 변경 없음.")
            return 0

        # --- 백업 ---
        print(f"\n[3] 백업 -> {backup_target()}")

        # --- 복제 ---
        print("\n[4] 복제")
        dst.execute("PRAGMA foreign_keys=OFF")
        dst.execute("BEGIN")
        copied = {}
        for t in plan:
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()[0]

            cur = src.execute(f'SELECT * FROM "{t}"')
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()

            dst.execute(f'DROP TABLE IF EXISTS "{t}"')
            dst.execute(ddl)
            if rows:
                collist = ", ".join(f'"{c}"' for c in cols)
                ph = ", ".join("?" * len(cols))
                dst.executemany(
                    f'INSERT INTO "{t}" ({collist}) VALUES ({ph})', rows
                )
            # 인덱스도 원본을 따라간다
            for (isql,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?"
                " AND sql IS NOT NULL", (t,)
            ):
                try:
                    dst.execute(isql)
                except sqlite3.Error as exc:
                    print(f"      [!] {t} 인덱스 생성 실패: {exc}")
            copied[t] = len(rows)
            print(f"      {t:26s} {len(rows):8d}행")

        # --- 검증: 행 수가 원본과 같아야 커밋 ---
        print("\n[5] 검증")
        mismatch = []
        for t, n in copied.items():
            got = row_count(dst, t)
            if got != n:
                mismatch.append((t, n, got))
        if mismatch:
            dst.execute("ROLLBACK")
            print("      행 수 불일치 — 롤백했습니다", file=sys.stderr)
            for t, want, got in mismatch:
                print(f"        {t}: 기대 {want}, 실제 {got}", file=sys.stderr)
            return 3
        print(f"      {len(copied)}개 테이블 행 수 일치")

        dst.execute("COMMIT")

        # --- 고아 검사 ---
        print("\n[6] 고아 검사 — 홈페이지에서 사라진 행을 참조하는 reco_ 행")
        orphans = check_orphans(dst)
        if not orphans:
            print("      없음")
        else:
            for reco, col, target, n in orphans:
                print(f"      {reco}.{col} → {target}  {n}행")
            if args.prune_orphans:
                dst.execute("BEGIN")
                removed = 0
                for reco, col, target, _n in orphans:
                    cur = dst.execute(
                        f'DELETE FROM "{reco}"'
                        f' WHERE "{col}" IS NOT NULL'
                        f'   AND NOT EXISTS (SELECT 1 FROM "{target}" t'
                        f'                   WHERE t.id = "{reco}"."{col}")'
                    )
                    removed += cur.rowcount
                dst.execute("COMMIT")
                print(f"      --prune-orphans: {removed}행 삭제")
            else:
                print(
                    "      보고만 했습니다. 삭제하려면 --prune-orphans 를 쓰세요.\n"
                    "      (추천 이력이 딸려 사라지므로 내용을 확인한 뒤 실행하십시오)"
                )

        print("\n완료.")
        return 0

    finally:
        dst.close()
        src.close()


if __name__ == "__main__":
    sys.exit(main())
