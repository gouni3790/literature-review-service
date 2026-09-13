"""저널 발굴 점수 계산 — 순수 함수 모음.

    Score = (0.5·Relevance + 0.3·Volume + 0.2·Growth) × Novelty × QualityGate

Novelty 와 QualityGate 가 가중항이 아니라 **곱셈항**인 것이 핵심이다.
구 DB 실데이터(후보 86종)로 검증한 결과, 가중치 0.1로는 연구실이 이미 잘 아는
저널을 밀어내지 못했다 — 상위 10종 중 7종이 익숙한 저널이었다. 익숙한 저널은
Relevance 와 Volume 을 둘 다 최상위로 받아 0.8 배점을 거의 만점 가져가기
때문이다. 곱셈으로 바꾸면 같은 데이터에서 익숙한 저널이 2종으로 줄었다.

이 모듈은 Scopus·DB 에 직접 접근하지 않는다. 입력을 받아 계산만 하므로
고정 데이터로 단위 검증이 가능하다. 수집·저장은 journal_recommender 가 맡는다.
"""

import logging
import math
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# --- 가중치 (첫 실행 후 실측으로 재조정할 것) ---
W_RELEVANCE = 0.5
W_VOLUME = 0.3
W_GROWTH = 0.2

# 관련 논문 선정 하한 — 이 유사도 미만은 후보에서 제외
SIMILARITY_FLOOR = 0.35

# Relevance 베이지안 축소 상수. 논문 n편인 저널의 평균을 전체 평균 쪽으로 당긴다.
# n=1 인 저널이 요행으로 1위를 차지하는 것을 막는다.
SHRINKAGE_M = 5

# Growth 는 표본이 작으면 요동이 심하다 (2편/1편 = 200% 성장).
# 이 미만이면 중립값 1.0 으로 고정한다.
GROWTH_MIN_PAPERS = 5

# 최근/이전 구간 경계 — 최근 RECENT_YEARS 년을 "최근"으로 본다
RECENT_YEARS = 2

# Growth 상한. 없으면 이전 구간이 희박할 때 값이 발산해 Growth 가 Volume 을
# 복제해 버린다 (대형 저널이 두 지표에서 모두 최고점을 받는 이중 가산).
GROWTH_CAP = 4.0

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s&]")


def normalize_journal_name(name: str | None) -> str:
    """저널명 대조용 정규화.

    familiarity 는 참고문헌 저널명(자유 문자열)과 대조해야 하므로 표기 흔들림을
    흡수한다. 소문자화, 구두점 제거, '&'→'and', 공백 정규화.

    NOTE: 근본적으로는 Scopus source-id 로 대조하는 것이 옳다. 다만 참고문헌
    쪽에 source-id 가 없는 행이 많아, 이름 정규화를 폴백으로 함께 쓴다.
    """
    if not name:
        return ""
    s = name.strip().lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


@dataclass
class JournalAggregate:
    """저널 하나에 대한 집계 결과."""

    journal_name: str
    scopus_source_id: str | None = None
    issn: str | None = None

    similarities: list[float] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)  # 근거 논문 (유사도 상위)

    # 연도별 전수 건수 — Scopus totalResults 로 채운다 (표본이 아니라 전수)
    year_totals: dict[int, int] = field(default_factory=dict)

    @property
    def related_count(self) -> int:
        return len(self.similarities)

    @property
    def raw_mean_similarity(self) -> float:
        return sum(self.similarities) / len(self.similarities) if self.similarities else 0.0


def aggregate_by_journal(
    papers: list[dict],
    max_samples: int = 3,
) -> dict[str, JournalAggregate]:
    """관련 논문을 저널별로 묶는다.

    Args:
        papers: [{journal, year, similarity, title, doi, scopus_id,
                  source_id?, issn?}]
        max_samples: 저널당 남길 근거 논문 수.

    Returns:
        정규화 저널명 -> JournalAggregate
    """
    aggs: dict[str, JournalAggregate] = {}

    for p in papers:
        raw_name = (p.get("journal") or "").strip()
        if not raw_name:
            continue
        key = normalize_journal_name(raw_name)
        if not key:
            continue

        agg = aggs.get(key)
        if agg is None:
            agg = JournalAggregate(
                journal_name=raw_name,
                scopus_source_id=p.get("source_id"),
                issn=p.get("issn"),
            )
            aggs[key] = agg
        elif agg.scopus_source_id is None and p.get("source_id"):
            agg.scopus_source_id = p["source_id"]

        sim = float(p.get("similarity") or 0.0)
        agg.similarities.append(sim)
        if p.get("year"):
            agg.years.append(int(p["year"]))
        agg.samples.append(
            {
                "title": p.get("title"),
                "doi": p.get("doi"),
                "scopus_id": p.get("scopus_id"),
                "year": p.get("year"),
                "similarity": round(sim, 4),
            }
        )

    # 근거 논문은 유사도 상위만 남긴다
    for agg in aggs.values():
        agg.samples.sort(key=lambda s: s["similarity"], reverse=True)
        del agg.samples[max_samples:]

    return aggs


def compute_growth(
    agg: JournalAggregate,
    current_year: int,
    total_years: int = 5,
) -> float:
    """최근 RECENT_YEARS 년의 **연평균 발행 속도** 대비 그 이전의 비율.

    반드시 연 단위 속도로 비교한다. 단순히 건수를 나누면 최근 2년과 이전 3년의
    기간 길이가 달라 항상 이전 쪽이 유리해진다.

    반환값은 GROWTH_CAP 으로 상한을 둔다. 상한이 없으면 older 가 0에 가까울 때
    값이 발산해 Growth 가 사실상 Volume 을 복제한다 — 실제로 초기 구현에서
    older=0 인 표본에 대해 growth 가 `건수+1` 이 되어 대형 저널이 Growth 에서도
    최고점을 받는 이중 가산이 발생했다.

    측정 불가한 경우(관련 논문 부족, 이전 구간 표본 없음)는 중립값 1.0 이다.
    새 저널을 '무한 성장'으로 보상하지 않는다 — 그건 Novelty 가 담당한다.

    year_totals(Scopus 전수 조회)가 있으면 그것을 쓴다. 없으면 표본 연도 분포로
    대체하는데, 표본은 검색 상한에 잘려 있을 수 있어 정확도가 떨어진다.
    """
    if agg.related_count < GROWTH_MIN_PAPERS:
        return 1.0

    cutoff = current_year - RECENT_YEARS + 1
    older_years = max(1, total_years - RECENT_YEARS)

    if agg.year_totals:
        recent = sum(c for y, c in agg.year_totals.items() if y >= cutoff)
        older = sum(c for y, c in agg.year_totals.items() if y < cutoff)
    else:
        recent = sum(1 for y in agg.years if y >= cutoff)
        older = sum(1 for y in agg.years if y < cutoff)

    if older == 0:
        # 이전 구간 표본이 없으면 성장 여부를 판정할 수 없다.
        # 신생 저널일 수도, 검색 상한에 잘린 것일 수도 있어 중립 처리한다.
        return 1.0

    recent_rate = recent / RECENT_YEARS
    older_rate = older / older_years
    return min(recent_rate / older_rate, GROWTH_CAP)


def build_familiarity(
    citation_counts: dict[str, int],
    published_counts: dict[str, int] | None = None,
    target_journal_names: set[str] | None = None,
    w_citation: float = 1.0,
    w_published: float = 3.0,
    w_target: float = 2.0,
) -> dict[str, float]:
    """연구실 단위 familiarity 지수. 정규화 저널명 -> 0~1.

    "연구실이 이 저널을 얼마나 이미 보고 있는가"를 세 신호로 합산한다.
    연구원별이 아니라 **연구실 전체** 기준이다 (결정 사항).

    Args:
        citation_counts:  저널명 -> 연구실 논문이 인용한 횟수
        published_counts: 저널명 -> 연구실이 게재한 편수
        target_journal_names: 이미 수집 타겟으로 등록된 저널명
        w_published: 게재는 인용보다 강한 친숙 신호라 가중치를 높게 준다
        w_target:    이미 타겟이면 발굴 대상이 아니므로 역시 높게

    Returns:
        정규화 저널명 -> 0~1 (1 = 가장 친숙)
    """
    published_counts = published_counts or {}
    target_journal_names = target_journal_names or set()

    raw: dict[str, float] = {}

    for name, cnt in citation_counts.items():
        key = normalize_journal_name(name)
        if key:
            raw[key] = raw.get(key, 0.0) + w_citation * math.log1p(cnt)

    for name, cnt in published_counts.items():
        key = normalize_journal_name(name)
        if key:
            raw[key] = raw.get(key, 0.0) + w_published * math.log1p(cnt)

    for name in target_journal_names:
        key = normalize_journal_name(name)
        if key:
            raw[key] = raw.get(key, 0.0) + w_target

    if not raw:
        logger.warning(
            "familiarity 재료가 비어 있습니다. Novelty 가 전부 1.0 이 되어 "
            "게이트가 동작하지 않습니다 (참고문헌 이관 여부를 확인하세요)."
        )
        return {}

    hi = max(raw.values())
    if hi <= 0:
        return {}
    return {k: v / hi for k, v in raw.items()}


def _minmax(values: list[float]):
    lo, hi = min(values), max(values)
    span = hi - lo
    if span == 0:
        return lambda _v: 0.0
    return lambda v: (v - lo) / span


def score_journals(
    aggs: dict[str, JournalAggregate],
    familiarity: dict[str, float],
    current_year: int,
    quality_gate: dict[str, float] | None = None,
    min_related: int = 3,
) -> list[dict]:
    """집계 결과에 점수를 매겨 내림차순 정렬한다.

    Args:
        aggs: aggregate_by_journal() 결과
        familiarity: build_familiarity() 결과
        current_year: Growth 구간 계산 기준 연도
        quality_gate: 정규화 저널명 -> 0~1. None 이면 전부 1.0 (게이트 미적용)
        min_related: 관련 논문이 이 미만인 저널은 후보에서 제외

    Returns:
        [{journal_name, scopus_source_id, issn, related_paper_count,
          avg_similarity, growth_rate, familiarity, novelty, quality,
          score, evidence}]  — score 내림차순
    """
    pool = {k: a for k, a in aggs.items() if a.related_count >= min_related}
    if not pool:
        return []

    # 전체 평균 — 베이지안 축소의 사전값
    all_sims = [s for a in pool.values() for s in a.similarities]
    global_mean = sum(all_sims) / len(all_sims)

    rows = []
    for key, agg in pool.items():
        n = agg.related_count
        shrunk = (n * agg.raw_mean_similarity + SHRINKAGE_M * global_mean) / (
            n + SHRINKAGE_M
        )
        rows.append(
            {
                "key": key,
                "agg": agg,
                "relevance_raw": shrunk,
                "volume_raw": math.log1p(n),
                "growth_raw": compute_growth(agg, current_year),
            }
        )

    n_rel = _minmax([r["relevance_raw"] for r in rows])
    n_vol = _minmax([r["volume_raw"] for r in rows])
    n_gro = _minmax([r["growth_raw"] for r in rows])

    results = []
    for r in rows:
        key, agg = r["key"], r["agg"]
        fam = familiarity.get(key, 0.0)
        novelty = 1.0 - fam
        quality = 1.0 if quality_gate is None else quality_gate.get(key, 1.0)

        base = (
            W_RELEVANCE * n_rel(r["relevance_raw"])
            + W_VOLUME * n_vol(r["volume_raw"])
            + W_GROWTH * n_gro(r["growth_raw"])
        )
        results.append(
            {
                "journal_name": agg.journal_name,
                "scopus_source_id": agg.scopus_source_id,
                "issn": agg.issn,
                "related_paper_count": agg.related_count,
                "avg_similarity": round(r["relevance_raw"], 6),
                "growth_rate": round(r["growth_raw"], 4),
                "familiarity": round(fam, 4),
                "novelty": round(novelty, 4),
                "quality": round(quality, 4),
                "score": round(base * novelty * quality, 6),
                "evidence": agg.samples,
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, row in enumerate(results, 1):
        row["rank"] = i
    return results
