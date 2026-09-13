# -*- coding: utf-8 -*-
"""SQLite DB를 엑셀 파일로 내보낸다 — 테이블 하나당 시트 하나.

DB를 눈으로 훑어보기 위한 도구. 원본 DB는 읽기만 한다.

사용:
    python tools/db_to_excel.py                      # instance/bist.db → bist_db.xlsx
    python tools/db_to_excel.py <db경로> [출력.xlsx]
    python tools/db_to_excel.py <db경로> --max-rows 500
"""

import sqlite3
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# 엑셀 셀 한도는 32,767자. 임베딩 같은 초장문은 잘라 넣는다.
MAX_CELL = 800
# 임베딩처럼 사람이 볼 의미가 없는 컬럼은 길이만 표시
OPAQUE_COLS = {"embedding", "representative_vector", "centroid", "password_hash"}

HEADER_FILL = PatternFill("solid", fgColor="1F6F5C")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)


def cell_value(col: str, v):
    if v is None:
        return ""
    if col in OPAQUE_COLS:
        n = len(str(v))
        return f"<{col} {n}자 생략>" if n else ""
    s = str(v)
    if len(s) > MAX_CELL:
        return s[:MAX_CELL] + f"… (총 {len(s)}자)"
    return s


def export(db_path: str, out_path: str, max_rows: int | None = None) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    tables = [
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]

    wb = Workbook()
    wb.remove(wb.active)

    # 첫 시트: 목차 (테이블 목록 + 행 수)
    toc = wb.create_sheet("0_목차")
    toc.append(["테이블", "행 수", "컬럼 수", "비고"])
    summary = []

    for t in tables:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        cols = [c["name"] for c in con.execute(f'PRAGMA table_info("{t}")')]
        note = "홈페이지 소유" if not t.startswith("reco_") else "추천 서비스"
        if n == 0:
            note += " · 비어 있음"
        summary.append((t, n, len(cols), note))
        toc.append([t, n, len(cols), note])

    for t, n, ncol, note in summary:
        # 시트명은 31자 제한 + 일부 문자 불가
        name = t[:31].replace("/", "_").replace("\\", "_")
        ws = wb.create_sheet(name)
        cols = [c["name"] for c in con.execute(f'PRAGMA table_info("{t}")')]
        ws.append(cols)

        sql = f'SELECT * FROM "{t}"'
        if max_rows:
            sql += f" LIMIT {int(max_rows)}"
        written = 0
        for row in con.execute(sql):
            ws.append([cell_value(c, row[c]) for c in cols])
            written += 1

        # 헤더 서식 + 고정
        for i in range(1, len(cols) + 1):
            c = ws.cell(row=1, column=i)
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(vertical="center")
            # 컬럼 폭: 헤더 길이와 첫 20행 값 길이를 보고 대략 맞춤
            width = len(cols[i - 1]) + 2
            for r in range(2, min(written + 2, 22)):
                width = max(width, min(len(str(ws.cell(row=r, column=i).value or "")) + 2, 60))
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A2"
        print(f"  {t:<32} {written:>6}행 / {len(cols):>2}컬럼")

    # 목차 서식
    for i in range(1, 5):
        c = toc.cell(row=1, column=i)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
    for i, w in enumerate((34, 10, 10, 26), start=1):
        toc.column_dimensions[get_column_letter(i)].width = w
    toc.freeze_panes = "A2"

    con.close()
    wb.save(out_path)
    print(f"\n저장: {out_path}")
    print(f"시트 {len(tables) + 1}개 (목차 1 + 테이블 {len(tables)})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(__file__).resolve().parent.parent
    db = args[0] if args else str(root / "instance" / "bist.db")
    out = args[1] if len(args) > 1 else str(Path(db).with_suffix("").name + "_view.xlsx")
    mr = None
    if "--max-rows" in sys.argv:
        mr = int(sys.argv[sys.argv.index("--max-rows") + 1])
    print(f"읽는 DB: {db} (읽기 전용)")
    export(db, out, mr)
