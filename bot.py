from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import sqlite3
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

# ==========================================
# Sil'mir Telegram bot in one file.
# Put this file, .env, requirements.txt and Сильмир.zip in the same place.
# ==========================================

APP_DIR = Path(__file__).resolve().parent
DEFAULT_ZIP_NAME = "Сильмир.zip"
DEFAULT_INDEX_NAME = "silmir.sqlite3"
MAX_PHRASE_WORDS = 7

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("silmir-bot")

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёæøəśź'’]+|\d+|[^\w\s]", re.UNICODE)
RUSSIAN_RE = re.compile(r"[А-Яа-яЁё]")
SPLIT_RE = re.compile(r"\s+-\s+", re.UNICODE)

GRAMMAR_MARKERS = (
    " как прямой объект",
    " как принадлежность",
    " как действующее лицо",
    " как эргатив",
    " как инструмент",
    " как сравнение",
    " при сравнении",
    " в состояние",
    " внутри",
    " внутрь",
    " изнутри",
)

LOW_PRIORITY_STARTS = (
    "два ",
    "много ",
    "относящийся к ",
    "человек, связанный с ",
    "человек связанный с ",
    "действие или изделие из ",
    "собрание или масса ",
    "маленький ",
    "большой ",
    "точка у ",
    "со стороны ",
    "из-за ",
    "ради ",
    "во время ",
    "к ",
    "у ",
    "от ",
    "с помощью ",
)

NEGATION_WORDS = {"не", "нет"}
YES_NO_QUESTION_WORDS = {"ли", "разве"}
CASE_OBJECT_SUFFIX = "ən"

TRANSITIVE_VERBS_RU = {
    "вижу", "видишь", "видит", "видят", "увидел", "увидела", "увидели", "увижу", "увидишь", "увидит",
    "даю", "даёт", "дает", "дают", "дал", "дала", "дали", "дам", "дашь", "даст",
}

SUBJECT_WORDS = {"я", "ты", "он", "она", "оно", "мы", "вы", "они", "кто"}


@dataclass(frozen=True)
class Candidate:
    silmir: str
    ru_raw: str
    priority: int = 0


@dataclass(frozen=True)
class TranslationResult:
    translated: str
    unknown: tuple[str, ...]


CORE_TRANSLATIONS: dict[str, Candidate] = {}


def normalize_ru(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = text.lower().strip()
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"[\t\n\r]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .,!?:;()[]{}\"«»„“”")
    return text


def add_core(ru: str, silmir: str, priority: int = 10_000) -> None:
    CORE_TRANSLATIONS[normalize_ru(ru)] = Candidate(silmir=silmir, ru_raw=ru, priority=priority)


# Базовые слова и примеры из правил языка, чтобы бот работал даже без полного словаря.
for ru, silmir in {
    "я": "yaś",
    "ты": "tyś",
    "он": "sy",
    "она": "sy",
    "оно": "sy",
    "мы": "myś",
    "вы": "vyś",
    "они": "syn",
    "ли": "li",
    "кто": "lirəś",
    "что": "linən",
    "камень": "kair",
    "камень как прямой объект": "kairən",
    "два камня": "kairet",
    "камни": "kairin",
    "много камней": "kairin",
    "каменный": "kairyb",
    "дом": "dom",
    "два дома": "domet",
    "дома": "domin",
    "водопад": "hylsəlum",
    "ледяная река": "ryvəpæl",
    "сводный брат": "svodəbrat",
    "вода": "lum",
    "воду": "lumən",
    "вода как прямой объект": "lumən",
    "король": "kral",
    "король как действующее лицо": "kraləś",
    "твёрдый": "hard",
    "твердый": "hard",
    "твёрже": "hardyv",
    "тверже": "hardyv",
    "самый твёрдый": "hardyvm",
    "самый твердый": "hardyvm",
    "нетвёрдый": "nahard",
    "нетвердый": "nahard",
    "не твёрдый": "nahard",
    "не твердый": "nahard",
    "никто": "najem",
    "ничто": "najem",
    "я вижу": "yaś velims",
    "вижу": "velims",
    "видишь": "veliśs",
    "видит": "velis",
    "видят": "velins",
    "смотреть": "vevel",
    "наблюдать": "vevel",
    "даю": "dariml",
    "даёт": "daris",
    "дает": "daris",
    "дать": "dar",
    "давать": "dadar",
    "раздавать": "dadar",
    "идёт": "tekis",
    "идет": "tekis",
    "идти": "tek",
    "ходить": "tetek",
}.items():
    add_core(ru, silmir)


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall(text)


def is_word(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-zА-Яа-яЁёæøəśź'’]+|\d+", token, re.UNICODE))


def has_russian(text: str) -> bool:
    return bool(RUSSIAN_RE.search(text))


def join_tokens(tokens: list[str]) -> str:
    if not tokens:
        return ""

    no_space_before = set(".,!?;:%)]}»")
    no_space_after = set("([{«")
    result = ""
    prev = ""

    for token in tokens:
        if not result:
            result = token
        elif token in no_space_before:
            result += token
        elif prev in no_space_after:
            result += token
        else:
            result += " " + token
        prev = token

    return result


def find_dictionary_zip() -> Path:
    load_dotenv(APP_DIR / ".env")
    raw = os.getenv("SILMIR_ZIP", DEFAULT_ZIP_NAME).strip() or DEFAULT_ZIP_NAME
    path = Path(raw)
    if not path.is_absolute():
        path = APP_DIR / path
    if path.exists():
        return path

    # Fallback: берём первый zip рядом с bot.py, кроме архивов проекта.
    for item in APP_DIR.glob("*.zip"):
        if item.name.lower().startswith(("silmir_bot", "project", "bot_project")):
            continue
        return item

    raise FileNotFoundError(
        f"Не нашёл словарь. Положи {DEFAULT_ZIP_NAME} рядом с bot.py "
        "или укажи путь SILMIR_ZIP в .env."
    )


class SilmirIndex:
    def __init__(self, index_path: Path):
        self.index_path = index_path
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = sqlite3.connect(self.index_path)
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def has_index(self) -> bool:
        if not self.index_path.exists():
            return False
        conn = self.connect()
        try:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
            if not row:
                return False
            count = conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]
            return count > 0
        except sqlite3.Error:
            return False

    def lookup(self, ru_text: str) -> Candidate | None:
        key = normalize_ru(ru_text)
        if not key:
            return None

        core = CORE_TRANSLATIONS.get(key)
        if core:
            return core

        conn = self.connect()
        row = conn.execute(
            """
            SELECT silmir, ru_raw, priority
            FROM entries
            WHERE ru_norm = ?
            ORDER BY priority DESC, LENGTH(silmir) ASC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if not row:
            return None
        return Candidate(silmir=row["silmir"], ru_raw=row["ru_raw"], priority=row["priority"])

    def count(self) -> int:
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()
        return int(row["c"])

    def build_from_zip(self, zip_path: Path) -> int:
        conn = self.connect()
        conn.execute("DROP TABLE IF EXISTS entries")
        conn.execute(
            """
            CREATE TABLE entries (
                ru_norm TEXT NOT NULL,
                silmir TEXT NOT NULL,
                ru_raw TEXT NOT NULL,
                priority INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX idx_entries_ru_norm ON entries(ru_norm)")

        total = 0
        batch: list[tuple[str, str, str, int]] = []
        with zipfile.ZipFile(zip_path) as zf, conn:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".txt"):
                    continue
                logger.debug("Читаю словарь: %s", info.filename)
                with zf.open(info, "r") as raw:
                    stream = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
                    for line in stream:
                        parsed = parse_dictionary_line(line)
                        if not parsed:
                            continue
                        silmir, meanings = parsed
                        for ru_raw in meanings:
                            ru_norm = normalize_ru(ru_raw)
                            if not ru_norm:
                                continue
                            batch.append((ru_norm, silmir, ru_raw, priority_for(ru_norm)))
                            if len(batch) >= 10_000:
                                total += insert_batch(conn, batch)
                                batch.clear()
            if batch:
                total += insert_batch(conn, batch)
                batch.clear()

        # Убираем полные дубли и оставляем лучший вариант для каждого русского ключа.
        with conn:
            conn.execute(
                """
                CREATE TABLE best_entries AS
                SELECT ru_norm, silmir, ru_raw, priority
                FROM entries e
                WHERE rowid = (
                    SELECT rowid FROM entries x
                    WHERE x.ru_norm = e.ru_norm
                    ORDER BY x.priority DESC, LENGTH(x.silmir) ASC, x.rowid ASC
                    LIMIT 1
                )
                """
            )
            conn.execute("DROP TABLE entries")
            conn.execute("ALTER TABLE best_entries RENAME TO entries")
            conn.execute("CREATE INDEX idx_entries_ru_norm ON entries(ru_norm)")

        return self.count()


def insert_batch(conn: sqlite3.Connection, batch: list[tuple[str, str, str, int]]) -> int:
    conn.executemany(
        "INSERT INTO entries (ru_norm, silmir, ru_raw, priority) VALUES (?, ?, ?, ?)",
        batch,
    )
    return len(batch)


def parse_dictionary_line(line: str) -> tuple[str, list[str]] | None:
    line = line.strip()
    if not line or line.startswith("#") or " - " not in line:
        return None
    parts = SPLIT_RE.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    silmir = parts[0].strip()
    ru = parts[1].strip()
    if not silmir or not ru:
        return None

    meanings = [x.strip() for x in re.split(r"[,;]", ru) if x.strip()]
    if ru not in meanings:
        meanings.insert(0, ru)
    return silmir, meanings


def priority_for(ru_norm: str) -> int:
    priority = 1000
    if any(marker in ru_norm for marker in GRAMMAR_MARKERS):
        priority -= 100
    if any(ru_norm.startswith(prefix) for prefix in LOW_PRIORITY_STARTS):
        priority -= 50
    if len(ru_norm.split()) <= 2:
        priority += 10
    return priority


def ensure_index(rebuild: bool = False) -> SilmirIndex:
    index_path = Path(os.getenv("SILMIR_INDEX", DEFAULT_INDEX_NAME))
    if not index_path.is_absolute():
        index_path = APP_DIR / index_path
    index = SilmirIndex(index_path)

    if rebuild or not index.has_index():
        zip_path = find_dictionary_zip()
        logger.info("Строю индекс из %s", zip_path.name)
        count = index.build_from_zip(zip_path)
        logger.info("Индекс готов: %s записей", count)
    return index


class SilmirTranslator:
    def __init__(self, index: SilmirIndex, max_phrase_words: int = MAX_PHRASE_WORDS):
        self.index = index
        self.max_phrase_words = max_phrase_words

    def translate(self, text: str) -> TranslationResult:
        tokens = tokenize(text)
        result: list[str] = []
        unknown: list[str] = []
        i = 0
        object_pending = False

        while i < len(tokens):
            token = tokens[i]

            if not is_word(token):
                result.append(token)
                i += 1
                continue

            norm = normalize_ru(token)

            # Отрицание перед глаголом: na пишется отдельно.
            if norm in NEGATION_WORDS:
                negated = self._try_negated_word(tokens, i)
                if negated:
                    result.append(negated)
                    i += 2
                    object_pending = False
                    continue
                result.append("na")
                i += 1
                continue

            match = self._find_longest(tokens, i)
            if match:
                phrase_norm, phrase_len, silmir = match
                if object_pending and phrase_norm not in SUBJECT_WORDS and phrase_norm not in TRANSITIVE_VERBS_RU:
                    silmir = self._as_direct_object(phrase_norm, silmir)
                    object_pending = False
                result.append(silmir)
                object_pending = self._is_transitive(phrase_norm)
                i += phrase_len
                continue

            # Неизвестные русские слова не выдумываем: показываем, что их надо добавить в словарь.
            if has_russian(token):
                result.append(f"[{token}]")
                if token not in unknown:
                    unknown.append(token)
                object_pending = False
            else:
                # Латиница, числа, emoji и прочие символы остаются как есть.
                result.append(token)
            i += 1

        return TranslationResult(join_tokens(result), tuple(unknown))

    def _find_longest(self, tokens: list[str], start: int) -> tuple[str, int, str] | None:
        max_len = min(self.max_phrase_words, len(tokens) - start)
        for length in range(max_len, 0, -1):
            chunk = tokens[start : start + length]
            if not all(is_word(x) for x in chunk):
                continue
            phrase = " ".join(chunk)
            phrase_norm = normalize_ru(phrase)
            candidate = self.index.lookup(phrase_norm)
            if candidate:
                return phrase_norm, length, candidate.silmir
        return None

    def _try_negated_word(self, tokens: list[str], start: int) -> str | None:
        if start + 1 >= len(tokens) or not is_word(tokens[start + 1]):
            return None
        next_norm = normalize_ru(tokens[start + 1])

        # Если это глагол, отрицание должно быть отдельной частицей: na velims.
        next_candidate = self.index.lookup(next_norm)
        if next_norm in TRANSITIVE_VERBS_RU or (next_candidate and looks_like_verb(next_candidate.silmir)):
            return None

        # Если это обычное слово, пробуем найти слитную форму: nahard, najem и т.д.
        for key in (f"не {next_norm}", f"отсутствие {next_norm}"):
            candidate = self.index.lookup(key)
            if candidate:
                return candidate.silmir
        return None

    def _as_direct_object(self, phrase_norm: str, silmir_base: str) -> str:
        candidate = self.index.lookup(f"{phrase_norm} как прямой объект")
        if candidate:
            return candidate.silmir
        if silmir_base.endswith(CASE_OBJECT_SUFFIX):
            return silmir_base
        return f"{silmir_base}{CASE_OBJECT_SUFFIX}"

    def _is_transitive(self, phrase_norm: str) -> bool:
        words = phrase_norm.split()
        return bool(words and words[-1] in TRANSITIVE_VERBS_RU)


def looks_like_verb(silmir: str) -> bool:
    # Грубый признак по готовым примерам: velims, velis, daris, tekis.
    return silmir.endswith(("ims", "iśs", "ins", "is", "iml"))


dp = Dispatcher()
translator: SilmirTranslator | None = None
index_global: SilmirIndex | None = None
bot_username_global: str | None = None


def get_translator(rebuild: bool = False) -> SilmirTranslator:
    global translator, index_global
    if translator is None or rebuild:
        index_global = ensure_index(rebuild=rebuild)
        translator = SilmirTranslator(index_global)
    return translator


def normalize_bot_username(username: str | None) -> str:
    return (username or "").lstrip("@").lower().strip()


def is_group_message(message: Message) -> bool:
    return message.chat.type in {"group", "supergroup"}


def text_mentions_bot(text: str, bot_username: str | None) -> bool:
    username = normalize_bot_username(bot_username)
    if not username:
        return False
    return bool(re.search(rf"(?i)(^|\s)@{re.escape(username)}\b", text or ""))


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    username = normalize_bot_username(bot_username)
    cleaned = text or ""
    if username:
        cleaned = re.sub(rf"(?i)(^|\s)@{re.escape(username)}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^[\s:,.!\-—–]+", "", cleaned).strip()
    cleaned = re.sub(r"[\s:,.!\-—–]+$", "", cleaned).strip()
    return cleaned


def is_reply_to_this_bot(message: Message, bot_username: str | None) -> bool:
    reply = message.reply_to_message
    if not reply or not reply.from_user:
        return False
    username = normalize_bot_username(bot_username)
    reply_username = normalize_bot_username(reply.from_user.username)
    return bool(reply.from_user.is_bot and username and reply_username == username)


def extract_text_for_translation(message: Message, bot_username: str | None) -> str | None:
    """Private chats: translate any text. Groups: translate only @mention or reply to bot."""
    text = (message.text or "").strip()
    if not text:
        return None

    if not is_group_message(message):
        return text

    if text_mentions_bot(text, bot_username):
        return strip_bot_mention(text, bot_username)

    if is_reply_to_this_bot(message, bot_username):
        return text

    # В группах не трогаем обычные сообщения, чтобы бот не спамил.
    return None


async def send_long(message: Message, text: str) -> None:
    limit = 3900
    for start in range(0, len(text), limit):
        await message.answer(text[start : start + limit], parse_mode="HTML")


def format_result(result: TranslationResult) -> str:
    text = f"<b>Sil'mir:</b>\n<code>{html.escape(result.translated)}</code>"
    if result.unknown:
        shown = ", ".join(html.escape(x) for x in result.unknown[:20])
        extra = "" if len(result.unknown) <= 20 else f" и ещё {len(result.unknown) - 20}"
        text += f"\n\nНе нашёл в словаре: {shown}{extra}."
    return text


@dp.message(Command("start"))
async def start(message: Message) -> None:
    logger.info("INCOMING /start from id=%s username=%s text=%r", message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, message.text)
    await message.answer(
        "Привет. Напиши русский текст, и я переведу его на Sil'mir.\n\n"
        "Команды:\n"
        "/translate текст — перевести текст\n"
        "/status — проверить словарь\n"
        "/reload — пересобрать индекс словаря\n\n"
        "В группе напиши: @имя_бота я вижу камень. "
        "Обычные сообщения в группах я игнорирую."
    )


@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    logger.info("INCOMING /help from id=%s username=%s text=%r", message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, message.text)
    await start(message)


@dp.message(Command("ping"))
async def ping(message: Message) -> None:
    logger.info("INCOMING /ping from id=%s username=%s text=%r", message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, message.text)
    await message.answer("pong ✅ Бот получает сообщения.")


@dp.message(Command("status"))
async def status(message: Message) -> None:
    logger.info("INCOMING /status from id=%s username=%s text=%r", message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, message.text)
    try:
        zip_path = find_dictionary_zip()
        index_path = Path(os.getenv("SILMIR_INDEX", DEFAULT_INDEX_NAME))
        if not index_path.is_absolute():
            index_path = APP_DIR / index_path
        idx = SilmirIndex(index_path)
        if idx.has_index():
            await message.answer(f"Словарь: {html.escape(zip_path.name)}\nИндекс готов. Записей: {idx.count()}")
        else:
            await message.answer(
                f"Словарь найден: {html.escape(zip_path.name)}\n"
                "Индекс ещё не построен. Отправь текст для перевода или команду /reload."
            )
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Проблема со словарём: {html.escape(str(exc))}")


@dp.message(Command("reload"))
async def reload_dictionary(message: Message) -> None:
    logger.info("INCOMING /reload from id=%s username=%s text=%r", message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, message.text)
    await message.answer("Пересобираю индекс словаря...")
    try:
        get_translator(rebuild=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Dictionary reload failed")
        await message.answer(f"Не получилось пересобрать индекс: {html.escape(str(exc))}")
        return
    await message.answer("Индекс словаря пересобран.")


@dp.message(Command("translate"))
async def translate_command(message: Message) -> None:
    logger.info("INCOMING /translate from id=%s username=%s text=%r", message.from_user.id if message.from_user else None, message.from_user.username if message.from_user else None, message.text)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напиши так: /translate я не вижу камень")
        return
    await translate_text(message, parts[1].strip())


@dp.message(F.text)
async def any_text(message: Message) -> None:
    logger.info(
        "INCOMING text chat_type=%s from id=%s username=%s text=%r",
        message.chat.type,
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
        message.text,
    )
    text = extract_text_for_translation(message, bot_username_global)
    if text is None:
        return
    if not text:
        await message.answer("Напиши запрос после упоминания, например: @имя_бота я вижу камень")
        return
    if not has_russian(text):
        await message.answer("Пока я перевожу только с русского на Sil'mir.")
        return
    await translate_text(message, text)


async def translate_text(message: Message, text: str) -> None:
    try:
        # Если индекс ещё не готов, честно предупреждаем: первый запуск может занять время.
        index_path = Path(os.getenv("SILMIR_INDEX", DEFAULT_INDEX_NAME))
        if not index_path.is_absolute():
            index_path = APP_DIR / index_path
        if not SilmirIndex(index_path).has_index():
            await message.answer("Первый запуск: собираю словарь. Это может занять 1–3 минуты, потом будет быстро.")
        result = get_translator().translate(text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Translation failed")
        await message.answer(f"Ошибка перевода: {html.escape(str(exc))}")
        return
    await send_long(message, format_result(result))


async def main() -> None:
    global bot_username_global

    load_dotenv(APP_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Нет BOT_TOKEN. Создай .env рядом с bot.py и вставь токен от @BotFather.")

    # Важно: не строим словарь до запуска polling.
    # Иначе на хостинге бот долго показывает только "Читаю словарь" и не отвечает даже на /start.
    # Индекс строится лениво при первом переводе или через /reload.
    if os.getenv("SILMIR_REBUILD_INDEX", "0").lower() in {"1", "true", "yes", "да"}:
        logger.info("SILMIR_REBUILD_INDEX включён: индекс будет пересобран при первом переводе или /reload")

    bot = Bot(token=token)
    me = await bot.get_me()
    bot_username_global = me.username
    logger.info("Бот запущен: @%s, id=%s, name=%s", me.username, me.id, me.full_name)
    # На случай, если ранее был включён webhook: polling и webhook вместе не работают.
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Жду сообщения. Проверь /ping в Telegram.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def cli() -> None:
    load_dotenv(APP_DIR / ".env")
    if "--build-index" in sys.argv:
        idx = ensure_index(rebuild=True)
        print(f"Индекс готов: {idx.count()} записей")
        return
    if "--test" in sys.argv:
        tr = SilmirTranslator(ensure_index(rebuild=False))
        sample = "я не вижу камень 😊"
        result = tr.translate(sample)
        print(sample)
        print(result.translated)
        if result.unknown:
            print("Не найдено:", ", ".join(result.unknown))
        return
    asyncio.run(main())


if __name__ == "__main__":
    cli()
