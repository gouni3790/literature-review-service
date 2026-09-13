"""연구원별 저자 키워드(author keywords) 분포 분석.

윤성민(researcher id=1)을 제외하고 scopus_id가 있는 연구원들의
모든 reference_papers에 연결된 paper_keywords(저자 지정 키워드)를 모아
빈도 분포를 출력한다.

- 키워드 정규화: 양 끝 공백 제거, 대소문자 통일(소문자), 내부 공백 단일화
- 동일 논문 내 중복 키워드는 1회로 카운트
- 저자 키워드 자체를 신뢰하므로 별도 불용어 처리 없음

실행:
    python extract_author_keywords.py [--top N] [--min-count M]
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "instance",
    "litreview.db",
)

EXCLUDED_RESEARCHER_NAME_KEYS = ("윤성민", "sungmin yoon", "yoon, sungmin")

WS_RE = re.compile(r"\s+")


def normalize(keyword: str) -> str:
    """키워드 표기 정규화: trim, 내부 공백 단일화, 소문자."""
    if not keyword:
        return ""
    return WS_RE.sub(" ", keyword.strip()).lower()


def fetch_target_researchers(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """scopus_id가 있고 윤성민이 아닌 연구원 목록."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, scopus_id FROM researchers "
        "WHERE scopus_id IS NOT NULL AND scopus_id != '' "
        "ORDER BY id"
    )
    out = []
    for rid, name, sid in cur.fetchall():
        name_l = (name or "").lower()
        if any(key in name_l for key in EXCLUDED_RESEARCHER_NAME_KEYS):
            continue
        if rid == 1:  # 윤성민 안전장치
            continue
        out.append((rid, name, sid))
    return out


def fetch_keywords_per_paper(
    conn: sqlite3.Connection, researcher_id: int
) -> list[set[str]]:
    """연구원의 논문별 키워드 집합 리스트. 논문 내 중복은 set으로 제거."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rp.id, pk.keyword
        FROM reference_papers rp
        LEFT JOIN paper_keywords pk ON pk.paper_id = rp.id
        WHERE rp.researcher_id = ?
        ORDER BY rp.id
        """,
        (researcher_id,),
    )
    by_paper: dict[int, set[str]] = {}
    for paper_id, keyword in cur.fetchall():
        by_paper.setdefault(paper_id, set())
        if keyword:
            kw = normalize(keyword)
            if kw:
                by_paper[paper_id].add(kw)
    return list(by_paper.values())


def best_display_form(conn: sqlite3.Connection, normalized_kw: str) -> str:
    """정규화된 키워드에 해당하는 원문 표기 중 가장 흔한 것을 골라 반환."""
    cur = conn.cursor()
    cur.execute("SELECT keyword FROM paper_keywords")
    forms = Counter()
    for (raw,) in cur.fetchall():
        if normalize(raw) == normalized_kw:
            forms[raw.strip()] += 1
    if not forms:
        return normalized_kw
    return forms.most_common(1)[0][0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=20, help="연구원별 상위 키워드 개수 (기본 20)")
    parser.add_argument("--min-count", type=int, default=1, help="최소 등장 횟수 컷오프 (기본 1)")
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite DB 경로 (기본 {DB_PATH})")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERROR] DB not found: {args.db}")
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    researchers = fetch_target_researchers(conn)

    # 원문 표기 룩업을 한 번만 수행 (전체 paper_keywords를 1회 스캔)
    cur = conn.cursor()
    cur.execute("SELECT keyword FROM paper_keywords")
    display_lookup: dict[str, Counter] = {}
    for (raw,) in cur.fetchall():
        if not raw:
            continue
        norm = normalize(raw)
        if not norm:
            continue
        display_lookup.setdefault(norm, Counter())[raw.strip()] += 1

    def display(norm_kw: str) -> str:
        forms = display_lookup.get(norm_kw)
        return forms.most_common(1)[0][0] if forms else norm_kw

    print("=" * 78)
    print(
        f"연구원별 저자 키워드 분포 (윤성민 제외, scopus_id 보유 {len(researchers)}명)"
    )
    print(f"DB: {args.db}")
    print(f"옵션: top={args.top}, min_count={args.min_count}")
    print("=" * 78)

    for rid, name, sid in researchers:
        per_paper = fetch_keywords_per_paper(conn, rid)
        total_papers = len(per_paper)
        papers_with_kw = sum(1 for s in per_paper if s)

        counter: Counter = Counter()
        for kw_set in per_paper:
            counter.update(kw_set)

        print()
        print("-" * 78)
        print(f"[{rid}] {name}  (scopus_id={sid})")
        print(
            f"    총 논문 {total_papers}편 / 키워드 보유 {papers_with_kw}편 / "
            f"고유 키워드 {len(counter)}개 / 총 등장 {sum(counter.values())}회"
        )

        filtered = [(kw, c) for kw, c in counter.most_common() if c >= args.min_count]
        top = filtered[: args.top]

        if not top:
            print("    ※ 표시할 키워드 없음")
            continue

        width = max(len(display(kw)) for kw, _ in top)
        for kw, freq in top:
            share = freq / total_papers * 100 if total_papers else 0
            print(f"    {display(kw).ljust(width)}  {freq:3d}  ({share:5.1f}%)")

    conn.close()


if __name__ == "__main__":
    main()
