"""DB(litreview.db) → bist_data.ttl 생성기.

스키마: bist_ontology.ttl
인스턴스 그래프:
    :BISTLab a bist:ResearchLab ;
              bist:hasMember :Researcher_<id> ; ...

    :Researcher_<id> a bist:Researcher ;
                     foaf:name "..." ;
                     bist:scopusId "..." ;
                     bist:researcherType "A" ;
                     bist:inGroup :Group_HVAC ;
                     bist:paperCount 18 ;
                     bist:hasKeyword :KW_<slug>, ... .

    :KW_<slug> a bist:ResearchKeyword ;
               rdfs:label "Digital twins"@en ;
               bist:keywordRaw "Digital twins" .

    [] a bist:KeywordUsage ;
       bist:usageBy :Researcher_<id> ;
       bist:usageOf :KW_<slug> ;
       bist:frequency 6 .

실행:
    python build_data_ttl.py
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "instance", "litreview.db")
DB = os.path.abspath(DB)
OUT = os.path.join(HERE, "bist_data.ttl")


# IRI 안전한 슬러그로
_NON_ASCII = re.compile(r"[^A-Za-z0-9_]")


def slug(text: str, max_len: int = 60) -> str:
    """임의 문자열 → IRI 로컬네임 슬러그."""
    s = re.sub(r"\s+", "_", (text or "").strip())
    s = _NON_ASCII.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "x"
    return s[:max_len]


def ttl_string(s: str) -> str:
    """문자열 리터럴 → TTL escape (큰따옴표 안)."""
    if s is None:
        return '""'
    return (
        '"'
        + s.replace("\\", "\\\\")
           .replace('"', '\\"')
           .replace("\n", "\\n")
           .replace("\r", "")
           .replace("\t", " ")
        + '"'
    )


def display_name(raw: str) -> str:
    """'구자범 (jabeom koo)' → '구자범 (Koo Jabeom)' / 괄호 없으면 원본."""
    if not raw:
        return ""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", raw)
    if not m:
        return raw.strip()
    ko, en = m.group(1).strip(), m.group(2).strip()
    parts = en.split()
    if len(parts) >= 2:
        last = parts[-1].capitalize()
        rest = " ".join(p.capitalize() for p in parts[:-1])
        return f"{ko} ({last} {rest})"
    return f"{ko} ({en.capitalize()})"


def main() -> None:
    if not os.path.exists(DB):
        print(f"[ERROR] DB not found: {DB}")
        sys.exit(1)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    out: list[str] = []
    p = out.append

    # ---- header ----
    p(f"# bist_data.ttl — generated {date.today().isoformat()} from litreview.db")
    p("# 스키마: bist_ontology.ttl")
    p("")
    p("@prefix bist:    <http://example.org/bist/onto#> .")
    p("@prefix :        <http://example.org/bist/data#> .")
    p("@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .")
    p("@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .")
    p("@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .")
    p("@prefix foaf:    <http://xmlns.com/foaf/0.1/> .")
    p("")

    # ---- BIST Lab + 그룹 ----
    cur.execute(
        'SELECT DISTINCT "group" FROM researchers '
        'WHERE "group" IS NOT NULL AND "group" != \'\' '
        'ORDER BY "group"'
    )
    groups = [r[0] for r in cur.fetchall()]

    p("# ===== ResearchLab =====")
    p(":BISTLab a bist:ResearchLab ;")
    p('    rdfs:label "BIST Lab"@en, "BIST 연구실"@ko .')
    p("")

    p("# ===== Research Groups =====")
    for g in groups:
        p(f":Group_{slug(g)} a bist:ResearchGroup ;")
        p(f"    rdfs:label {ttl_string(g)} .")
        p("")

    # ---- Researchers ----
    cur.execute(
        'SELECT id, name, scopus_id, "group", researcher_type, '
        '   (SELECT COUNT(*) FROM reference_papers WHERE researcher_id=researchers.id) '
        'FROM researchers ORDER BY id'
    )
    researchers = cur.fetchall()

    # hasMember (lab → researchers 한 줄로 묶음)
    member_iris = ", ".join(f":Researcher_{rid}" for rid, *_ in researchers)
    p("# ===== Lab → Members =====")
    p(f":BISTLab bist:hasMember {member_iris} .")
    p("")

    p("# ===== Researchers =====")
    for rid, name, sid, grp, rtype, pcount in researchers:
        disp = display_name(name)
        p(f":Researcher_{rid} a bist:Researcher ;")
        p(f"    foaf:name {ttl_string(disp)} ;")
        p(f"    rdfs:label {ttl_string(disp)}@ko ;")
        p(f"    bist:memberOf :BISTLab ;")
        if sid:
            p(f"    bist:scopusId {ttl_string(sid)} ;")
        if rtype:
            p(f"    bist:researcherType {ttl_string(rtype)} ;")
        if grp:
            p(f"    bist:inGroup :Group_{slug(grp)} ;")
        p(f"    bist:paperCount {int(pcount or 0)} .")
        p("")

    # ---- Keywords (canonical 노드 모음) + per-researcher 사용 ----
    # 모든 unique keyword 수집
    cur.execute(
        "SELECT DISTINCT pk.keyword FROM paper_keywords pk "
        "WHERE pk.keyword IS NOT NULL AND pk.keyword != '' "
        "ORDER BY pk.keyword"
    )
    all_keywords = [r[0] for r in cur.fetchall()]

    # slug 충돌 회피
    slug_map: dict[str, str] = {}
    used = set()
    for kw in all_keywords:
        s = slug(kw)
        if s in used:
            i = 2
            while f"{s}_{i}" in used:
                i += 1
            s = f"{s}_{i}"
        slug_map[kw] = s
        used.add(s)

    p("# ===== Research Keywords (raw) =====")
    for kw in all_keywords:
        s = slug_map[kw]
        p(f":KW_{s} a bist:ResearchKeyword ;")
        p(f"    rdfs:label {ttl_string(kw)}@en ;")
        p(f"    bist:keywordRaw {ttl_string(kw)} .")
        p("")

    # 연구원별 키워드 사용 빈도 (논문 단위 distinct)
    cur.execute(
        """
        SELECT rp.researcher_id, pk.keyword, COUNT(DISTINCT rp.id)
        FROM paper_keywords pk
        JOIN reference_papers rp ON rp.id = pk.paper_id
        WHERE pk.keyword IS NOT NULL AND pk.keyword != ''
        GROUP BY rp.researcher_id, pk.keyword
        ORDER BY rp.researcher_id, 3 DESC
        """
    )
    rows = cur.fetchall()

    # researcher → list of (kw_iri)
    kw_by_researcher: dict[int, list[str]] = {}
    for rid, kw, _cnt in rows:
        kw_by_researcher.setdefault(rid, []).append(slug_map[kw])

    p("# ===== Researcher → hasKeyword (direct edges) =====")
    for rid, kws in kw_by_researcher.items():
        if not kws:
            continue
        chunk = 5
        # objectList 한 개를 멀티라인으로 — 줄 사이 ',' 로 잇고 마지막에 '.'
        p(f":Researcher_{rid} bist:hasKeyword")
        for i in range(0, len(kws), chunk):
            piece = ", ".join(f":KW_{k}" for k in kws[i : i + chunk])
            is_last_chunk = (i + chunk >= len(kws))
            sep = " ." if is_last_chunk else ","
            p(f"    {piece}{sep}")
        p("")

    p("# ===== KeywordUsage (frequency) =====")
    for rid, kw, cnt in rows:
        p(f"[] a bist:KeywordUsage ;")
        p(f"    bist:usageBy :Researcher_{rid} ;")
        p(f"    bist:usageOf :KW_{slug_map[kw]} ;")
        p(f"    bist:frequency {int(cnt)} .")
    p("")

    conn.close()

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    # 통계
    print(f"  Researchers     : {len(researchers)}")
    print(f"  ResearchGroups  : {len(groups)}")
    print(f"  ResearchKeywords: {len(all_keywords)}")
    print(f"  KeywordUsage    : {len(rows)}")
    print(f"  → wrote {OUT}")


if __name__ == "__main__":
    main()
