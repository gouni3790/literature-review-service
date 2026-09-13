"""초록 임베딩 클러스터링 (10편+ 연구원만). K-Means + cosine silhouette.

클러스터링 후 자동 주제(research_topics, source_type='auto') 생성:
- 클러스터별 대표 논문 1편 + centroid 인접 2편 = 3편
- 3편의 초록을 이어붙여 → 주제 대표 벡터
"""

import logging

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import normalize

from app import db
from app.models import (
    ReferencePaper,
    ResearchCluster,
    ResearchTopic,
    Researcher,
    TopicReferencePaper,
)

logger = logging.getLogger(__name__)

MIN_PAPERS_FOR_CLUSTERING = 10
MIN_K = 2
MAX_K = 8


def _find_optimal_k(embeddings_norm: np.ndarray) -> int:
    """Silhouette score(cosine) 기반 최적 K 탐색.

    Args:
        embeddings_norm: L2 정규화된 임베딩 행렬.
    """
    n = len(embeddings_norm)
    max_k = min(MAX_K, n - 1)
    if max_k < MIN_K:
        return MIN_K

    best_k = MIN_K
    best_score = -1.0

    for k in range(MIN_K, max_k + 1):
        km = KMeans(n_clusters=k, n_init=20, random_state=42)
        labels = km.fit_predict(embeddings_norm)
        score = silhouette_score(embeddings_norm, labels, metric="cosine")
        logger.debug("K=%d, silhouette(cosine)=%.4f", k, score)
        if score > best_score:
            best_score = score
            best_k = k

    logger.info("Optimal K=%d (silhouette=%.4f)", best_k, best_score)
    return best_k


def analyze_clusters(researcher_id: int) -> dict:
    """연구원의 기존 논문 임베딩으로 클러스터링 → 자동 주제 생성.

    개선 사항 (독립 스크립트 대비 동일 품질):
    - L2 정규화 후 클러스터링 (벡터 크기 영향 제거)
    - cosine 메트릭 silhouette score
    - cosine similarity 기반 대표 논문 선정
    - MAX_K=8, n_init=20

    Args:
        researcher_id: researchers.id

    Returns:
        {cluster_count, silhouette, clusters: [{label, paper_count, ...}]}
    """
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        raise ValueError(f"Researcher {researcher_id} not found")

    papers = (
        ReferencePaper.query.filter_by(researcher_id=researcher_id)
        .filter(ReferencePaper.embedding.isnot(None))
        .all()
    )

    if len(papers) < MIN_PAPERS_FOR_CLUSTERING:
        logger.warning(
            "Researcher %d has %d embedded papers (need %d+), skipping clustering",
            researcher_id,
            len(papers),
            MIN_PAPERS_FOR_CLUSTERING,
        )
        return {"cluster_count": 0, "clusters": [], "skipped": True}

    embeddings_raw = np.array([p.embedding for p in papers])

    # L2 정규화 → 유클리드 거리 ∝ 코사인 거리
    embeddings_norm = normalize(embeddings_raw)

    # 기존 클러스터 + 자동 주제 제거
    ResearchCluster.query.filter_by(researcher_id=researcher_id).delete()
    ReferencePaper.query.filter_by(researcher_id=researcher_id).update(
        {"cluster_id": None, "is_representative": False}
    )

    # 기존 자동 주제 삭제 (수동 주제는 유지)
    # 단, 사용자가 키워드를 직접 편집한 주제(keywords_locked)는 삭제하지 않고
    # 수동 주제로 승격시킨다. 재클러스터링은 클러스터 번호를 새로 배정하므로
    # 번호로 매칭해 되돌릴 수 없고, 그냥 지우면 사용자 편집분이 조용히 사라진다.
    auto_topics = ResearchTopic.query.filter_by(
        researcher_id=researcher_id, source_type="auto"
    ).all()
    for at in auto_topics:
        if at.keywords_locked:
            at.source_type = "manual"
            at.cluster_label = None
            logger.info(
                "Researcher %d: locked auto topic %d ('%s') promoted to manual "
                "to survive re-clustering",
                researcher_id,
                at.id,
                at.name,
            )
        else:
            db.session.delete(at)

    db.session.flush()

    # K 탐색 + 클러스터링 (정규화된 벡터 사용)
    k = _find_optimal_k(embeddings_norm)
    km = KMeans(n_clusters=k, n_init=20, random_state=42)
    labels = km.fit_predict(embeddings_norm)
    sil_score = float(silhouette_score(embeddings_norm, labels, metric="cosine"))

    cluster_results = []
    for label in range(k):
        mask = labels == label
        cluster_papers = [p for p, m in zip(papers, mask) if m]
        cluster_embeddings = embeddings_norm[mask]
        centroid = km.cluster_centers_[label]

        # cosine similarity 기반 대표 논문 선정
        # 정규화된 벡터에서 centroid와의 내적 = cosine similarity
        centroid_norm = centroid / (np.linalg.norm(centroid) + 1e-10)
        similarities = cluster_embeddings @ centroid_norm
        sorted_indices = np.argsort(-similarities)  # 유사도 높은 순

        # 대표 논문 1편 + 인접 2편 = 최대 3편
        top_n = min(3, len(cluster_papers))
        representative_papers = [cluster_papers[sorted_indices[i]] for i in range(top_n)]
        representative = representative_papers[0]

        # research_clusters 저장
        cluster_row = ResearchCluster(
            researcher_id=researcher_id,
            cluster_label=label,
            paper_count=len(cluster_papers),
            centroid=centroid.tolist(),
        )
        db.session.add(cluster_row)
        db.session.flush()

        # reference_papers 업데이트
        for p in cluster_papers:
            p.cluster_id = cluster_row.id
            p.is_representative = p.id == representative.id

        # 클러스터 내 키워드 빈도 (상위 5)
        kw_freq: dict[str, int] = {}
        for p in cluster_papers:
            for kw_obj in p.keywords:
                kw_freq[kw_obj.keyword] = kw_freq.get(kw_obj.keyword, 0) + 1
        top_keywords = sorted(kw_freq, key=kw_freq.get, reverse=True)[:5]

        # --- 자동 주제 생성 ---
        topic_name = f"Cluster {label}: {', '.join(top_keywords[:3])}"
        topic = ResearchTopic(
            researcher_id=researcher_id,
            name=topic_name,
            source_type="auto",
            keywords=top_keywords[:5],
            cluster_label=label,
            sort_order=label,
        )
        db.session.add(topic)
        db.session.flush()

        # 대표 3편을 topic_reference_papers에 저장
        for rp in representative_papers:
            trp = TopicReferencePaper(
                topic_id=topic.id,
                scopus_id=rp.scopus_id,
                title=rp.title,
                authors=rp.authors,
                journal=rp.journal,
                year=rp.year,
                doi=rp.doi,
                abstract=rp.abstract,
            )
            db.session.add(trp)

        cluster_results.append(
            {
                "label": label,
                "cluster_db_id": cluster_row.id,
                "topic_id": topic.id,
                "paper_count": len(cluster_papers),
                "representative_papers": [
                    {
                        "id": rp.id,
                        "title": rp.title,
                        "keywords": [kw.keyword for kw in rp.keywords],
                    }
                    for rp in representative_papers
                ],
                "top_keywords": top_keywords,
            }
        )

    db.session.commit()

    # 자동 주제 대표 벡터 생성
    try:
        from app.litreview.recommendation.embedder import Embedder
        embedder = Embedder()
        for cr in cluster_results:
            embedder.embed_topic_representative(cr["topic_id"])
    except Exception:
        logger.exception("Failed to generate topic vectors for researcher %d", researcher_id)

    # researcher_type 갱신 (클러스터링 했으므로 A)
    researcher.researcher_type = "A"
    db.session.commit()

    logger.info(
        "Researcher %d: %d clusters → %d auto topics, silhouette=%.4f",
        researcher_id,
        k,
        len(cluster_results),
        sil_score,
    )

    return {
        "cluster_count": k,
        "silhouette": sil_score,
        "clusters": cluster_results,
    }
