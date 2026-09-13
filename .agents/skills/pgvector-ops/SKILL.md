---
name: pgvector-ops
description: pgvector 운영 노하우 — Vector 컬럼 정의, cosine 쿼리, 인덱스 선택(ivfflat vs hnsw), 차원 일관성. similarity.py / models.py / 마이그레이션 작성 시 사용.
---

# pgvector 운영 가이드

## 확장 활성화

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

→ 마이그레이션 첫 단계 또는 db-reset 스크립트에 포함.

## SQLAlchemy 모델

```python
from pgvector.sqlalchemy import Vector

class ReferencePaper(Base):
    __tablename__ = "reference_papers"
    embedding = Column(Vector(1536), nullable=True)  # text-embedding-3-small
```

- 차원은 임베딩 모델에 맞춰 **상수화** (config.EMBEDDING_DIM).
- nullable=True — 임베딩 미생성 상태 허용.

## 유사도 연산자

| 연산자 | 의미 | 비고 |
|---|---|---|
| `<->` | L2 distance | |
| `<#>` | inner product (negative) | |
| `<=>` | cosine distance | **OpenAI 임베딩 권장** |

→ `1 - (a <=> b)` 가 cosine similarity (0~1).

## 추천 점수 쿼리

```sql
-- collected_paper 1건 vs researcher 의 reference_papers
SELECT
    cp.id AS new_paper,
    1 - (cp.embedding <=> rp.embedding) AS sim
FROM collected_papers cp
CROSS JOIN reference_papers rp
WHERE cp.id = :new_id
  AND rp.researcher_id = :researcher_id
  AND rp.embedding IS NOT NULL
ORDER BY sim DESC;
```

## similarity_method 별 SQL 패턴

```python
# top_k_avg: 상위 K 평균
top_k = max(3, int(paper_count * top_k_ratio))
"""
WITH sims AS (
    SELECT 1 - (cp.embedding <=> rp.embedding) AS s
    FROM reference_papers rp
    WHERE rp.researcher_id = :rid AND rp.embedding IS NOT NULL
    ORDER BY s DESC LIMIT :k
)
SELECT AVG(s) FROM sims;
"""

# max
"SELECT MAX(1 - (cp.embedding <=> rp.embedding)) FROM ..."

# avg
"SELECT AVG(1 - (cp.embedding <=> rp.embedding)) FROM ..."
```

## 인덱스 선택

| 인덱스 | 쿼리 시간 | 빌드 시간 | 정확도 | 권장 시점 |
|---|---|---|---|---|
| 없음 | O(N) full scan | 0 | 100% | 1k 행 미만 |
| `ivfflat` | 빠름 | 보통 | ~95% | 1k~1M |
| `hnsw` | 가장 빠름 | 느림 | ~99% | 빈번한 쿼리 |

```sql
-- ivfflat (cosine)
CREATE INDEX ON reference_papers
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);   -- lists = sqrt(N)

-- hnsw
CREATE INDEX ON reference_papers
USING hnsw (embedding vector_cosine_ops);
```

→ 본 프로젝트 규모(연구원 20명 × 평균 50편 = 1k 행)는 **인덱스 없이 충분**. 10k 넘어가면 ivfflat 도입.

## 함정

- `Vector(1536)` 차원 불일치 시 INSERT 실패 — 임베딩 차원 변경 시 마이그레이션 필요.
- ivfflat 인덱스는 데이터 다 들어간 뒤 만들어야 lists 통계가 의미 있음.
- `<=>` 결과는 `0(동일) ~ 2(반대)` — similarity 로 변환 잊지 말 것.
- 빈 벡터 비교 시 NULL 반환, ORDER BY 에서 NULLS LAST 명시.
