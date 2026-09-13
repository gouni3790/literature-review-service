"""Core 논문 요약 + 추천 이유 작성. 유일한 LLM 사용처 (Claude API)."""

import json
import logging

import anthropic
from flask import current_app

from app import db
from app.models import (
    CollectedPaper,
    PaperRecommendation,
    ReferencePaper,
    Researcher,
    ResearchTopic,
)

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """당신은 학술 논문 분석 전문가입니다.

## 작업 1: 논문 요약
아래 논문을 읽고 구조화된 요약을 작성하세요. **반드시 한국어로 작성**하세요.
1. core_topic: 핵심 연구 질문 (1-2 문장)
2. purpose: 이 논문이 해결하려는 문제 (2-3 문장)
3. method: 방법론, 도구, 접근법 (2-3 문장)
4. results: 주요 결과 (2-3 문장)
5. limitations: 명시된 한계점 (1-2 문장)
6. future_work: 제안된 후속 연구 방향 (1-2 문장)

## 작업 2: 추천 이유 작성
아래 각 연구원에 대해, 이 논문이 왜 관련 있는지 **한국어로** 설명하세요.
연구원의 주제 및 연구 분야를 명시적으로 언급하세요.

## 논문 내용
{paper_content}

## 연구원 정보
{researcher_profiles}

다음 JSON 구조로 정확히 출력하세요. 모든 텍스트 값은 한국어로 작성하세요:
{{
  "summary": {{
    "core_topic": "...",
    "purpose": "...",
    "method": "...",
    "results": "...",
    "limitations": "...",
    "future_work": "..."
  }},
  "recommendations": [
    {{
      "researcher_id": 1,
      "reason": "..."
    }}
  ]
}}

중요: 모든 출력은 반드시 한국어여야 합니다. 영어로 작성하지 마세요."""

MAX_PAPER_LENGTH = 30000
MAX_RETRIES = 2

# 단가표(app/litreview/usage/pricing.py) 조회 키와 같아야 비용이 계산된다
CLAUDE_MODEL = "claude-sonnet-5"
# 연구 소개 노트를 모두 이어 붙였을 때의 상한 (프롬프트 비용 통제)
MAX_RESEARCH_FOCUS_LENGTH = 2000


def _get_paper_content(paper: CollectedPaper) -> str:
    """전문 또는 abstract 반환."""
    if paper.full_text:
        return paper.full_text[:MAX_PAPER_LENGTH]
    return paper.abstract or ""


def _build_researcher_profile(
    researcher_id: int, paper_id: int
) -> str:
    """추천 이유 작성을 위한 연구원 프로필 + 관련 주제."""
    researcher = db.session.get(Researcher, researcher_id)
    if not researcher:
        return ""

    rec = PaperRecommendation.query.filter_by(
        paper_id=paper_id, researcher_id=researcher_id
    ).first()

    profile_parts = [
        f"Researcher ID: {researcher.id}",
        f"Name: {researcher.name}",
    ]

    # 연구 소개 — 여러 건 작성할 수 있어 모두 이어 붙인다.
    # research_notes가 비어 있으면 구버전 단일 필드로 폴백한다
    # (마이그레이션 전 서버에서도 동작해야 하므로).
    from app.models import ResearchNote

    notes = (
        ResearchNote.query.filter_by(researcher_id=researcher_id)
        .order_by(ResearchNote.sort_order, ResearchNote.id)
        .all()
    )
    if notes:
        parts = []
        for n in notes:
            body = (n.content or "").strip()
            if not body:
                continue
            parts.append(f"[{n.title.strip()}] {body}" if n.title else body)
        focus = " / ".join(parts)
        # 노트가 늘어날수록 프롬프트가 커져 요약 비용이 오르므로 상한을 둔다.
        if len(focus) > MAX_RESEARCH_FOCUS_LENGTH:
            focus = focus[:MAX_RESEARCH_FOCUS_LENGTH] + "..."
        if focus:
            profile_parts.append(f"Research focus: {focus}")
    elif researcher.research_description:
        profile_parts.append(f"Research focus: {researcher.research_description}")

    # 추천된 주제 정보
    if rec and rec.topic:
        topic = rec.topic
        profile_parts.append(f"Matched topic: {topic.name}")
        if topic.keywords:
            profile_parts.append(f"Topic keywords: {', '.join(topic.keywords[:10])}")
        if topic.description:
            profile_parts.append(f"Topic description: {topic.description[:200]}")

        # 주제 레퍼런스 논문
        if topic.reference_papers:
            profile_parts.append("Topic reference papers:")
            for trp in topic.reference_papers[:5]:
                profile_parts.append(f'  - "{trp.title}"')

    # 유사도 점수
    if rec and rec.similarity_score:
        profile_parts.append(f"Similarity score: {rec.similarity_score:.4f}")

    return "\n".join(profile_parts)


SYSTEM_MESSAGE = """당신은 학술 논문 분석 전문가입니다.

핵심 원칙:
- **모든 출력은 반드시 한국어로 작성합니다.** 영어, 영중혼용, 영한혼용 모두 금지입니다.
- 영어 논문을 읽고 분석하더라도, 결과 텍스트(요약, 추천 이유 등)는 100% 한국어로 작성합니다.
- 전문 용어는 한국어 번역 + 괄호 안 영어 원어 표기를 허용합니다 (예: "디지털 트윈(Digital Twin)").
- JSON 구조 키 이름은 영어로 유지하되, 모든 값(value)은 한국어 문장으로 작성합니다.
- 자연스러운 한국어 학술 문체로 작성하며, 직역 투의 어색한 문장은 피합니다."""


def _record_claude_usage(response) -> None:
    """요약 호출의 토큰 사용량을 기록한다. 실패해도 요약은 계속 진행한다."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        from app.litreview.usage import record

        record(
            provider="anthropic",
            model=getattr(response, "model", None) or CLAUDE_MODEL,
            operation="summary",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception:
        logger.debug("요약 사용량 기록 실패", exc_info=True)


def _call_claude(prompt: str) -> dict:
    """Claude API 호출 + JSON 파싱."""
    client = anthropic.Anthropic(
        api_key=current_app.config["ANTHROPIC_API_KEY"]
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                # Sonnet 5 토크나이저는 한국어 기준 토큰 소모가 커서(구모델 대비 ~30%↑)
                # 2000이면 요약 JSON이 중간에 잘려 파싱 실패 — 여유 있게 상향
                max_tokens=8000,
                # Sonnet 5는 thinking이 기본 활성 — 요약 작업엔 불필요한 토큰이라 끔
                thinking={"type": "disabled"},
                system=SYSTEM_MESSAGE,
                messages=[{"role": "user", "content": prompt}],
            )
            _record_claude_usage(response)
            text = next(b.text for b in response.content if b.type == "text")

            # JSON 블록 추출
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            return json.loads(text.strip())

        except json.JSONDecodeError:
            logger.warning("JSON parse failed (attempt %d), raw: %.200s", attempt + 1, text)
            if attempt == MAX_RETRIES:
                raise
        except anthropic.APIError:
            logger.exception("Claude API error (attempt %d)", attempt + 1)
            if attempt == MAX_RETRIES:
                raise


def summarize_paper(
    paper_id: int,
    researcher_ids: list[int],
) -> dict:
    """Core 등급 논문 요약 + 연구원별 추천 이유.

    Args:
        paper_id: collected_papers.id
        researcher_ids: 이 논문이 core로 추천된 연구원 ID 리스트.

    Returns:
        {paper_id, summary: {...}, recommendations: [{researcher_id, reason}]}
    """
    paper = db.session.get(CollectedPaper, paper_id)
    if not paper:
        raise ValueError(f"Paper {paper_id} not found")

    paper_content = _get_paper_content(paper)
    if not paper_content:
        logger.warning("Paper %d has no content to summarize", paper_id)
        return {"paper_id": paper_id, "summary": None, "recommendations": []}

    # 연구원 ��로필 구성
    profiles = []
    for rid in researcher_ids:
        profile = _build_researcher_profile(rid, paper_id)
        if profile:
            profiles.append(profile)

    prompt = SUMMARY_PROMPT.format(
        paper_content=paper_content,
        researcher_profiles="\n\n---\n\n".join(profiles) if profiles else "No researcher profiles available.",
    )

    logger.info("Summarizing paper %d for %d researchers", paper_id, len(researcher_ids))
    result = _call_claude(prompt)

    summary = result.get("summary", {})
    recommendations = result.get("recommendations", [])

    # DB 업데이트
    for rec_data in recommendations:
        rid = rec_data.get("researcher_id")
        reason = rec_data.get("reason", "")

        # 해당 연구원의 모든 core 추천 레코드 업데이트 (주제별로 여러 개 있을 수 있음)
        recs = PaperRecommendation.query.filter_by(
            paper_id=paper_id, researcher_id=rid, grade="core"
        ).all()
        for rec in recs:
            rec.summary_core_topic = summary.get("core_topic")
            rec.summary_purpose = summary.get("purpose")
            rec.summary_method = summary.get("method")
            rec.summary_results = summary.get("results")
            rec.summary_limitations = summary.get("limitations")
            rec.summary_future = summary.get("future_work")
            rec.recommendation_reason = reason

    # researcher_ids에 있지만 Claude 응답에 없는 연구원도 요약은 채우기
    responded_ids = {r.get("researcher_id") for r in recommendations}
    for rid in researcher_ids:
        if rid not in responded_ids:
            recs = PaperRecommendation.query.filter_by(
                paper_id=paper_id, researcher_id=rid, grade="core"
            ).all()
            for rec in recs:
                rec.summary_core_topic = summary.get("core_topic")
                rec.summary_purpose = summary.get("purpose")
                rec.summary_method = summary.get("method")
                rec.summary_results = summary.get("results")
                rec.summary_limitations = summary.get("limitations")
                rec.summary_future = summary.get("future_work")

    db.session.commit()

    logger.info("Paper %d summarized, %d recommendations written", paper_id, len(recommendations))
    return {
        "paper_id": paper_id,
        "summary": summary,
        "recommendations": recommendations,
    }


def summarize_core_papers(researcher_id: int) -> list[dict]:
    """연구원의 core 등급 미요약 논문 전부 요약."""
    recs = PaperRecommendation.query.filter_by(
        researcher_id=researcher_id,
        grade="core",
    ).filter(
        PaperRecommendation.summary_core_topic.is_(None),
    ).all()

    if not recs:
        logger.info("No unsummarized core papers for researcher %d", researcher_id)
        return []

    results = []
    # paper_id로 그룹핑하여 중복 요약 방지
    paper_ids_seen = set()
    for rec in recs:
        if rec.paper_id in paper_ids_seen:
            continue
        paper_ids_seen.add(rec.paper_id)

        # 이 논문이 core인 모든 연구원 수집
        all_core_recs = PaperRecommendation.query.filter_by(
            paper_id=rec.paper_id, grade="core"
        ).all()
        all_researcher_ids = list(set(r.researcher_id for r in all_core_recs))

        try:
            result = summarize_paper(rec.paper_id, all_researcher_ids)
            results.append(result)
        except Exception:
            logger.exception("Failed to summarize paper %d", rec.paper_id)

    return results
