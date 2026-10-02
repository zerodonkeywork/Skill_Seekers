"""Discover command to crawl and extract all matching URLs from a site.

Generates a ready-to-use config file with all discovered URLs in ``start_urls``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from skill_seekers.cli.doc_scraper import _normalize_url
from skill_seekers.cli.exit_codes import EXIT_ERROR, EXIT_SUCCESS
from skill_seekers.cli.html_parsing import parse_html

logger = logging.getLogger(__name__)

PAGINATION_INPUT_NAMES = {
    "pn",
    "page",
    "pageno",
    "page_no",
    "pageindex",
    "page_index",
    "p",
    "start",
    "offset",
}

TOTAL_PAGES_INPUT_NAMES = {
    "pa",
    "totalpages",
    "total_pages",
    "pagecount",
    "page_count",
    "maxpage",
    "max_page",
}


class DiscoverCommand:
    """Discover all URLs matching patterns across pages and pagination."""

    def __init__(self, args: Any) -> None:
        self.args = args
        self.quiet: bool = getattr(args, "quiet", False)
        self.verbose: bool = getattr(args, "verbose", False)
        self.timeout: int = getattr(args, "timeout", 30)
        self.rate_limit: float = getattr(args, "rate_limit", 0.5)
        self.no_cache: bool = getattr(args, "no_cache", False)
        self.cache_dir: Path | None = None

    def execute(self) -> int:
        """Run the discover command."""
        source_str = str(self.args.source)

        try:
            config, doc_source, is_url = self._load_or_create_config(source_str)
        except Exception as e:
            self._log_error(f"Failed to load source: {e}")
            return EXIT_ERROR

        base_url = doc_source["base_url"]
        start_urls = doc_source.get("start_urls", [base_url])
        url_patterns = doc_source.get("url_patterns", {})
        include_patterns = (
            self.args.include_patterns
            if self.args.include_patterns is not None
            else url_patterns.get("include", [])
        )
        exclude_patterns = (
            self.args.exclude_patterns
            if self.args.exclude_patterns is not None
            else url_patterns.get("exclude", [])
        )

        rate_limit_val = getattr(self.args, "rate_limit", None)
        if rate_limit_val is None:
            rate_limit_val = doc_source.get("rate_limit", 0.5)
        self.rate_limit = float(rate_limit_val if rate_limit_val is not None else 0.5)
        doc_source["rate_limit"] = self.rate_limit

        name = self.args.name or config.get("name", "discovered-skill")
        config["name"] = name
        doc_source["name"] = f"{name}-docs" if is_url else doc_source.get("name", f"{name}-docs")

        # Setup cache dir
        if not self.no_cache:
            self.cache_dir = self._resolve_cache_dir(name)

        if not self.quiet:
            self._log_info("=" * 60)
            self._log_info(f"🔍 Skill Seekers Discover: {name}")
            self._log_info("=" * 60)
            self._log_info(f"Base URL: {base_url}")
            self._log_info(f"Start URLs: {len(start_urls)}")
            self._log_info(f"Include patterns: {include_patterns}")
            if exclude_patterns:
                self._log_info(f"Exclude patterns: {exclude_patterns}")
            if self.cache_dir and self.cache_dir.exists():
                self._log_info(f"Cache directory: {self.cache_dir}")
            self._log_info("")

        # Run discovery
        discovered_urls, total_expected = self._discover(
            base_url=base_url,
            start_urls=start_urls,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )

        if not discovered_urls:
            self._log_error("❌ No matching URLs discovered.")
            return EXIT_ERROR

        # Check completeness
        if total_expected and len(discovered_urls) < total_expected * 0.9:
            ratio = len(discovered_urls) / total_expected
            self._log_warning(
                f"⚠️  Discovered only {len(discovered_urls)} / {total_expected} URLs "
                f"({ratio:.1%}). List may be incomplete."
            )

        if not self.quiet:
            self._log_info(f"\n✅ Discovered {len(discovered_urls)} unique matching URLs!")

        # Update config with discovered URLs
        doc_source["start_urls"] = sorted(discovered_urls)
        doc_source["url_patterns"] = {
            "include": include_patterns,
            "exclude": exclude_patterns,
        }

        # Output config
        output_file = getattr(self.args, "output_file", None)
        config_json = json.dumps(config, indent=2, ensure_ascii=False)

        if output_file:
            out_path = Path(output_file)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(config_json, encoding="utf-8")
            if not self.quiet:
                self._log_info(f"💾 Saved configuration to: {output_file}")
                self._log_info(
                    f"👉 Run 'skill-seekers create {output_file} --name {name}' to crawl."
                )
        else:
            # Print to stdout
            print(config_json)

        return EXIT_SUCCESS

    def _load_or_create_config(
        self, source_str: str
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Load config from file or create new unified config from URL.

        Returns:
            Tuple of (root_config, doc_source_dict, is_url_bool)
        """
        if source_str.startswith("http://") or source_str.startswith("https://"):
            parsed = urlparse(source_str)
            domain_name = parsed.netloc.split(":")[0].replace(".", "-")
            config = {
                "name": domain_name,
                "sources": [
                    {
                        "type": "documentation",
                        "name": f"{domain_name}-docs",
                        "base_url": source_str,
                        "start_urls": [source_str],
                        "selectors": {
                            "main_content": "main, div.content, article, div.page, body",
                            "title": "title",
                        },
                        "url_patterns": {"include": [], "exclude": []},
                    }
                ],
            }
            return config, config["sources"][0], True

        source_path = Path(source_str)
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_str}")

        with open(source_path, encoding="utf-8") as f:
            raw_config = json.load(f)

        # Handle unified format
        if "sources" in raw_config and isinstance(raw_config["sources"], list):
            config = copy.deepcopy(raw_config)
            doc_sources = [s for s in config["sources"] if s.get("type") == "documentation"]
            if not doc_sources:
                raise ValueError("Config does not contain any 'documentation' sources")
            return config, doc_sources[0], False

        # Handle legacy format
        doc_source = {
            "type": "documentation",
            "name": f"{raw_config.get('name', 'skill')}-docs",
            "base_url": raw_config["base_url"],
            "start_urls": raw_config.get("start_urls", [raw_config["base_url"]]),
            "selectors": raw_config.get("selectors", {}),
            "url_patterns": raw_config.get("url_patterns", {}),
            "rate_limit": raw_config.get("rate_limit", 0.5),
        }
        unified_config = {
            "name": raw_config.get("name", "skill"),
            "description": raw_config.get("description", ""),
            "sources": [doc_source],
        }
        return unified_config, doc_source, False

    def _resolve_cache_dir(self, name: str) -> Path:
        """Resolve a suitable cache directory for discovery pages."""
        if getattr(self.args, "cache_dir", None):
            cdir = Path(self.args.cache_dir)
            cdir.mkdir(parents=True, exist_ok=True)
            return cdir

        candidates = [
            Path("../listcache"),
            Path("listcache"),
            Path(f".skillseeker-cache/{name}/discover_cache"),
            Path(f"../.skillseeker-cache/{name}/discover_cache"),
        ]
        for c in candidates:
            if c.exists() and any(c.glob("*.html")):
                return c

        default_dir = Path(f".skillseeker-cache/{name}/discover_cache")
        default_dir.mkdir(parents=True, exist_ok=True)
        return default_dir

    def _discover(
        self,
        base_url: str,
        start_urls: list[str],
        include_patterns: list[str],
        exclude_patterns: list[str],
    ) -> tuple[set[str], int | None]:
        """Perform discovery crawl across site navigation and pagination."""
        discovered: set[str] = set()
        visited_pages: set[str] = set()
        pending_pages: deque[str] = deque(start_urls)
        crawled_forms: set[str] = set()
        total_expected: int | None = None

        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Documentation Scraper - Discover)"})

        base_prefix = base_url if base_url.endswith("/") else base_url + "/"

        max_pages = getattr(self.args, "max_pages", -1)
        unlimited = max_pages in (-1, None)

        while pending_pages and (unlimited or len(visited_pages) < max_pages):
            curr_url = pending_pages.popleft()
            curr_url = _normalize_url(curr_url)

            if curr_url in visited_pages:
                continue
            visited_pages.add(curr_url)

            # Check if curr_url itself matches include patterns
            if self._matches_patterns(curr_url, include_patterns, exclude_patterns):
                discovered.add(curr_url)

            # Fetch page HTML
            html = self._fetch_page(session, curr_url)
            if not html:
                continue

            soup = parse_html(html, context=curr_url)

            # 1. Extract all target URLs and navigational links
            for link in soup.find_all("a", href=True):
                raw_href = str(link.get("href") or "").strip()
                if not raw_href or raw_href.startswith("#") or raw_href.startswith("javascript:"):
                    continue

                full_url = urljoin(curr_url, raw_href).split("#")[0]
                full_url = _normalize_url(full_url)

                if self._matches_patterns(full_url, include_patterns, exclude_patterns):
                    discovered.add(full_url)
                elif full_url.startswith(base_prefix) and full_url not in visited_pages:
                    # Enqueue navigational list/category pages
                    parsed_path = urlparse(full_url).path.lower()
                    if not re.search(r"\.(png|jpg|jpeg|gif|svg|pdf|zip|css|js)$", parsed_path):
                        pending_pages.append(full_url)

            # 2. Check for form-based pagination on this page
            paging_info = self._detect_pagination_form(soup, html, curr_url)
            if paging_info:
                form_action, method, base_data, page_param, num_pages, expected_items = paging_info
                form_key = f"{method}:{form_action}:{page_param}:{num_pages}"
                if form_key not in crawled_forms:
                    crawled_forms.add(form_key)
                    if expected_items:
                        total_expected = expected_items

                    if not self.quiet:
                        self._log_info(
                            f"📑 Pagination detected: {method.upper()} {form_action} "
                            f"({num_pages} pages, param '{page_param}')"
                        )

                    self._crawl_pagination(
                        session=session,
                        action_url=form_action,
                        method=method,
                        base_data=base_data,
                        page_param=page_param,
                        total_pages=num_pages,
                        include_patterns=include_patterns,
                        exclude_patterns=exclude_patterns,
                        discovered=discovered,
                    )

        return discovered, total_expected

    def _matches_patterns(
        self, url: str, include_patterns: list[str], exclude_patterns: list[str]
    ) -> bool:
        """Check if URL matches include patterns and not exclude patterns."""
        if include_patterns and not any(p in url for p in include_patterns):
            return False
        return not any(p in url for p in exclude_patterns)

    def _detect_pagination_form(
        self, soup: BeautifulSoup, html: str, current_url: str
    ) -> tuple[str, str, dict[str, Any], str, int, int | None] | None:
        """Detect form-based or onclick pagination on the page."""
        # Find all forms
        for form in soup.find_all("form"):
            inputs: dict[str, str] = {}
            for inp in form.find_all("input"):
                name = str(inp.get("name") or "").strip()
                if name:
                    inputs[name] = str(inp.get("value") or "").strip()

            lower_names = {k.lower(): k for k in inputs}
            matching_page_param = set(lower_names.keys()) & PAGINATION_INPUT_NAMES
            if not matching_page_param:
                continue

            page_param_key = lower_names[list(matching_page_param)[0]]
            action = urljoin(current_url, str(form.get("action") or current_url))
            method = str(form.get("method") or "post").lower()

            # Determine total pages
            total_pages = 1
            matching_total_param = set(lower_names.keys()) & TOTAL_PAGES_INPUT_NAMES
            if matching_total_param:
                val = inputs[lower_names[list(matching_total_param)[0]]]
                if val.isdigit():
                    total_pages = max(total_pages, int(val))

            # Also check onclick patterns like doPaging(241)
            onclick_pages = [
                int(x)
                for x in re.findall(
                    r"(?:doPaging|goToPage|gotoPage|setPage)\s*\(\s*(\d+)\s*\)", html
                )
            ]
            if onclick_pages:
                total_pages = max(total_pages, max(onclick_pages))

            # Expected items (e.g. iA=4812 or "共 4812 筆")
            expected_items = None
            if "ia" in lower_names and inputs[lower_names["ia"]].isdigit():
                expected_items = int(inputs[lower_names["ia"]])
            else:
                items_match = re.search(r"共\s*(\d+)\s*筆", html)
                if items_match:
                    expected_items = int(items_match.group(1))

            if total_pages > 1:
                return action, method, inputs, page_param_key, total_pages, expected_items

        return None

    def _crawl_pagination(
        self,
        session: requests.Session,
        action_url: str,
        method: str,
        base_data: dict[str, Any],
        page_param: str,
        total_pages: int,
        include_patterns: list[str],
        exclude_patterns: list[str],
        discovered: set[str],
    ) -> None:
        """Crawl all pages of a detected pagination form."""
        start_time = time.time()
        for page_num in range(1, total_pages + 1):
            page_data = dict(base_data)
            page_data[page_param] = str(page_num)

            html = self._fetch_pagination_page(
                session=session,
                action_url=action_url,
                method=method,
                page_data=page_data,
                page_num=page_num,
            )
            if not html:
                continue

            # Extract URLs
            new_this_page = 0
            for href in re.findall(r'href=["\']([^"\']+)["\']', html):
                full_url = urljoin(action_url, href).split("#")[0]
                full_url = _normalize_url(full_url)
                if (
                    self._matches_patterns(full_url, include_patterns, exclude_patterns)
                    and full_url not in discovered
                ):
                    discovered.add(full_url)
                    new_this_page += 1

            if not self.quiet and (page_num % 10 == 0 or page_num == total_pages or page_num == 1):
                elapsed = time.time() - start_time
                rate = page_num / elapsed if elapsed > 0 else 0
                self._log_info(
                    f"  ⏳ [{page_num}/{total_pages}] "
                    f"Discovered: {len(discovered)} URLs ({rate:.1f} pages/s)"
                )

    def _fetch_page(self, session: requests.Session, url: str) -> str | None:
        """Fetch a single page with retries and cache check."""
        # Try disk cache first
        if self.cache_dir:
            cache_file = self.cache_dir / f"page_{hash(url) & 0xFFFFFFFF:08x}.html"
            if cache_file.exists() and cache_file.stat().st_size > 1000:
                with contextlib.suppress(OSError):
                    return cache_file.read_text(encoding="utf-8", errors="ignore")

        for attempt in range(1, 4):
            try:
                resp = session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                html = resp.text
                if self.cache_dir and len(html) > 1000:
                    cache_file = self.cache_dir / f"page_{hash(url) & 0xFFFFFFFF:08x}.html"
                    with contextlib.suppress(OSError):
                        cache_file.write_text(html, encoding="utf-8")
                if self.rate_limit and self.rate_limit > 0:
                    time.sleep(self.rate_limit)
                return html
            except Exception as e:
                if self.verbose:
                    self._log_warning(f"Fetch {url} attempt {attempt} failed: {e}")
                time.sleep(1)
        return None

    def _fetch_pagination_page(
        self,
        session: requests.Session,
        action_url: str,
        method: str,
        page_data: dict[str, Any],
        page_num: int,
    ) -> str | None:
        """Fetch a pagination page (using cache if available)."""
        # Check standard cache name (e.g. p0001.html)
        if self.cache_dir:
            cache_file = self.cache_dir / f"p{page_num:04d}.html"
            if cache_file.exists() and cache_file.stat().st_size > 1000:
                with contextlib.suppress(OSError):
                    return cache_file.read_text(encoding="utf-8", errors="ignore")

        # Live request
        for attempt in range(1, 4):
            try:
                if method == "post":
                    resp = session.post(action_url, data=page_data, timeout=self.timeout)
                else:
                    resp = session.get(action_url, params=page_data, timeout=self.timeout)
                resp.raise_for_status()
                html = resp.text

                if self.cache_dir and len(html) > 1000:
                    cache_file = self.cache_dir / f"p{page_num:04d}.html"
                    with contextlib.suppress(OSError):
                        cache_file.write_text(html, encoding="utf-8")

                if self.rate_limit and self.rate_limit > 0:
                    time.sleep(self.rate_limit)
                return html
            except Exception as e:
                if self.verbose:
                    self._log_warning(f"Page {page_num} attempt {attempt} failed: {e}")
                time.sleep(1)
        return None

    def _log_info(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def _log_warning(self, msg: str) -> None:
        print(msg, file=sys.stderr)

    def _log_error(self, msg: str) -> None:
        print(f"Error: {msg}", file=sys.stderr)
