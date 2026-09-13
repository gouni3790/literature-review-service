"""Scopus ID가 있는데 아직 논문을 안 가져온 연구원에 대해 전체 파이프라인 실행.

각 연구원별로:
1. Scopus에서 논문 가져오기
2. 임베딩
3. 유형별 자동 주제 생성 (A: 클러스터링, B: 자동 주제 1개)
4. 참고문헌 역추적 → target_journals 등록
"""
import sys
import io
import time
import logging
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from app import create_app, db
from app.models import Researcher, ReferencePaper
from app.litreview.profile.scopus_fetcher import fetch_researcher_publications
from app.litreview.recommendation.embedder import Embedder
from app.litreview.profile.cluster_analyzer import analyze_clusters
from app.litreview.profile.topic_manager import create_auto_topic_type_b
from app.litreview.journal.journal_discovery import discover_journals_from_references


def run_pipeline(rid: int) -> dict:
    """단일 연구원에 대한 전체 파이프라인."""
    r = db.session.get(Researcher, rid)
    if not r or not r.scopus_id:
        return {"error": "no scopus_id"}

    print(f"\n{'='*70}")
    print(f"▶ Researcher {rid}: {r.name}  (scopus_id={r.scopus_id})")
    print(f"{'='*70}")

    result = {"id": rid, "name": r.name}

    # 1. Scopus 논문 가져오기
    try:
        fetch_result = fetch_researcher_publications(r.scopus_id, rid)
        result.update(fetch_result)
        print(f"  ✓ Fetched: {fetch_result.get('papers_added', 0)}편 신규")
        print(f"  ✓ researcher_type: {fetch_result.get('researcher_type')}")
    except Exception as e:
        print(f"  ✗ Fetch 실패: {e}")
        traceback.print_exc()
        return {**result, "error": str(e)}

    paper_count = ReferencePaper.query.filter_by(researcher_id=rid).count()
    if paper_count == 0:
        print(f"  ⚠ 논문 0편 → C유형 유지, 더 진행 안 함")
        return result

    # 2. 임베딩
    try:
        embedder = Embedder()
        embedded = embedder.embed_paper_abstracts(
            table="reference_papers", researcher_id=rid
        )
        result["embedded"] = embedded
        print(f"  ✓ Embedded: {embedded}편")
    except Exception as e:
        print(f"  ✗ 임베딩 실패: {e}")
        traceback.print_exc()
        return {**result, "error": str(e)}

    # 3. 유형별 자동 주제
    rtype = result.get("researcher_type")
    try:
        if rtype == "A":
            cr = analyze_clusters(rid)
            result["clusters"] = {
                "k": cr.get("cluster_count"),
                "silhouette": cr.get("silhouette"),
            }
            print(f"  ✓ Clustering: K={cr.get('cluster_count')}, "
                  f"silhouette={cr.get('silhouette'):.4f}" if cr.get('silhouette') else "")
        elif rtype == "B":
            bt = create_auto_topic_type_b(rid)
            result["auto_topic"] = bt
            print(f"  ✓ B-type auto topic 생성")
    except Exception as e:
        print(f"  ✗ 자동 주제 생성 실패: {e}")
        traceback.print_exc()

    # 4. 참고문헌 역추적
    try:
        ref_journals = discover_journals_from_references(rid)
        result["reference_journals"] = len(ref_journals)
        print(f"  ✓ Reference journals: {len(ref_journals)}개 등록")
    except Exception as e:
        print(f"  ✗ 저널 역추적 실패: {e}")
        traceback.print_exc()

    return result


def main():
    app = create_app()
    with app.app_context():
        # 논문 0편 + Scopus ID 있는 연구원
        targets = (
            Researcher.query.filter(
                Researcher.scopus_id.isnot(None),
                Researcher.scopus_id != "",
            )
            .order_by(Researcher.id)
            .all()
        )
        # 이미 논문이 있는 연구원은 제외 (재실행 방지)
        pending = []
        for r in targets:
            count = ReferencePaper.query.filter_by(researcher_id=r.id).count()
            if count == 0:
                pending.append(r)

        print(f"\n총 {len(pending)}명 처리 예정:")
        for r in pending:
            print(f"  ID {r.id}: {r.name}")

        results = []
        for r in pending:
            try:
                res = run_pipeline(r.id)
                results.append(res)
            except Exception as e:
                print(f"\n[CRITICAL] {r.name} 처리 중 예외:")
                traceback.print_exc()
                results.append({"id": r.id, "error": str(e)})

            # 다음 연구원 사이에 간격
            time.sleep(2)

        # 최종 요약
        print(f"\n\n{'='*70}")
        print("=== 최종 요약 ===")
        print(f"{'='*70}")
        for res in results:
            paper_count = ReferencePaper.query.filter_by(researcher_id=res["id"]).count()
            err = res.get("error", "")
            print(f"  ID {res['id']}: {res['name']:<40} 논문={paper_count:<4} {'✗ ' + err if err else '✓'}")


if __name__ == "__main__":
    main()
