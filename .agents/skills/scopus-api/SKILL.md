---
name: scopus-api
description: Scopus API (Search / Author Retrieval / Full-Text) 호출 시 운영 노하우 — rate limit, 재시도, 키 헤더, 쿼리 형식. scopus_fetcher / paper_collector / fulltext_downloader 작성·디버깅 시 사용.
---

# Scopus API 운영 가이드

명세에 없는 운영 노하우만. 인증 / 엔드포인트 스펙은 명세 §4 참고.

## Rate Limit (학교 네트워크 기준)
| API | 주간 한도 | 초당 |
|---|---|---|
| Search | 20,000 | 9 req/s |
| Author Retrieval | 5,000 | 3 req/s |
| Abstract Retrieval | 10,000 | 9 req/s |
| Full-Text | 10,000 | 9 req/s |

→ 안전 마진 70% 로 사용.

## 쿼리 형식 (paper_collector)

```
SOURCE-ID(저널ID OR ...)
  AND TITLE-ABS-KEY(키워드 OR "정확한 구문" OR ...)
  AND PUBYEAR > 2022
```

- `SOURCE-ID` 가 `ISSN` 보다 정확. ISSN 은 변경됨.
- 키워드에 공백 있으면 큰따옴표.
- `OPENACCESS(1)` 추가 시 전문 다운로드 성공률 ↑.

## 재시도 전략

```python
# 권장 패턴
import time, requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

retry = Retry(
    total=5,
    backoff_factor=2.0,           # 0, 2, 4, 8, 16s
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=retry))
```

- 429 응답 헤더 `X-RateLimit-Reset` (epoch) 존중.
- 401 은 키 만료 — 재시도 ❌, 즉시 alert.

## 페이지네이션

- `cursor=*` 로 시작 → 응답의 `@next` 토큰 사용.
- `start` 파라미터는 5000 건 한도 → 큰 결과셋엔 cursor 필수.
- 한 페이지 최대 25 (legacy) ~ 200 (cursor mode).

## 응답 파싱 함정

- `dc:identifier` = `SCOPUS_ID:1234567` 형식 → prefix 잘라서 저장.
- `authkeywords` 가 `|` 로 구분됨 (배열 아님).
- `prism:doi` 없을 수 있음 — null 허용.
- Full-Text 는 `<xml>` (Elsevier DTD) 형식, BeautifulSoup `lxml-xml` 파서.

## 헤더

```python
headers = {
    "X-ELS-APIKey": os.environ["SCOPUS_API_KEY"],
    "X-ELS-Insttoken": os.environ.get("SCOPUS_INST_TOKEN", ""),  # 학교
    "Accept": "application/json",
}
```

## 디버깅 팁
- API 키 검증: `GET /content/search/scopus?query=ALL("test")&count=1` 으로 200 확인.
- 학교 네트워크 외부에서 Full-Text 호출 시 401 — VPN 필수.
- `X-ELS-Status: WARNING — ...` 헤더 무시 가능, error ❌.
