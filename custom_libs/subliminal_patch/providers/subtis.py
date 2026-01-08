# -*- coding: utf-8 -*-
"""Subtis provider for subliminal."""

from __future__ import annotations

import logging
import os
from json import JSONDecodeError
from typing import TYPE_CHECKING
from urllib.parse import quote

from guessit import guessit
from requests import Session
from requests.exceptions import HTTPError, RequestException, Timeout
from subliminal.video import Episode, Movie
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches
from subzero.language import Language

if TYPE_CHECKING:
    from subliminal.video import Video as SubtitleVideo

__version__ = "0.9.1"

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.subt.is/v1"
USER_AGENT = f"Bazarr/Subtis/{__version__}"
REQUEST_TIMEOUT_SECONDS = 10
DOWNLOAD_TIMEOUT_SECONDS = 30


class SubtisSubtitle(Subtitle):
    """Subtitle representation for the Subtis provider.

    Represents a Spanish subtitle from the subt.is API with metadata
    for matching against video files.
    """

    provider_name: str = "subtis"
    hash_verifiable: bool = False

    def __init__(
        self,
        language: Language,
        video: Movie,
        page_link: str,
        title: str,
        download_url: str,
        is_synced: bool = True,
    ) -> None:
        super().__init__(language, hearing_impaired=False, page_link=page_link)
        self.video = video
        self.download_url = download_url
        self.is_synced = is_synced
        self._title = str(title).strip()
        sync_indicator = "" if is_synced else " [fuzzy match]"
        self.release_info = f"{self._title}{sync_indicator}"

    @property
    def id(self) -> str:
        return self.page_link

    def get_matches(self, video: SubtitleVideo) -> set[str]:
        matches: set[str] = set()

        if isinstance(video, Movie):
            matches |= guess_matches(video, guessit(self._title, {"type": "movie"}))

        return matches


class SubtisProvider(Provider):
    """Subtis subtitle provider for Spanish language subtitles.

    Searches the subt.is API for subtitles by matching video file name
    and size. Falls back to fuzzy matching by filename only if exact
    match fails. Currently supports movies only.
    """

    languages: set[Language] = {Language.fromalpha2("es")}
    video_types: tuple[type[Movie], ...] = (Movie,)
    provider_name: str = "subtis"
    version: str = __version__

    def __init__(self) -> None:
        self.session: Session | None = None

    def initialize(self) -> None:
        self.session = Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )

    def terminate(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def _encode_filename(self, filename: str) -> str:
        return quote(filename, safe="")

    def _build_subtitle_url(self, file_size: int, filename: str) -> str:
        encoded = self._encode_filename(filename)
        return f"{API_BASE_URL}/subtitle/file/name/{file_size}/{encoded}"

    def _build_alternative_url(self, filename: str) -> str:
        encoded = self._encode_filename(filename)
        return f"{API_BASE_URL}/subtitle/file/alternative/{encoded}"

    def _parse_api_response(
        self,
        response_data: dict[str, object],
    ) -> tuple[str, str] | None:
        """Extract subtitle link and title from API response.

        Expects response in format:
            {"subtitle": {"subtitle_link": "..."}, "title": {"title_name": "..."}}

        Returns:
            Tuple of (subtitle_link, title_name), or None if required fields
            are missing. Uses "Unknown" as fallback for missing title_name.
        """
        if not isinstance(response_data, dict):
            return None

        subtitle_data = response_data.get("subtitle")
        if not isinstance(subtitle_data, dict):
            return None

        subtitle_link = subtitle_data.get("subtitle_link")
        if not isinstance(subtitle_link, str) or not subtitle_link:
            return None

        title_data = response_data.get("title", {})
        title_name = (
            title_data.get("title_name", "Unknown")
            if isinstance(title_data, dict)
            else "Unknown"
        )

        return subtitle_link, str(title_name)

    def _fetch_subtitle(
        self,
        url: str,
        filename: str,
    ) -> tuple[str, str] | None:
        """Fetch and parse subtitle from URL.

        Returns tuple of (subtitle_link, title_name) or None if not found.
        """
        if self.session is None:
            logger.warning("Session not initialized")
            return None

        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            return self._parse_api_response(data)
        except Timeout:
            logger.warning("Request timed out for %s", filename)
        except HTTPError as error:
            if error.response.status_code != 404:
                logger.warning("HTTP %s for %s", error.response.status_code, filename)
        except RequestException as error:
            logger.warning("Network error for %s: %s", filename, error)
        except JSONDecodeError as error:
            logger.warning("Invalid JSON response for %s: %s", filename, error)
        return None

    def query(self, language: Language, video: Movie | Episode) -> list[SubtisSubtitle]:
        if not video.name:
            logger.warning("Missing video name")
            return []

        filename = os.path.basename(video.name)

        # Try primary search (exact match by size + filename)
        if video.size:
            primary_url = self._build_subtitle_url(video.size, filename)
            logger.debug("Trying primary search for %s", filename)
            parsed = self._fetch_subtitle(primary_url, filename)
            if parsed:
                subtitle_link, title_name = parsed
                logger.debug("Found subtitle via primary search")
                return [
                    SubtisSubtitle(
                        language=language,
                        video=video,
                        page_link=primary_url,
                        title=title_name,
                        download_url=subtitle_link,
                        is_synced=True,
                    )
                ]

        # Fallback to alternative search (fuzzy match by filename only)
        alternative_url = self._build_alternative_url(filename)
        logger.debug("Trying alternative search for %s", filename)
        parsed = self._fetch_subtitle(alternative_url, filename)
        if parsed:
            subtitle_link, title_name = parsed
            logger.debug("Found subtitle via alternative search (fuzzy)")
            return [
                SubtisSubtitle(
                    language=language,
                    video=video,
                    page_link=alternative_url,
                    title=title_name,
                    download_url=subtitle_link,
                    is_synced=False,
                )
            ]

        logger.info("No subtitle found for %s", filename)
        return []

    def list_subtitles(
        self,
        video: Movie | Episode,
        languages: set[Language],
    ) -> list[SubtisSubtitle]:
        if isinstance(video, Episode):
            logger.debug("TV show support not yet implemented")
            return []

        subtitles: list[SubtisSubtitle] = []
        for language in languages:
            subtitles.extend(self.query(language, video))
        return subtitles

    def download_subtitle(self, subtitle: SubtisSubtitle) -> None:
        """Download subtitle content from the API.

        Fetches the subtitle file from subtitle.download_url and stores
        the content in subtitle.content. Handles network errors gracefully.
        """
        if self.session is None:
            logger.warning("Session not initialized")
            return

        if not subtitle.download_url:
            logger.warning("No download URL available")
            return

        try:
            response = self.session.get(
                subtitle.download_url,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            if not response.content:
                logger.warning("Empty subtitle content")
                return

            subtitle.content = response.content

        except Timeout:
            logger.warning(
                "Download timed out from %s",
                subtitle.download_url,
            )
        except HTTPError as error:
            logger.warning(
                "HTTP error %s downloading from %s",
                error.response.status_code,
                subtitle.download_url,
            )
        except RequestException as error:
            logger.warning(
                "Network error downloading from %s: %s",
                subtitle.download_url,
                error,
            )
