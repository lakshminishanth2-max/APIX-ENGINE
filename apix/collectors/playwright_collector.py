"""Browser-backed collection tier (Playwright, synchronous API).

Why Playwright, and why the *sync* API
--------------------------------------
Fare pages are single-page applications: the price a passenger sees is rendered
from an XHR/fetch response, not from server-side HTML.  Playwright can attach to
that network layer, so we capture and hash **the JSON the carrier itself
served** rather than text scraped out of a rendered DOM.  For a statistical
publication that distinction is the whole ballgame - the immutable
``RawObservation`` must hold source-authored bytes, not our interpretation of
pixels.

Celery's prefork worker is a synchronous process, so ``playwright.sync_api`` is
the correct binding: no event loop bridge, no ``asgiref.sync_to_async`` dance,
no risk of a stray coroutine leaking across a fork.  Scrapy was rejected because
its Twisted reactor cannot cohabit with the Celery prefork model and it still
needs a browser for SPA pricing; Selenium was rejected because it offers no
first-class response interception and costs roughly three times the memory per
session.

Compliance posture - read this before extending the class
---------------------------------------------------------
This collector performs **no evasion of any kind**.  Specifically it does not,
and must never:

* patch ``navigator.webdriver`` or load any stealth/fingerprint-spoofing plugin;
* rotate user agents, proxies or residential IPs;
* solve, bypass or replay a CAPTCHA;
* ignore ``robots.txt`` or a ``Crawl-delay`` directive.

It identifies itself honestly through a contactable ``User-Agent``, is gated by
the token bucket, and when it detects an anti-bot challenge it **stops** - the
:class:`BotChallengeDetected` exception halts the source and requires an
operator to resume it.  A halted source is a policy outcome, not an incident to
engineer around.
"""

from __future__ import annotations

import logging
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

if TYPE_CHECKING:  # pragma: no cover - import cost only paid in the worker
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Response

__all__ = ["GenericJsonApiCollector", "PlaywrightCollector"]

logger = logging.getLogger(__name__)
UTC = timezone.utc

_DEFAULT_WAIT_MS: Final[int] = 45_000


class PlaywrightCollector(BaseCollector):
    """Reusable browser base class.  Subclass and set ``source_code``.

    Lifecycle: one :class:`~playwright.sync_api.Browser` per collector instance,
    one :class:`~playwright.sync_api.BrowserContext` per task (contexts are
    cheap and give per-search cookie isolation, which keeps searches
    independent), and the browser torn down in :meth:`close`.  Celery recycles
    the worker child every ``CELERY_WORKER_MAX_TASKS_PER_CHILD`` tasks because
    long-lived Chromium processes accumulate memory.
    """

    requires_browser: ClassVar[bool] = True
    kind: ClassVar[str] = SourceKind.PERMITTED_WEB

    #: Substring or regex matched against response URLs to find the fare API.
    fare_response_pattern: ClassVar[str] = "/api/"
    #: Selector proving the results actually rendered (belt and braces).
    results_ready_selector: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        cfg = settings.APIX
        self._headless: bool = bool(options.get("headless", cfg["PLAYWRIGHT_HEADLESS"]))
        self._browser_name: str = str(options.get("browser", cfg["PLAYWRIGHT_BROWSER"]))
        self._timeout_ms: int = int(options.get("timeout_ms", cfg.get("PLAYWRIGHT_TIMEOUT_MS", _DEFAULT_WAIT_MS)))
        self._locale: str = str(options.get("locale", cfg["PLAYWRIGHT_LOCALE"]))
        self._timezone: str = str(options.get("timezone", cfg["PLAYWRIGHT_TIMEZONE"]))
        self._user_agent: str = str(options.get("user_agent", cfg["USER_AGENT"]))
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def open(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - deployment error
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
                "--disable-dev-shm-usage",   # container-safe shared memory
                "--no-sandbox",              # required in the slim container image
                "--disable-gpu",
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
            # An honest, contactable identity.  Never a spoofed consumer UA.
            user_agent=self._user_agent,
            locale=self._locale,
            timezone_id=self._timezone,
            viewport={"width": 1440, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=False,
        )

    # -- fetch -------------------------------------------------------------- #
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
            except Exception:  # noqa: BLE001 - non-JSON responses are not ours
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

            # Fail-safe before anything else: if the origin is challenging us,
            # stop immediately.  Do not retry, do not "wait it out".
            self._assert_no_challenge(page, http_status, dict(navigation.headers) if navigation else {})

            if self.results_ready_selector:
                page.wait_for_selector(self.results_ready_selector, timeout=self._timeout_ms)
            else:
                page.wait_for_load_state("networkidle", timeout=self._timeout_ms)

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
        except Exception as exc:  # noqa: BLE001
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
        """Halt the source the instant an anti-bot interstitial is recognised."""
        try:
            snippet = page.content()[:8_192]
        except Exception:  # noqa: BLE001 - a torn-down page is itself suspicious
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
    """Configuration-driven collector for a robots-permitted SPA.

    Rather than hard-coding one airline's DOM, the response pattern and the
    field mapping come from ``Source.attributes``, so onboarding a newly
    permitted site is a data change reviewed by the compliance officer, not a
    code deploy::

        {
          "search_url_template": "https://example.test/search?from={origin}&to={destination}&d={departure_date}",
          "fare_response_pattern": "/api/v3/availability",
          "results_path": ["data", "flights"],
          "field_map": {
            "carrier_iata":  ["airlineCode"],
            "flight_number": ["flightNo"],
            "departure_local": ["departure", "local"],
            "total_fare":    ["fare", "totalAmount"],
            "base_fare":     ["fare", "baseAmount"],
            "udf":           ["fare", "taxes", "UDF"],
            "psf":           ["fare", "taxes", "PSF"],
            "asf":           ["fare", "taxes", "ASF"],
            "gst":           ["fare", "taxes", "GST"],
            "seats_remaining": ["seatsLeft"]
          },
          "sold_out_when": {"path": ["status"], "equals": "SOLD_OUT"}
        }
    """

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

    # -- extraction --------------------------------------------------------- #
    def extract(
        self, payload: dict[str, Any] | list[Any], task: CollectionTask
    ) -> list[ExtractedFare]:
        """Walk the configured paths.  A missing path yields a missing key.

        This is the load-bearing subtlety of the whole platform: when a mapped
        path is absent we *omit* the key.  We never write ``0``.  Downstream,
        an absent key becomes SQL ``NULL`` and a ``PARTIAL_BREAKDOWN`` flag.
        """
        if not isinstance(payload, dict):
            raise ParseError("expected an object payload", source_code=self.source_code)

        rows: list[Any] = []
        for capture in payload.get("captured", []):
            body = capture.get("body")
            rows.extend(self._dig(body, self._results_path) or [])

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
                # Strip any price the page may have shown next to a sold-out
                # cabin (frequently a stale "from" price).
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
        """Follow a dotted path, returning ``None`` the moment it breaks."""
        current = node
        for segment in path:
            if isinstance(current, dict):
                if segment not in current:
                    return None
                current = current[segment]
            elif isinstance(current, list) and segment.isdigit():
                index = int(segment)
                if index >= len(current):
                    return None
                current = current[index]
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
