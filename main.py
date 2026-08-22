from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from hashlib import sha256
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

ROOT = Path(__file__).parent
PROFILE_DIR = ROOT / ".browser-profile"
PROCESSED_FILE = ROOT / "processed_offers.json"
LOG_DIR = ROOT / "logs"
activity_log = logging.getLogger("activity")

# Спільний кеш оброблених оферів та замок для безпечного запису
_processed_cache: set[str] = set()
_processed_lock = asyncio.Lock()


@dataclass(frozen=True)
class Settings:
    offers_url: str
    refresh_seconds: float
    minimum_amount_uah: Decimal
    offer_selector: str
    offer_id_attribute: str
    offer_key_selector: str
    amount_selector: str
    currency_selector: str
    status_selector: str
    action_menu_button_selector: str
    accept_button_selector: str
    active_statuses: tuple[str, ...]


@dataclass(frozen=True)
class Offer:
    offer_id: str
    amount: Decimal
    currency: str
    status: str
    element: Locator


@dataclass(frozen=True)
class ApiOffer:
    """Safe subset of an offer returned by Paychain's trading API."""
    offer_id: str
    amount: Decimal
    currency: str
    status: str


def load_settings(config_path: Path) -> Settings:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    required = (
        "offers_url", "refresh_seconds", "minimum_amount_uah", "offer_selector",
        "amount_selector", "currency_selector",
    )
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"В config.json відсутні поля: {', '.join(missing)}")
    return Settings(
        offers_url=str(raw["offers_url"]),
        refresh_seconds=float(raw["refresh_seconds"]),
        minimum_amount_uah=Decimal(str(raw["minimum_amount_uah"])),
        offer_selector=str(raw["offer_selector"]),
        offer_id_attribute=str(raw.get("offer_id_attribute", "")),
        offer_key_selector=str(raw.get("offer_key_selector", "")),
        amount_selector=str(raw["amount_selector"]),
        currency_selector=str(raw["currency_selector"]),
        status_selector=str(raw.get("status_selector", "")),
        action_menu_button_selector=str(raw.get("action_menu_button_selector", "")),
        accept_button_selector=str(raw.get("accept_button_selector", "")),
        active_statuses=tuple(str(x).casefold() for x in raw.get("active_statuses", ["active"])),
    )


def parse_amount(raw: str) -> Decimal:
    cleaned = raw.replace("\u00a0", " ").replace(" ", "")
    cleaned = re.sub(r"[^0-9,.-]", "", cleaned).replace(",", ".")
    if cleaned.count(".") > 1:
        raise ValueError(f"Не вдалося розібрати суму: {raw!r}")
    try:
        return Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError(f"Не вдалося розібрати суму: {raw!r}") from error


def normalize_currency(raw: str) -> str:
    match = re.search(r"\b(UAH|USD|EUR|USDT)\b", raw.upper())
    return match.group(1) if match else raw.strip().upper()


def extract_api_offers(payload: object) -> list[ApiOffer]:
    """Extract payout offers from the API response's ``data`` structure."""
    found: list[ApiOffer] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        if value.get("type") == "pay-out" and value.get("id") and value.get("fiatAmount") is not None:
            try:
                currency = normalize_currency(str(value.get("fiatCurrency", "")))
                if currency:
                    raw_status = str(value.get("status", "")).casefold()
                    # The API calls actionable payout rows ``pending`` while
                    # the UI exposes them with an Accept button.
                    status = "active" if raw_status == "pending" else raw_status
                    found.append(ApiOffer(
                        offer_id=str(value["id"]),
                        amount=parse_amount(str(value["fiatAmount"])),
                        currency=currency,
                        status=status,
                    ))
            except (InvalidOperation, ValueError):
                pass

        for nested in value.values():
            visit(nested)

    visit(payload)
    unique: dict[str, ApiOffer] = {offer.offer_id: offer for offer in found}
    return list(unique.values())


def merge_api_offers(api_offers: list[ApiOffer], dom_offers: list[Offer]) -> list[Offer]:
    """Apply API ID/amount/status to visible rows without dropping DOM rows.

    Paychain can emit several trading responses while Angular is replacing the
    table.  A stale response must not make a visible row disappear from the
    scan, so unmatched DOM rows are retained with their stable fallback ID.
    """
    remaining = list(dom_offers)
    merged: list[Offer] = []
    for api_offer in api_offers:
        match_index = next(
            (index for index, dom_offer in enumerate(remaining)
             if dom_offer.amount == api_offer.amount and dom_offer.currency == api_offer.currency),
            None,
        )
        if match_index is None:
            continue
        dom_offer = remaining.pop(match_index)
        merged.append(replace(
            dom_offer,
            offer_id=api_offer.offer_id,
            amount=api_offer.amount,
            currency=api_offer.currency,
            status=api_offer.status,
        ))
    # Keep rows that were visible but did not have a matching API item.  They
    # still have a valid row-scoped Accept button and can be checked by amount.
    merged.extend(remaining)
    return merged


async def load_processed() -> set[str]:
    """Завантажує список оброблених оферів у кеш."""
    global _processed_cache
    if _processed_cache:
        return _processed_cache.copy()
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        _processed_cache = set(data)
        return _processed_cache.copy()
    except (OSError, json.JSONDecodeError):
        return set()


async def save_processed(processed: set[str]) -> None:
    """Безпечно зберігає список оброблених оферів (із замком)."""
    async with _processed_lock:
        _processed_cache.update(processed)
        PROCESSED_FILE.write_text(json.dumps(sorted(_processed_cache), indent=2), encoding="utf-8")


async def text_in(locator: Locator, selector: str) -> str:
    target = locator.locator(selector).first
    # ``inner_text()`` otherwise waits Playwright's default 30 seconds when a
    # header/empty row does not contain the requested cell.
    if await target.count() == 0:
        raise ValueError(f"Селектор не знайдено: {selector}")
    return await target.inner_text(timeout=1_000)


def parse_offer_cells(cell_texts: list[str]) -> tuple[Decimal, str]:
    """Parse one payout row from a DOM snapshot, without live locators."""
    # In the Paychain payout table the third data cell is the fiat amount
    # (the column labelled ``К отправке``).  It remains stable even when the
    # generated classes and text selectors change.
    amount_cell_text = cell_texts[2] if len(cell_texts) > 2 else ""
    if amount_cell_text:
        # This cell is the fiat payout column on the Paychain pay-out page.
        # Parse it even if the UI omits the literal ``UAH`` in textContent.
        number = re.search(r"[0-9][0-9 \t.,]*", amount_cell_text)
        if number:
            return parse_amount(number.group(0)), "UAH"

    # Reading the individual cells is more reliable than a CSS text selector:
    # the visible cell may contain line breaks or Angular-generated wrappers.
    for cell_text in cell_texts:
        if re.search(r"\bUAH\b", cell_text, flags=re.IGNORECASE):
            number = re.search(r"[0-9][0-9 \t.,]*", cell_text)
            if number:
                return parse_amount(number.group(0)), "UAH"

    row_text = " ".join(cell_texts)
    # Current Paychain rows contain text such as ``UAH 1717.60``.  Prefer
    # this stable visible text before relying on a generated Angular selector.
    match = re.search(r"\bUAH\s+([0-9][0-9 \t.,]*)", row_text, flags=re.IGNORECASE)
    if match:
        return parse_amount(match.group(1)), "UAH"

    raise ValueError("У рядку не знайдено суму в третій клітинці")


async def extract_offer_amount_and_currency(
    element: Locator, settings: Settings, cell_texts: list[str] | None = None
) -> tuple[Decimal, str]:
    """Read the UAH amount while tolerating Angular row replacement."""
    if cell_texts is None:
        cell_texts = []
        # Angular can replace the row node while the table timer is updating.
        # Re-read the same locator briefly so a transient detached node is not
        # treated as a malformed offer.
        for _ in range(5):
            try:
                cell_texts = await element.evaluate(
                    "el => Array.from(el.querySelectorAll('td')).map(td => td.innerText || td.textContent || '')",
                    timeout=300,
                )
                if len(cell_texts) >= 3:
                    break
            except Exception:
                cell_texts = []
            await page_wait(50)
        if not cell_texts:
            try:
                cell_texts = await element.locator("td").all_text_contents()
            except Exception:
                cell_texts = []
    return parse_offer_cells(cell_texts)


async def _scan_card_locator(cards: Locator, settings: Settings) -> list[Offer]:
    """Read offers one row at a time, like the original working monitor.

    Paychain replaces individual Angular rows while the table is rendering.
    Reading a live row with ``count``/``nth`` is more reliable here than taking
    a page-wide DOM snapshot: each amount is parsed from that row's third
    ``td`` and the action button is checked in the same row.
    """
    count = await cards.count()
    offers: list[Offer] = []
    for index in range(count):
        card = cards.nth(index)
        try:
            cell_texts = await card.locator("td").all_text_contents()
            if len(cell_texts) < 3:
                continue

            # The Paychain payout table keeps the UAH amount in the third
            # cell.  Fall back to the configured selectors only for a row
            # whose generated markup differs from the normal table.
            try:
                amount, currency = parse_offer_cells(cell_texts)
            except ValueError:
                amount = None
                currency = ""
                selectors = [settings.amount_selector, "td:nth-child(3)", "td"]
                for selector in dict.fromkeys(value for value in selectors if value):
                    try:
                        raw_amount = await text_in(card, selector)
                        amount = parse_amount(raw_amount)
                        currency = normalize_currency(raw_amount) or "UAH"
                        break
                    except (PlaywrightTimeoutError, ValueError):
                        continue
                if amount is None:
                    raise ValueError("У рядку не знайдено суму")

            offer_id = None
            if settings.offer_id_attribute:
                offer_id = await card.get_attribute(settings.offer_id_attribute)
            if not offer_id and settings.offer_key_selector:
                key = await text_in(card, settings.offer_key_selector)
                offer_id = sha256(key.encode("utf-8")).hexdigest()
            if not offer_id:
                key = "\n".join(cell_texts).strip()
                offer_id = sha256(key.encode("utf-8")).hexdigest()

            if settings.status_selector:
                status = (await text_in(card, settings.status_selector)).strip().casefold()
            else:
                accept_count = 0
                if settings.accept_button_selector:
                    accept_count = await card.locator(settings.accept_button_selector).count()
                if not accept_count:
                    accept_count = await card.get_by_role(
                        "button", name=re.compile(r"Принять|Подтвердить|Accept", re.IGNORECASE)
                    ).count()
                status = "active" if accept_count else ""

            if status:
                offers.append(Offer(
                    offer_id=str(offer_id),
                    amount=amount,
                    currency=currency,
                    status=status,
                    element=card,
                ))
        except (PlaywrightTimeoutError, ValueError) as error:
            logging.warning("Оффер у рядку %d пропущено: %s", index + 1, error)
    return offers


async def scan_offers(page: Page, settings: Settings) -> list[Offer]:
    """Scan live Paychain rows using the original per-row method."""
    cards = page.locator(settings.offer_selector)
    offers = await _scan_card_locator(cards, settings)
    if offers:
        return offers

    # ``:has-text`` can briefly return no rows while Angular is replacing the
    # table.  The table itself is stable, so retry with its body rows.
    fallback = page.locator("tbody tr")
    if await fallback.count() and settings.offer_selector != "tbody tr":
        return await _scan_card_locator(fallback, settings)
    return offers


async def wait_for_offer_table(page: Page, timeout_ms: int = 8_000) -> None:
    """Wait for Angular to attach the payout table after a reload.

    ``domcontentloaded`` only means that the shell loaded.  Paychain then
    requests the offers and creates ``tbody tr`` asynchronously, so scanning
    immediately can legitimately see zero rows even while the browser is
    about to display them.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if await page.locator("tbody tr").count():
            return
        # An explicitly rendered empty state is also a completed table load.
        if await page.locator("app-empty-table:visible").count():
            return
        await page.wait_for_timeout(250)


async def wait_for_rendered_rows(page: Page, expected_count: int = 0, timeout_ms: int = 3_000) -> None:
    """Wait until Angular has rendered the offer rows, not just empty shells."""
    # A zero-item API response is already a completed empty result.  Do not
    # spend the full render timeout waiting for rows that cannot appear.
    if expected_count <= 0:
        await page.wait_for_timeout(100)
        return
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            rendered = await page.locator("tbody tr").evaluate_all(
                "rows => rows.filter(row => { const cells = Array.from(row.querySelectorAll('td')); "
                "return cells.length > 2 && /[0-9]/.test(cells[2].innerText || cells[2].textContent || ''); }).length"
            )
        except Exception:
            rendered = 0
        if rendered and (not expected_count or rendered >= expected_count):
            return
        await page.wait_for_timeout(100)


async def looks_authenticated(page: Page) -> bool:
    """Recognize the logged-in Paychain shell even when there are no offers."""
    if "/pay-out" not in page.url.lower():
        return False
    try:
        title = (await page.title()).casefold()
        if title and "paychain" not in title:
            return False
        # Login forms remain visible when the account is not authenticated.
        if await page.locator("input[type='password'], input[autocomplete='current-password']").count():
            return False
        return bool(await page.locator("app-main-layout, app-pay-out, app-header, tbody").count())
    except Exception:
        return False


async def page_wait(milliseconds: int) -> None:
    """Small cancellable delay used while Angular replaces table nodes."""
    await asyncio.sleep(milliseconds / 1000)


async def select_ten_rows(page: Page) -> None:
    """Keep ten rows per page so the next page is checked automatically."""
    dropdown = page.locator("p-select.p-paginator-rpp-dropdown").first
    try:
        await dropdown.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        return
    label = dropdown.locator("[role='combobox']").first
    if (await label.inner_text()).strip() == "10":
        return
    await dropdown.locator("[role='button'][aria-label='dropdown trigger']").click(timeout=5_000)
    option = page.locator("[role='option']").filter(has_text=re.compile(r"^\s*10\s*$")).last
    await option.click(timeout=5_000)
    await page.wait_for_timeout(300)


async def go_to_next_offer_page(page: Page) -> bool:
    """Move to the next paginator page, returning False on the last page."""
    paginator = page.locator("p-paginator").first
    buttons = paginator.locator("button") if await paginator.count() else page.locator("button")
    count = await buttons.count()
    for index in range(count):
        button = buttons.nth(index)
        label = ((await button.get_attribute("aria-label")) or "").casefold()
        classes = ((await button.get_attribute("class")) or "").casefold()
        if "next" not in label and "paginator-next" not in classes:
            continue
        disabled = await button.is_disabled() or (await button.get_attribute("aria-disabled")) == "true"
        if disabled:
            return False
        await button.click()
        await wait_for_offer_table(page)
        await page.wait_for_timeout(150)
        return True
    return False


async def go_to_first_offer_page(page: Page) -> None:
    """Return to page one after scanning the second page."""
    paginator = page.locator("p-paginator").first
    buttons = paginator.locator("button") if await paginator.count() else page.locator("button")
    count = await buttons.count()
    for index in range(count):
        button = buttons.nth(index)
        label = ((await button.get_attribute("aria-label")) or "").casefold()
        classes = ((await button.get_attribute("class")) or "").casefold()
        if "first" not in label and "paginator-first" not in classes:
            continue
        if await button.is_disabled() or (await button.get_attribute("aria-disabled")) == "true":
            return
        await button.click()
        await wait_for_offer_table(page)
        await page.wait_for_timeout(150)
        return


async def find_offer_elements(
    page: Page, settings: Settings, timeout_ms: int = 8_000
) -> tuple[list[Locator], list[list[str]]]:
    """Wait for Paychain's asynchronously rendered offer rows."""
    row_locators = [
        page.locator(settings.offer_selector),
        page.locator("tr").filter(has_text=re.compile(r"\bUAH\b", re.IGNORECASE)),
        page.locator("tbody tr"),
        page.locator("tr[role='row']"),
    ]
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for row_locator in row_locators:
            elements = await row_locator.all()
            if elements:
                try:
                    snapshots = await row_locator.evaluate_all(
                        "rows => rows.map(row => Array.from(row.querySelectorAll('td')).map(td => td.innerText || td.textContent || ''))"
                    )
                except Exception:
                    snapshots = []
                # Angular creates empty <tr> nodes before filling their cells.
                # Do not return those skeleton rows as real offers.
                if any(
                    len(snapshot) > 2 and re.search(r"[0-9]", snapshot[2])
                    for snapshot in snapshots
                ):
                    return elements, snapshots
                if time.monotonic() >= deadline:
                    return [], []
        # Stop early when Paychain explicitly renders the empty-table marker.
        if await page.locator("app-empty-table:visible").count():
            return [], []
        if time.monotonic() >= deadline:
            return [], []
        await page.wait_for_timeout(250)


async def accept_offer_with_double_click(page: Page, offer: Offer, settings: Settings) -> bool:
    """
    Подвійний клік по кнопці з інтервалом 50 мс.
    Після кліків перевіряє, чи зник офер або змінився статус.
    """
    try:
        if settings.action_menu_button_selector:
            await offer.element.locator(settings.action_menu_button_selector).first.click(timeout=5_000)

        accept_button = offer.element.locator(settings.accept_button_selector).first if settings.accept_button_selector else None
        if accept_button is None or await accept_button.count() == 0:
            # Keep the action row-scoped, but tolerate Paychain changing the
            # generated PrimeNG attributes around the visible button.
            accept_button = offer.element.get_by_role(
                "button", name=re.compile(r"Принять|Подтвердить|Accept", re.IGNORECASE)
            ).first
        if await accept_button.count() == 0:
            raise ValueError("Кнопка прийняття не знайдена в рядку")

        # Collect the POST result without waiting for it between the two
        # clicks.  This keeps the required 50 ms double-click while allowing
        # the agent to report a successful API response to Telegram.
        accept_statuses: list[int] = []

        def capture_accept_response(response) -> None:
            try:
                if response.request.method == "POST" and response.url.rstrip("/").endswith("/accept"):
                    accept_statuses.append(response.status)
            except Exception:
                pass

        page.on("response", capture_accept_response)

        try:
            # Перший клік
            await accept_button.click(timeout=5_000)

            # Пауза 50 мс – завжди, незалежно від відповіді
            await page.wait_for_timeout(50)

            # Другий клік (ігноруємо помилки, якщо кнопка вже зникла)
            try:
                await accept_button.click(timeout=2_000)
            except Exception:
                pass

            # Дати браузеру отримати відповідь, але не блокувати моніторинг.
            await page.wait_for_timeout(500)
        finally:
            page.remove_listener("response", capture_accept_response)

        if any(200 <= status < 300 for status in accept_statuses):
            activity_log.info("ПРИЙНЯТО | оффер %s | %s %s", offer.offer_id, offer.amount, offer.currency)
            return True

        # Невелика пауза для оновлення DOM
        await page.wait_for_timeout(300)

        # Перевіряємо, чи офер ще існує або чи змінився статус
        try:
            if settings.status_selector:
                new_status = (await text_in(offer.element, settings.status_selector)).strip().casefold()
                if new_status not in settings.active_statuses:
                    activity_log.info("ПРИЙНЯТО | оффер %s | %s %s", offer.offer_id, offer.amount, offer.currency)
                    return True
            else:
                # Якщо селектор статусу не задано, перевіряємо, чи зник елемент
                if await offer.element.count() == 0:
                    activity_log.info("ПРИЙНЯТО | оффер %s | %s %s", offer.offer_id, offer.amount, offer.currency)
                    return True
        except Exception:
            # Якщо елемент зник або сталася помилка – вважаємо, що прийнято
            if await offer.element.count() == 0:
                activity_log.info("ПРИЙНЯТО | оффер %s | %s %s", offer.offer_id, offer.amount, offer.currency)
                return True

        activity_log.warning("Офер %s залишився активним після подвійного кліку.", offer.offer_id[:8])
        return False

    except PlaywrightTimeoutError:
        logging.warning("Таймаут під час кліку на офер %s", offer.offer_id[:8])
        return False
    except Exception as e:
        logging.error("Помилка при прийнятті офера %s: %s", offer.offer_id[:8], e)
        return False


async def process_offer_batch(
    page: Page,
    offers: list[Offer],
    settings: Settings,
    auto_accept: bool,
    window_id: int,
    page_number: int,
    reported: set[str],
    seen_in_dry_run: set[str],
    processed: set[str],
) -> None:
    """Compare and optionally accept every offer on the currently visible page."""
    activity_log.info("Вікно %d | СТОРІНКА %d | СКАНУВАННЯ | знайдено рядків: %d", window_id, page_number, len(offers))
    for offer in offers:
        # Only skip an offer after it qualifies and was already accepted.  This
        # lets a changed threshold re-evaluate offers that were previously below
        # the threshold, while still preventing duplicate accepts.
        qualifies = (
            offer.currency == "UAH"
            and offer.status in settings.active_statuses
            and offer.amount >= settings.minimum_amount_uah
        )
        if not qualifies:
            reasons: list[str] = []
            if offer.currency != "UAH":
                reasons.append(f"валюта {offer.currency}")
            if offer.status not in settings.active_statuses:
                reasons.append(f"статус {offer.status}")
            if offer.amount < settings.minimum_amount_uah:
                reasons.append(f"сума {offer.amount} < порога {settings.minimum_amount_uah}")
            if offer.offer_id not in reported:
                activity_log.info(
                    "Вікно %d | ПРОПУЩЕНО | оффер %s | %s %s | %s",
                    window_id, offer.offer_id[:8], offer.amount, offer.currency, "; ".join(reasons),
                )
                reported.add(offer.offer_id)
            continue

        async with _processed_lock:
            if offer.offer_id in _processed_cache:
                continue
        logging.warning(
            "Вікно %d | НОВИЙ ОФФЕР: %s — %s %s (%s)",
            window_id, offer.offer_id[:8], offer.amount, offer.currency, offer.status,
        )
        if auto_accept:
            async with _processed_lock:
                if offer.offer_id in _processed_cache:
                    continue
            if await accept_offer_with_double_click(page, offer, settings):
                await save_processed({offer.offer_id})
                processed.add(offer.offer_id)
        elif offer.offer_id not in seen_in_dry_run:
            activity_log.info(
                "Вікно %d | ПІДХОДИТЬ, АЛЕ НЕ ПРИЙНЯТО | оффер %s | %s %s | тестовий режим",
                window_id, offer.offer_id[:8], offer.amount, offer.currency,
            )
            seen_in_dry_run.add(offer.offer_id)

async def run_instance(settings: Settings, auto_accept: bool, start_signal: Path | None,
                         login_signal: Path | None, page: Page, window_id: int) -> None:
    """Один екземпляр моніторингу на окремій сторінці."""
    processed = await load_processed()
    reported = set()
    seen_in_dry_run = set()
    network_offer_event = asyncio.Event()
    api_offers: list[ApiOffer] = []
    response_tasks: set[asyncio.Task[None]] = set()

    async def capture_offer_response(response) -> None:
        """Read the authenticated JSON response containing payout offers."""
        nonlocal api_offers
        try:
            request = response.request
            url = response.url.lower()
            if request.resource_type not in {"xhr", "fetch"}:
                return
            if "/user/trading" not in url:
                return
            if url.rstrip("/").endswith("/accept"):
                return
            if "type=pay-out" not in url and "/pay-out" not in url:
                return
            payload = await response.json()
            parsed = extract_api_offers(payload)
            api_offers = parsed
            activity_log.info("API | отримано оферів: %d", len(parsed))
            network_offer_event.set()
        except Exception:
            # Non-JSON responses and disposed responses are irrelevant here.
            pass

    def on_response(response) -> None:
        task = asyncio.create_task(capture_offer_response(response))
        response_tasks.add(task)
        task.add_done_callback(response_tasks.discard)

    page.on("response", on_response)

    await page.goto(settings.offers_url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1_000)
    # The login command keeps monitoring paused, but we still wait for the
    # authenticated payout table so the agent can tell Telegram that manual
    # login has completed.
    await wait_for_offer_table(page, timeout_ms=15_000)
    if login_signal and (
        await page.locator("tbody tr").count()
        or await page.locator("app-empty-table:visible").count()
        or await looks_authenticated(page)
    ):
        login_signal.parent.mkdir(parents=True, exist_ok=True)
        login_signal.write_text("ready", encoding="utf-8")
    try:
        await select_ten_rows(page)
    except PlaywrightTimeoutError:
        logging.warning("Не вдалося встановити 10 оферів на сторінці")

    if start_signal:
        while not start_signal.exists():
            await asyncio.sleep(0.5)

    monitoring_started = False
    while True:
        try:
            # The agent pauses monitoring by removing the signal file.  Keep
            # this browser context alive so the Paychain login session stays
            # available for the next Start command.
            if start_signal and not start_signal.exists():
                await asyncio.sleep(0.5)
                continue

            if page.is_closed():
                logging.warning("Вікно %d: сторінка закрита", window_id)
                break

            if monitoring_started:
                try:
                    # Network responses trigger an immediate scan.  If the
                    # page has no internal polling, the timeout keeps the old
                    # periodic reload as a reliable fallback.
                    await asyncio.wait_for(
                        network_offer_event.wait(),
                        timeout=max(1.0, settings.refresh_seconds),
                    )
                    network_offer_event.clear()
                    await page.wait_for_timeout(100)
                except asyncio.TimeoutError:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(800)
            else:
                # Scan the page that the user logged into; do not force an
                # extra reload on the first start command.
                monitoring_started = True
                network_offer_event.clear()

            await wait_for_offer_table(page, timeout_ms=3_000)

            # Scan pages 1 through 10.  Each page is processed while its DOM
            # locators are still valid, then we return to page 1.
            page_number = 1
            while page_number <= 10:
                if page_number > 1:
                    api_offers = []
                    network_offer_event.clear()
                    if not await go_to_next_offer_page(page):
                        break
                    try:
                        await asyncio.wait_for(network_offer_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                # API responses can arrive before Angular has painted every
                # row.  Wait for the expected number of rendered amount cells
                # before taking the snapshot used for threshold checks.
                await wait_for_rendered_rows(page, expected_count=len(api_offers), timeout_ms=3_000)
                dom_offers = await scan_offers(page, settings)
                # API data supplies authoritative IDs/status; DOM rows scope
                # the button click on the currently visible page.
                current_offers = merge_api_offers(api_offers, dom_offers) if api_offers else dom_offers
                if not current_offers:
                    row_count = await page.locator("tr").count()
                    tbody_count = await page.locator("tbody tr").count()
                    role_row_count = await page.locator("[role='row']").count()
                    activity_log.info(
                        "Вікно %d | СТОРІНКА %d | url=%s | title=%s | tr=%d | tbody_tr=%d | role_row=%d",
                        window_id, page_number, page.url, await page.title(), row_count, tbody_count, role_row_count,
                    )
                await process_offer_batch(
                    page, current_offers, settings, auto_accept, window_id, page_number,
                    reported, seen_in_dry_run, processed,
                )
                # With no API rows and no visible DOM rows there cannot be a
                # second page to inspect.  Avoid needless paginator waits.
                if page_number == 1 and not current_offers and not api_offers:
                    break
                page_number += 1
            await go_to_first_offer_page(page)

        except PlaywrightTimeoutError:
            logging.warning("Вікно %d: таймаут", window_id)
        except Exception:
            logging.exception("Вікно %d: помилка циклу", window_id)

        # The next iteration is released by a network response or by the
        # configured periodic reload timeout above.


async def run(settings: Settings, auto_accept: bool, start_signal: Path | None,
              login_signal: Path | None, minimized: bool, windows: int) -> None:
    """Запускає один браузер з кількома сторінками (вікнами)."""
    # Завантажуємо кеш оброблених оферів перед запуском
    await load_processed()

    async with async_playwright() as p:
        launch_args = ["--start-minimized"] if minimized else []
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, args=launch_args
        )

        # Використовуємо вже відкриту сторінку persistent-контексту для
        # першого вікна.  Саме так працювала стара версія: не створюється
        # зайва вкладка, а введений вручну Paychain-сеанс лишається тим самим.
        existing_pages = [page for page in context.pages if not page.is_closed()]
        tasks = []
        for i in range(windows):
            page = existing_pages[i] if i < len(existing_pages) else await context.new_page()
            task = asyncio.create_task(
                run_instance(settings, auto_accept, start_signal, login_signal, page, i + 1)
            )
            tasks.append(task)

        # Чекаємо завершення всіх задач (вони працюють вічно)
        await asyncio.gather(*tasks)


def configure_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")],
    )
    activity_log.setLevel(logging.INFO)
    activity_log.propagate = False
    activity_handler = logging.FileHandler(LOG_DIR / "activity.log", encoding="utf-8")
    activity_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    activity_log.addHandler(activity_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paychain offer monitor")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--auto-accept", action="store_true", help="Приймати оффери після успішного dry-run тесту")
    parser.add_argument("--minimum-amount", type=Decimal, help="Перевизначити мінімальну суму UAH")
    parser.add_argument("--refresh-seconds", type=float, help="Інтервал оновлення сторінки в секундах")
    parser.add_argument("--windows", type=int, default=1, help="Кількість одночасних вікон моніторингу (за замовчуванням 1)")
    parser.add_argument("--start-signal", type=Path, help="Файл-сигнал запуску для вікна керування")
    parser.add_argument("--login-signal", type=Path, help="Файл-сигнал успішного входу в Paychain")
    parser.add_argument("--minimized", action="store_true", help="Запустити браузер мінімізованим")
    args = parser.parse_args()

    configure_logging()
    try:
        settings = load_settings(args.config)
        if args.minimum_amount is not None:
            settings = replace(settings, minimum_amount_uah=args.minimum_amount)
        if args.refresh_seconds is not None:
            settings = replace(settings, refresh_seconds=args.refresh_seconds)
        if settings.minimum_amount_uah < 0:
            raise ValueError("minimum_amount не може бути від'ємним.")
        if settings.refresh_seconds < 1:
            raise ValueError("refresh_seconds не може бути меншим за 1 секунду.")
        if args.windows < 1:
            raise ValueError("windows має бути >= 1")

        asyncio.run(run(settings, args.auto_accept, args.start_signal, args.login_signal, args.minimized, args.windows))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logging.error("Конфігурація: %s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
