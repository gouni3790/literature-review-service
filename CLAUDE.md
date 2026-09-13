# Literature Review Assistant

연구원의 기존 논문 데이터 기반 논문 자동 추천 + 타겟 저널 모니터링 시스템.
상세 구현 명세: paper-recommendation-system-spec-v2.md

## 기술 스택
- Flask + Flask-SocketIO, PostgreSQL + pgvector, SQLAlchemy
- Claude API (core 논문 요약 전용), OpenAI Embeddings (text-embedding-3-small, 1536d)
- Scopus API (Search, Author Retrieval, Full-Text), APScheduler, Flask-Mail
- scikit-learn (클러스터링)

## 프로젝트 구조
```
app/
├── config.py, models.py
├── litreview/
│   ├── routes.py, socketio_handlers.py
│   ├── profile/        # scopus_fetcher, cluster_analyzer, profile_manager
│   ├── collection/     # query_builder, paper_collector, fulltext_downloader
│   ├── recommendation/ # embedder, similarity, grader, summarizer
│   ├── journal/        # journal_analyzer, journal_monitor, journal_discovery
│   ├── notification/   # email_sender
│   └── scheduler/      # jobs
```

## 핵심 규칙 — 반드시 준수

### 연구원 유형별 분기
- A (10편+): 초록 임베딩 → 클러스터링 → 클러스터별 키워드+저널로 검색
- B (2~9편): 키워드 빈도 상위 + 게재 저널로 검색
- C (0편): 수동 키워드 + 관리자 설정 저널로 검색
- 공통: UI에서 관심 키워드/저널 수동 추가 가능

### 유사도 비교
- 신규 논문 abstract 임베딩 vs 기존 논문 초록 임베딩 직접 비교
- 단일 유사도 점수 (3축 구분 없음)
- 비교 방식 설정 가능: top_k_avg | max | avg (테스트 후 튜닝)

### 추천 등급 (단일 유사도 상위 구간)
- core: 전문 다운로드 + 에이전트 요약
- related: 메타데이터만
- reference: 메타데이터만
- 구간 값은 연구원별 설정 가능 (테스트 후 튜닝)

### 저널 확장
- 참고문헌 역추적 (시스템 시작 시)
- 사용자 피드백 루프 (useful → 저널 자동 추가)
- 월간 키워드 기반 신규 저널 탐색

## Flask 연결
```python
app.register_blueprint(litreview_blueprint)
init_litreview(socketio)
```

## 코딩 규칙
- Python 3.10+, docstring 필수
- API 호출 시 rate limit 고려 + 적절한 delay
- DB: SQLAlchemy session 관리, 트랜잭션 단위 commit
- 환경변수: python-dotenv (.env)
- 에러 핸들링: try/except + logging
