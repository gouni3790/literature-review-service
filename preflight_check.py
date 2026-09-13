"""서버 반영 전 진단 — 무엇을 해야 하는지 알려 준다. 아무것도 변경하지 않는다.

서버의 literature_v2 폴더에 두고 실행한다.

    python preflight_check.py

확인하는 것
    1. DB 파일 위치와 스키마 세대 (구 litreview.db 인지, 신 bist.db 인지)
    2. 적용된 마이그레이션과 남은 마이그레이션
    3. 저널 발굴 파이프라인의 입력(주제·임베딩) 준비 상태
    4. Web_server.py 연동 5가지 (WEB_SERVER_INTEGRATION.py 참조)
    5. .env 설정
"""

import os
import re
import sqlite3
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
INSTANCE = os.path.join(HERE, "instance")

OK, WARN, BAD = "  [OK]  ", "  [확인] ", "  [문제] "
todo: list[str] = []
blockers: list[str] = []


def tables(path):
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def count(path, table):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        con.close()


def cols(path, table):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


print("=" * 72)
print("서버 반영 전 진단")
print("=" * 72)
print(f"기준 폴더: {HERE}\n")

# ---------------------------------------------------------------- 1. DB
print("[1] DB 파일")
found = {}
for name in ("bist.db", "litreview.db", "reco_papers.db"):
    p = os.path.join(INSTANCE, name)
    if os.path.exists(p):
        found[name] = p
        print(f"{OK}{name:18s} {os.path.getsize(p)/1024/1024:8.2f} MB")
    else:
        print(f"       {name:18s} 없음")

main_db = found.get("bist.db") or found.get("litreview.db")
new_schema = False
if not main_db:
    print(f"{BAD}메인 DB를 찾지 못했습니다.")
    blockers.append("instance/ 에 DB가 없습니다")
else:
    t = tables(main_db) or set()
    new_schema = "reco_member_settings" in t and "members" in t
    old_schema = "researchers" in t and "reference_papers" in t
    print("\n[2] 스키마 세대")
    if new_schema:
        print(f"{OK}신(bist.db) 스키마 — members / reco_* 구조")
    elif old_schema:
        print(f"{BAD}구(litreview.db) 스키마 — researchers / reference_papers 구조")
        print("       이번 코드는 신 스키마 전용입니다.")
        blockers.append(
            "bist.db 전환이 선행되어야 합니다 "
            "(migrations/bist_reco_schema.py → bist_member_papers.py)"
        )
    else:
        print(f"{WARN}판별 불가 — 테이블 구성이 예상과 다릅니다.")
        blockers.append("스키마 판별 불가")

# ---------------------------------------------------------------- 3. 마이그레이션
if new_schema:
    print("\n[3] 마이그레이션 상태")
    moved = (
        "reco_collected_papers", "reco_author_papers", "reco_interest_papers",
        "reco_paper_references", "reco_batch_logs", "reco_search_queries",
    )
    still_in_main = [x for x in moved if x in t]
    papers_path = found.get("reco_papers.db")

    if papers_path and not still_in_main:
        print(f"{OK}split_reco_papers_db.py — 적용됨")
    else:
        print(f"{WARN}split_reco_papers_db.py — 미적용")
        todo.append("python migrations/split_reco_papers_db.py")

    if papers_path:
        n = count(papers_path, "reco_paper_references")
        if n > 0:
            print(f"{OK}참고문헌 — {n}건")
        else:
            print(f"{WARN}참고문헌 0건 — familiarity 가 전부 0이 되어 "
                  "저널 발굴의 Novelty 게이트가 무력해집니다")
            todo.append("python migrations/import_paper_references.py  (구 DB 있을 때)")

    for tbl, script in (
        ("reco_journal_recommendations", "add_journal_recommendations.py"),
        ("reco_api_usage", "add_api_usage.py"),
    ):
        if tbl in t:
            print(f"{OK}{script} — 적용됨 ({count(main_db, tbl)}행)")
        else:
            print(f"{WARN}{script} — 미적용")
            todo.append(f"python migrations/{script}")

    rec_cols = cols(main_db, "reco_recommendations")
    if "summary_source" in rec_cols:
        print(f"{OK}add_summary_source.py — 적용됨")
    else:
        print(f"{WARN}add_summary_source.py — 미적용")
        todo.append("python migrations/add_summary_source.py")

    if "reco_journal_metrics" in t:
        print(f"{WARN}reco_journal_metrics — 미사용 테이블이 남아 있음")
        todo.append("python migrations/drop_journal_metrics.py")

    # ------------------------------------------------ 4. 파이프라인 입력
    print("\n[4] 저널 발굴 파이프라인 입력")
    for tbl, label, need in (
        ("reco_member_papers", "본인 논문", True),
        ("reco_topics", "연구 주제", True),
        ("reco_clusters", "클러스터", False),
        ("reco_target_journals", "수집 타겟 저널", False),
    ):
        n = count(main_db, tbl)
        mark = OK if n > 0 else (BAD if need else WARN)
        print(f"{mark}{label:14s} {n if n >= 0 else '테이블 없음'}행")
        if need and n <= 0:
            blockers.append(f"{label}({tbl})이 비어 있어 파이프라인을 돌릴 수 없습니다")

    con = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    try:
        emb = con.execute(
            "SELECT COUNT(*) FROM reco_member_papers WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        vec = con.execute(
            "SELECT COUNT(*) FROM reco_topics WHERE representative_vector IS NOT NULL"
        ).fetchone()[0]
        print(f"{OK if emb else WARN}본인 논문 임베딩 {emb}건")
        print(f"{OK if vec else WARN}대표벡터 보유 주제 {vec}개 "
              "(저널 발굴은 이 주제들만 처리)")
        if not emb or not vec:
            todo.append("프로필 구축 선행 — 임베딩 → 클러스터링 → 주제 생성")
    except sqlite3.Error:
        pass
    finally:
        con.close()

# ---------------------------------------------------------------- 5. Web_server.py
print("\n[5] Web_server.py 연동  (자세한 내용은 WEB_SERVER_INTEGRATION.py)")
parent = os.path.dirname(HERE)
ws = os.path.join(parent, "Web_server.py")
if not os.path.exists(ws):
    print(f"{WARN}Web_server.py 를 찾지 못했습니다: {ws}")
    todo.append("WEB_SERVER_INTEGRATION.py 의 [1]~[5] 를 직접 확인하세요")
else:
    src = open(ws, encoding="utf-8", errors="replace").read()

    # [1][2] ATTACH 초기화
    if re.search(r"init_papers_db\s*\(\s*app\s*\)", src):
        print(f"{OK}[2] init_papers_db(app) 호출 있음")
    else:
        print(f"{BAD}[2] init_papers_db(app) 호출 없음")
        print("       → papers 스키마가 붙지 않아 수집물 접근이 전부 실패합니다")
        blockers.append(
            "Web_server.py 의 db.init_app(app) 다음 줄에 init_papers_db(app) 추가"
        )

    # [3] 템플릿 로더
    if "ChoiceLoader" in src or "jinja_loader" in src:
        print(f"{OK}[3] 템플릿 추가 로더 설정 있음")
    else:
        lit_tpl = os.path.join(HERE, "templates", "home_v3.html")
        root_tpl = os.path.join(parent, "templates", "home_v3.html")
        if os.path.exists(root_tpl):
            print(f"{WARN}[3] 템플릿을 루트로 복사해 둔 상태 "
                  "(ChoiceLoader 방식을 권장 — 중복 관리 불필요)")
        else:
            print(f"{BAD}[3] literature_v2/templates 가 연결돼 있지 않음")
            print("       → home_v3.html / mypage.html 을 찾지 못해 500 이 납니다")
            blockers.append(
                "Web_server.py 에 ChoiceLoader 로 literature_v2/templates 추가"
            )

    # [4] 페이지 라우트
    m = re.search(r"render_template\(\s*['\"](home_v\d\.html)['\"]", src)
    if m:
        tpl = m.group(1)
        if tpl == "home_v3.html":
            print(f"{OK}[4] /home 이 home_v3.html 을 서빙")
        else:
            print(f"{BAD}[4] /home 이 {tpl} 을 서빙 — 이번 UI 변경이 안 보입니다")
            blockers.append(f"/home 을 home_v3.html 로 변경 (현재 {tpl})")
    else:
        print(f"{WARN}[4] /home 라우트를 찾지 못했습니다")

    for path, pat, label in (
        ("/mypage", r"['\"]/mypage['\"]", "마이페이지"),
        ("/litreview/register", r"['\"]/litreview/register['\"]", "회원가입"),
    ):
        if re.search(pat, src):
            print(f"{OK}[4] {path} 라우트 있음")
        else:
            print(f"{WARN}[4] {path} 라우트 없음 ({label})")
            todo.append(f"Web_server.py 에 {path} 라우트 추가")

    # BIST_Office 보존 확인
    if os.path.isdir(os.path.join(HERE, "BIST_Office")):
        print(f"{OK}BIST_Office/ 보존됨")
    else:
        print(f"{WARN}BIST_Office/ 가 없습니다 — VR 진입(/bist-office/)이 깨집니다")

# ---------------------------------------------------------------- 6. .env
print("\n[6] .env")
env = os.path.join(HERE, ".env")
if not os.path.exists(env):
    print(f"{BAD}.env 없음 (배포 패키지에 포함하지 않았습니다 — 서버 것 유지)")
    blockers.append(".env 가 없습니다")
else:
    txt = open(env, encoding="utf-8", errors="replace").read()

    dburl = re.search(r"^DATABASE_URL=(.+)$", txt, re.M)
    if dburl:
        val = dburl.group(1).strip()
        if "litreview.db" in val:
            print(f"{BAD}DATABASE_URL 이 구 DB 를 가리킴 — {val}")
            blockers.append("DATABASE_URL 을 bist.db 절대경로로 변경")
        else:
            print(f"{OK}DATABASE_URL = {val}")
    else:
        print(f"{BAD}DATABASE_URL 미설정 — Web_server.py 기본값(litreview.db)이 쓰입니다")
        blockers.append("DATABASE_URL 을 bist.db 절대경로로 명시")

    secret = re.search(r"^FLASK_SECRET_KEY=(.+)$", txt, re.M)
    if secret and secret.group(1).strip():
        print(f"{OK}FLASK_SECRET_KEY 설정됨")
    else:
        print(f"{BAD}FLASK_SECRET_KEY 미설정 — 'dev-secret-key' 가 쓰여 "
              "세션 쿠키 위조가 가능합니다")
        blockers.append("FLASK_SECRET_KEY 를 랜덤 값으로 설정")

    if "PAPERS_DATABASE_PATH" in txt:
        print(f"{OK}PAPERS_DATABASE_PATH 설정됨")
    else:
        print(f"{OK}PAPERS_DATABASE_PATH 미설정 — instance/reco_papers.db 기본값 사용")

    for k in ("SCOPUS_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        print(f"{OK if re.search(rf'^{k}=.+$', txt, re.M) else WARN}{k}")

# ---------------------------------------------------------------- 요약
print()
print("=" * 72)
if blockers:
    print(f"먼저 해결해야 할 것 {len(blockers)}건")
    for i, b in enumerate(blockers, 1):
        print(f"  {i}. {b}")
else:
    print("차단 요소 없음")

if todo:
    print(f"\n실행할 작업 {len(todo)}건 (위에서 아래 순서)")
    for i, s in enumerate(todo, 1):
        print(f"  {i}. {s}")
else:
    print("\n실행할 작업 없음")
print("=" * 72)
