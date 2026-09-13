"""Scopus Serial Title API — 저널 식별자와 품질 지표.

두 가지 목적이 있다.

1. **source-id 확보.** Scopus 검색의 `SRCTITLE("Energy")` 는 정확 일치가 아니라
   부분 매칭이라, 이름에 Energy 가 든 저널을 전부 끌어온다. 실측으로 기존 수집
   저널 238종 중 191종(80%)이 이렇게 딸려온 것이었다. source-id 로 대조해야
   집계가 신뢰할 수 있다.

2. **QualityGate.** "새 저널 발굴"은 Volume 가중치가 높으면 메가저널이 상위를
   차지한다. CiteScore 로 하위권을 걸러낸다.

API 가 없어도 파이프라인이 멈추지 않는다. 조회 실패 시 해당 저널은
`quality=1.0`(게이트 미적용)으로 통과시키고 경고를 남긴다. 결과에는
`quality_checked=False` 가 붙어 게이트가 실제로 적용됐는지 구분할 수 있다.
"""

import logging
import time

import requests
from flask import current_app
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

SERIAL_URL = "https://api.elsevier.com/content/serial/title"
SERIAL_DELAY = 0.15
REQUEST_TIMEOUT = 20

# 세션 수명 동안 유지되는 조회 캐시 (정규화 저널명 -> dict | None)
_cache: dict[str, dict | None] = {}


def _session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config.get("SCOPUS_API_KEY", ""),
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "application/json",
    }


def _latest_citescore(entry: dict) -> float | None:
    """citeScoreYearInfoList 에서 가장 최근 CiteScore 를 뽑는다.

    Elsevier 응답 구조가 버전에 따라 흔들려서 방어적으로 읽는다.
    """
    info = entry.get("citeScoreYearInfoList") or {}
    current = info.get("citeScoreCurrentMetric")
    if current:
        try:
            return float(current)
        except (TypeError, ValueError):
            pass

    years = info.get("citeScoreYearInfo") or []
    best_year, best_val = None, None
    for y in years:
        try:
            yr = int(y.get("@year"))
            val = float(
                (y.get("citeScoreInformationList") or [{}])[0]
                .get("citeScoreInfo", [{}])[0]
                .get("citeScore")
            )
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if best_year is None or yr > best_year:
            best_year, best_val = yr, val
    return best_val


def lookup_journal(name: str, session: requests.Session | None = None) -> dict | None:
    """저널명으로 Scopus 메타데이터를 조회한다.

    Returns:
        {source_id, issn, title, citescore, subject_areas} 또는 None(조회 실패).
    """
    from app.litreview.journal.journal_scoring import normalize_journal_name

    key = normalize_journal_name(name)
    if not key:
        return None
    if key in _cache:
        return _cache[key]

    own_session = session is None
    session = session or _session()
    try:
        time.sleep(SERIAL_DELAY)
        resp = session.get(
            SERIAL_URL,
            headers=_headers(),
            params={"title": name, "count": 5},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        entries = (
            resp.json().get("serial-metadata-response", {}).get("entry", []) or []
        )
    except requests.RequestException as exc:
        logger.warning("Serial Title 조회 실패 '%s': %s", name[:60], exc)
        _cache[key] = None
        return None
    finally:
        if own_session:
            session.close()

    # 정규화 이름이 정확히 같은 항목을 우선, 없으면 첫 항목
    best = None
    for e in entries:
        if normalize_journal_name(e.get("dc:title")) == key:
            best = e
            break
    if best is None and entries:
        best = entries[0]
    if best is None:
        _cache[key] = None
        return None

    result = {
        "source_id": best.get("source-id"),
        "issn": best.get("prism:issn") or best.get("prism:eIssn"),
        "title": best.get("dc:title"),
        "citescore": _latest_citescore(best),
        "subject_areas": [
            a.get("$") for a in (best.get("subject-area") or []) if a.get("$")
        ],
    }
    _cache[key] = result
    return result


def build_quality_gate(
    journal_names: list[str],
    percentile_cut: float = 25.0,
) -> tuple[dict[str, float], dict[str, dict], bool]:
    """후보 저널들의 CiteScore 를 조회해 품질 게이트를 만든다.

    Args:
        journal_names: 원본 저널명 리스트
        percentile_cut: CiteScore 하위 몇 %를 탈락시킬지

    Returns:
        (gate, meta, checked)
        gate    : 정규화 저널명 -> 1.0(통과) | 0.0(탈락)
        meta    : 정규화 저널명 -> lookup_journal 결과
        checked : 실제로 조회가 성공해 게이트가 적용됐는지.
                  False 면 gate 는 전부 1.0 이고 게이트가 무의미하다.
    """
    from app.litreview.journal.journal_scoring import normalize_journal_name

    gate: dict[str, float] = {}
    meta: dict[str, dict] = {}
    session = _session()
    try:
        for name in journal_names:
            info = lookup_journal(name, session=session)
            if info:
                meta[normalize_journal_name(name)] = info
    finally:
        session.close()

    if not meta:
        logger.warning(
            "Serial Title 조회가 하나도 성공하지 못했습니다. "
            "QualityGate 를 적용하지 않고 진행합니다 (전부 통과). "
            "Scopus API 키/기관 IP 를 확인하세요."
        )
        return {normalize_journal_name(n): 1.0 for n in journal_names}, {}, False

    scores = sorted(
        v["citescore"] for v in meta.values() if v.get("citescore") is not None
    )
    threshold = None
    if scores:
        idx = int(len(scores) * percentile_cut / 100)
        threshold = scores[min(idx, len(scores) - 1)]
        logger.info(
            "QualityGate: CiteScore 하위 %.0f%% 컷 = %.2f (표본 %d종)",
            percentile_cut, threshold, len(scores),
        )

    for name in journal_names:
        key = normalize_journal_name(name)
        info = meta.get(key)
        if not info:
            # 조회 실패한 저널은 통과시킨다 — 게이트가 없는 것이 잘못 떨구는 것보다 낫다
            gate[key] = 1.0
            continue
        cs = info.get("citescore")
        if cs is None or threshold is None:
            gate[key] = 1.0
        else:
            gate[key] = 1.0 if cs >= threshold else 0.0

    return gate, meta, True


def clear_cache() -> None:
    """테스트/재실행용 캐시 비우기."""
    _cache.clear()
