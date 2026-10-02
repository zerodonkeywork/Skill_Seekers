"""Discover subcommand parser."""

from __future__ import annotations

from .base import SubcommandParser


class DiscoverParser(SubcommandParser):
    """Parser for discover subcommand."""

    @property
    def name(self) -> str:
        return "discover"

    @property
    def help(self) -> str:
        return "Discover all URLs matching patterns and export ready-to-use config"

    @property
    def description(self) -> str:
        return (
            "Crawl and discover all URLs matching pattern filters across pages and "
            "pagination, outputting a config ready for 'create'"
        )

    def add_arguments(self, parser):
        """Add discover-specific arguments."""
        parser.add_argument(
            "source",
            help="Source config JSON file or base URL to discover from",
        )
        parser.add_argument(
            "--include",
            action="append",
            dest="include_patterns",
            help="URL patterns to include (can be specified multiple times)",
        )
        parser.add_argument(
            "--exclude",
            action="append",
            dest="exclude_patterns",
            help="URL patterns to exclude (can be specified multiple times)",
        )
        parser.add_argument(
            "--out",
            "-o",
            dest="output_file",
            help="Path to write output config JSON (prints to stdout if omitted)",
        )
        parser.add_argument(
            "--name",
            help="Skill name (defaults to name from config or inferred from URL)",
        )
        parser.add_argument(
            "--rate-limit",
            type=float,
            help="Delay between requests in seconds (defaults to config or 0.5s)",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="HTTP timeout in seconds (default: 30)",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=-1,
            help="Maximum pages to traverse (-1 for unlimited, default: -1)",
        )
        parser.add_argument(
            "--cache-dir",
            help="Directory to cache discovery pages for speed and reproducibility",
        )
        parser.add_argument(
            "--no-cache",
            action="store_true",
            help="Disable reading from / writing to page cache during discovery",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=1,
            help="Concurrent worker count for fetching pagination pages (default: 1)",
        )
        parser.add_argument(
            "--verbose",
            "-v",
            action="store_true",
            help="Enable verbose output",
        )
        parser.add_argument(
            "--quiet",
            "-q",
            action="store_true",
            help="Minimize output (only errors and final JSON output)",
        )
