"""Scopus Full-Text API로 core 등급 논문 전문 다운로드. 실패 시 abstract fallback."""

import logging
import time

import requests
from bs4 import BeautifulSoup
from flask import current_app
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.models import CollectedPaper

logger = logging.getLogger(__name__)

FULLTEXT_URL = "https://api.elsevier.com/content/article/scopus_id/{scopus_id}"
FULLTEXT_DELAY = 0.15


def _create_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503],
        allowed_methods=["GET"],
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _headers() -> dict:
    return {
        "X-ELS-APIKey": current_app.config["SCOPUS_API_KEY"],
        "X-ELS-Insttoken": current_app.config.get("SCOPUS_INST_TOKEN", ""),
        "Accept": "text/xml",
    }


def _extract_text_from_xml(xml_content: str) -> str:
    """Elsevier Full-Text XML에서 본문 텍스트 추출."""
    soup = BeautifulSoup(xml_content, "lxml-xml")

    sections = soup.find_all("ce:sections")
    if sections:
        return sections[0].get_text(separator="\n", strip=True)

    body = soup.find("body")
    if body:
        return body.get_text(separator="\n", strip=True)

    return soup.get_text(separator="\n", strip=True)


def download_fulltext(
    paper_ids: list[int],
    progress_callback=None,
) -> dict:
    """core 등급 논문 전문 다운로드.

    Args:
        paper_ids: collected_papers.id 리스트 (core 등급).
        progress_callback: fn(done, total).

    Returns:
        {downloaded, failed, fallback_to_abstract}
    """
    session = _create_session()
    downloaded = 0
    failed = 0
    fallback = 0
    total = len(paper_ids)

    for idx, pid in enumerate(paper_ids):
        paper = db.session.get(CollectedPaper, pid)
        if not paper:
            failed += 1
            continue

        if paper.full_text:
            if progress_callback:
                progress_callback(idx + 1, total)
            continue

        if not paper.scopus_id:
            fallback += 1
            if progress_callback:
                progress_callback(idx + 1, total)
            continue

        time.sleep(FULLTEXT_DELAY)
        url = FULLTEXT_URL.format(scopus_id=paper.scopus_id)

        try:
            resp = session.get(url, headers=_headers(), timeout=30)
            resp.raise_for_status()
            full_text = _extract_text_from_xml(resp.text)

            if full_text and len(full_text) > 200:
                paper.full_text = full_text
                downloaded += 1
                logger.debug("Downloaded fulltext for paper %d (%s)", pid, paper.scopus_id)
            else:
                fallback += 1
                logger.debug("Fulltext too short for paper %d, using abstract", pid)

        except requests.RequestException:
            fallback += 1
            logger.warning(
                "Fulltext download failed for paper %d (%s), falling back to abstract",
                pid,
                paper.scopus_id,
            )

        if (idx + 1) % 10 == 0:
            db.session.commit()

        if progress_callback:
            progress_callback(idx + 1, total)

    db.session.commit()

    logger.info(
        "Fulltext download: %d downloaded, %d failed, %d fallback to abstract",
        downloaded,
        failed,
        fallback,
    )
    return {
        "downloaded": downloaded,
        "failed": failed,
        "fallback_to_abstract": fallback,
    }
