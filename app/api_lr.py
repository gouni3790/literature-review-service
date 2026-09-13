"""UI-facing API routes — /api/lr/ prefix. 주제 기반 통합 구조."""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, session
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from app import db
from app.models import (
    CollectedPaper,
    JournalRecommendation,
    PaperKeyword,
    PaperRecommendation,
    ReferencePaper,
    Researcher,
    ResearchTopic,
    TargetJournal,
    TopicReferencePaper,
)

logger = logging.getLogger(__name__)

api_lr_bp = Blueprint("api_lr", __name__, url_prefix="/api/lr")


# =========================================================================
# Auth
# =========================================================================


@api_lr_bp.route("/auth/register", methods=["POST"])
def register():
    """회원가입 — 홈페이지에 등록된 구성원에게 로그인 비밀번호를 설정한다.

    이전에는 매칭되는 구성원이 없으면 members 에 새 행을 INSERT 했다. members 는
    연구실 홈페이지가 소유하는 테이블이라 이 앱이 회원을 만들면 안 되고, 실제로도
    name/group 이 읽기 전용 속성이라 AttributeError 로 죽고 있었다.

    이제는 **기존 구성원 매칭에 성공했을 때만** 가입을 받는다. 비밀번호는
    members 가 아니라 reco_member_settings 에 저장된다.

    이름 매칭 규칙:
      DB "윤성민 (sungmin yoon)" ← 입력 "윤성민" → 매칭 성공
      DB "Min Htet Myint"        ← 입력 "Min Htet Myint" → 매칭 성공
    """
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    pw = data.get("password") or ""

    if not name or not email:
        return jsonify({"success": False, "error": "이름과 이메일은 필수입니다."}), 400
    if len(pw) < 4:
        return jsonify({"success": False, "error": "비밀번호는 4자리 이상이어야 합니다."}), 400

    existing = _match_researcher(name)

    if existing is None:
        return jsonify({
            "success": False,
            "error": "연구실 홈페이지에 등록된 구성원이 아닙니다. "
                     "먼저 홈페이지에 구성원으로 등록된 뒤 다시 시도해 주세요.",
        }), 404

    if existing.password_hash:
        return jsonify({
            "success": False,
            "error": "이미 비밀번호가 설정된 계정입니다. 로그인해 주세요.",
        }), 409

    # 비밀번호·활성화는 reco_member_settings 에 기록한다 (members 는 건드리지 않음)
    st = existing.ensure_settings()
    st.password_hash = generate_password_hash(pw)
    st.is_active = True
    db.session.commit()

    session["researcher_id"] = existing.id
    logger.info("Researcher %d (%s) 비밀번호 설정 완료", existing.id, existing.name)
    return jsonify({
        "success": True,
        "id": existing.id,
        "name": existing.name,
        "matched": True,
    })


def _match_researcher(input_name: str) -> Researcher | None:
    """입력된 이름으로 구성원 매칭.

    Researcher.name 은 name_ko/name_en 을 합쳐 만드는 파이썬 속성이라 SQL에
    쓸 수 없다. 실제 컬럼(name_ko, name_en)을 직접 조회한다.

    매칭 순서: 한글 정확 → 영문 정확 → 한글 부분 → 영문 부분
    """
    q = (input_name or "").strip().lower()
    if not q:
        return None

    r = Researcher.query.filter(func.lower(Researcher.name_ko) == q).first()
    if r:
        return r

    r = Researcher.query.filter(func.lower(Researcher.name_en) == q).first()
    if r:
        return r

    for col in (Researcher.name_ko, Researcher.name_en):
        r = Researcher.query.filter(func.lower(col).contains(q)).first()
        if r:
            return r

    return None


@api_lr_bp.route("/auth/login", methods=["POST"])
def login():
    """로그인 — 이름 + 비밀번호."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    pw = data.get("password") or ""

    if not name or not pw:
        return jsonify({"success": False, "error": "이름과 비밀번호를 입력하세요."}), 400

    r = _match_researcher(name)
    if not r or not r.password_hash:
        return jsonify({"success": False, "error": "등록되지 않은 사용자입니다."}), 401
    if not check_password_hash(r.password_hash, pw):
        return jsonify({"success": False, "error": "비밀번호가 올바르지 않습니다."}), 401

    session["researcher_id"] = r.id
    logger.info("Login: researcher %d (%s)", r.id, r.name)
    return jsonify({
        "success": True,
        "id": r.id,
        "name": r.name,
        "group": r.group,
        "researcher_type": r.researcher_type,
    })


@api_lr_bp.route("/auth/me")
def auth_me():
    """현재 로그인 상태 조회."""
    rid = session.get("researcher_id")
    if not rid:
        return jsonify({"logged_in": False})

    r = db.session.get(Researcher, rid)
    if not r:
        session.pop("researcher_id", None)
        return jsonify({"logged_in": False})

    return jsonify({
        "logged_in": True,
        "researcher_id": r.id,
        "name": r.name,
        "email": r.email,
        "group": r.group,
        "researcher_type": r.researcher_type,
    })


@api_lr_bp.route("/auth/logout", methods=["POST"])
def logout():
    """로그아웃."""
    session.pop("researcher_id", None)
    return jsonify({"success": True})


# =========================================================================
# Lab Stats — 대시보드 집계
# =========================================================================


@api_lr_bp.route("/lab-stats")
def lab_stats():
    """랩 전체 통계."""
    return jsonify(_build_lab_dataset())


def _build_lab_dataset() -> dict:
    """랩 전체 통계 집계 (lab-stats API + 엑셀 export 공용).

    연구원별 데이터(researchers)는 각자의 논문을 그대로 담지만,
    랩 단위 집계(annual_output, top_venues, all_papers)는 공저 논문을
    scopus_id → doi → 제목 순 키로 중복 제거하여 논문 단위로 센다.
    """
    researchers_q = Researcher.query.order_by(Researcher.name_ko).all()

    _NAME_ALIAS = {"Syed Mostasim Hasnain Saif": "사이에드"}

    def _display_name(full_name):
        if full_name in _NAME_ALIAS:
            return _NAME_ALIAS[full_name]
        if "(" in full_name:
            return full_name.split("(")[0].strip()
        return full_name

    def _split_authors(s):
        if not s:
            return []
        sep = ";" if ";" in s else ","
        return [a.strip() for a in s.split(sep) if a.strip()]

    def _dedup_key(p):
        if p.scopus_id:
            return f"s:{p.scopus_id}"
        if p.doi:
            return f"d:{p.doi.lower()}"
        return "t:" + "".join((p.title or "").lower().split())

    researchers_data = {}
    unique_papers: dict[str, dict] = {}

    for r in researchers_q:
        papers = ReferencePaper.query.filter_by(researcher_id=r.id).all()
        display = _display_name(r.name)
        paper_list = []
        for p in papers:
            db_source = "Scopus" if p.scopus_id else "Manual"
            # 결과 목록 뱃지: 분류(SCIE/KCI) 우선, 없으면 Scopus/Manual
            label = p.classification or db_source
            paper_dict = {
                "id": p.id,
                "title": p.title,
                "authors": _split_authors(p.authors),
                "journal": p.journal,
                "classification": p.classification or "",
                "volume": p.volume or "",
                "publisher": p.publisher or "",
                "year": p.year,
                "citations": p.cited_by or 0,
                "cover_date": str(p.created_at.date()) if p.created_at else "",
                "doi": p.doi or "",
                "db_source": db_source,
                "source_type": p.source or "scopus",
                "badge": label,
                "status": p.status or "published",
            }
            paper_list.append(paper_dict)

            key = _dedup_key(p)
            u = unique_papers.get(key)
            if u is None:
                u = {**paper_dict, "researchers": [], "groups": []}
                unique_papers[key] = u
            else:
                u["citations"] = max(u["citations"], p.cited_by or 0)
                # 게재 상태가 하나라도 있으면 게재로 승격
                if paper_dict["status"] == "published":
                    u["status"] = "published"
                # 메타데이터 보강: 한쪽 레코드에만 있는 값 채우기
                for fld in ("volume", "publisher", "doi", "journal", "classification"):
                    if not u.get(fld) and paper_dict.get(fld):
                        u[fld] = paper_dict[fld]
            if display not in u["researchers"]:
                u["researchers"].append(display)
            if r.group and r.group not in u["groups"]:
                u["groups"].append(r.group)

        researchers_data[display] = {
            "id": r.id,
            "full_name": r.name,
            "group": r.group or "",
            "paper_count": len(papers),
            "citation_count": sum(p.cited_by or 0 for p in papers),
            "h_index": 0,
            "papers": paper_list,
        }

    all_papers = list(unique_papers.values())
    # 지표·차트는 '게재(published)' 논문만 집계 (투고/심사중 제외)
    published = [p for p in all_papers if p.get("status", "published") == "published"]

    annual_counts: dict[str, int] = {}
    for p in published:
        if p["year"]:
            annual_counts[str(p["year"])] = annual_counts.get(str(p["year"]), 0) + 1
    annual_output = dict(sorted(annual_counts.items()))

    venue_counts: dict[str, int] = {}
    for p in published:
        if p["journal"]:
            venue_counts[p["journal"]] = venue_counts.get(p["journal"], 0) + 1
    top_venues = [
        {"name": name, "count": cnt, "citations": 0}
        for name, cnt in sorted(
            venue_counts.items(), key=lambda x: x[1], reverse=True
        )[:10]
    ]

    # 랩 단위 지표 (게재 논문, 논문 단위 dedup 기준)
    total_papers = len(published)
    total_citations = sum(p["citations"] for p in published)
    citations_per_pub = round(total_citations / total_papers, 1) if total_papers else 0

    # h-index: 인용수 내림차순 정렬 후 (인용수 >= 순위) 최대 순위
    cite_sorted = sorted((p["citations"] for p in published), reverse=True)
    h_index = 0
    for i, c in enumerate(cite_sorted, start=1):
        if c >= i:
            h_index = i
        else:
            break

    # 심사중 집계 (게재 전)
    metrics = {
        "scholarly_output": total_papers,
        "citation_count": total_citations,
        "citations_per_publication": citations_per_pub,
        "h_index": h_index,
        "under_review": sum(1 for p in all_papers if p.get("status") == "under_review"),
    }

    return {
        "annual_output": annual_output,
        "top_venues": top_venues,
        "researchers": researchers_data,
        "all_papers": all_papers,
        "total_papers": total_papers,
        "metrics": metrics,
    }


@api_lr_bp.route("/papers/export")
def export_papers_excel():
    """연구실 논문 현황을 하나의 엑셀 파일로 내보내기.

    시트 2개:
      - '논문 목록': dedup된 전체 논문(게재+투고+심사중), 상태·저널·Vol·Publisher·인용·DB·DOI
      - '요약': 지표(Scholarly Output/Citation Count/Citations per Pub/h-index/투고·심사중) + 연도별 게재수
    """
    import io

    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    data = _build_lab_dataset()
    all_papers = data["all_papers"]
    metrics = data["metrics"]
    annual = data["annual_output"]

    status_ko = {"published": "게재", "under_review": "심사중"}

    wb = Workbook()

    # ---- 시트 1: 논문 목록 ----
    ws = wb.active
    ws.title = "논문 목록"
    headers = [
        "No", "상태", "제목", "저자", "참여 연구원", "저널", "분류",
        "Volume", "Publisher", "연도", "피인용", "출처", "DOI",
    ]
    ws.append(headers)

    head_fill = PatternFill("solid", fgColor="1F4E46")
    head_font = Font(bold=True, color="FFFFFF")
    for c in ws[1]:
        c.fill = head_fill
        c.font = head_font
        c.alignment = Alignment(vertical="center")

    # 게재 최신순 → 투고/심사중은 위로
    def _sort_key(p):
        st = p.get("status", "published")
        return (0 if st != "published" else 1, -(p.get("year") or 0))

    for i, p in enumerate(sorted(all_papers, key=_sort_key), start=1):
        ws.append([
            i,
            status_ko.get(p.get("status", "published"), p.get("status", "")),
            p.get("title", ""),
            ", ".join(p.get("authors", [])),
            ", ".join(p.get("researchers", [])),
            p.get("journal", ""),
            p.get("classification", ""),
            p.get("volume", ""),
            p.get("publisher", ""),
            p.get("year", "") or "",
            p.get("citations", 0),
            p.get("db_source", ""),
            p.get("doi", ""),
        ])

    widths = [5, 9, 60, 30, 20, 32, 8, 9, 22, 7, 8, 9, 28]
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = w
    ws.freeze_panes = "A2"

    # ---- 시트 2: 요약 ----
    ws2 = wb.create_sheet("요약")
    ws2.append(["지표", "값"])
    for c in ws2[1]:
        c.fill = head_fill
        c.font = head_font
    ws2.append(["Scholarly Output (게재 논문 수)", metrics["scholarly_output"]])
    ws2.append(["Citation Count (총 피인용)", metrics["citation_count"]])
    ws2.append(["Citations / Publication", metrics["citations_per_publication"]])
    ws2.append(["h-index", metrics["h_index"]])
    ws2.append(["심사중 (under review)", metrics.get("under_review", 0)])
    ws2.append([])
    ws2.append(["연도", "게재 논문 수"])
    for c in ws2[ws2.max_row]:
        c.font = Font(bold=True)
    for yr, cnt in sorted(annual.items()):
        ws2.append([yr, cnt])
    ws2.column_dimensions["A"].width = 34
    ws2.column_dimensions["B"].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = f"BIST_publications_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


# =========================================================================
# 출판물 동기화 — 상태 조회 / 수동 트리거 / DOI 수동 등록
# =========================================================================


@api_lr_bp.route("/sync-status")
def sync_status():
    """출판물 동기화 배치의 최근 실행 상태."""
    from app.models import BatchLog

    log = (
        BatchLog.query.filter_by(job_type="publication_sync")
        .order_by(BatchLog.started_at.desc())
        .first()
    )
    if not log:
        return jsonify({"last_sync": None})

    d = log.details or {}
    kst = log.started_at + timedelta(hours=9) if log.started_at else None
    return jsonify({
        "last_sync": kst.strftime("%Y-%m-%d %H:%M") if kst else None,
        "status": log.status,
        "total_new": d.get("total_new", 0),
        "total_updated": d.get("total_updated", 0),
        "error_count": len(d.get("errors", [])),
    })


@api_lr_bp.route("/sync-publications", methods=["POST"])
def trigger_publication_sync():
    """출판물 동기화 수동 트리거 (로그인 필요). 백그라운드 실행."""
    if not session.get("researcher_id"):
        return jsonify({"error": "로그인 필요"}), 401

    from app.models import BatchLog

    running = (
        BatchLog.query.filter_by(job_type="publication_sync", status="running")
        .filter(BatchLog.started_at >= datetime.utcnow() - timedelta(minutes=30))
        .first()
    )
    if running:
        return jsonify({"error": "동기화가 이미 실행 중입니다."}), 409

    from threading import Thread

    from flask import current_app

    app_obj = current_app._get_current_object()

    def _run():
        with app_obj.app_context():
            from app.litreview.scheduler.jobs import weekly_publication_sync
            try:
                weekly_publication_sync()
            except Exception:
                logger.exception("Manual publication sync failed")

    Thread(target=_run, daemon=True).start()
    return jsonify({
        "success": True,
        "message": "동기화가 시작되었습니다. 수 분 후 새로고침하세요.",
    })


@api_lr_bp.route("/papers/manual", methods=["POST"])
def add_manual_paper():
    """DOI로 논문 수동 등록 (로그인 필요).

    Crossref API로 메타데이터를 채워 ReferencePaper에 저장한다.
    Scopus 미반영 최신 논문·국내지·in-press 대응용.

    body: {"doi": "...", "researcher_id": <선택, 기본=로그인 연구원>}
    """
    rid = session.get("researcher_id")
    if not rid:
        return jsonify({"error": "로그인 필요"}), 401

    data = request.get_json() or {}
    doi = (data.get("doi") or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    if not doi:
        return jsonify({"error": "DOI를 입력하세요."}), 400

    target_rid = data.get("researcher_id") or rid
    target = db.session.get(Researcher, target_rid)
    if not target:
        return jsonify({"error": "연구원을 찾을 수 없습니다."}), 404

    exists = ReferencePaper.query.filter(
        ReferencePaper.researcher_id == target_rid,
        func.lower(ReferencePaper.doi) == doi.lower(),
    ).first()
    if exists:
        return jsonify({"error": "이미 등록된 논문입니다.", "paper_id": exists.id}), 409

    import re

    import requests as _requests

    try:
        resp = _requests.get(
            f"https://api.crossref.org/works/{doi}",
            timeout=15,
            headers={"User-Agent": "BIST-LitReview/1.0"},
        )
    except _requests.RequestException:
        logger.exception("Crossref request failed for DOI %s", doi)
        return jsonify({"error": "Crossref 조회에 실패했습니다. 잠시 후 다시 시도하세요."}), 502

    if resp.status_code == 404:
        return jsonify({"error": "해당 DOI를 Crossref에서 찾을 수 없습니다."}), 404
    if not resp.ok:
        return jsonify({"error": f"Crossref 오류 (HTTP {resp.status_code})"}), 502

    m = resp.json().get("message", {})

    title_list = m.get("title") or []
    title = title_list[0] if title_list else ""
    if not title:
        return jsonify({"error": "논문 제목을 가져올 수 없습니다."}), 422

    authors = "; ".join(
        f"{a.get('family', '')} {a.get('given', '')}".strip()
        for a in (m.get("author") or [])
        if a.get("family") or a.get("given")
    )
    container = m.get("container-title") or []
    journal = container[0] if container else ""
    volume = m.get("volume") or None
    publisher = m.get("publisher") or None

    year = None
    for date_field in ("published-print", "published-online", "issued"):
        parts = (m.get(date_field) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break

    abstract = m.get("abstract") or ""
    if abstract:
        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()

    cited_by = int(m.get("is-referenced-by-count") or 0)

    paper = ReferencePaper(
        researcher_id=target_rid,
        scopus_id=None,
        title=title,
        authors=authors,
        journal=journal,
        volume=volume,
        publisher=publisher,
        year=year,
        abstract=abstract,
        doi=doi,
        cited_by=cited_by,
        source="manual",
        status="published",
    )
    db.session.add(paper)
    db.session.commit()

    logger.info(
        "Manual paper added for researcher %d by %d: %s", target_rid, rid, doi
    )
    return jsonify({
        "success": True,
        "paper": {
            "id": paper.id,
            "title": title,
            "authors": authors,
            "journal": journal,
            "volume": volume,
            "publisher": publisher,
            "year": year,
            "doi": doi,
        },
    }), 201


# =========================================================================
# 투고 논문(게재 전) 등록 / 상태 관리
# =========================================================================

_PROGRESS_STATUSES = ("under_review", "published")


@api_lr_bp.route("/papers/progress", methods=["GET"])
def list_progress_papers():
    """관리자용 수동 등록 논문 목록.

    source='manual'인 모든 논문(수동 DOI 게재 + 심사중). 상태 변경·삭제 대상.
    """
    q = (
        ReferencePaper.query
        .filter(ReferencePaper.source == "manual")
        .order_by(ReferencePaper.created_at.desc())
    )
    _NAME_ALIAS = {"Syed Mostasim Hasnain Saif": "사이에드"}

    def _display(name):
        if name in _NAME_ALIAS:
            return _NAME_ALIAS[name]
        return name.split("(")[0].strip() if "(" in name else name

    out = []
    for p in q.all():
        r = db.session.get(Researcher, p.researcher_id)
        out.append({
            "id": p.id,
            "title": p.title,
            "journal": p.journal or "",
            "status": p.status,
            "year": p.year,
            "researcher_id": p.researcher_id,
            "researcher_name": _display(r.name) if r else "",
            "created_at": str(p.created_at.date()) if p.created_at else "",
        })
    return jsonify(out)


@api_lr_bp.route("/papers/progress", methods=["POST"])
def create_progress_paper():
    """논문 수동 등록 (로그인 필요).

    body: {title, first_author, journal, classification(SCIE|KCI), status, year}
    주저자 이름을 직접 입력받아 authors 에 저장. DOI·Scopus 없이 등록.
    소유(관리) 주체는 로그인한 관리자(session researcher_id).
    """
    rid = session.get("researcher_id")
    if not rid:
        return jsonify({"error": "로그인 필요"}), 401

    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    first_author = (data.get("first_author") or "").strip()
    journal = (data.get("journal") or "").strip()
    classification = (data.get("classification") or "").strip().upper() or None
    status = (data.get("status") or "under_review").strip()

    if not title:
        return jsonify({"error": "논문 제목은 필수입니다."}), 400
    if status not in ("under_review", "published"):
        return jsonify({"error": "상태는 under_review(심사중) 또는 published(게재) 여야 합니다."}), 400
    if classification and classification not in ("SCIE", "KCI"):
        return jsonify({"error": "분류는 SCIE 또는 KCI 여야 합니다."}), 400

    year = data.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None

    paper = ReferencePaper(
        researcher_id=rid,
        scopus_id=None,
        title=title,
        authors=first_author,
        journal=journal or None,
        classification=classification,
        year=year,
        doi=None,
        cited_by=0,
        source="manual",
        status=status,
    )
    db.session.add(paper)
    db.session.commit()

    logger.info(
        "Manual paper added (status=%s, class=%s) by %d: %s",
        status, classification, rid, title[:50],
    )
    return jsonify({
        "success": True,
        "paper": {
            "id": paper.id,
            "title": title,
            "first_author": first_author,
            "journal": journal,
            "classification": classification,
            "status": status,
            "researcher_id": rid,
        },
    }), 201


@api_lr_bp.route("/papers/<int:pid>/status", methods=["PATCH"])
def update_paper_status(pid):
    """투고 논문 상태 변경 (로그인 필요).

    body: {status: under_review|submitted|published}
    예) 심사중 → 투고완료(submitted), 투고완료 → 게재(published).
    published 로 바꾸면 논문 페이지의 게재 목록·지표에 반영됨.
    """
    if not session.get("researcher_id"):
        return jsonify({"error": "로그인 필요"}), 401

    p = db.session.get(ReferencePaper, pid)
    if not p:
        return jsonify({"error": "논문을 찾을 수 없습니다."}), 404

    data = request.get_json() or {}
    new_status = (data.get("status") or "").strip()
    if new_status not in _PROGRESS_STATUSES:
        return jsonify({"error": f"status는 {_PROGRESS_STATUSES} 중 하나여야 합니다."}), 400

    p.status = new_status
    db.session.commit()
    logger.info("Paper %d status -> %s", pid, new_status)
    return jsonify({"success": True, "id": p.id, "status": new_status})


@api_lr_bp.route("/papers/<int:pid>", methods=["DELETE"])
def delete_paper(pid):
    """수동 등록 논문 삭제 (로그인 필요, manual source 만).

    Scopus 자동 수집 논문은 삭제 불가(다음 동기화 때 복원되므로).
    """
    if not session.get("researcher_id"):
        return jsonify({"error": "로그인 필요"}), 401

    p = db.session.get(ReferencePaper, pid)
    if not p:
        return jsonify({"error": "논문을 찾을 수 없습니다."}), 404
    if p.source != "manual":
        return jsonify({"error": "자동 수집 논문은 삭제할 수 없습니다."}), 400

    db.session.delete(p)
    db.session.commit()
    logger.info("Manual paper %d deleted", pid)
    return jsonify({"success": True, "id": pid})


# =========================================================================
# 논문 저장 / 피드백
# =========================================================================


@api_lr_bp.route("/save_paper", methods=["POST"])
def save_paper():
    """UI에서 논문 저장 (북마크)."""
    rid = session.get("researcher_id")
    if not rid:
        return jsonify({"error": "로그인 필요"}), 401

    data = request.get_json() or {}
    rec_id = data.get("recommendation_id")

    if rec_id:
        rec = db.session.get(PaperRecommendation, rec_id)
        if rec and rec.researcher_id == rid:
            rec.is_saved = True
            rec.is_read = True
            db.session.commit()
            return jsonify({"saved": True, "id": rec.id})

    return jsonify({"error": "recommendation_id 필요"}), 400


@api_lr_bp.route("/feedback", methods=["POST"])
def feedback():
    """추천 논문 피드백 (useful / not_relevant).

    useful 시 백그라운드로:
      1. interest_papers UPSERT (Scopus 메타 보강 + OpenAI 임베딩)
      2. 저널 자동 추가 (즉시, 동기)
    """
    rid = session.get("researcher_id")
    if not rid:
        return jsonify({"error": "로그인 필요"}), 401

    data = request.get_json() or {}
    rec_id = data.get("recommendation_id")
    fb = data.get("feedback")

    if fb not in ("useful", "not_relevant"):
        return jsonify({"error": "feedback: 'useful' or 'not_relevant'"}), 400

    rec = db.session.get(PaperRecommendation, rec_id)
    if not rec or rec.researcher_id != rid:
        return jsonify({"error": "Not found"}), 404

    rec.user_feedback = fb
    rec.is_read = True
    db.session.commit()

    if fb == "useful" and rec.paper:
        # 즉시 동기: 저널 자동 추가 (가벼움)
        from app.litreview.journal.journal_analyzer import add_feedback_journal
        add_feedback_journal(rec.researcher_id, rec.paper.journal)

        # 백그라운드: interest_papers 보강 (Scopus + OpenAI 호출 ~3초)
        from flask import current_app
        from app.litreview.feedback.interest_paper_service import (
            trigger_useful_feedback_async,
        )
        trigger_useful_feedback_async(
            current_app._get_current_object(),
            rec.researcher_id,
            rec.paper.scopus_id,
        )

    return jsonify({"success": True, "feedback": fb, "id": rec.id})


# =========================================================================
# 연구 주제 CRUD (자동+수동 통합)
# =========================================================================


@api_lr_bp.route("/researchers/<int:rid>/topics")
def list_topics(rid):
    """연구원의 연구 주제 목록."""
    topics = (
        ResearchTopic.query
        .filter_by(researcher_id=rid)
        .order_by(ResearchTopic.sort_order, ResearchTopic.id)
        .all()
    )
    return jsonify([_topic_dict(t) for t in topics])


@api_lr_bp.route("/researchers/<int:rid>/topics", methods=["POST"])
def create_topic(rid):
    """수동 주제 추가 + DOI 기반 레퍼런스 수집 + 대표 벡터 생성."""
    rid_session = session.get("researcher_id")
    if rid_session != rid:
        return jsonify({"error": "권한 없음"}), 403

    data = request.get_json() or {}
    name = (data.get("name") or data.get("topic_name") or "").strip()
    if not name:
        return jsonify({"error": "주제 이름은 필수입니다."}), 400

    keywords_raw = (data.get("keywords") or "").strip()
    keywords = [k.strip() for k in keywords_raw.split(";") if k.strip()] if keywords_raw else []

    dois_raw = (data.get("dois") or "").strip()
    dois = [d.strip() for d in dois_raw.split(";") if d.strip()] if dois_raw else []

    description = (data.get("description") or "").strip()

    if not keywords and not dois and not description:
        return jsonify({"error": "키워드, DOI, 설명 중 하나 이상 입력해야 합니다."}), 400

    from app.litreview.profile.topic_manager import create_manual_topic

    try:
        result = create_manual_topic(rid, name, keywords, dois, description)
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api_lr_bp.route("/topics/<int:tid>", methods=["PUT"])
def update_topic(tid):
    """주제 수정."""
    t = db.session.get(ResearchTopic, tid)
    if not t:
        return jsonify({"error": "Not found"}), 404
    if session.get("researcher_id") != t.researcher_id:
        return jsonify({"error": "권한 없음"}), 403
    if t.source_type == "auto":
        return jsonify({"error": "자동 생성 주제는 수정할 수 없습니다."}), 400

    data = request.get_json() or {}

    keywords_raw = (data.get("keywords") or "").strip()
    new_keywords = [k.strip() for k in keywords_raw.split(";") if k.strip()] if keywords_raw else None

    new_dois_raw = (data.get("new_dois") or data.get("dois") or "").strip()
    new_dois = [d.strip() for d in new_dois_raw.split(";") if d.strip()] if new_dois_raw else None

    from app.litreview.profile.topic_manager import update_manual_topic

    try:
        result = update_manual_topic(
            tid,
            name=data.get("name") or data.get("topic_name"),
            keywords=new_keywords,
            new_dois=new_dois,
            description=data.get("description"),
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@api_lr_bp.route("/topics/<int:tid>/keywords", methods=["PATCH"])
def update_topic_keywords(tid):
    """주제의 검색 키워드만 교체. 자동 주제도 허용한다.

    주제 키워드는 Scopus 검색 쿼리(TITLE-ABS-KEY)를 직접 결정하는 유일한 값이라
    사용자가 직접 손볼 수 있어야 한다. 이름/DOI 수정은 기존 PUT /topics/<tid>가
    담당하고 자동 주제에 대해서는 계속 막아 둔다.

    자동 주제를 편집하면 keywords_locked=True가 되어 자동 갱신이 덮어쓰지 않는다.

    body: {"keywords": ["kw1", "kw2", ...]}
    """
    t = db.session.get(ResearchTopic, tid)
    if not t:
        return jsonify({"error": "주제를 찾을 수 없습니다."}), 404
    if session.get("researcher_id") != t.researcher_id:
        return jsonify({"error": "권한 없음"}), 403

    data = request.get_json() or {}
    raw = data.get("keywords")
    if raw is None:
        return jsonify({"error": "keywords 필드가 필요합니다."}), 400
    if isinstance(raw, str):
        raw = raw.split(";")
    if not isinstance(raw, list):
        return jsonify({"error": "keywords는 배열이어야 합니다."}), 400

    # 공백 제거 + 빈 값 제거 + 대소문자 무시 중복 제거(입력 순서 유지)
    cleaned, seen = [], set()
    for item in raw:
        kw = str(item).strip()
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        cleaned.append(kw)

    if len(cleaned) > 30:
        return jsonify({"error": "키워드는 최대 30개까지 등록할 수 있습니다."}), 400

    t.keywords = cleaned
    if t.source_type == "auto":
        t.keywords_locked = True
    t.updated_at = datetime.utcnow()
    db.session.commit()

    # 키워드가 바뀌면 벡터도 낡는데, 벡터를 만드는 방식이 주제 종류마다 다르다.
    #   keyword_seeded : 키워드와 매칭된 본인 논문들의 임베딩 평균 → 매칭 재계산
    #   텍스트 기반    : 이름+키워드+설명 임베딩 → 재임베딩
    #   레퍼런스 초록  : 초록에서 나오므로 키워드와 무관 → 그대로 둠
    #   B유형 자동     : 벡터 없이 개별 논문 max 비교 → 새로 만들지 않음
    from app.litreview.profile.topic_manager import (
        KEYWORD_SEEDED_SOURCE,
        vector_is_text_based,
    )

    revectorized = False
    try:
        if t.source_type == KEYWORD_SEEDED_SOURCE:
            from app.litreview.profile.keyword_seeded_topic import (
                refresh_keyword_seeded_topic,
            )

            revectorized = refresh_keyword_seeded_topic(t)
        elif vector_is_text_based(t) and (
            t.representative_vector is not None or t.source_type == "manual"
        ):
            from app.litreview.recommendation.embedder import Embedder

            revectorized = Embedder().embed_topic_representative(t.id)
    except Exception:
        logger.exception("Failed to regenerate vector for topic %d", tid)

    logger.info(
        "Topic %d keywords updated by researcher %d: %d개 (locked=%s, revectorized=%s)",
        tid,
        t.researcher_id,
        len(cleaned),
        bool(t.keywords_locked),
        revectorized,
    )
    return jsonify({**_topic_dict(t), "revectorized": revectorized})


@api_lr_bp.route("/topics/<int:tid>", methods=["DELETE"])
def delete_topic(tid):
    """주제 삭제."""
    t = db.session.get(ResearchTopic, tid)
    if not t:
        return jsonify({"error": "Not found"}), 404
    if session.get("researcher_id") != t.researcher_id:
        return jsonify({"error": "권한 없음"}), 403

    from app.litreview.profile.topic_manager import delete_topic as _delete

    deleted = _delete(tid)
    if not deleted:
        return jsonify({"error": "삭제 실패"}), 500
    return jsonify({"success": True})


@api_lr_bp.route("/researchers/<int:rid>/profile/save-all", methods=["POST"])
def save_all_topics(rid):
    """전체 수동 주제 일괄 저장 (추가/수정/삭제 한번에)."""
    if session.get("researcher_id") != rid:
        return jsonify({"error": "권한 없음"}), 403

    data = request.get_json() or {}
    topics_data = data.get("topics", [])

    # 기존 수동 주제만 대상 (자동 주제는 건드리지 않음)
    existing = {
        t.id: t
        for t in ResearchTopic.query.filter_by(
            researcher_id=rid, source_type="manual"
        ).all()
    }
    incoming_ids = set()

    for i, td in enumerate(topics_data):
        name = (td.get("name") or td.get("topic_name") or "").strip()
        if not name:
            continue

        keywords_raw = (td.get("keywords") or "").strip()
        keywords = [k.strip() for k in keywords_raw.split(";") if k.strip()] if isinstance(keywords_raw, str) else keywords_raw

        tid = td.get("id")
        if tid and tid in existing:
            t = existing[tid]
            t.name = name
            t.keywords = keywords or []
            t.description = (td.get("description") or "").strip() or None
            t.sort_order = i
            incoming_ids.add(tid)
        else:
            t = ResearchTopic(
                researcher_id=rid,
                name=name,
                source_type="manual",
                keywords=keywords or [],
                description=(td.get("description") or "").strip() or None,
                sort_order=i,
            )
            db.session.add(t)

    for old_id, old_t in existing.items():
        if old_id not in incoming_ids:
            db.session.delete(old_t)

    db.session.commit()

    updated = (
        ResearchTopic.query
        .filter_by(researcher_id=rid)
        .order_by(ResearchTopic.sort_order, ResearchTopic.id)
        .all()
    )
    logger.info("Bulk-saved %d topics for researcher %d", len(updated), rid)
    return jsonify({"success": True, "topics": [_topic_dict(t) for t in updated]})


def _topic_dict(t):
    return {
        "id": t.id,
        "name": t.name,
        "source_type": t.source_type,
        "keywords": t.keywords or [],
        "keywords_locked": bool(t.keywords_locked),
        "description": t.description or "",
        "cluster_label": t.cluster_label,
        "has_vector": t.representative_vector is not None,
        "sort_order": t.sort_order,
        "reference_paper_count": len(t.reference_papers) if t.reference_papers else 0,
        "recommendation_count": len(t.recommendations) if t.recommendations else 0,
        "created_at": str(t.created_at) if t.created_at else "",
        "updated_at": str(t.updated_at) if t.updated_at else "",
    }


# =========================================================================
# 연구원 목록 / 상세
# =========================================================================


@api_lr_bp.route("/researchers")
def list_researchers():
    """연구실 구성원 목록 (홈페이지 members 기준).

    기본은 재직 중(status='current')인 구성원만 반환한다.
    Query: ?include_alumni=1 을 붙이면 졸업생까지 포함.

    연구원당 COUNT를 따로 돌리면 20명 × 7회 = 140쿼리가 되므로
    group by 집계 4번으로 줄였다.
    """
    from app.models import Team, TeamMembership

    include_alumni = request.args.get("include_alumni") in ("1", "true", "yes")

    q = Researcher.query
    if not include_alumni:
        q = q.filter(Researcher.status == "current")
    # 홈페이지가 정한 표시 순서(roster_order)를 그대로 따른다
    researchers = q.order_by(Researcher.roster_order, Researcher.name_ko).all()

    def _grouped(model, *filters):
        rows = (
            db.session.query(model.researcher_id, func.count(model.id))
            .filter(*filters)
            .group_by(model.researcher_id)
            .all()
        )
        return {rid: c for rid, c in rows}

    paper_counts = _grouped(ReferencePaper)
    topic_counts = _grouped(ResearchTopic)
    saved_counts = _grouped(PaperRecommendation, PaperRecommendation.is_saved.is_(True))

    # 등급별 추천 수는 한 번에 (researcher_id, grade) 로 묶어서 가져온다
    grade_rows = (
        db.session.query(
            PaperRecommendation.researcher_id,
            PaperRecommendation.grade,
            func.count(PaperRecommendation.id),
        )
        .group_by(PaperRecommendation.researcher_id, PaperRecommendation.grade)
        .all()
    )
    by_grade: dict[int, dict[str, int]] = {}
    for rid, grade, cnt in grade_rows:
        by_grade.setdefault(rid, {})[grade] = cnt

    # 팀 이름도 한 번에 (Researcher.group 은 속성이라 N+1을 만든다)
    team_rows = (
        db.session.query(TeamMembership.member_id, Team.name)
        .join(Team, Team.id == TeamMembership.team_id)
        .order_by(Team.sort_order)
        .all()
    )
    teams: dict[int, str] = {}
    for mid, tname in team_rows:
        teams.setdefault(mid, tname)

    results = []
    for r in researchers:
        g = by_grade.get(r.id, {})
        results.append({
            "id": r.id,
            "name": r.name,
            "display_name": r.name_ko,
            "name_en": r.name_en or "",
            "role": r.role or "",
            "status": r.status or "",
            "email": r.email,
            "group": teams.get(r.id, ""),
            "researcher_type": r.researcher_type or "C",
            "scopus_id": r.scopus_id,
            "orcid": r.orcid,
            "photo_filename": r.photo_filename,
            "research_description": r.research_description or "",
            "paper_count": paper_counts.get(r.id, 0),
            "topic_count": topic_counts.get(r.id, 0),
            "recommendation_count": sum(g.values()),
            "core_count": g.get("core", 0),
            "related_count": g.get("related", 0),
            "reference_count": g.get("reference", 0),
            "saved_count": saved_counts.get(r.id, 0),
        })

    return jsonify(results)


@api_lr_bp.route("/researchers/<int:rid>/detail")
def researcher_detail(rid):
    """연구원 상세 정보."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    ref_count = ReferencePaper.query.filter_by(researcher_id=rid).count()

    rec_total = PaperRecommendation.query.filter_by(researcher_id=rid).count()
    rec_core = PaperRecommendation.query.filter_by(
        researcher_id=rid, grade="core"
    ).count()
    rec_related = PaperRecommendation.query.filter_by(
        researcher_id=rid, grade="related"
    ).count()
    rec_reference = PaperRecommendation.query.filter_by(
        researcher_id=rid, grade="reference"
    ).count()
    saved_count = PaperRecommendation.query.filter_by(
        researcher_id=rid, is_saved=True
    ).count()
    read_count = PaperRecommendation.query.filter_by(
        researcher_id=rid, is_read=True
    ).count()

    top_kws = (
        db.session.query(PaperKeyword.keyword, func.count(PaperKeyword.id))
        .join(
            ReferencePaper, PaperKeyword.paper_id == ReferencePaper.publication_id
        )
        .filter(ReferencePaper.researcher_id == rid)
        .group_by(PaperKeyword.keyword)
        .order_by(func.count(PaperKeyword.id).desc())
        .limit(8)
        .all()
    )

    topics = (
        ResearchTopic.query.filter_by(researcher_id=rid)
        .order_by(ResearchTopic.sort_order)
        .all()
    )

    return jsonify({
        "id": r.id,
        "name": r.name,
        "email": r.email,
        "group": r.group or "",
        "researcher_type": r.researcher_type or "C",
        "scopus_id": r.scopus_id,
        "research_description": r.research_description or "",
        "paper_count": ref_count,
        "recommendation_total": rec_total,
        "core_count": rec_core,
        "related_count": rec_related,
        "reference_count": rec_reference,
        "saved_count": saved_count,
        "read_count": read_count,
        "top_keywords": [{"keyword": kw, "count": cnt} for kw, cnt in top_kws],
        "topics": [_topic_dict(t) for t in topics],
    })


# =========================================================================
# 추천 목록 (주제별 단일 유사도)
# =========================================================================


@api_lr_bp.route("/researchers/<int:rid>/recommendations")
def researcher_recommendations(rid):
    """연구원의 추천 논문 목록 — 주제별 단일 유사도 + 등급 + 구조화 요약."""
    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    grade = request.args.get("grade")
    topic_id = request.args.get("topic_id", type=int)

    query = PaperRecommendation.query.filter_by(researcher_id=rid)
    if grade:
        query = query.filter_by(grade=grade)
    if topic_id:
        query = query.filter_by(topic_id=topic_id)

    recs = query.order_by(PaperRecommendation.similarity_score.desc()).all()

    results = []
    for rec in recs:
        p = rec.paper
        topic = rec.topic

        results.append({
            "id": rec.id,
            "paper_id": rec.paper_id,
            "title": p.title if p else "",
            "authors": p.authors if p else "",
            "journal": p.journal if p else "",
            "year": p.year if p else None,
            "doi": p.doi if p else "",
            "abstract": (p.abstract or "")[:300] if p else "",
            # 주제
            "topic_id": rec.topic_id,
            "topic_name": topic.name if topic else "",
            "topic_source_type": topic.source_type if topic else "",
            # 등급
            "grade": rec.grade,
            "grade_reason": rec.grade_reason or "",
            # 단일 유사도
            "score": round((rec.similarity_score or 0) * 100),
            "similarity_score": rec.similarity_score,
            "percentile_rank": rec.percentile_rank,
            # 구조화 요약 6필드
            "summary_source": rec.summary_source,
            "summary_source": rec.summary_source,
        "summary_core_topic": rec.summary_core_topic or "",
            "summary_purpose": rec.summary_purpose or "",
            "summary_method": rec.summary_method or "",
            "summary_results": rec.summary_results or "",
            "summary_limitations": rec.summary_limitations or "",
            "summary_future": rec.summary_future or "",
            "recommendation_reason": rec.recommendation_reason or "",
            # 유사도 상세
            "similarity_details": rec.similarity_details or {},
            # 상태
            "is_saved": rec.is_saved,
            "is_read": rec.is_read,
            "user_feedback": rec.user_feedback,
            "created_at": str(rec.created_at) if rec.created_at else "",
        })

    return jsonify(results)


@api_lr_bp.route("/recommendations/<int:rec_id>/share", methods=["POST"])
def share_recommendation(rec_id):
    """추천 논문 1건을 선택한 연구원에게 이메일로 공유."""
    data = request.get_json() or {}
    try:
        target_researcher_id = int(data.get("target_researcher_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "target_researcher_id 필요"}), 400

    rec = db.session.get(PaperRecommendation, rec_id)
    target = db.session.get(Researcher, target_researcher_id)
    if not rec:
        return jsonify({"success": False, "error": "추천을 찾을 수 없습니다."}), 404
    if not target or not target.email:
        return jsonify({"success": False, "error": "공유 대상 연구원 이메일이 없습니다."}), 404

    from app.litreview.notification.email_sender import send_shared_recommendation_email

    success = send_shared_recommendation_email(
        recommendation_id=rec_id,
        target_researcher_id=target_researcher_id,
        shared_by_researcher_id=session.get("researcher_id") or rec.researcher_id,
    )
    if not success:
        return jsonify({"success": False, "error": "메일 발송에 실패했습니다."}), 500

    return jsonify({
        "success": True,
        "target_researcher_id": target.id,
        "target_name": target.name,
        "target_email": target.email,
    })


# =========================================================================
# useful 트랙 추천 — useful 논문별 1대1 매칭 (별도 트랙)
# =========================================================================


@api_lr_bp.route("/researchers/<int:rid>/useful-recommendations")
def useful_recommendations(rid):
    """useful 별도 트랙 추천 — useful 논문별로 그룹핑된 결과.

    Query params:
        threshold: cosine 임계치 (기본 0.6)

    Returns:
        [{useful_paper, recommendations: [...]}]
    """
    if not db.session.get(Researcher, rid):
        return jsonify({"error": "Not found"}), 404

    threshold = request.args.get("threshold", default=0.6, type=float)

    from app.litreview.feedback.useful_track import calculate_useful_recommendations
    results = calculate_useful_recommendations(rid, threshold=threshold)
    return jsonify(results)


# =========================================================================
# 사이드바용: 최근 추천 + 트렌딩 키워드
# =========================================================================


@api_lr_bp.route("/recent-recommendations")
def recent_recommendations():
    """최근 N일 추천 논문 — 라운드 로빈으로 연구원 다양성 확보.

    각 연구원의 score 상위 논문을 1편씩 순회하며 수집해서
    한 연구원이 결과를 독점하지 않도록 한다. 결과는 score 내림차순으로 정렬.
    """
    from collections import defaultdict

    days = int(request.args.get("days", 7))
    limit = int(request.args.get("limit", 10))
    cutoff = datetime.utcnow() - timedelta(days=days)

    all_recs = (
        PaperRecommendation.query
        .filter(PaperRecommendation.created_at >= cutoff)
        .order_by(PaperRecommendation.similarity_score.desc())
        .all()
    )

    # 연구원별로 그룹핑 (각 그룹은 이미 score 내림차순)
    by_researcher: dict[int, list] = defaultdict(list)
    for rec in all_recs:
        by_researcher[rec.researcher_id].append(rec)

    # 라운드 로빈: 라운드마다 연구원당 1편씩 픽
    seen_papers: set[int] = set()
    picked = []
    round_idx = 0
    max_rounds = 50
    while len(picked) < limit and round_idx < max_rounds:
        progress = False
        for rid, recs_list in by_researcher.items():
            if round_idx >= len(recs_list):
                continue
            rec = recs_list[round_idx]
            if rec.paper_id in seen_papers:
                continue
            seen_papers.add(rec.paper_id)
            picked.append(rec)
            progress = True
            if len(picked) >= limit:
                break
        if not progress:
            break
        round_idx += 1

    # score 내림차순 재정렬 (UI 표시용)
    picked.sort(key=lambda r: -(r.similarity_score or 0))

    results = []
    for rec in picked:
        p = rec.paper
        researcher = db.session.get(Researcher, rec.researcher_id)
        researcher_name = researcher.name if researcher else ""
        if researcher_name and "(" in researcher_name:
            researcher_name = researcher_name.split("(")[0].strip()
        results.append({
            "paper_id": rec.paper_id,
            "title": p.title if p else "",
            "journal": p.journal if p else "",
            "year": p.year if p else None,
            "doi": p.doi if p else "",
            "grade": rec.grade,
            "score": round((rec.similarity_score or 0) * 100),
            "researcher_id": rec.researcher_id,
            "researcher_name": researcher_name,
            "created_at": str(rec.created_at) if rec.created_at else "",
        })

    return jsonify(results)


@api_lr_bp.route("/api-usage")
def api_usage():
    """API 토큰 사용량·비용 집계.

    쿼리 파라미터:
        days   집계 기간 (기본 30일)
        weeks  주간 추이 개수 (기본 8)

    원화는 **호출 시점 환율**로 계산해 행마다 저장해 둔 값을 합산한 것이다.
    시장 기준환율(ECB)이라 카드사 실제 청구액과는 수수료만큼 차이가 난다.
    """
    days = request.args.get("days", default=30, type=int)
    weeks = request.args.get("weeks", default=8, type=int)
    since = datetime.utcnow() - timedelta(days=days)

    from app.litreview.usage import summarize, weekly_series

    period = summarize(since=since)
    allt = summarize()
    return jsonify({
        "days": days,
        "period": period,
        "all_time": {
            "calls": allt["calls"],
            "total_tokens": allt["total_tokens"],
            "usd": allt["usd"],
            "krw": allt["krw"],
        },
        "weekly": weekly_series(weeks),
        "rate": {
            "usd_krw": period["latest_rate"] or allt["latest_rate"],
            "date": period["rate_date"] or allt["rate_date"],
            "source": period.get("rate_source") or allt.get("rate_source"),
        },
        "note": "시장 기준환율 기준. 카드사 청구액은 수수료만큼 더 높습니다.",
    })


@api_lr_bp.route("/journal-monitoring")
def journal_monitoring():
    """저널 모니터링 — 모니터링 중인 저널 + 새로 발견된 저널.

    쿼리 파라미터:
        researcher_id  없으면 연구실 전체
        days           신규 판정 기간 (기본 30일)
        limit          각 목록의 최대 개수 (기본 5)

    "새로 발견된 저널"은 두 경로 중 있는 쪽을 쓴다.
      1) 저널 발굴 파이프라인 결과(reco_journal_recommendations)
      2) 파이프라인 실행 전이면, 최근 추천 논문의 저널 중
         모니터링 목록에 없는 것

    2)는 임시 대체다. 파이프라인이 돌면 1)이 유사도·성장률·친숙도까지 계산한
    제대로 된 결과를 주므로 그쪽이 우선한다.
    """
    rid = request.args.get("researcher_id", type=int)
    days = request.args.get("days", default=30, type=int)
    limit = request.args.get("limit", default=5, type=int)
    since = datetime.utcnow() - timedelta(days=days)

    # --- 모니터링 중인 저널 ---
    tq = TargetJournal.query.filter_by(is_active=True)
    if rid:
        tq = tq.filter_by(researcher_id=rid)
    monitored_rows = tq.all()

    monitored_names = {t.journal_name for t in monitored_rows}

    # 저널별 최근 추천 건수 — 어느 저널이 실제로 논문을 내주고 있는지 보여준다
    cq = (
        db.session.query(
            CollectedPaper.journal, func.count(PaperRecommendation.id)
        )
        .join(
            PaperRecommendation,
            PaperRecommendation.paper_id == CollectedPaper.id,
        )
        .filter(
            PaperRecommendation.created_at >= since,
            CollectedPaper.journal.isnot(None),
        )
    )
    if rid:
        cq = cq.filter(PaperRecommendation.researcher_id == rid)
    recent_counts = dict(cq.group_by(CollectedPaper.journal).all())

    # 최근 추천이 많은 저널을 위로. 없으면 이름순.
    monitored = sorted(
        (
            {
                "journal": t.journal_name,
                "issn": t.issn,
                "source_type": t.source_type,
                "count": recent_counts.get(t.journal_name, 0),
                "last_checked": str(t.last_checked) if t.last_checked else None,
            }
            for t in monitored_rows
        ),
        key=lambda x: (-x["count"], x["journal"] or ""),
    )

    # --- 새로 발견된 저널 (경로 1: 발굴 파이프라인) ---
    jq = JournalRecommendation.query.filter(
        JournalRecommendation.status.in_(("new", "seen"))
    )
    if rid:
        jq = jq.filter_by(researcher_id=rid)
    discovered = (
        jq.order_by(JournalRecommendation.score.desc()).limit(limit * 3).all()
    )

    new_journals = []
    seen = set()
    for d in discovered:
        if d.journal_name in seen or d.journal_name in monitored_names:
            continue
        seen.add(d.journal_name)
        new_journals.append({
            "journal": d.journal_name,
            "issn": d.issn,
            "count": d.related_paper_count,
            "score": d.score,
            "novelty": d.novelty,
            "source": "discovery",
            "recommendation_id": d.id,
        })
        if len(new_journals) >= limit:
            break

    # --- 경로 2: 파이프라인 결과가 없을 때의 임시 대체 ---
    if not new_journals:
        rq = (
            db.session.query(
                CollectedPaper.journal,
                func.count(PaperRecommendation.id),
            )
            .join(
                PaperRecommendation,
                PaperRecommendation.paper_id == CollectedPaper.id,
            )
            .filter(
                PaperRecommendation.created_at >= since,
                CollectedPaper.journal.isnot(None),
                CollectedPaper.journal != "",
            )
        )
        if rid:
            rq = rq.filter(PaperRecommendation.researcher_id == rid)

        rows = (
            rq.group_by(CollectedPaper.journal)
            .order_by(func.count(PaperRecommendation.id).desc())
            .limit(limit * 4)
            .all()
        )
        for journal, cnt in rows:
            if journal in monitored_names:
                continue
            new_journals.append({
                "journal": journal,
                "issn": None,
                "count": cnt,
                "score": None,
                "novelty": None,
                "source": "recent_recommendations",
                "recommendation_id": None,
            })
            if len(new_journals) >= limit:
                break

    return jsonify({
        "days": days,
        "monitored_count": len(monitored),
        "monitored": monitored[:limit],
        "new_journals": new_journals,
    })


@api_lr_bp.route("/trending-keywords")
def trending_keywords():
    """트렌딩 키워드.

    쿼리:
        researcher_id (선택): 지정하면 해당 연구원의 reference_papers 키워드만 집계.
                              미지정 시 전체 lab 집계.
    """
    limit = int(request.args.get("limit", 10))
    researcher_id = request.args.get("researcher_id", type=int)

    q = db.session.query(PaperKeyword.keyword, func.count(PaperKeyword.id))

    if researcher_id is not None:
        q = q.join(
            ReferencePaper, PaperKeyword.paper_id == ReferencePaper.publication_id
        ).filter(ReferencePaper.researcher_id == researcher_id)

    rows = (
        q.group_by(PaperKeyword.keyword)
        .order_by(func.count(PaperKeyword.id).desc())
        .limit(limit)
        .all()
    )

    return jsonify([
        {"keyword": kw, "count": cnt}
        for kw, cnt in rows
    ])


# =========================================================================
# Lab Agent — 연구실 온톨로지 기반 챗
# =========================================================================


# =========================================================================
# 파이프라인 실시간 진행 상황
# =========================================================================
# 매주 같은 순서로 반복되는 서비스 전 과정(동기화 → 추천 파이프라인 → 저널
# 모니터링 → 메일 발송)이 지금 어디까지 왔는지를 한 번에 돌려준다.
# 진행률의 원본은 scheduler/jobs.py 의 _progress() 가 BatchLog.details["progress"]
# 에 남기는 하트비트다.

#: 한국은 DST가 없어 고정 +09:00 으로 충분하다 (tzdata 의존을 피한다).
KST = timezone(timedelta(hours=9))

#: 진행 하트비트가 이 시간 넘게 멈춰 있으면 죽은 배치로 본다
STALE_HEARTBEAT_SEC = 30 * 60
#: progress 기록이 아예 없는 옛 로그는 시작 시각 기준으로 판단한다
STALE_NO_PROGRESS_SEC = 6 * 3600

_KO_DOW = "월화수목금토일"


def _to_kst(dt: datetime | None):
    """naive UTC datetime(=DB 저장값) 을 KST aware datetime 으로."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(KST)


def _kst_label(dt: datetime | None) -> str:
    k = _to_kst(dt)
    if k is None:
        return ""
    return f"{k.month}/{k.day}({_KO_DOW[k.weekday()]}) {k:%H:%M}"


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _scheduled_at(meta: dict, ref_kst: datetime, forward: bool) -> datetime:
    """meta 의 cron 설정 기준 ref 직후(forward) 또는 직전 예정 시각 (KST)."""
    hour, minute = meta["hour"], meta["minute"]

    if meta["cycle"] == "weekly":
        days = (meta["dow"] - ref_kst.weekday()) % 7
        cand = (ref_kst + timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if cand <= ref_kst:
            cand += timedelta(days=7)
        return cand if forward else cand - timedelta(days=7)

    # monthly — 매월 meta["day"] 일
    day = meta.get("day", 1)
    cand = ref_kst.replace(
        day=day, hour=hour, minute=minute, second=0, microsecond=0
    )
    if cand <= ref_kst:
        cand = (cand.replace(day=1) + timedelta(days=32)).replace(
            day=day, hour=hour, minute=minute, second=0, microsecond=0
        )
    if forward:
        return cand
    prev_month_end = cand.replace(day=1) - timedelta(days=1)
    return prev_month_end.replace(
        day=day, hour=hour, minute=minute, second=0, microsecond=0
    )


def _running_info(log, now_utc: datetime) -> dict:
    """실행 중인 BatchLog 하나를 화면용 dict 로."""
    details = log.details or {}
    prog = details.get("progress") or {}

    beat = _parse_iso(prog.get("at"))
    last_beat = beat or log.started_at
    idle_sec = (now_utc - last_beat).total_seconds() if last_beat else None
    limit = STALE_HEARTBEAT_SEC if beat else STALE_NO_PROGRESS_SEC

    current, total = prog.get("current"), prog.get("total")
    percent = None
    if isinstance(current, int) and isinstance(total, int) and total > 0:
        percent = max(0, min(100, round(current / total * 100)))

    return {
        "step": prog.get("step") or "",
        "detail": prog.get("detail") or "",
        "current": current,
        "total": total,
        "percent": percent,
        "elapsed_sec": int((now_utc - log.started_at).total_seconds())
        if log.started_at else None,
        "idle_sec": int(idle_sec) if idle_sec is not None else None,
        # 하트비트가 멈춘 배치를 "진행 중"으로 보여주면 화면이 거짓말을 한다
        "stale": bool(idle_sec is not None and idle_sec > limit),
        "has_progress": bool(prog),
    }


@api_lr_bp.route("/pipeline-status")
def pipeline_status():
    """주간 서비스 사이클의 실시간 진행 상황.

    Returns:
        active:    지금 실행 중인 배치(진행 단계·퍼센트 포함)
        cycle:     주간 4단계 + 월간 1건의 이번 주기 상태
        scheduler: APScheduler 가 실제로 떠 있는지. 떠 있지 않으면 next_run 은
                   cron 설정으로 계산한 값이며 실제로 실행되지는 않는다.
    """
    from app.litreview.scheduler.jobs import (
        JOB_META,
        WEEKLY_CYCLE,
        scheduler_next_runs,
    )
    from app.models import BatchLog

    now_utc = datetime.utcnow()
    now_kst = _to_kst(now_utc)
    sched_running, sched_next = scheduler_next_runs()

    running_by_type: dict[str, object] = {}
    for log in (
        BatchLog.query.filter(BatchLog.status == "running")
        .order_by(BatchLog.started_at.desc())
        .all()
    ):
        running_by_type.setdefault(log.job_type, log)

    def entry(job_type: str) -> dict:
        meta = JOB_META[job_type]
        running = running_by_type.get(job_type)

        last = (
            BatchLog.query.filter(
                BatchLog.job_type == job_type, BatchLog.status != "running"
            )
            .order_by(BatchLog.started_at.desc())
            .first()
        )

        prev_due_kst = _scheduled_at(meta, now_kst, forward=False)
        prev_due_utc = prev_due_kst.astimezone(timezone.utc).replace(tzinfo=None)

        info = _running_info(running, now_utc) if running is not None else None

        if running is not None:
            state = "stale" if info["stale"] else "running"
        elif last is None:
            state = "never"
        elif last.started_at and last.started_at >= prev_due_utc:
            # 이번 주기 예정 시각 이후에 돌았다 → 이번 주기 처리 완료
            state = last.status
        else:
            state = "pending"

        duration = None
        if last is not None and last.started_at and last.completed_at:
            duration = int((last.completed_at - last.started_at).total_seconds())

        next_iso = sched_next.get(job_type)
        next_dt = _parse_iso(next_iso)
        if next_dt is not None:
            next_label = (
                f"{next_dt.month}/{next_dt.day}"
                f"({_KO_DOW[next_dt.weekday()]}) {next_dt:%H:%M}"
            )
            next_source = "scheduler"
        else:
            nx = _scheduled_at(meta, now_kst, forward=True)
            next_label = f"{nx.month}/{nx.day}({_KO_DOW[nx.weekday()]}) {nx:%H:%M}"
            next_source = "계산"

        return {
            "job_type": job_type,
            "label": meta["label"],
            "desc": meta["desc"],
            "when": meta["when"],
            "cycle": meta["cycle"],
            "state": state,
            "running": info,
            "last_status": last.status if last is not None else None,
            "last_started": _kst_label(last.started_at) if last is not None else "",
            "last_finished": _kst_label(last.completed_at) if last is not None else "",
            "duration_sec": duration,
            "started_label": _kst_label(running.started_at) if running is not None else "",
            "next_run": next_label,
            "next_run_source": next_source,
        }

    cycle = [entry(jt) for jt in WEEKLY_CYCLE]
    monthly = entry("monthly_journal_discovery")

    active = [c for c in cycle + [monthly] if c["running"] is not None]

    return jsonify({
        "now": now_kst.isoformat(),
        "now_label": _kst_label(now_utc),
        "scheduler": {
            "running": sched_running,
            "note": "스케줄러 실행 중"
            if sched_running
            else "스케줄러가 실행 중이 아닙니다 — 예정 시각은 설정값 기준 계산치입니다.",
        },
        "active": active,
        "cycle": cycle,
        "monthly": monthly,
    })


def _onto_slug(s: str) -> str:
    import re as _re
    s = _re.sub(r"\s+", "_", (s or "").strip())
    s = _re.sub(r"[^A-Za-z0-9_]", "_", s)
    return _re.sub(r"_+", "_", s).strip("_") or "x"


def _onto_display_name(raw: str) -> str:
    import re as _re
    if not raw:
        return ""
    m = _re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", raw)
    if not m:
        return raw.strip()
    ko, en = m.group(1).strip(), m.group(2).strip()
    parts = en.split()
    if len(parts) >= 2:
        return f"{ko}\n({parts[-1].capitalize()} {' '.join(p.capitalize() for p in parts[:-1])})"
    return f"{ko} ({en.capitalize()})"


@api_lr_bp.route("/ontology/tbox")
def ontology_tbox():
    """TBox (스키마) 그래프 — 클래스와 프로퍼티 관계."""
    nodes = [
        {"id": "C_FoafPerson", "label": "foaf:Person", "group": "class_external",
         "title": "External: foaf:Person (FOAF vocabulary)"},
        {"id": "C_ResearchLab", "label": "ResearchLab", "group": "class_lab",
         "title": "An academic research laboratory"},
        {"id": "C_ResearchGroup", "label": "ResearchGroup", "group": "class_group",
         "title": "Sub-team within a Research Lab"},
        {"id": "C_Researcher", "label": "Researcher", "group": "class_researcher",
         "title": "A member researcher"},
        {"id": "C_ResearchKeyword", "label": "ResearchKeyword", "group": "class_keyword",
         "title": "Author keyword from papers"},
        {"id": "C_KeywordUsage", "label": "KeywordUsage", "group": "class_usage",
         "title": "Reified frequency: who used which keyword how many times"},
    ]
    edges = [
        {"from": "C_Researcher", "to": "C_FoafPerson",
         "label": "rdfs:subClassOf", "rel": "subClassOf", "dashes": True},
        {"from": "C_ResearchLab", "to": "C_Researcher",
         "label": "hasMember", "rel": "hasMember"},
        {"from": "C_Researcher", "to": "C_ResearchLab",
         "label": "memberOf", "rel": "memberOf", "dashes": True},
        {"from": "C_Researcher", "to": "C_ResearchGroup",
         "label": "inGroup", "rel": "inGroup"},
        {"from": "C_Researcher", "to": "C_ResearchKeyword",
         "label": "hasKeyword", "rel": "hasKeyword"},
        {"from": "C_KeywordUsage", "to": "C_ResearchKeyword",
         "label": "usageOf", "rel": "usageOf"},
        {"from": "C_KeywordUsage", "to": "C_Researcher",
         "label": "usageBy", "rel": "usageBy"},
    ]
    # datatype 프로퍼티는 chip으로 표현
    datatype_props = {
        "C_Researcher": ["scopusId", "researcherType", "paperCount"],
        "C_ResearchKeyword": ["keywordRaw"],
        "C_KeywordUsage": ["frequency"],
    }
    return jsonify({
        "nodes": nodes,
        "edges": edges,
        "datatype_props": datatype_props,
        "stats": {
            "classes": len(nodes),
            "object_properties": len([e for e in edges if not e.get("dashes")]),
        },
    })


@api_lr_bp.route("/ontology/abox")
def ontology_abox():
    """ABox (인스턴스) 그래프 — 실제 BIST Lab 데이터 전체."""
    top_n_kw = request.args.get("top_n_kw", type=int)  # 미지정 = 전체

    nodes: list[dict] = []
    edges: list[dict] = []

    # Lab
    nodes.append({
        "id": "LAB", "label": "BIST Lab", "group": "lab",
        "title": "BIST Research Lab", "size": 30,
    })

    # Groups
    # 팀은 홈페이지 teams 테이블에서 읽는다
    # (Researcher.group 은 파이썬 속성이라 SQL 문맥에서 쓸 수 없다)
    from app.models import Team

    groups = [(t.name,) for t in Team.query.order_by(Team.sort_order).all()]
    group_ids = set()
    for (g,) in groups:
        gid = f"G_{_onto_slug(g)}"
        if gid in group_ids:
            continue
        group_ids.add(gid)
        nodes.append({"id": gid, "label": g, "group": "group",
                      "title": f"Group: {g}", "size": 20})
        edges.append({"from": "LAB", "to": gid,
                      "label": "hasMember", "rel": "hasMember"})

    # Researchers
    researchers = Researcher.query.order_by(Researcher.id).all()
    for r in researchers:
        rid = f"R_{r.id}"
        title_parts = []
        if r.scopus_id: title_parts.append(f"Scopus: {r.scopus_id}")
        if r.researcher_type: title_parts.append(f"Type: {r.researcher_type}")
        nodes.append({
            "id": rid,
            "label": _onto_display_name(r.name),
            "group": "researcher",
            "title": " · ".join(title_parts) or r.name,
            "size": 15,
        })
        if r.group:
            edges.append({"from": f"G_{_onto_slug(r.group)}", "to": rid,
                          "label": "hasMember", "rel": "hasMember"})
        else:
            edges.append({"from": "LAB", "to": rid,
                          "label": "hasMember", "rel": "hasMember"})

    # Keywords (per researcher freq) + Researcher→Keyword 엣지
    seen_kw: dict[str, str] = {}
    for r in researchers:
        q = (
            db.session.query(PaperKeyword.keyword, func.count(PaperKeyword.id))
            .join(ReferencePaper, ReferencePaper.publication_id == PaperKeyword.paper_id)
            .filter(ReferencePaper.researcher_id == r.id)
            .group_by(PaperKeyword.keyword)
            .order_by(func.count(PaperKeyword.id).desc())
        )
        if top_n_kw:
            q = q.limit(top_n_kw)
        for kw, cnt in q.all():
            kid = f"KW_{_onto_slug(kw)}"
            if kid not in seen_kw:
                seen_kw[kid] = kw
                nodes.append({
                    "id": kid, "label": kw, "group": "keyword",
                    "title": f"Keyword: {kw}", "size": 8,
                })
            edges.append({
                "from": f"R_{r.id}", "to": kid,
                "label": str(cnt), "rel": "hasKeyword", "freq": int(cnt),
            })

    return jsonify({
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "researchers": len(researchers),
            "groups": len(group_ids),
            "keywords": len(seen_kw),
            "edges": len(edges),
            "total_nodes": len(nodes),
        },
    })


# 하위 호환 — 기존 호출이 있을 수 있어 유지 (abox로 위임)
@api_lr_bp.route("/ontology/graph")
def ontology_graph():
    return ontology_abox()


# =========================================================================
# 주간 피드 — home_v3 (화이트 테마 3단 레이아웃) 전용
# =========================================================================


def _display_name(name: str) -> str:
    if name and "(" in name:
        return name.split("(")[0].strip()
    return name or ""


def _rec_dict(rec) -> dict:
    """피드 카드용 추천 직렬화."""
    p = rec.paper
    return {
        "id": rec.id,
        "paper_id": rec.paper_id,
        "title": p.title if p else "",
        "authors": p.authors if p else "",
        "journal": p.journal if p else "",
        "year": p.year if p else None,
        "doi": p.doi if p else "",
        "abstract": (p.abstract or "")[:400] if p else "",
        "grade": rec.grade,
        "score": round((rec.similarity_score or 0) * 100),
        "topic_name": rec.topic.name if rec.topic else "",
        "summary_core_topic": rec.summary_core_topic or "",
        "summary_purpose": rec.summary_purpose or "",
        "summary_method": rec.summary_method or "",
        "summary_results": rec.summary_results or "",
        "recommendation_reason": rec.recommendation_reason or "",
        "is_saved": rec.is_saved,
        "is_read": rec.is_read,
        "user_feedback": rec.user_feedback,
        "created_at": str(rec.created_at) if rec.created_at else "",
    }


@api_lr_bp.route("/feed/weekly")
def weekly_feed():
    """최근 N일 추천을 연구원별로 그룹핑한 종합 피드.

    Query:
        days: 조회 기간 (기본 7일)
        per_researcher: 연구원당 최대 논문 수 (기본 10)

    Returns:
        {
          period: {days, cutoff},
          totals: {recommendations, core, related, reference, saved, unread},
          top_journals: [{journal, count}],
          researchers: [{id, name, display_name, group, count, unread_count,
                         recommendations: [...]}]
        }
    """
    days = int(request.args.get("days", 7))
    per_researcher = int(request.args.get("per_researcher", 10))
    cutoff = datetime.utcnow() - timedelta(days=days)

    recs = (
        PaperRecommendation.query
        .filter(PaperRecommendation.created_at >= cutoff)
        .order_by(PaperRecommendation.similarity_score.desc())
        .all()
    )

    # 연구원별 그룹핑 (동일 논문이 주제별로 중복 추천될 수 있어 paper_id로 dedup)
    grade_order = {"core": 0, "related": 1, "reference": 2}
    by_researcher: dict[int, list] = {}
    seen: dict[int, set] = {}
    for rec in recs:
        rid = rec.researcher_id
        seen.setdefault(rid, set())
        if rec.paper_id in seen[rid]:
            continue
        seen[rid].add(rec.paper_id)
        by_researcher.setdefault(rid, []).append(rec)

    totals = {"recommendations": 0, "core": 0, "related": 0, "reference": 0,
              "saved": 0, "unread": 0}
    journal_freq: dict[str, int] = {}
    researchers_out = []

    for rid, rec_list in by_researcher.items():
        r = db.session.get(Researcher, rid)
        if not r:
            continue

        # 등급 우선 + 점수 순 정렬
        rec_list.sort(key=lambda x: (grade_order.get(x.grade, 9),
                                     -(x.similarity_score or 0)))

        unread = sum(1 for x in rec_list if not x.is_read)
        for x in rec_list:
            totals["recommendations"] += 1
            if x.grade in totals:
                totals[x.grade] += 1
            if x.is_saved:
                totals["saved"] += 1
            if x.paper and x.paper.journal:
                journal_freq[x.paper.journal] = journal_freq.get(x.paper.journal, 0) + 1
        totals["unread"] += unread

        researchers_out.append({
            "id": rid,
            "name": r.name,
            "display_name": _display_name(r.name),
            "group": r.group or "",
            "count": len(rec_list),
            "unread_count": unread,
            "recommendations": [_rec_dict(x) for x in rec_list[:per_researcher]],
        })

    # 추천 수 많은 순
    researchers_out.sort(key=lambda x: -x["count"])

    top_journals = sorted(journal_freq.items(), key=lambda x: -x[1])[:5]

    return jsonify({
        "period": {"days": days, "cutoff": str(cutoff)},
        "totals": totals,
        "top_journals": [{"journal": j, "count": c} for j, c in top_journals],
        "researchers": researchers_out,
    })


@api_lr_bp.route("/notifications")
def notifications():
    """알림 — 최근 배치 실행 + 이메일 발송 + (로그인 시) 내 미열람 추천 수."""
    from app.models import BatchLog, EmailLog

    items = []

    for log in BatchLog.query.order_by(BatchLog.started_at.desc()).limit(5).all():
        items.append({
            "type": "batch",
            "title": log.job_type,
            "status": log.status,
            "at": str(log.started_at),
        })

    rid = session.get("researcher_id")
    unread_count = 0
    if rid:
        unread_count = PaperRecommendation.query.filter_by(
            researcher_id=rid, is_read=False
        ).count()
        for el in (
            EmailLog.query.filter_by(researcher_id=rid)
            .order_by(EmailLog.sent_at.desc())
            .limit(3)
            .all()
        ):
            items.append({
                "type": "email",
                "title": el.subject,
                "status": el.status,
                "at": str(el.sent_at),
            })

    items.sort(key=lambda x: x["at"], reverse=True)
    return jsonify({"unread_count": unread_count, "items": items[:8]})


# =========================================================================
# 관심 저자 — OpenAlex Author ID 기준
#
# 저자는 이름으로 식별하지 않는다. 동명이인이 흔하고(OpenAlex 기준
# 'Sungmin Yoon' 33명) 한 사람도 논문마다 표기가 달라지기 때문에,
# 검색 결과를 사용자가 직접 고르게 한 뒤 Author ID로 저장한다.
#
# 등록 경로 3가지:
#   1) 이름 검색   : GET  /authors/search?q=...  → POST /authors/follow
#   2) 논문 상세   : GET  /papers/<pid>/authors  → POST /authors/follow
#   3) ORCID 직접  : GET  /authors/by-orcid?orcid=... → POST /authors/follow
# =========================================================================


def _require_login(rid: int | None = None):
    """로그인 확인. rid를 주면 본인 여부까지 확인. 문제 없으면 None."""
    me = session.get("researcher_id")
    if not me:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    if rid is not None and me != rid:
        return jsonify({"error": "권한 없음"}), 403
    return None


@api_lr_bp.route("/authors/search")
def search_authors_api():
    """이름으로 후보 저자 검색 (경로 1).

    바로 등록하지 않고 소속·연구분야·대표논문·최근논문을 붙여 반환한다.
    사용자가 카드에서 정확한 저자를 고르면 그때 /authors/follow로 등록한다.

    Query: q=이름, limit=후보 수(기본 5, 최대 10)
    """
    err = _require_login()
    if err:
        return err

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "두 글자 이상 입력하세요.", "candidates": []}), 400

    limit = min(request.args.get("limit", default=5, type=int), 10)

    from app.litreview.author import openalex, service

    candidates = openalex.search_authors(q, limit=limit)
    following = service.followed_author_ids(session["researcher_id"])
    for c in candidates:
        c["following"] = c["openalex_author_id"] in following

    return jsonify({"query": q, "count": len(candidates), "candidates": candidates})


@api_lr_bp.route("/authors/by-orcid")
def author_by_orcid_api():
    """ORCID로 저자 조회 (경로 3). 등록 전에 확인용으로 보여준다."""
    err = _require_login()
    if err:
        return err

    from app.litreview.author import openalex, service

    raw = (request.args.get("orcid") or "").strip()
    norm = openalex.normalize_orcid(raw)
    if not norm:
        return jsonify({
            "error": "ORCID 형식이 올바르지 않습니다. 0000-0000-0000-0000 형태로 입력하세요."
        }), 400

    author = openalex.get_author_by_orcid(norm)
    if not author:
        return jsonify({"error": f"ORCID {norm} 에 해당하는 저자를 찾지 못했습니다."}), 404

    aid = author["openalex_author_id"]
    author["representative_papers"] = openalex.author_top_papers(aid, 3)
    author["recent_papers"] = openalex.author_recent_papers(aid, 2)
    author["following"] = aid in service.followed_author_ids(session["researcher_id"])
    return jsonify({"orcid": norm, "author": author})


@api_lr_bp.route("/papers/<int:pid>/authors")
def paper_authors_api(pid):
    """논문의 저자 목록을 Author ID까지 붙여 반환 (경로 2).

    Scopus 수집 경로가 저자를 저장하지 못하므로(STANDARD view 제약)
    논문 DOI로 OpenAlex에서 저자를 해석한다. 이름 검색보다 정확하다.
    """
    err = _require_login()
    if err:
        return err

    paper = db.session.get(CollectedPaper, pid)
    if not paper:
        return jsonify({"error": "논문을 찾을 수 없습니다."}), 404
    if not paper.doi:
        return jsonify({
            "paper_id": pid, "authors": [],
            "error": "이 논문에는 DOI가 없어 저자를 식별할 수 없습니다.",
        })

    from app.litreview.author import openalex, service

    authors = openalex.authors_of_doi(paper.doi)
    following = service.followed_author_ids(session["researcher_id"])
    for a in authors:
        a["following"] = a["openalex_author_id"] in following

    return jsonify({"paper_id": pid, "doi": paper.doi, "authors": authors})


@api_lr_bp.route("/authors/follow", methods=["POST"])
def follow_author_api():
    """관심 저자 등록 — 세 경로가 모두 이 엔드포인트를 쓴다.

    body: {"openalex_author_id": "A5045676373", "source": "name_search"}
    저자 상세는 서버가 OpenAlex에서 다시 확인해 저장하므로,
    클라이언트가 보낸 이름/소속을 신뢰하지 않는다.
    """
    err = _require_login()
    if err:
        return err
    rid = session["researcher_id"]

    data = request.get_json() or {}
    oa_id = (data.get("openalex_author_id") or "").strip()
    source = data.get("source") or "name_search"
    if source not in ("name_search", "paper_detail", "orcid"):
        source = "name_search"

    from app.litreview.author import openalex, service

    if not oa_id:
        # ORCID만 준 경우도 허용
        orcid = openalex.normalize_orcid(data.get("orcid"))
        if not orcid:
            return jsonify({"error": "openalex_author_id 또는 orcid가 필요합니다."}), 400
        brief = openalex.get_author_by_orcid(orcid)
        source = "orcid"
    else:
        brief = openalex.get_author(oa_id)

    if not brief or not brief.get("openalex_author_id"):
        return jsonify({"error": "OpenAlex에서 저자를 찾지 못했습니다."}), 404

    result = service.follow_author(rid, brief, source=source)
    return jsonify(result), 200 if result["already_following"] else 201


@api_lr_bp.route("/researchers/<int:rid>/authors", methods=["GET"])
def list_followed_authors_api(rid):
    """관심 저자 목록."""
    from app.litreview.author import service

    if not db.session.get(Researcher, rid):
        return jsonify({"error": "Researcher not found"}), 404
    return jsonify(service.list_followed_authors(rid))


@api_lr_bp.route("/researchers/<int:rid>/authors/<int:author_id>", methods=["DELETE"])
def unfollow_author_api(rid, author_id):
    """관심 저자 해제."""
    err = _require_login(rid)
    if err:
        return err

    from app.litreview.author import service

    if not service.unfollow_author(rid, author_id):
        return jsonify({"error": "등록되지 않은 저자입니다."}), 404
    return jsonify({"deleted": True})


@api_lr_bp.route("/researchers/<int:rid>/author-papers", methods=["GET"])
def list_author_papers_api(rid):
    """연구원이 트래킹 중인 저자들의 신규 논문.

    쿼리 파라미터:
        days   최초 발견 기준 기간. 0 이면 전체 (기본 0)
        limit  최대 개수 (기본 100)

    저자 단위로 묶지 않고 논문을 시간 역순으로 돌려준다 — 화면이 "무엇이
    새로 나왔나"를 보여주는 피드이기 때문이다. 각 논문에 어느 저자를 통해
    발견됐는지 붙인다.
    """
    from app.models import Author, AuthorFollow, AuthorPaper

    if not db.session.get(Researcher, rid):
        return jsonify({"error": "Researcher not found"}), 404

    days = request.args.get("days", default=0, type=int)
    limit = request.args.get("limit", default=100, type=int)

    q = (
        db.session.query(AuthorPaper, Author)
        .join(Author, Author.id == AuthorPaper.author_id)
        .join(AuthorFollow, AuthorFollow.author_id == Author.id)
        .filter(AuthorFollow.researcher_id == rid)
    )
    if days > 0:
        q = q.filter(AuthorPaper.first_seen_at >= datetime.utcnow() - timedelta(days=days))

    rows = (
        q.order_by(
            AuthorPaper.first_seen_at.desc(),
            AuthorPaper.year.desc(),
        )
        .limit(limit)
        .all()
    )

    followed = (
        db.session.query(Author)
        .join(AuthorFollow, AuthorFollow.author_id == Author.id)
        .filter(AuthorFollow.researcher_id == rid)
        .all()
    )

    papers = []
    for ap, author in rows:
        papers.append({
            "id": ap.id,
            "title": ap.title,
            "journal": ap.journal,
            "year": ap.year,
            "publication_date": ap.publication_date,
            "doi": ap.doi,
            "abstract": ap.abstract,
            "cited_by_count": ap.cited_by_count,
            "openalex_work_id": ap.openalex_work_id,
            "collected_paper_id": ap.collected_paper_id,
            "first_seen_at": str(ap.first_seen_at) if ap.first_seen_at else None,
            "author": {
                "id": author.id,
                "display_name": author.display_name,
                "affiliation": author.affiliation,
                "openalex_author_id": author.openalex_author_id,
                "orcid": author.orcid,
            },
        })

    return jsonify({
        "researcher_id": rid,
        "followed_authors": [
            {
                "id": a.id,
                "display_name": a.display_name,
                "affiliation": a.affiliation,
                "orcid": a.orcid,
                "openalex_author_id": a.openalex_author_id,
                "last_checked_at": str(a.last_checked_at) if a.last_checked_at else None,
            }
            for a in followed
        ],
        "followed_count": len(followed),
        "paper_count": len(papers),
        "papers": papers,
    })


@api_lr_bp.route("/researchers/<int:rid>/authors/track", methods=["POST"])
def track_authors_api(rid):
    """관심 저자 신규 논문 즉시 확인 (주간 배치와 동일 로직).

    Author ID로 조회하므로 표기가 달라도 같은 저자로 추적된다.
    """
    err = _require_login(rid)
    if err:
        return err

    from app.litreview.author import service

    result = service.track_followed_authors(rid)
    total_new = sum(a["new_papers"] for a in result["authors"])
    return jsonify({**result, "total_new_papers": total_new})


# =========================================================================
# 마이페이지 설정 조회
# =========================================================================


# =========================================================================
# 내 연구 소개 (research_notes) — 관심 주제별로 여러 건 작성/수정/삭제
#
# Scopus 검색이나 유사도 순위에는 관여하지 않고, core 등급 논문의 '추천 이유'를
# Claude가 쓸 때 연구원 프로필로 전달된다
# (recommendation/summarizer.py의 _build_researcher_profile).
# =========================================================================

MAX_NOTE_LENGTH = 4000


def _note_dict(n) -> dict:
    return {
        "id": n.id,
        "title": n.title or "",
        "content": n.content or "",
        "length": len(n.content or ""),
        "sort_order": n.sort_order,
        "created_at": str(n.created_at) if n.created_at else "",
        "updated_at": str(n.updated_at) if n.updated_at else "",
    }


def _list_notes(rid: int) -> list[dict]:
    from app.models import ResearchNote

    notes = (
        ResearchNote.query.filter_by(researcher_id=rid)
        .order_by(ResearchNote.sort_order, ResearchNote.id)
        .all()
    )
    return [_note_dict(n) for n in notes]


@api_lr_bp.route("/researchers/<int:rid>/research-notes", methods=["GET"])
def list_research_notes(rid):
    """내 연구 소개 목록."""
    if not db.session.get(Researcher, rid):
        return jsonify({"error": "Not found"}), 404
    return jsonify(_list_notes(rid))


@api_lr_bp.route("/researchers/<int:rid>/research-notes", methods=["POST"])
def create_research_note(rid):
    """연구 소개 추가. 관심 주제가 여러 개면 여러 건 만들 수 있다."""
    if session.get("researcher_id") != rid:
        return jsonify({"error": "권한 없음"}), 403
    if not db.session.get(Researcher, rid):
        return jsonify({"error": "Not found"}), 404

    from app.models import ResearchNote

    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    title = (data.get("title") or "").strip()
    if not content:
        return jsonify({"error": "내용을 입력하세요."}), 400
    if len(content) > MAX_NOTE_LENGTH:
        return jsonify({
            "error": f"연구 소개는 {MAX_NOTE_LENGTH}자까지 저장할 수 있습니다."
        }), 400

    max_order = (
        db.session.query(func.max(ResearchNote.sort_order))
        .filter_by(researcher_id=rid)
        .scalar()
    ) or 0

    note = ResearchNote(
        researcher_id=rid,
        title=title[:200] or None,
        content=content,
        sort_order=max_order + 1,
    )
    db.session.add(note)
    db.session.commit()
    logger.info("Research note created: researcher %d, note %d (%d자)",
                rid, note.id, len(content))
    return jsonify(_note_dict(note)), 201


@api_lr_bp.route("/research-notes/<int:nid>", methods=["PUT"])
def update_research_note(nid):
    """연구 소개 수정."""
    from app.models import ResearchNote

    note = db.session.get(ResearchNote, nid)
    if not note:
        return jsonify({"error": "Not found"}), 404
    if session.get("researcher_id") != note.researcher_id:
        return jsonify({"error": "권한 없음"}), 403

    data = request.get_json() or {}
    if "content" in data:
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "내용을 비울 수 없습니다. 삭제하려면 삭제 버튼을 쓰세요."}), 400
        if len(content) > MAX_NOTE_LENGTH:
            return jsonify({
                "error": f"연구 소개는 {MAX_NOTE_LENGTH}자까지 저장할 수 있습니다."
            }), 400
        note.content = content
    if "title" in data:
        note.title = (data.get("title") or "").strip()[:200] or None

    note.updated_at = datetime.utcnow()
    db.session.commit()
    logger.info("Research note updated: note %d (researcher %d)", nid, note.researcher_id)
    return jsonify(_note_dict(note))


@api_lr_bp.route("/research-notes/<int:nid>", methods=["DELETE"])
def delete_research_note(nid):
    """연구 소개 삭제."""
    from app.models import ResearchNote

    note = db.session.get(ResearchNote, nid)
    if not note:
        return jsonify({"error": "Not found"}), 404
    if session.get("researcher_id") != note.researcher_id:
        return jsonify({"error": "권한 없음"}), 403

    db.session.delete(note)
    db.session.commit()
    logger.info("Research note deleted: note %d", nid)
    return jsonify({"deleted": True})


@api_lr_bp.route("/researchers/<int:rid>/my-settings")
def my_settings(rid):
    """마이페이지 설정 조회 — 추천 주기 + 주제 키워드 + 관심 키워드/저자.

    키워드는 성격이 다른 3종류가 있고 쓰이는 곳도 다르다:
      topics[].keywords : 주간 Scopus 검색 쿼리를 만드는 유일한 값 (편집 대상)
      paper_keywords    : 본인 논문의 저자 키워드 (자동 주제 생성 재료, 읽기 전용)
      custom_keywords   : 월간 저널 탐색에만 쓰임 (논문 검색에는 미반영)
    """
    from app.litreview.author import service as author_service
    from app.models import CustomKeyword

    r = db.session.get(Researcher, rid)
    if not r:
        return jsonify({"error": "Not found"}), 404

    keywords = CustomKeyword.query.filter_by(researcher_id=rid).all()

    topics = (
        ResearchTopic.query.filter_by(researcher_id=rid)
        .order_by(ResearchTopic.sort_order, ResearchTopic.id)
        .all()
    )

    # 본인 논문에서 추출된 저자 키워드 (빈도순) — 주제 키워드에 담을 후보
    paper_kw_rows = (
        db.session.query(PaperKeyword.keyword, func.count(PaperKeyword.id))
        .join(ReferencePaper, PaperKeyword.paper_id == ReferencePaper.publication_id)
        .filter(ReferencePaper.researcher_id == rid)
        .group_by(PaperKeyword.keyword)
        .order_by(func.count(PaperKeyword.id).desc())
        .limit(30)
        .all()
    )

    return jsonify({
        "topics": [_topic_dict(t) for t in topics],
        "search_keyword_count": sum(len(t.keywords or []) for t in topics),
        "paper_keywords": [
            {"keyword": kw, "count": cnt} for kw, cnt in paper_kw_rows
        ],
        "id": r.id,
        "name": r.name,
        "display_name": _display_name(r.name),
        "email": r.email,
        "group": r.group or "",
        "researcher_type": r.researcher_type or "C",
        # 연구 소개는 여러 건 작성 가능 (research_notes). 구버전 단일 필드는
        # 마이그레이션으로 노트에 옮겨졌고, 아직 남아 있으면 그대로 노출한다.
        "research_notes": _list_notes(rid),
        "research_description": r.research_description or "",
        "email_cycle_weeks": r.email_cycle_weeks or 1,
        "settings": {
            "core_percent": r.core_percent,
            "related_percent": r.related_percent,
            "reference_percent": r.reference_percent,
            "target_year_range": r.target_year_range,
        },
        "custom_keywords": [
            {"id": k.id, "keyword": k.keyword} for k in keywords
        ],
        # OpenAlex Author ID 기준 관심 저자 (이름 기반 구버전 대체)
        "followed_authors": author_service.list_followed_authors(rid),
    })


# =========================================================================
# Lab Agent — 연구실 온톨로지 기반 챗
# =========================================================================


@api_lr_bp.route("/agent/chat", methods=["POST"])
def agent_chat():
    """연구실 온톨로지 기반 룰 챗.

    body: {"message": "<질문>"}
    return: {"answer": str, "intent": str, "data": dict}
    """
    from app.litreview.agent import lab_agent

    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"answer": "질문을 입력해 주세요.", "intent": "empty", "data": {}}), 400

    try:
        result = lab_agent.answer(message)
        return jsonify(result)
    except Exception:
        logger.exception("Lab agent failed")
        return jsonify({
            "answer": "죄송합니다, 응답 생성 중 오류가 발생했습니다.",
            "intent": "error",
            "data": {},
        }), 500
