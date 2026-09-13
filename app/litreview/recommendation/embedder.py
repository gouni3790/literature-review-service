"""OpenAI Embeddings API 래퍼. text-embedding-3-small (1536d).

주제 대표 벡터 생성 포함.
"""

import logging
import time

from flask import current_app
from openai import OpenAI

from app import db
from app.models import CollectedPaper, ReferencePaper, ResearchTopic

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_TEXT_LENGTH = 8000
RETRY_DELAY = 2.0


class Embedder:
    """OpenAI 임베딩 호출. 배치 처리로 API 호출 최소화."""

    def __init__(self, model: str | None = None):
        self._model = model
        self._client: OpenAI | None = None

    @property
    def model(self) -> str:
        if self._model:
            return self._model
        return current_app.config.get("EMBEDDING_MODEL", "text-embedding-3-small")

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=current_app.config["OPENAI_API_KEY"])
        return self._client

    def embed_text(self, text: str) -> list[float]:
        """단일 텍스트 임베딩."""
        text = text[:MAX_TEXT_LENGTH].replace("\n", " ").strip()
        if not text:
            return []

        resp = self.client.embeddings.create(
            input=[text],
            model=self.model,
        )
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """배치 임베딩. 빈 텍스트는 빈 리스트로 반환."""
        if not texts:
            return []

        cleaned = []
        valid_indices = []
        for i, t in enumerate(texts):
            t = (t or "")[:MAX_TEXT_LENGTH].replace("\n", " ").strip()
            if t:
                cleaned.append(t)
                valid_indices.append(i)

        if not cleaned:
            return [[] for _ in texts]

        all_embeddings: list[list[float]] = []
        for start in range(0, len(cleaned), BATCH_SIZE):
            batch = cleaned[start : start + BATCH_SIZE]
            embs = self._call_with_retry(batch)
            all_embeddings.extend(embs)

            if start + BATCH_SIZE < len(cleaned):
                time.sleep(0.1)

        result: list[list[float]] = [[] for _ in texts]
        for idx, emb in zip(valid_indices, all_embeddings):
            result[idx] = emb

        return result

    def _record_usage(self, resp) -> None:
        """임베딩 호출의 토큰 사용량을 기록한다.

        기록 실패가 임베딩을 막으면 안 되므로 예외를 삼킨다.
        """
        try:
            usage = getattr(resp, "usage", None)
            if usage is None:
                return
            from app.litreview.usage import record

            record(
                provider="openai",
                model=self.model,
                operation="embedding",
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=0,
            )
        except Exception:
            logger.debug("임베딩 사용량 기록 실패", exc_info=True)

    def _call_with_retry(
        self, texts: list[str], max_retries: int = 3
    ) -> list[list[float]]:
        """재시도 포함 배치 호출."""
        for attempt in range(max_retries):
            try:
                resp = self.client.embeddings.create(
                    input=texts,
                    model=self.model,
                )
                self._record_usage(resp)
                return [d.embedding for d in resp.data]
            except Exception:
                if attempt == max_retries - 1:
                    raise
                wait = RETRY_DELAY * (2**attempt)
                logger.warning(
                    "Embedding API retry %d/%d, waiting %.1fs",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
        return []

    @staticmethod
    def _make_embedding_text(paper) -> str:
        """논문 임베딩용 텍스트 조합: Title + Keywords + Abstract.

        제목과 키워드를 포함하면 논문 간 구분이 더 뚜렷해져
        클러스터링 품질이 향상된다.
        """
        parts = []
        title = getattr(paper, "title", None) or ""
        if title:
            parts.append(f"Title: {title}")

        # reference_papers는 keywords 관계, collected_papers는 없음
        keywords = getattr(paper, "keywords", None)
        if keywords:
            kw_list = []
            for kw in keywords:
                if hasattr(kw, "keyword"):
                    kw_list.append(kw.keyword)
                elif isinstance(kw, str):
                    kw_list.append(kw)
            if kw_list:
                parts.append(f"Keywords: {'; '.join(kw_list)}")

        abstract = getattr(paper, "abstract", None) or ""
        if abstract:
            parts.append(f"Abstract: {abstract}")

        return "\n".join(parts)

    def embed_paper_abstracts(
        self,
        paper_ids: list[int] | None = None,
        table: str = "reference_papers",
        researcher_id: int | None = None,
        progress_callback=None,
    ) -> int:
        """논문 임베딩 → embedding 컬럼 업데이트.

        reference_papers: Title + Keywords + Abstract 조합
        collected_papers: Abstract만 (키워드 없음)

        Args:
            paper_ids: 특정 논문만. None이면 embedding이 NULL인 전체.
            table: 'reference_papers' 또는 'collected_papers'.
            researcher_id: reference_papers 테이블 필터링.
            progress_callback: fn(done, total).

        Returns:
            임베딩 처리된 논문 수.
        """
        Model = ReferencePaper if table == "reference_papers" else CollectedPaper

        # 초록이 빈 문자열인 행을 제외한다. isnot(None) 만으로는 ""가 통과해
        # 임베딩 대상에 들어가고, embed_batch 가 빈 텍스트를 걸러내면서
        # "Embedding N papers" 뒤에 "Embedded 0/N" 이 찍히는 조용한 실패가 된다.
        # 호출부가 필터를 빠뜨려도 여기서 막히도록 한 번 더 건다.
        query = Model.query.filter(
            Model.embedding.is_(None),
            Model.abstract.isnot(None),
            Model.abstract != "",
        )
        if paper_ids:
            query = query.filter(Model.id.in_(paper_ids))
        if researcher_id and table == "reference_papers":
            query = query.filter(ReferencePaper.researcher_id == researcher_id)

        papers = query.all()
        if not papers:
            logger.info("No papers to embed in %s", table)
            return 0

        total = len(papers)
        logger.info("Embedding %d papers from %s", total, table)
        embedded_count = 0

        for start in range(0, total, BATCH_SIZE):
            batch = papers[start : start + BATCH_SIZE]

            if table == "reference_papers":
                # Title + Keywords + Abstract
                texts = [self._make_embedding_text(p) for p in batch]
            else:
                # collected_papers: abstract만 (키워드 관계 없음)
                texts = [p.abstract or "" for p in batch]

            embeddings = self.embed_batch(texts)

            for paper, emb in zip(batch, embeddings):
                if emb:
                    paper.embedding = emb
                    embedded_count += 1

            db.session.commit()

            if progress_callback:
                progress_callback(min(start + BATCH_SIZE, total), total)

        logger.info("Embedded %d/%d papers in %s", embedded_count, total, table)
        return embedded_count

    def embed_topic_representative(self, topic_id: int) -> bool:
        """주제 대표 벡터 생성.

        자동 주제: 대표 논문 + centroid 인접 2편의 초록을 이어붙여 임베딩
        수동 주제 (DOI 있음): 레퍼런스 논문 초록을 이어붙여 임베딩
        수동 주제 (DOI 없음): 설명 텍스트를 임베딩

        Args:
            topic_id: research_topics.id

        Returns:
            True if vector was created.
        """
        topic = db.session.get(ResearchTopic, topic_id)
        if not topic:
            logger.warning("Topic %d not found", topic_id)
            return False

        text_to_embed = ""

        # 레퍼런스 논문 초록 이어붙이기 (자동/수동 공통)
        if topic.reference_papers:
            abstracts = [
                trp.abstract
                for trp in topic.reference_papers
                if trp.abstract
            ]
            if abstracts:
                text_to_embed = " ".join(abstracts)

        # 레퍼런스 논문이 없거나 초록이 없으면 → 주제명 + 키워드 + 설명 합산
        if not text_to_embed:
            parts = []
            if topic.name:
                parts.append(topic.name)
            if topic.keywords:
                parts.append(", ".join(topic.keywords))
            if topic.description:
                parts.append(topic.description)
            text_to_embed = " ".join(parts)

        if not text_to_embed:
            logger.warning(
                "Topic %d '%s' has no content for representative vector",
                topic_id,
                topic.name,
            )
            return False

        emb = self.embed_text(text_to_embed)
        if emb:
            topic.representative_vector = emb
            db.session.commit()
            logger.info(
                "Generated representative vector for topic %d '%s' "
                "(source=%s, text_len=%d)",
                topic_id,
                topic.name,
                topic.source_type,
                len(text_to_embed),
            )
            return True

        return False
