# Claude Code 프롬프트 가이드 v2

이 문서는 Claude Code에게 단계별로 전달할 프롬프트입니다.
각 Phase를 순서대로 실행하세요.

---

## 시작하기 전에

프로젝트 폴더에 아래 파일들이 있는지 확인:
- `CLAUDE.md` (프로젝트 루트)
- `docs/paper-recommendation-system-spec.md` (구현 명세서)

---

## Phase 1: 기반 구축

### 프롬프트:
```
docs/paper-recommendation-system-spec.md를 읽고 프로젝트 기반을 구축해줘.

1. requirements.txt 생성 및 의존성 설치
2. app/config.py — 환경변수 기반 설정 클래스 (.env 로드)
   - 추천 기본값 포함: DEFAULT_SIMILARITY_METHOD, DEFAULT_TOP_K_RATIO,
     DEFAULT_CORE_PERCENT, DEFAULT_RELATED_PERCENT, DEFAULT_REFERENCE_PERCENT,
     DEFAULT_TARGET_YEAR_RANGE
3. PostgreSQL에 pgvector 확장 활성화
4. app/models.py — 명세서 섹션 3의 DB 스키마를 SQLAlchemy 모델로 정의
   - researchers (추천 설정 필드 포함)
   - reference_papers (embedding vector 포함)
   - paper_keywords, paper_references
   - research_clusters
   - custom_keywords, custom_journals
   - collected_papers (embedding vector 포함)
   - paper_recommendations (similarity_details JSONB 포함)
   - target_journals
   - search_queries, batch_logs, email_logs
5. app/__init__.py — Flask 앱 팩토리 + SQLAlchemy 초기화
6. DB 마이그레이션 스크립트 (테이블 생성)
7. .env.example 파일

pgvector의 Vector 타입을 사용하고, relationship과 cascade 설정을 포함해줘.
```

### 확인 사항:
- [ ] 모든 테이블이 생성되는지
- [ ] pgvector 확장이 정상 작동하는지
- [ ] .env 설정이 올바르게 로드되는지

---

## Phase 2: 연구자 프로필 구축

### 프롬프트:
```
Phase 1 위에 연구자 프로필 구축 파이프라인을 구현해줘.
명세서 섹션 4.2를 참고해.

1. app/litreview/profile/scopus_fetcher.py
   - Scopus Author Retrieval API + Search API로 연구자의 논문 목록 가져오기
   - 각 논문에서: 메타데이터, 저자 키워드(author keywords), 참고문헌 저널 정보 추출
   - reference_papers, paper_keywords, paper_references 테이블에 저장
   - 논문 수에 따라 researcher_type 자동 설정 (A: 30+, B: 1-29, C: 0)
   - API rate limit 고려

2. app/litreview/recommendation/embedder.py
   - OpenAI Embeddings API (text-embedding-3-small) 래퍼
   - embed_text, embed_batch 기본 메서드
   - embed_paper_abstracts: reference_papers 또는 collected_papers의 초록 임베딩
   - 배치 처리로 API 호출 최소화

3. app/litreview/profile/cluster_analyzer.py
   - 30편 이상 연구원만 대상
   - reference_papers의 임베딩으로 K-Means 또는 HDBSCAN 클러스터링
   - K 자동 결정 (silhouette score 기반)
   - 각 클러스터에서 centroid에 가장 가까운 논문을 대표 논문으로 선정
   - research_clusters 테이블에 저장
   - reference_papers의 cluster_id, is_representative 업데이트

4. app/litreview/profile/profile_manager.py
   - 관심 키워드 CRUD (custom_keywords)
   - 관심 저널 CRUD (custom_journals, added_by: 'user' 또는 'admin')
   - 연구 설명 수정 (research_description)
   - 추천 설정 수정 (similarity_method, top_k_ratio, 등급 구간 등)
```

### 확인 사항:
- [ ] Scopus API 호출이 정상 동작하는지
- [ ] 저자 키워드와 참고문헌 저널이 올바르게 추출되는지
- [ ] 임베딩이 DB에 정상 저장되는지
- [ ] 클러스터링이 동작하고 대표 논문이 선정되는지
- [ ] researcher_type이 논문 수에 따라 자동 설정되는지

---

## Phase 3: 논문 수집 및 추천

### 프롬프트:
```
프로필 구축 위에 논문 수집 및 추천 모듈을 구현해줘.
명세서 섹션 4.3, 4.4를 참고해.

1. app/litreview/collection/query_builder.py
   - 연구원 유형별 분기:
     * A (30편+): 클러스터별 대표 논문 키워드 + 해당 클러스터 주요 저널로 쿼리 생성
     * B (1~29편): 키워드 빈도 상위 + 게재 저널로 쿼리 생성
     * C (0편): 수동 키워드 + 관리자 설정 저널로 쿼리 생성
   - 공통: custom_keywords, custom_journals, target_journals도 포함
   - 쿼리 형식: SOURCE-ID(저널) AND TITLE-ABS-KEY(키워드 OR ...) AND PUBYEAR > ...
   - 키워드 빈도, 저널 빈도는 DB에서 실시간 집계 (별도 테이블 없음)

2. app/litreview/collection/paper_collector.py
   - 쿼리로 Scopus Search API 호출
   - 중복 제거 (scopus_id), collected_papers에 upsert
   - API rate limit 고려

3. app/litreview/recommendation/similarity.py
   - 신규 논문 embedding vs 연구원의 reference_papers embedding 직접 비교
   - pgvector <=> 연산자로 cosine similarity 계산
   - similarity_method에 따라 점수 산출:
     * top_k_avg: 상위 K편 평균 (K = max(3, 논문수 * top_k_ratio))
     * max: 최대값
     * avg: 전체 평균
   - similarity_details(JSONB)에 개별 유사도 저장

4. app/litreview/recommendation/grader.py
   - 단일 유사도 상위 구간으로 등급:
     * core: 상위 core_percent%
     * related: core ~ related_percent%
     * reference: related ~ reference_percent%
   - 구간 값은 연구원별 설정에서 읽음
   - paper_recommendations에 저장

5. app/litreview/collection/fulltext_downloader.py
   - core 등급만 대상
   - Scopus Full-Text API (학교 네트워크)
   - 실패 시 abstract fallback

6. app/litreview/recommendation/summarizer.py
   - 유일한 LLM 사용처 (Claude API)
   - core 등급 논문만 대상
   - 논문 전문/abstract 읽고 Schema 기반 요약:
     core_topic, purpose, method, results, limitations, future_work
   - 추천된 연구원들에 대해 추천 이유 작성
   - 유사도 높은 기존 논문 정보 참조하여 구체적 이유 제시
   - 명세서의 에이전트 프롬프트 설계를 기반으로 구현
```

### 확인 사항:
- [ ] 연구원 유형별 쿼리가 올바르게 생성되는지
- [ ] 유사도 계산 3가지 방식이 모두 동작하는지
- [ ] 등급 부여가 정확한지
- [ ] core만 전문 다운로드 + 요약이 되는지

---

## Phase 4: 저널 관리 + 알림 + 스케줄러

### 프롬프트:
```
추천 모듈 위에 저널 관리, 이메일 알림, 스케줄러를 구현해줘.
명세서 섹션 4.5, 4.6, 5를 참고해.

1. app/litreview/journal/journal_analyzer.py
   - 저널 빈도 실시간 집계 (별도 저장 안 함):
     * 게재 저널 (reference_papers)
     * 참고문헌 저널 (paper_references)
     * useful 피드백 논문의 저널 (paper_recommendations)
   - target_journals 업데이트 (source_type 구분)

2. app/litreview/journal/journal_discovery.py
   - 참고문헌 역추적: paper_references 저널 빈도 → 미게재 상위 저널 추가
   - 월간 저널 탐색: 핵심 키워드로 Scopus 전체 검색 → 저널 분포 분석
     → 기존 타겟에 없는 저널 후보 제안

3. app/litreview/journal/journal_monitor.py
   - 타겟 저널 신규 논문 감지 (last_checked 이후)
   - 신규 논문 임베딩 + 연구원 매칭 + 등급 + core 요약

4. app/litreview/notification/email_sender.py
   - Flask-Mail HTML 이메일
   - 등급별 내용 구성: core(제목+이유+요약), related(제목+저널), reference(제목 목록)
   - email_logs 기록

5. app/litreview/scheduler/jobs.py
   - APScheduler persistent jobstore (SQLAlchemy)
   - 배치 4개:
     * 매일 02:00 daily_paper_pipeline
     * 매일 04:00 daily_journal_monitoring
     * 매일 08:00 daily_email_notification
     * 매월 1일 03:00 monthly_journal_discovery
   - 시간대: Asia/Seoul
   - 각 배치에 batch_logs 기록
```

### 확인 사항:
- [ ] 저널 빈도가 실시간 집계되는지
- [ ] 참고문헌 역추적이 동작하는지
- [ ] 저널 모니터링이 신규 논문만 감지하는지
- [ ] 이메일 정상 발송되는지
- [ ] 스케줄러 4개 배치가 정상 실행되는지

---

## Phase 5: API 라우트 + SocketIO + 이벤트

### 프롬프트:
```
마지막으로 API, SocketIO, 이벤트 시스템을 구현해줘.
명세서 섹션 6, 7을 참고해.

1. app/litreview/routes.py — 명세서의 API 엔드포인트 전체 구현
   - 연구원 관리, 프로필 관리, 추천, 저널, 시스템

2. app/litreview/socketio_handlers.py
   - Scopus 수집 진행 상태
   - 클러스터링 진행 상태
   - 배치 작업 진행 상태

3. 이벤트 기반 업데이트:
   - 논문 저장 + useful 피드백 → 저널 자동 추가
   - 관심 키워드/저널 변경 → 다음 배치에 반영
   - 새 논문 게재 → 임베딩 + 30편 도달 시 자동 클러스터링 + type 전환
   - 관리자 기본 저널 설정

4. 에러 핸들링:
   - 모든 API에 try/except + HTTP 상태 코드
   - 에이전트/API 실패 시 재시도 로직
   - Python logging
```

### 확인 사항:
- [ ] 모든 API 엔드포인트 동작
- [ ] SocketIO 실시간 업데이트 동작
- [ ] 이벤트 체인 동작 (피드백 → 저널 추가 등)
- [ ] 에러 상황에서 적절한 응답

---

## Phase 간 점검 프롬프트

```
방금 구현한 코드를 점검해줘.
1. 명세서(docs/paper-recommendation-system-spec.md)와 비교해서 빠진 부분
2. 모듈 간 import와 의존성
3. DB 모델과 실제 사용 필드 일치 여부
4. 에러 핸들링 누락
확인하고 수정해줘.
```

---

## 전체 통합 테스트 프롬프트

```
전체 파이프라인 end-to-end 테스트 스크립트를 작성해줘.

테스트 시나리오:
1. 연구원 등록 (Scopus ID)
2. Scopus에서 논문 수집 → 키워드/참고문헌 저널 추출
3. 초록 임베딩
4. (30편+면) 클러스터링 → 대표 논문 선정
5. 검색 쿼리 생성 (유형별)
6. 논문 수집 → 임베딩 → 유사도 → 등급
7. core 전문 다운로드 → 요약
8. 저널 분석 → 참고문헌 역추적
9. 이메일 발송 (테스트 주소)

각 단계 입출력을 로그로 남기고 성공/실패 리포트해줘.
```
