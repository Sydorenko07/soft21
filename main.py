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


async def extract_offer_amount_and_currency(element: Locator, settings: Settings) -> tuple[Decimal, str]:
    """Read the UAH amount even when Paychain changes inner-cell markup."""
    try:
        cell_texts = await element.evaluate(
            "el => Array.from(el.querySelectorAll('td')).map(td => td.innerText || td.textContent || '')"
        )
    except Exception:
        cell_texts = await element.locator("td").all_text_contents()

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


async def find_offer_elements(page: Page, settings: Settings, timeout_ms: int = 8_000) -> list[Locator]:
    """Wait for Paychain's asynchronously rendered offer rows."""
    selectors = tuple(dict.fromkeys((
        settings.offer_selector,
        "tbody tr",
        "tr[role='row']:has(td:has-text('UAH'))",
        "tr:has(td:has-text('UAH'))",
        "tr[role='row']",
    )))
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for selector in selectors:
            elements = await page.locator(selector).all()
            if elements:
                return elements
        # Stop early when Paychain explicitly renders the empty-table marker.
        if await page.locator("app-empty-table:visible").count():
            return []
        if time.monotonic() >= deadline:
            return []
        await page.wait_for_timeout(250)


async def verify_amount_twice(page: Page, offer: Offer, settings: Settings) -> Decimal | None:
    try:
        await page.wait_for_timeout(100)
        second_amount, _ = await extract_offer_amount_and_currency(offer.element, settings)
    except (PlaywrightTimeoutError, ValueError):
        return None
    if second_amount != offer.amount:
        return None
    return second_amount


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

            await page.reload(timeout=10_000)
            await page.wait_for_load_state("load")

            # Paychain renders the table asynchronously after reload. Poll
            # for rows instead of reading the DOM only once.
            offer_elements = await find_offer_elements(page, settings)
            activity_log.info("Вікно %d | СКАНУВАННЯ | знайдено рядків: %d", window_id, len(offer_elements))
            if not offer_elements:
                row_count = await page.locator("tr").count()
                tbody_count = await page.locator("tbody tr").count()
                role_row_count = await page.locator("[role='row']").count()
                activity_log.info(
                    "Вікно %d | СТОРІНКА | url=%s | title=%s | tr=%d | tbody_tr=%d | role_row=%d",
                    window_id, page.url, await page.title(), row_count, tbody_count, role_row_count,
                )
            current_offers = []

            for element in offer_elements:
                try:
                    if settings.offer_id_attribute:
                        offer_id = await element.get_attribute(settings.offer_id_attribute)
                    else:
                        text = await element.inner_text()
                        offer_id = sha256(text.encode()).hexdigest()

                    amount, currency = await extract_offer_amount_and_currency(element, settings)

                    if settings.status_selector:
                        status = (await text_in(element, settings.status_selector)).strip().casefold()
                    else:
                        status = "active"

                    current_offers.append(Offer(
                        offer_id=offer_id,
                        amount=amount,
                        currency=currency,
                        status=status,
                        element=element
                    ))
                except Exception as error:
                    activity_log.info(
                        "Вікно %d | РЯДОК НЕ РОЗІБРАНО | %s | %s",
                        window_id, type(error).__name__, str(error)[:120],
                    )
                    continue

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

        # Створюємо окремі сторінки для кожного вікна
        tasks = []
        for i in range(windows):
            page = await context.new_page()
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
