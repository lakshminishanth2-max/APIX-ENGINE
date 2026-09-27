"""Browser-backed collection tier (Playwright, synchronous API).

Optimized for robust live network interception and SPA handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Final

from django.conf import settings

from apix.collectors.base import (
    BaseCollector,
    CollectionTask,
    ExtractedFare,
    ParseError,
    RawCollectorResponse,
    TransportError,
)
from apix.collectors.registry import SourceRegistry
from apix.enums import InventoryStatus, PayloadKind, SourceKind
from apix.policies.challenge import BotChallengeDetected, BotChallengeDetector

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Response

__all__ = ["GenericJsonApiCollector", "PlaywrightCollector"]

logger = logging.getLogger(__name__)
UTC = timezone.utc

_DEFAULT_WAIT_MS: Final[int] = 45_000


class PlaywrightCollector(BaseCollector):
    """Reusable browser base class."""

    requires_browser: ClassVar[bool] = True
    kind: ClassVar[str] = SourceKind.PERMITTED_WEB
    fare_response_pattern: ClassVar[str] = "/api/"
    results_ready_selector: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        cfg = settings.APIX
        self._headless: bool = bool(options.get("headless", cfg.get("PLAYWRIGHT_HEADLESS", True)))
        self._browser_name: str = str(options.get("browser", cfg.get("PLAYWRIGHT_BROWSER", "chromium")))
        self._timeout_ms: int = int(options.get("timeout_ms", cfg.get("PLAYWRIGHT_TIMEOUT_MS", _DEFAULT_WAIT_MS)))
        self._locale: str = str(options.get("locale", cfg.get("PLAYWRIGHT_LOCALE", "en-IN")))
        self._timezone: str = str(options.get("timezone", cfg.get("PLAYWRIGHT_TIMEZONE", "Asia/Kolkata")))
        self._user_agent: str = str(
            options.get(
                "user_agent",
                cfg.get(
                    "USER_AGENT",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                ),
            )
        )
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def open(self) -> None:
        if self._browser is not None:
            return

        # Ensure Windows event loop policy allows async subprocess execution
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise TransportError(
                "playwright is not installed; run `pip install playwright && playwright install chromium`",
                source_code=self.source_code,
                retryable=False,
            ) from exc

        self._playwright = sync_playwright().start()
        launcher = getattr(self._playwright, self._browser_name)
        self._browser = launcher.launch(
            headless=self._headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.log.info("browser launched", extra={"browser": self._browser_name, "headless": self._headless})

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
            self._playwright = None

    def _new_context(self) -> BrowserContext:
        if self._browser is None:
            self.open()
        assert self._browser is not None
        return self._browser.new_context(
            user_agent=self._user_agent,
            locale=self._locale,
            timezone_id=self._timezone,
            viewport={"width": 1440, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=False,
        )

    def fetch(self, task: CollectionTask) -> RawCollectorResponse:
        """Drive the SPA and capture the fare API response verbatim."""
        url = self.search_url(task)
        context = self._new_context()
        captured: list[dict[str, Any]] = []
        status_seen: dict[str, Any] = {}

        def _on_response(response: Response) -> None:
            if self.fare_response_pattern not in response.url:
                return
            try:
                body = response.json()
            except Exception:
                try:
                    text_body = response.text()
                    body = json.loads(text_body)
                except Exception:
                    return

            captured.append({"url": response.url, "status": response.status, "body": body})
            status_seen.setdefault("status", response.status)
            status_seen.setdefault("headers", dict(response.headers))

        page: Page | None = None
        try:
            page = context.new_page()
            page.set_default_timeout(self._timeout_ms)
            page.on("response", _on_response)

            navigation = page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            http_status = navigation.status if navigation is not None else None

            self._assert_no_challenge(page, http_status, dict(navigation.headers) if navigation else {})

            # Prefer targeted selector wait or event wait over networkidle
            if self.results_ready_selector:
                page.wait_for_selector(self.results_ready_selector, timeout=self._timeout_ms)
            else:
                # Wait up to 10 seconds for the target XHR call to register
                for _ in range(20):
                    if captured:
                        break
                    page.wait_for_timeout(500)

            if not captured:
                raise ParseError(
                    f"no response matching {self.fare_response_pattern!r} was observed at {url}",
                    source_code=self.source_code,
                )

            payload: dict[str, Any] = {
                "captured": captured,
                "page_url": page.url,
                "search": {
                    "origin": task.origin,
                    "destination": task.destination,
                    "departure_date": task.departure_date.isoformat(),
                    "lead_window_days": task.lead_window_days,
                    "cabin": task.cabin,
                },
            }
            return RawCollectorResponse(
                payload=payload,
                request_url=url,
                payload_kind=PayloadKind.JSON,
                http_status=status_seen.get("status", http_status),
                response_headers=status_seen.get("headers", {}),
                captured_at=datetime.now(UTC),
                collector=self.source_code,
            )
        except BotChallengeDetected:
            raise
        except ParseError:
            raise
        except Exception as exc:
            raise TransportError(
                f"browser navigation failed for {url}: {exc}", source_code=self.source_code
            ) from exc
        finally:
            if page is not None:
                page.close()
            context.close()

    def _assert_no_challenge(
        self, page: Page, http_status: int | None, headers: dict[str, str]
    ) -> None:
        try:
            snippet = page.content()[:8_192]
        except Exception:
            snippet = ""
        signal = BotChallengeDetector.detect(
            status_code=http_status or 200, headers=headers, body_snippet=snippet
        )
        if signal is not None:
            raise BotChallengeDetected(signal, source_code=self.source_code)


@SourceRegistry.register(
    "permitted_web_json",
    kind=SourceKind.PERMITTED_WEB,
    tags=("browser", "spa", "configurable"),
)
class GenericJsonApiCollector(PlaywrightCollector):
    """Configuration-driven collector for an SPA."""

    source_code: ClassVar[str] = "permitted_web_json"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        attributes: dict[str, Any] = dict(options.get("attributes") or {})
        self._search_template: str = attributes.get("search_url_template", "")
        self.fare_response_pattern = attributes.get("fare_response_pattern", "/api/")
        self.results_ready_selector = attributes.get("results_ready_selector", "")
        self._results_path: list[str] = list(attributes.get("results_path") or [])
        self._field_map: dict[str, list[str]] = dict(attributes.get("field_map") or {})
        self._sold_out_when: dict[str, Any] = dict(attributes.get("sold_out_when") or {})

    def search_url(self, task: CollectionTask) -> str:
        if not self._search_template:
            raise ParseError(
                f"source {self.source_code!r} has no 'search_url_template' configured",
                source_code=self.source_code,
            )
        return self._search_template.format(
            origin=task.origin,
            destination=task.destination,
            departure_date=task.departure_date.isoformat(),
            cabin=task.cabin,
            currency=task.currency,
        )

    def extract(
        self, payload: dict[str, Any] | list[Any], task: CollectionTask
    ) -> list[ExtractedFare]:
        if not isinstance(payload, dict):
            raise ParseError("expected an object payload", source_code=self.source_code)

        rows: list[Any] = []
        for capture in payload.get("captured", []):
            body = capture.get("body")
            extracted = self._dig(body, self._results_path)
            if isinstance(extracted, list):
                rows.extend(extracted)
            elif isinstance(extracted, dict):
                rows.append(extracted)

        if not rows:
            raise ParseError(
                f"no rows at results_path {self._results_path!r}", source_code=self.source_code
            )

        fares: list[ExtractedFare] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            fare: ExtractedFare = {"cabin": task.cabin, "currency": task.currency}
            for field_name, path in self._field_map.items():
                value = self._dig(row, path)
                if value is not None:
                    fare[field_name] = value  # type: ignore[literal-required]
            fare["inventory_status"] = (
                InventoryStatus.SOLD_OUT if self._is_sold_out(row) else InventoryStatus.AVAILABLE
            )
            if fare["inventory_status"] == InventoryStatus.SOLD_OUT:
                for key in ("base_fare", "udf", "psf", "asf", "gst", "other_charges", "total_fare"):
                    fare.pop(key, None)  # type: ignore[misc]
            fares.append(fare)
        return fares

    def _is_sold_out(self, row: dict[str, Any]) -> bool:
        if not self._sold_out_when:
            return False
        actual = self._dig(row, list(self._sold_out_when.get("path", [])))
        if "equals" in self._sold_out_when:
            return actual == self._sold_out_when["equals"]
        if "in" in self._sold_out_when:
            return actual in self._sold_out_when["in"]
        return bool(actual)

    @staticmethod
    def _dig(node: Any, path: list[str]) -> Any:
        current = node
        for segment in path:
            if isinstance(current, dict):
                if segment not in current:
                    return None
                current = current[segment]
            elif isinstance(current, list):
                if segment.isdigit():
                    idx = int(segment)
                    if idx >= len(current):
                        return None
                    current = current[idx]
                else:
                    sub = [item.get(segment) for item in current if isinstance(item, dict) and segment in item]
                    current = sub[0] if sub else None
            else:
                return None
        return current

    def health_check(self) -> dict[str, Any]:
        return {
            "source_code": self.source_code,
            "kind": self.kind,
            "configured": bool(self._search_template and self._field_map),
            "response_pattern": self.fare_response_pattern,
            "healthy": bool(self._search_template),
        }