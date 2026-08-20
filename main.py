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


async def page_wait(milliseconds: int) -> None:
    """Small cancellable delay used while Angular replaces table nodes."""
    await asyncio.sleep(milliseconds / 1000)


async def select_thirty_rows(page: Page) -> None:
    """Set Paychain's paginator to show 30 offers when the control is present."""
    dropdown = page.locator("p-select.p-paginator-rpp-dropdown").first
    try:
        await dropdown.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        return
    label = dropdown.locator("[role='combobox']").first
    if (await label.inner_text()).strip() == "30":
        return
    await dropdown.locator("[role='button'][aria-label='dropdown trigger']").click(timeout=5_000)
    option = page.locator("[role='option']").filter(has_text=re.compile(r"^\s*30\s*$")).last
    await option.click(timeout=5_000)
    await page.wait_for_timeout(300)


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


async def verify_amount_twice(page: Page, offer: Offer, settings: Settings) -> Decimal | None:
    """Read the same row a second time before clicking Accept."""
    try:
        await page.wait_for_timeout(80)
        cells = await offer.element.locator("td").all_text_contents()
        second_amount, _ = parse_offer_cells(cells)
        return second_amount if second_amount == offer.amount else None
    except (PlaywrightTimeoutError, ValueError):
        return None


async def accept_offer_with_double_click(page: Page, offer: Offer, settings: Settings) -> bool:
    """
    Подвійний клік по кнопці з інтервалом 50 мс.
    Після кліків перевіряє, чи зник офер або змінився статус.
    """
    try:
        if settings.action_menu_button_selector:
            await offer.element.locator(settings.action_menu_button_selector).first.click(timeout=5_000)

        accept_button = offer.element.locator(settings.accept_button_selector).first

        # Перший клік
        await accept_button.click(timeout=5_000)

        # Пауза 50 мс – завжди, незалежно від відповіді
        await page.wait_for_timeout(50)

        # Другий клік (ігноруємо помилки, якщо кнопка зникла)
        try:
            await accept_button.click(timeout=2_000)
        except Exception:
            pass

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


async def run_instance(settings: Settings, auto_accept: bool, start_signal: Path | None,
                        page: Page, window_id: int) -> None:
    """Один екземпляр моніторингу на окремій сторінці."""
    processed = await load_processed()
    reported = set()
    seen_in_dry_run = set()

    await page.goto(settings.offers_url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1_000)
    try:
        await select_thirty_rows(page)
    except PlaywrightTimeoutError:
        logging.warning("Не вдалося вибрати 30 оферів на сторінці")

    if start_signal:
        while not start_signal.exists():
            await asyncio.sleep(0.5)

    while True:
        iteration_start = time.monotonic()

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

            await page.reload(wait_until="domcontentloaded", timeout=30_000)
            # Paychain renders rows asynchronously after DOMContentLoaded.
            # The original monitor waited briefly, then scanned each row live.
            await page.wait_for_timeout(800)

            current_offers = await scan_offers(page, settings)
            activity_log.info("Вікно %d | СКАНУВАННЯ | знайдено рядків: %d", window_id, len(current_offers))
            if not current_offers:
                row_count = await page.locator("tr").count()
                tbody_count = await page.locator("tbody tr").count()
                role_row_count = await page.locator("[role='row']").count()
                activity_log.info(
                    "Вікно %d | СТОРІНКА | url=%s | title=%s | tr=%d | tbody_tr=%d | role_row=%d",
                    window_id, page.url, await page.title(), row_count, tbody_count, role_row_count,
                )
            for offer in current_offers:
                # Перевіряємо спільний кеш (з замком)
                async with _processed_lock:
                    if offer.offer_id in _processed_cache:
                        continue

                # Подвійна перевірка суми
                verified_amount = await verify_amount_twice(page, offer, settings)
                if verified_amount is None:
                    activity_log.info("Вікно %d | ПРОПУЩЕНО | оффер %s | сума змінилася", window_id, offer.offer_id[:8])
                    continue

                offer = replace(offer, amount=verified_amount)
                reasons: list[str] = []

                if offer.currency != "UAH":
                    reasons.append(f"валюта {offer.currency}")
                if offer.status not in settings.active_statuses:
                    reasons.append(f"статус {offer.status}")
                if offer.amount < settings.minimum_amount_uah:
                    reasons.append(f"сума {offer.amount} < порога {settings.minimum_amount_uah}")

                qualifies = (
                    offer.currency == "UAH"
                    and offer.status in settings.active_statuses
                    and offer.amount >= settings.minimum_amount_uah
                )

                if not qualifies:
                    if offer.offer_id not in reported:
                        logging.info("Вікно %d | Оффер %s: пропущено (%s).", window_id, offer.offer_id[:8], "; ".join(reasons))
                        activity_log.info("Вікно %d | ПРОПУЩЕНО | оффер %s | %s %s | %s", window_id, offer.offer_id[:8], offer.amount, offer.currency, "; ".join(reasons))
                        reported.add(offer.offer_id)
                    continue

                logging.warning("Вікно %d | НОВИЙ ОФФЕР: %s — %s %s (%s)", window_id, offer.offer_id[:8], offer.amount, offer.currency, offer.status)

                if auto_accept:
                    # Ще раз перевіряємо, чи не прийняв офер інший контекст (з замком)
                    async with _processed_lock:
                        if offer.offer_id in _processed_cache:
                            logging.info("Вікно %d | Офер %s вже прийнято іншим вікном, пропускаю.", window_id, offer.offer_id[:8])
                            continue

                    # Приймаємо
                    if await accept_offer_with_double_click(page, offer, settings):
                        await save_processed({offer.offer_id})
                        processed.add(offer.offer_id)
                else:
                    if offer.offer_id not in seen_in_dry_run:
                        logging.warning("Вікно %d | DRY-RUN: оффер не прийнято.", window_id)
                        activity_log.info("Вікно %d | ПІДХОДИТЬ, АЛЕ НЕ ПРИЙНЯТО | оффер %s | %s %s | тестовий режим", window_id, offer.offer_id[:8], offer.amount, offer.currency)
                        seen_in_dry_run.add(offer.offer_id)

        except PlaywrightTimeoutError:
            logging.warning("Вікно %d: таймаут", window_id)
        except Exception:
            logging.exception("Вікно %d: помилка циклу", window_id)

        elapsed = time.monotonic() - iteration_start
        sleep_time = max(0, settings.refresh_seconds - elapsed)
        await asyncio.sleep(sleep_time)


async def run(settings: Settings, auto_accept: bool, start_signal: Path | None,
              minimized: bool, windows: int) -> None:
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
                run_instance(settings, auto_accept, start_signal, page, i + 1)
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

        asyncio.run(run(settings, args.auto_accept, args.start_signal, args.minimized, args.windows))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logging.error("Конфігурація: %s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
