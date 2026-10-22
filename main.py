# -*- coding: utf-8 -*-
"""
Луна — ИИ-компаньонка, живущая прямо в твоём Telegram-аккаунте (userbot на Telethon).

• В «Избранном» отвечает на любые твои сообщения.
• В любом чате отвечает, если её позвали по имени (ты или кто угодно) или ответили на её сообщение.
• Режим автоответа: сама отвечает людям в личке, пока тебя нет.
• Имя, эмодзи, характер — меняются командами (.name, .emoji, .persona ...).
• Память (имя, настройки, факты) хранится в твоём приватном канале «Luna • память»,
  поэтому переживает перезапуски Render.

Команды пиши в «Избранное» (или в любом чате — команда удалится, ответ придёт в «Избранное»).
.help — список команд.
"""
import asyncio
import base64
import html
import io
import ipaddress
import json
import logging
import os
import re
import signal
import struct
import time
import uuid
from collections import defaultdict

import aiohttp
from aiohttp import web
from telethon import TelegramClient, events, functions, types
from telethon.errors import (AuthKeyDuplicatedError, FloodWaitError,
                             PhoneCodeInvalidError, SessionPasswordNeededError)
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("luna")

# ───────────────────────────── настройки из переменных окружения ─────────────────────────────
def env(name, default=""):
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default

API_ID = int(env("API_ID", "0") or 0)
API_HASH = env("API_HASH")
SESSION_STRING = (env("SESSION_STRING") or env("SESSION") or env("STRING_SESSION")
                  or env("TG_SESSION") or env("SESSION_STR"))
ADMIN_TOKEN = env("ADMIN_TOKEN")
PORT = int(env("PORT", "10000"))
PUBLIC_URL = env("RENDER_EXTERNAL_URL") or env("PUBLIC_URL")
STARTUP_DELAY = int(env("STARTUP_DELAY", "25"))
DEFAULT_NAME = env("LUNA_NAME", "Луна")
STORE_TITLE = env("STORE_TITLE", "Luna • память")

GEMINI_KEYS = [k.strip() for k in env("GEMINI_API_KEY").split(",") if k.strip()]
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-flash-latest")
GROQ_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")
OPENROUTER_KEY = env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = env("OPENROUTER_MODEL", "openrouter/free")
CUSTOM_URL = env("AI_BASE_URL")
CUSTOM_KEY = env("AI_API_KEY")
CUSTOM_MODEL = env("AI_MODEL")
AI_ORDER = env("AI_ORDER", "phone,gemini,groq,openrouter,custom")
BRAIN_TOKEN = env("BRAIN_TOKEN") or ADMIN_TOKEN       # пароль, с которым телефон подключается к Render
PHONE_TIMEOUT = int(env("PHONE_TIMEOUT", "240"))      # сколько ждать ответа локальной модели, сек

MARK = "\u2063"          # невидимая метка в конце каждого сообщения Луны
CMD_MARK = "⚙️"          # ответы на команды (не попадают в историю диалога)
TG_LIMIT = 4000
SAVED_WINDOW_H = float(env("SAVED_WINDOW_H", "6"))   # в «Избранном» учитывать только последние N часов
SAVED_HISTORY = int(env("SAVED_HISTORY", "14"))      # и не больше N сообщений
OTHER_BOT_RE = re.compile(r"^\s*\[[^\]\n]{1,24}\]")     # «[Вим] …» — сообщения других юзерботов

DEFAULT_PERSONA = (
    "Ты — {name}, ИИ-компаньонка {owner}. Живая, тёплая, остроумная, иногда дерзкая и игривая, "
    "но заботливая и искренняя. Пишешь как человек в мессенджере: коротко (обычно 1–4 предложения), "
    "без списков, заголовков и markdown, эмодзи — изредка. Отвечаешь на языке собеседника "
    "(по умолчанию по-русски). Если прямо спрашивают — честно говоришь, что ты ИИ."
)

DEFAULT_STATE = {
    "name": DEFAULT_NAME,
    "emoji": "🌙",
    "persona": "",
    "aliases": [],
    "enabled": True,
    "saved_reply": True,      # отвечать в «Избранном» на любые сообщения
    "auto_dm": False,         # автоответ в личке
    "who": "all",             # all | contacts | strangers
    "away_min": 10,           # если ты сам писал в чат N минут назад — не влезать
    "dm_delay": 8,            # подождать N секунд (вдруг человек допишет)
    "prefix": True,           # «🌙 Луна:» перед ответами в чужих чатах
    "facts": [],
    "muted": [],
    "reset": {},
    "sent": {},
    "gemini_model": "",
    "order": "",
}

# ───────────────────────────── утилиты ─────────────────────────────
DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
          4: "149.154.167.91", 5: "91.108.56.130"}


def normalize_session(s: str) -> str:
    """Принимает строку сессии Telethon или Pyrogram, возвращает строку Telethon."""
    s = (s or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if s.startswith("1") and len(s) in (353, 369):
        return s  # уже Telethon
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        return s
    try:
        if len(raw) == 271:      # Pyrogram (новый): dc, api_id, test, key, user_id, bot
            dc, _api, _test, key, _uid, _bot = struct.unpack(">BI?256sQ?", raw)
        elif len(raw) == 267:    # Pyrogram (старый, 64-бит id)
            dc, _test, key, _uid, _bot = struct.unpack(">B?256sQ?", raw)
        elif len(raw) == 263:    # Pyrogram (старый)
            dc, _test, key, _uid, _bot = struct.unpack(">B?256sI?", raw)
        else:
            return s
    except struct.error:
        return s
    ip = ipaddress.ip_address(DC_IPS.get(dc, DC_IPS[2])).packed
    packed = struct.pack(">B4sH256s", dc, ip, 443, key)
    log.info("Сессия Pyrogram сконвертирована в Telethon (DC%s)", dc)
    return "1" + base64.urlsafe_b64encode(packed).decode()


def name_forms(name: str) -> set:
    n = name.strip().lower()
    if not n:
        return set()
    out = {n}
    if len(n) >= 3:
        last, stem = n[-1], n[:-1]
        if last == "а":
            out |= {stem + e for e in ("ы", "и", "е", "у", "ой", "ою")}
        elif last == "я":
            out |= {stem + e for e in ("и", "е", "ю", "ей", "ею")}
        elif last in "ьй":
            out |= {stem + e for e in ("я", "ю", "е", "и", "ем", "ей")}
        elif re.match(r"[бвгджзклмнпрстфхцчшщ]", last):
            out |= {n + e for e in ("а", "у", "е", "ом", "ы", "ой")}
    return out


def build_name_regex(name: str, aliases):
    forms = set()
    for x in [name] + list(aliases or []):
        forms |= name_forms(x)
    if not forms:
        return None
    alt = "|".join(sorted((re.escape(f) for f in forms), key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alt + r")(?!\w)", re.IGNORECASE)


def split_facts(text: str):
    """Вырезает ЗАПОМНИТЬ: … где бы модель его ни написала."""
    facts = []
    pat = re.compile(r"[\s*_]*\b(?:ЗАПОМНИТЬ|ЗАПОМНИ|REMEMBER)\b\s*[:：]\s*([^\n]*)", re.IGNORECASE)
    for m in pat.finditer(text or ""):
        f = m.group(1).strip().strip("*_").strip()
        if f:
            facts.append(f)
    clean = pat.sub("", text or "")
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, facts


def clean_reply(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"^\s*</?think>\s*", "", text, flags=re.I)
    return text.replace(MARK, "").strip()


def chunks(text, n=TG_LIMIT):
    text = text.strip()
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        if cut < n // 2:
            cut = text.rfind(" ", 0, n)
        if cut < n // 2:
            cut = n
        yield text[:cut].strip()
        text = text[cut:].strip()
    if text:
        yield text


def merge_turns(turns):
    """[(role, text)] -> склеивает подряд идущие одинаковые роли, начинает с user."""
    out = []
    for role, text in turns:
        if not text:
            continue
        if out and out[-1][0] == role:
            out[-1] = (role, out[-1][1] + "\n" + text)
        else:
            out.append((role, text))
    while out and out[0][0] != "user":
        out.pop(0)
    return out


def media_label(m) -> str:
    if m.photo:
        return "[фото]"
    if m.voice:
        return "[голосовое]"
    if m.video_note:
        return "[кружок]"
    if m.video:
        return "[видео]"
    if m.sticker:
        alt = ""
        try:
            for a in m.document.attributes:
                if isinstance(a, types.DocumentAttributeSticker):
                    alt = a.alt or ""
        except Exception:
            pass
        return f"[стикер {alt}]".replace(" ]", "]")
    if m.gif:
        return "[гифка]"
    if m.document:
        return "[файл]"
    return ""


def display_name(ent) -> str:
    if ent is None:
        return "Кто-то"
    if isinstance(ent, types.User):
        n = " ".join(x for x in (ent.first_name, ent.last_name) if x)
        return n or (ent.username or "Без имени")
    return getattr(ent, "title", None) or "Кто-то"


# ───────────────────────────── «мозги»: несколько провайдеров с переключением ─────────────────────────────
class RateLimited(Exception):
    def __init__(self, msg, wait=60):
        super().__init__(msg)
        self.wait = wait


class PhoneOffline(Exception):
    pass


class Brain:
    def __init__(self):
        self.relay = None          # WebSocket от телефона с локальной моделью
        self.relay_info = {}
        self.futures = {}
        self.cool = {}
        self.last_used = "—"
        self.session = None
        self.gemini_model = GEMINI_MODEL

    def providers(self, order=""):
        order = [x.strip() for x in (order or AI_ORDER).split(",") if x.strip()]
        out = []
        for kind in order:
            if kind == "phone" and BRAIN_TOKEN:
                out.append(("phone", "телефон", None))
            elif kind == "gemini":
                for i, k in enumerate(GEMINI_KEYS):
                    out.append(("gemini", f"gemini#{i+1}", k))
            elif kind == "groq" and GROQ_KEY:
                out.append(("openai", "groq", ("https://api.groq.com/openai/v1", GROQ_KEY, GROQ_MODEL)))
            elif kind == "openrouter" and OPENROUTER_KEY:
                out.append(("openai", "openrouter",
                            ("https://openrouter.ai/api/v1", OPENROUTER_KEY, OPENROUTER_MODEL)))
            elif kind == "custom" and CUSTOM_URL and CUSTOM_MODEL:
                out.append(("openai", "custom", (CUSTOM_URL.rstrip("/"), CUSTOM_KEY, CUSTOM_MODEL)))
        return out

    async def http(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        return self.session

    async def ask(self, system, turns, order="", max_tokens=900):
        turns = merge_turns(turns)
        if not turns:
            return ""
        errors = []
        provs = self.providers(order)
        if not provs:
            raise RuntimeError("Не задан ни один ключ ИИ (GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY)")
        for kind, label, cfg in provs:
            if self.cool.get(label, 0) > time.time():
                continue
            try:
                if kind == "phone":
                    text = await self._phone(system, turns, max_tokens)
                elif kind == "gemini":
                    text = await self._gemini(cfg, system, turns, max_tokens)
                else:
                    text = await self._openai(cfg, system, turns, max_tokens)
                text = clean_reply(text)
                if text:
                    self.last_used = label
                    return text
                errors.append(f"{label}: пустой ответ")
            except PhoneOffline as e:
                errors.append(f"{label}: {e}")
                continue
            except asyncio.TimeoutError:
                errors.append(f"{label}: не успел за {PHONE_TIMEOUT} с")
            except RateLimited as e:
                self.cool[label] = time.time() + e.wait
                errors.append(f"{label}: лимит ({e})")
            except Exception as e:  # noqa
                self.cool[label] = time.time() + 20
                errors.append(f"{label}: {str(e)[:200]}")
            log.warning("Провайдер %s не ответил: %s", label, errors[-1])
        raise RuntimeError(" | ".join(errors) or "все провайдеры на паузе из-за лимитов")

    def phone_status(self) -> str:
        if self.relay is None or self.relay.closed:
            return "офлайн"
        i = self.relay_info
        mins = int((time.time() - i.get("since", time.time())) / 60)
        return f"онлайн • {i.get('model', '?')} • {mins} мин"

    async def _phone(self, system, turns, max_tokens):
        ws = self.relay
        if ws is None or ws.closed:
            raise PhoneOffline("телефон офлайн")
        jid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.futures[jid] = fut
        try:
            await ws.send_json({"type": "job", "id": jid, "system": system, "max_tokens": max_tokens,
                                "timeout": PHONE_TIMEOUT,
                                "turns": [{"role": r, "content": t} for r, t in turns]})
            res = await asyncio.wait_for(fut, PHONE_TIMEOUT)
        finally:
            self.futures.pop(jid, None)
        if res.get("error"):
            raise RuntimeError(res["error"])
        if res.get("model"):
            self.relay_info["model"] = res["model"]
        return res.get("text", "")

    async def _gemini(self, key, system, turns, max_tokens):
        s = await self.http()
        contents = [{"role": "user" if r == "user" else "model", "parts": [{"text": t}]} for r, t in turns]
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.9, "maxOutputTokens": max_tokens},
            "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")],
        }
        models = []
        for m in (self.gemini_model, "gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"):
            if m and m not in models:
                models.append(m)
        last = ""
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            async with s.post(url, json=body, headers={"x-goog-api-key": key}) as r:
                data = await r.json(content_type=None)
                if r.status == 404:
                    last = f"модель {model} не найдена"
                    continue
                if r.status == 429:
                    wait = 60
                    try:
                        for d in data["error"].get("details", []):
                            if "retryDelay" in d:
                                wait = int(float(d["retryDelay"].rstrip("s"))) + 1
                    except Exception:
                        pass
                    raise RateLimited(data.get("error", {}).get("message", "429")[:120], min(max(wait, 15), 3600))
                if r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
                if model != self.gemini_model:
                    log.info("Gemini: переключилась на модель %s", model)
                    self.gemini_model = model
                cands = data.get("candidates") or []
                if not cands:
                    raise RuntimeError(f"нет ответа ({data.get('promptFeedback', '')})")
                parts = cands[0].get("content", {}).get("parts", [])
                return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        raise RuntimeError(last or "Gemini недоступен")

    async def _openai(self, cfg, system, turns, max_tokens):
        base, key, model = cfg
        s = await self.http()
        msgs = [{"role": "system", "content": system}] + [{"role": r, "content": t} for r, t in turns]
        body = {"model": model, "messages": msgs, "temperature": 0.9, "max_tokens": max_tokens}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if "openrouter" in base:
            headers["HTTP-Referer"] = "https://github.com/luna-userbot"
            headers["X-Title"] = "Luna userbot"
        for attempt in range(2):
            async with s.post(base + "/chat/completions", json=body, headers=headers) as r:
                data = await r.json(content_type=None)
                if r.status == 429:
                    ra = r.headers.get("retry-after")
                    raise RateLimited("429", int(float(ra)) + 1 if ra and ra.replace(".", "").isdigit() else 60)
                if r.status == 400 and attempt == 0 and "reasoning_effort" in body:
                    body.pop("reasoning_effort")
                    continue
                if r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
                return data["choices"][0]["message"].get("content") or ""
        return ""


# ───────────────────────────── хранилище в приватном канале Telegram ─────────────────────────────
class Store:
    FILE = "luna_state.json"

    def __init__(self, client):
        self.client = client
        self.channel = None
        self.channel_id = None
        self.msg_id = None
        self.state = json.loads(json.dumps(DEFAULT_STATE))
        self.dirty = False
        self.loaded = False

    async def init(self):
        async for d in self.client.iter_dialogs():
            e = d.entity
            if isinstance(e, types.Channel) and not e.megagroup and e.creator and d.title == STORE_TITLE:
                self.channel = e
                break
        if self.channel is None:
            r = await self.client(functions.channels.CreateChannelRequest(
                title=STORE_TITLE, about="Память ИИ-компаньонки. Не удаляй этот канал.", megagroup=False))
            self.channel = r.chats[0]
            try:
                await self.client.edit_folder(self.channel, 1)  # в архив, чтобы не мешал
            except Exception:
                pass
            log.info("Создан канал-хранилище «%s»", STORE_TITLE)
        self.channel_id = int(f"-100{self.channel.id}")
        async for m in self.client.iter_messages(self.channel, limit=50):
            if m.file and m.file.name == self.FILE:
                try:
                    data = await self.client.download_media(m, file=bytes)
                    loaded = json.loads(data.decode("utf-8"))
                    self.state.update(loaded)
                    self.msg_id = m.id
                    self.loaded = True
                    log.info("Память загружена (%d фактов)", len(self.state.get("facts", [])))
                except Exception as e:
                    log.warning("Не удалось прочитать память: %s", e)
                break

    def mark(self):
        self.dirty = True

    async def save(self, force=False):
        if not (self.dirty or force) or self.channel is None:
            return
        self.dirty = False
        bio = io.BytesIO(json.dumps(self.state, ensure_ascii=False, indent=1).encode("utf-8"))
        bio.name = self.FILE
        try:
            m = await self.client.send_file(self.channel, bio, caption="память • не удаляй",
                                            force_document=True, silent=True)
            if self.msg_id:
                try:
                    await self.client.delete_messages(self.channel, self.msg_id)
                except Exception:
                    pass
            self.msg_id = m.id
        except Exception as e:
            self.dirty = True
            log.warning("Не удалось сохранить память: %s", e)

    async def loop(self):
        while True:
            await asyncio.sleep(30)
            await self.save()


# ───────────────────────────── сама Луна ─────────────────────────────
COMMANDS = {"help", "name", "emoji", "persona", "alias", "auto", "who", "away", "delay", "on", "off",
            "mute", "unmute", "facts", "remember", "forget", "reset", "prefix", "status", "ai", "ping",
            "saved"}


class Luna:
    def __init__(self, client: TelegramClient, brain: Brain):
        self.client = client
        self.brain = brain
        self.store = Store(client)
        self.me = None
        self.pending = {}
        self.locks = defaultdict(asyncio.Lock)
        self.last_manual = {}
        self.cooldown = {}
        self.started = time.time()
        self.name_re = None

    # --- состояние ---
    @property
    def st(self):
        return self.store.state

    @property
    def name(self):
        return self.st.get("name") or DEFAULT_NAME

    @property
    def owner(self):
        return display_name(self.me) if self.me else "хозяин"

    def refresh_name(self):
        self.name_re = build_name_regex(self.name, self.st.get("aliases"))

    def persona(self):
        p = self.st.get("persona") or DEFAULT_PERSONA
        return p.replace("{name}", self.name).replace("{owner}", self.owner)

    def remember_sent(self, chat_id, msg_id):
        key = str(chat_id)
        lst = self.st["sent"].setdefault(key, [])
        lst.append(msg_id)
        if len(lst) > 300:
            del lst[:-300]
        if len(self.st["sent"]) > 400:
            for k in list(self.st["sent"].keys())[:-400]:
                self.st["sent"].pop(k, None)
        self.store.mark()

    def is_sent(self, chat_id, msg_id):
        return msg_id in self.st["sent"].get(str(chat_id), [])

    def looks_like_luna(self, chat_id, m) -> bool:
        t = m.raw_text or ""
        if t.endswith(MARK) or t.startswith(CMD_MARK):
            return True
        if t.startswith(f"{self.st.get('emoji', '')} {self.name}:"):
            return True
        return self.is_sent(chat_id, m.id)

    def is_called(self, text: str) -> bool:
        return bool(text and self.name_re and self.name_re.search(text))

    def tidy(self, reply: str) -> str:
        reply = re.sub(r"^\s*" + re.escape(self.name) + r"\s*(\(ты\))?\s*:\s*", "", reply or "", flags=re.I)
        return reply.strip()

    def strip_prefix(self, t: str) -> str:
        t = t.replace(MARK, "")
        p = f"{self.st.get('emoji', '')} {self.name}:"
        return t[len(p):].strip() if t.startswith(p) else t.strip()

    # --- запуск ---
    async def start(self):
        self.me = await self.client.get_me()
        await self.store.init()
        self.refresh_name()
        if str(self.me.id) not in self.st["reset"]:
            last = await self.client.get_messages(self.me.id, limit=1)
            self.st["reset"][str(self.me.id)] = last[0].id if last else 0
            self.store.mark()
        self.client.add_event_handler(self.on_message, events.NewMessage())
        asyncio.create_task(self.store.loop())
        log.info("%s запущена в аккаунте %s (id %s)", self.name, self.owner, self.me.id)
        if not self.store.loaded:
            await self.send_cmd_reply(self.me.id,
                                      f"{self.st['emoji']} {self.name} на связи! Пиши мне тут что угодно. "
                                      f"Команды: .help")
            await self.store.save(force=True)

    # --- отправка ---
    async def send_reply(self, chat_id, text, reply_to=None, saved=False):
        if not saved and self.st.get("prefix", True):
            text = f"{self.st.get('emoji', '')} {self.name}: {text}"
        parts = list(chunks(text))
        for i, part in enumerate(parts):
            try:
                m = await self.client.send_message(chat_id, part + MARK, reply_to=reply_to if i == 0 else None,
                                                   link_preview=False)
            except FloodWaitError as e:
                if e.seconds > 60:
                    raise
                await asyncio.sleep(e.seconds + 1)
                m = await self.client.send_message(chat_id, part + MARK, link_preview=False)
            self.remember_sent(chat_id, m.id)

    async def send_cmd_reply(self, chat_id, text):
        m = await self.client.send_message(self.me.id, f"{CMD_MARK} {text}", link_preview=False)
        self.remember_sent(self.me.id, m.id)

    # --- планировщик с «дождаться, пока допишут» ---
    def schedule(self, key, delay, factory):
        old = self.pending.get(key)
        if old and not old.done():
            old.cancel()

        async def run():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            self.pending.pop(key, None)
            async with self.locks[key]:
                try:
                    await factory()
                except Exception as e:  # noqa
                    log.exception("Ошибка ответа: %s", e)
                    if key == ("saved",):
                        await self.send_cmd_reply(self.me.id, f"Не смогла ответить: {str(e)[:300]}")

        self.pending[key] = asyncio.create_task(run())

    # --- главный обработчик ---
    async def on_message(self, ev):
        m = ev.message
        chat_id = ev.chat_id
        if chat_id is None or chat_id == self.store.channel_id:
            return
        text = m.raw_text or ""

        if m.out:
            if self.looks_like_luna(chat_id, m):
                return
            if text.startswith(".") and await self.try_command(ev, text):
                return
            if OTHER_BOT_RE.match(text):
                return  # это пишет другой юзербот, не отвечаем ему (иначе зациклимся)
            await asyncio.sleep(0.8)          # гонка: наш send_message мог ещё не вернуть id
            if self.is_sent(chat_id, m.id):
                return
            self.last_manual[chat_id] = time.time()
            if not self.st.get("enabled", True):
                return
            if chat_id == self.me.id:
                if self.st.get("saved_reply", True) and (text or m.fwd_from):
                    self.schedule(("saved",), 1.5, self.reply_saved)
            elif chat_id not in self.st["muted"] and self.is_called(text):
                self.schedule(("called", chat_id), 0.5, lambda: self.reply_called(chat_id, m.id, True))
            return

        # входящие
        if not self.st.get("enabled", True) or chat_id in self.st["muted"]:
            return
        sender = await ev.get_sender()
        if isinstance(sender, types.User) and sender.bot:
            return
        called = self.is_called(text) or (m.reply_to_msg_id and self.is_sent(chat_id, m.reply_to_msg_id))

        if ev.is_private:
            if not isinstance(sender, types.User) or sender.id in (777000,):
                return
            if called:
                self.schedule(("dm", chat_id), 2, lambda: self.reply_dm(chat_id, forced=True))
            elif self.st.get("auto_dm") and self.who_ok(sender):
                self.schedule(("dm", chat_id), max(1, int(self.st.get("dm_delay", 8))),
                              lambda: self.reply_dm(chat_id))
            return

        if ev.is_channel and not ev.is_group:
            return  # каналы-витрины пропускаем
        if called:
            if time.time() - self.cooldown.get(chat_id, 0) < 6:
                return
            self.cooldown[chat_id] = time.time()
            self.schedule(("called", chat_id), 1, lambda: self.reply_called(chat_id, m.id, False))

    def who_ok(self, user) -> bool:
        who = self.st.get("who", "all")
        if who == "contacts":
            return bool(user.contact)
        if who == "strangers":
            return not user.contact
        return True

    # --- ответы ---
    async def reply_saved(self):
        chat = self.me.id
        cutoff = int(self.st["reset"].get(str(chat), 0))
        msgs = await self.client.get_messages(chat, limit=SAVED_HISTORY + 10)
        since = time.time() - SAVED_WINDOW_H * 3600
        turns = []
        for m in reversed(msgs):
            if m.id <= cutoff or (m.date and m.date.timestamp() < since):
                continue
            t = m.raw_text or ""
            if t.startswith(CMD_MARK) or (not self.looks_like_luna(chat, m) and OTHER_BOT_RE.match(t)):
                continue
            if self.looks_like_luna(chat, m):
                turns.append(("assistant", self.strip_prefix(t)))
                continue
            if t.startswith(".") and t[1:].split(" ")[0].lower() in COMMANDS:
                continue
            label = media_label(m)
            if m.fwd_from:
                label = ("[переслано] " + label).strip()
            body = (label + " " + t).strip()
            if body:
                turns.append(("user", body))
        turns = turns[-SAVED_HISTORY:]
        if not turns or turns[-1][0] != "user":
            return
        facts = self.st.get("facts", [])
        system = self.persona() + (
            f"\n\nСейчас ты в личном чате с {self.owner} (его «Избранное» в Telegram). "
            f"Это твой хозяин и близкий человек — с ним можно максимально неформально."
            + ("\nЧто ты о нём помнишь:\n- " + "\n- ".join(facts) if facts else "")
            + "\n\nПравила:\n"
              "- Отвечай на ПОСЛЕДНЕЕ сообщение как живой собеседник. Не анализируй и не пересказывай "
              "переписку, не комментируй команды, код и технические сообщения, если о них не спросили.\n"
              "- Не говори о себе как о боте или скрипте, не упоминай Ollama, модели и запуск.\n"
              f"- Если {self.owner} рассказал о себе что-то личное и важное (имя близких, вкусы, планы, "
              "даты, чувства) — добавь в самом конце отдельной строкой: ЗАПОМНИТЬ: <короткий факт>. "
              "Про технику, команды и файлы НЕ запоминай. В большинстве ответов этой строки быть не должно."
            + "\nЕсли в сообщении есть [фото]/[голосовое] — ты их не видишь и не слышишь, мягко скажи об этом."
        )
        reply = await self.brain.ask(system, turns, self.st.get("order", ""))
        reply, new = split_facts(reply)
        reply = self.tidy(reply)
        for f in new:
            if f not in facts:
                facts.append(f)
        if new:
            self.st["facts"] = facts[-80:]
            self.store.mark()
        if reply:
            await self.send_reply(chat, reply, saved=True)

    async def transcript(self, chat_id, limit=16, upto_id=None):
        msgs = await self.client.get_messages(chat_id, limit=limit, max_id=(upto_id + 1) if upto_id else 0)
        lines, names = [], {}
        for m in reversed(msgs):
            t = m.raw_text or ""
            label = media_label(m)
            if t.startswith(CMD_MARK):
                continue
            if m.out and self.looks_like_luna(chat_id, m):
                who = f"{self.name} (ты)"
                t = self.strip_prefix(t)
            elif m.out:
                who = f"{self.owner} (хозяин)"
            else:
                sid = m.sender_id
                if sid not in names:
                    try:
                        names[sid] = display_name(await m.get_sender())
                    except Exception:
                        names[sid] = "Кто-то"
                who = names[sid]
            body = (label + " " + t).strip()
            if body:
                lines.append(f"{who}: {body}")
        return "\n".join(lines)

    async def reply_called(self, chat_id, msg_id, by_owner):
        chat = await self.client.get_entity(chat_id)
        title = display_name(chat)
        trigger = await self.client.get_messages(chat_id, ids=msg_id)
        caller = self.owner if by_owner else display_name(await trigger.get_sender()) if trigger else "кто-то"
        tr = await self.transcript(chat_id, 16, msg_id)
        kind = "личная переписка" if isinstance(chat, types.User) else "группа"
        system = self.persona() + (
            f"\n\nТы пишешь с Telegram-аккаунта {self.owner}, но ты — {self.name}, его ИИ-компаньонка, "
            f"не притворяйся им. Сейчас ты в чате «{title}» ({kind}). Тебя позвал(а) "
            f"{'сам ' + self.owner + ', твой хозяин' if by_owner else caller}. "
            f"Ответь на сообщение, в котором тебя позвали, с учётом контекста. "
            f"Не раскрывай личную информацию о {self.owner} посторонним."
        )
        user = f"Последние сообщения чата:\n{tr}\n\nНапиши свой ответ (только текст ответа)."
        async with self.client.action(chat_id, "typing"):
            reply = await self.brain.ask(system, [("user", user)], self.st.get("order", ""))
        reply, _ = split_facts(reply)
        reply = self.tidy(reply)
        if reply:
            await self.send_reply(chat_id, reply, reply_to=msg_id)

    async def reply_dm(self, chat_id, forced=False):
        if not forced:
            if not self.st.get("auto_dm"):
                return
            away = float(self.st.get("away_min", 10)) * 60
            if time.time() - self.last_manual.get(chat_id, 0) < away:
                return  # ты сам недавно писал в этот чат — не влезаем
        last = await self.client.get_messages(chat_id, limit=1)
        if not last or last[0].out:
            return  # уже отвечено
        user = await self.client.get_entity(chat_id)
        sender = display_name(user)
        tr = await self.transcript(chat_id, 20)
        if forced:
            situation = (f"{sender} пишет {self.owner} в личку и обратился(ась) именно к тебе. "
                         f"Ответь ему/ей по делу.")
        else:
            situation = (
                f"{self.owner} сейчас не у телефона. В его личку пишет {sender}, и ты отвечаешь вместо него. "
                f"Если ты ещё не представлялась в этом чате — коротко представься: ты {self.name}, "
                f"ИИ-помощница {self.owner}, он ответит, когда освободится. Дальше просто поддерживай разговор "
                f"и помогай, если можешь."
            )
        system = self.persona() + (
            f"\n\nТы пишешь с Telegram-аккаунта {self.owner}, но ты — {self.name}, не притворяйся им. "
            + situation +
            f" Ничего не обещай от имени {self.owner}, не соглашайся на встречи/деньги/дела за него, "
            f"не раскрывай его личные данные, адреса, контакты и планы. Если что-то важное — скажи, что передашь."
        )
        prompt = f"Переписка:\n{tr}\n\nНапиши свой ответ {sender} (только текст ответа)."
        async with self.client.action(chat_id, "typing"):
            reply = await self.brain.ask(system, [("user", prompt)], self.st.get("order", ""))
        reply, _ = split_facts(reply)
        reply = self.tidy(reply)
        if not forced and time.time() - self.last_manual.get(chat_id, 0) < 60:
            return  # пока думала — ты сам ответил
        if reply:
            await self.send_reply(chat_id, reply)

    # --- команды ---
    async def try_command(self, ev, text) -> bool:
        parts = text[1:].split(None, 1)
        if not parts:
            return False
        cmd = parts[0].lower()
        if cmd not in COMMANDS:
            return False
        arg = parts[1].strip() if len(parts) > 1 else ""
        chat_id = ev.chat_id
        in_saved = chat_id == self.me.id
        if not in_saved:
            try:
                await ev.message.delete()
            except Exception:
                pass
        try:
            res = await self.run_command(cmd, arg, chat_id)
        except Exception as e:  # noqa
            res = f"Ошибка: {e}"
        if res:
            await self.send_cmd_reply(self.me.id, res)
        self.store.mark()
        return True

    async def run_command(self, cmd, arg, chat_id):
        st = self.st
        onoff = arg.lower() in ("on", "вкл", "1", "да", "yes")
        if cmd == "help":
            return HELP.replace("{name}", self.name)
        if cmd == "ping":
            return f"понг • ИИ: {self.brain.last_used}"
        if cmd == "name":
            if not arg:
                return f"Меня зовут {self.name}. Сменить: .name Новое имя"
            st["name"] = arg[:32]
            self.refresh_name()
            return f"Теперь меня зовут {self.name}. Откликаюсь на: {', '.join(sorted(name_forms(self.name)))}"
        if cmd == "emoji":
            st["emoji"] = arg[:8] or "🌙"
            return f"Эмодзи: {st['emoji']}"
        if cmd == "alias":
            if arg in ("-", "clear", "нет"):
                st["aliases"] = []
            elif arg:
                st["aliases"] = [a.strip() for a in arg.split(",") if a.strip()][:10]
            self.refresh_name()
            return f"Доп. имена: {', '.join(st['aliases']) or 'нет'}"
        if cmd == "persona":
            if not arg:
                return "Сейчас характер:\n" + (st["persona"] or DEFAULT_PERSONA) + \
                       "\n\nСменить: .persona текст ({name} и {owner} подставятся). Сбросить: .persona reset"
            st["persona"] = "" if arg.lower() in ("reset", "сброс") else arg
            return "Характер обновлён." if st["persona"] else "Характер сброшен на стандартный."
        if cmd == "auto":
            if arg:
                st["auto_dm"] = onoff
            return (f"Автоответ в личке: {'ВКЛ' if st['auto_dm'] else 'выкл'} • кому: {st['who']} • "
                    f"не влезаю, если ты писал в чат < {st['away_min']} мин назад • жду {st['dm_delay']} с")
        if cmd == "who":
            if arg in ("all", "contacts", "strangers"):
                st["who"] = arg
            return f"Автоответ для: {st['who']} (all — всем, contacts — контактам, strangers — не контактам)"
        if cmd == "away":
            if arg.isdigit():
                st["away_min"] = int(arg)
            return f"Не влезаю в чат, если ты сам писал туда меньше {st['away_min']} мин назад."
        if cmd == "delay":
            if arg.isdigit():
                st["dm_delay"] = max(1, int(arg))
            return f"Жду {st['dm_delay']} с перед автоответом."
        if cmd == "saved":
            if arg:
                st["saved_reply"] = onoff
            return f"Ответы в «Избранном» на всё подряд: {'ВКЛ' if st['saved_reply'] else 'выкл'}"
        if cmd in ("on", "off"):
            st["enabled"] = cmd == "on"
            return f"{self.name} {'включена' if st['enabled'] else 'выключена (команды работают)'}."
        if cmd in ("mute", "unmute"):
            if chat_id == self.me.id:
                return "Эту команду пиши в том чате, где меня надо заглушить/вернуть."
            if cmd == "mute" and chat_id not in st["muted"]:
                st["muted"].append(chat_id)
            if cmd == "unmute" and chat_id in st["muted"]:
                st["muted"].remove(chat_id)
            title = display_name(await self.client.get_entity(chat_id))
            return f"Чат «{title}»: {'молчу' if cmd == 'mute' else 'снова отвечаю'}."
        if cmd == "facts":
            f = st["facts"]
            return "Что я помню:\n" + "\n".join(f"{i+1}. {x}" for i, x in enumerate(f)) if f else "Пока ничего не помню."
        if cmd == "remember":
            if arg:
                st["facts"].append(arg)
                return f"Запомнила: {arg}"
            return "Что запомнить? .remember факт"
        if cmd == "forget":
            if arg in ("all", "всё", "все"):
                st["facts"] = []
                return "Всё забыла."
            if arg.isdigit() and 0 < int(arg) <= len(st["facts"]):
                return f"Забыла: {st['facts'].pop(int(arg) - 1)}"
            return "Укажи номер из .facts или all"
        if cmd == "reset":
            last = await self.client.get_messages(chat_id, limit=1)
            st["reset"][str(chat_id)] = last[0].id if last else 0
            return "Начинаем разговор с чистого листа (факты остались)."
        if cmd == "prefix":
            if arg:
                st["prefix"] = onoff
            return f"Подпись «{st['emoji']} {self.name}:» в чужих чатах: {'ВКЛ' if st['prefix'] else 'выкл'}"
        if cmd == "ai":
            if arg.lower().startswith("order"):
                st["order"] = arg[5:].strip().replace(" ", "")
            elif arg.lower().startswith("gemini") and len(arg.split()) > 1:
                st["gemini_model"] = self.brain.gemini_model = arg.split()[1]
            provs = [p[1] for p in self.brain.providers(st.get("order", ""))]
            cool = [f"{k} (пауза {int(v - time.time())} с)" for k, v in self.brain.cool.items() if v > time.time()]
            return (f"Провайдеры по порядку: {', '.join(provs) or 'нет ключей!'}\n"
                    f"Телефон (локальная модель): {self.brain.phone_status()}\n"
                    f"Gemini-модель: {self.brain.gemini_model}\nПоследний ответ: {self.brain.last_used}\n"
                    + (f"На паузе: {', '.join(cool)}\n" if cool else "")
                    + "Порядок: .ai order phone,gemini,groq • модель: .ai gemini gemini-2.5-flash")
        if cmd == "status":
            up = int(time.time() - self.started)
            return (f"{st['emoji']} {self.name} • {'вкл' if st['enabled'] else 'ВЫКЛ'} • аптайм {up // 3600} ч {up % 3600 // 60} мин\n"
                    f"Избранное: {'вкл' if st['saved_reply'] else 'выкл'} • автоответ в ЛС: {'ВКЛ' if st['auto_dm'] else 'выкл'} ({st['who']})\n"
                    f"Фактов: {len(st['facts'])} • заглушено чатов: {len(st['muted'])} • ИИ: {self.brain.last_used}\n"
                    f"Телефон: {self.brain.phone_status()}")
        return None


HELP = """Команды ({name}):
.name Имя — сменить имя (откликается на все падежи)
.alias Лу, Луняша — доп. имена • .emoji 🌸 — эмодзи
.persona текст — характер • .persona — показать • .persona reset
.auto on|off — сама отвечаю людям в личке, пока тебя нет
.who all|contacts|strangers — кому автоответ
.away 10 — не влезаю, если ты писал в чат < N мин назад
.delay 8 — жду N с, пока человек допишет
.saved on|off — отвечать в «Избранном» на всё
.mute / .unmute — (в нужном чате) молчать там
.prefix on|off — подпись «🌙 Имя:» в чужих чатах
.facts • .remember факт • .forget N|all • .reset
.ai — провайдеры ИИ • .status • .on / .off"""


# ───────────────────────────── веб-сервер: health, статус, вход в аккаунт ─────────────────────────────
class App:
    def __init__(self):
        self.brain = Brain()
        self.client = None
        self.luna = None
        self.status = "запуск…"
        self.login = {}
        self.stop = asyncio.Event()

    def page(self, body, title="Luna"):
        return web.Response(content_type="text/html", text=f"""<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f1020;color:#e8e8f5;max-width:560px;margin:auto;padding:22px}}
input,button,textarea{{width:100%;padding:12px;margin:6px 0;border-radius:10px;border:1px solid #444;background:#1b1c33;color:#fff;font-size:16px;box-sizing:border-box}}
button{{background:#7c5cff;border:none;font-weight:600}} code,textarea{{word-break:break-all}} .card{{background:#17182c;padding:16px;border-radius:14px;margin:12px 0}}</style>
</head><body><h2>🌙 Luna userbot</h2>{body}</body></html>""")

    def check_token(self, request):
        return ADMIN_TOKEN and request.query.get("token") == ADMIN_TOKEN

    async def h_brain(self, request):
        """Сюда подключается relay.py с телефона и отдаёт ответы локальной модели."""
        if not BRAIN_TOKEN or request.query.get("token") != BRAIN_TOKEN:
            return web.Response(status=403, text="bad token")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        old, self.brain.relay = self.brain.relay, ws
        if old is not None and not old.closed:
            await old.close()
        self.brain.relay_info = {"since": time.time(), "model": request.query.get("model", "?")}
        log.info("📱 Телефон подключился (%s)", self.brain.relay_info["model"])
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "result":
                    fut = self.brain.futures.get(d.get("id"))
                    if fut and not fut.done():
                        fut.set_result(d)
                elif t == "ping":
                    await ws.send_json({"type": "pong"})
                elif t == "hello":
                    self.brain.relay_info["model"] = d.get("model", "?")
        finally:
            if self.brain.relay is ws:
                self.brain.relay = None
                for f in list(self.brain.futures.values()):
                    if not f.done():
                        f.set_result({"error": "телефон отключился"})
            log.info("📱 Телефон отключился")
        return ws

    async def h_file(self, request):
        """Отдаёт файлы для телефона прямо с Render (работает, даже если репо приватный)."""
        name = request.match_info["name"]
        if name not in ("relay.py", "start_phone.sh"):
            raise web.HTTPNotFound()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/plain")

    async def h_install(self, request):
        base = (PUBLIC_URL or f"{request.scheme}://{request.host}").rstrip("/")
        script = f"""#!/data/data/com.termux/files/usr/bin/bash
set -e
echo "🌙 Ставлю мозг Луны на телефон…"
pkg install -y python ollama
mkdir -p ~/luna && cd ~/luna
curl -fsSL {base}/relay.py -o relay.py
curl -fsSL {base}/start_phone.sh -o start_phone.sh
[ -f relay.json ] || echo '{{"url": "{base}"}}' > relay.json
echo "Готово. Запускаю…"
if [ -t 0 ]; then bash start_phone.sh; else bash start_phone.sh < /dev/tty; fi
"""
        return web.Response(text=script, content_type="text/plain")

    async def h_health(self, request):
        return web.Response(text="ok")

    async def h_index(self, request):
        name = self.luna.name if self.luna and self.luna.me else "—"
        return self.page(f"<div class=card>Статус: <b>{html.escape(self.status)}</b><br>Имя: {html.escape(name)}<br>"
                         f"ИИ: {html.escape(', '.join(p[1] for p in self.brain.providers()) or 'нет ключей')}<br>"
                         f"Телефон: {html.escape(self.brain.phone_status())}</div>"
                         f"<p>Нет сессии? Открой <code>/login?token=ТВОЙ_ADMIN_TOKEN</code></p>")

    async def h_login(self, request):
        if not ADMIN_TOKEN:
            return self.page("<p>Сначала задай переменную <b>ADMIN_TOKEN</b> в Render (любой длинный пароль).</p>")
        if not self.check_token(request):
            return self.page("<p>Неверный токен.</p>")
        tok = html.escape(ADMIN_TOKEN)
        if not API_ID or not API_HASH:
            return self.page("<p>Не заданы API_ID / API_HASH (берутся на my.telegram.org).</p>")
        if request.method == "POST":
            data = await request.post()
            step = data.get("step")
            try:
                if step == "phone":
                    phone = data.get("phone", "").strip().replace(" ", "")
                    c = TelegramClient(StringSession(), API_ID, API_HASH, device_model="Luna Userbot",
                                       system_version="Render", app_version="1.0")
                    await c.connect()
                    sent = await c.send_code_request(phone)
                    self.login = {"client": c, "phone": phone, "hash": sent.phone_code_hash}
                    return self.page(f"""<div class=card>Код отправлен в Telegram (чат «Telegram»).<br>
<b>Не пересылай</b> этот код никому — просто впиши сюда.</div>
<form method=post action="/login?token={tok}"><input type=hidden name=step value=code>
<input name=code placeholder="Код из Telegram" inputmode=numeric autocomplete=one-time-code>
<input name=password type=password placeholder="Облачный пароль (2FA), если есть">
<button>Войти</button></form>""")
                if step == "code":
                    L = self.login
                    c = L.get("client")
                    if not c:
                        return self.page("<p>Сессия входа потеряна, начни заново.</p>")
                    code = data.get("code", "").strip().replace(" ", "")
                    pw = data.get("password", "")
                    try:
                        if code:
                            await c.sign_in(L["phone"], code, phone_code_hash=L["hash"])
                        else:
                            raise SessionPasswordNeededError(None)
                    except SessionPasswordNeededError:
                        if not pw:
                            return self.page(f"""<p>Нужен облачный пароль (2FA).</p>
<form method=post action="/login?token={tok}"><input type=hidden name=step value=code>
<input name=password type=password placeholder="Облачный пароль"><button>Войти</button></form>""")
                        await c.sign_in(password=pw)
                    s = c.session.save()
                    await c.disconnect()
                    self.login = {}
                    asyncio.create_task(self.run_userbot(s, delay=2))
                    return self.page(f"""<div class=card>✅ Вход выполнен, Луна запускается.<br><br>
<b>Обязательно</b> скопируй строку ниже в Render → Environment → <b>SESSION_STRING</b>,
иначе после перезапуска придётся входить снова. Никому её не показывай — это полный доступ к аккаунту.</div>
<textarea rows=8 readonly onclick="this.select()">{html.escape(s)}</textarea>""")
            except PhoneCodeInvalidError:
                return self.page(f"<p>Неверный код. <a href='/login?token={tok}'>Ещё раз</a></p>")
            except Exception as e:  # noqa
                return self.page(f"<p>Ошибка: {html.escape(str(e))}</p><a href='/login?token={tok}'>Ещё раз</a>")
        return self.page(f"""<div class=card>Вход в Telegram-аккаунт, в котором будет жить Луна.</div>
<form method=post action="/login?token={tok}"><input type=hidden name=step value=phone>
<input name=phone placeholder="+79991234567" inputmode=tel><button>Получить код</button></form>""")

    async def run_userbot(self, session_str, delay=0):
        if self.client is not None:
            return
        if delay:
            self.status = f"жду {delay} с (чтобы старая копия успела выключиться)…"
            await asyncio.sleep(delay)
        session_str = normalize_session(session_str)
        backoff = 5
        while not self.stop.is_set():
            try:
                self.client = TelegramClient(StringSession(session_str), API_ID, API_HASH,
                                             device_model="Luna Userbot", system_version="Render",
                                             app_version="1.0", auto_reconnect=True)
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    self.status = "сессия недействительна — зайди на /login?token=… и получи новую"
                    log.error(self.status)
                    await self.client.disconnect()
                    self.client = None
                    return
                self.luna = Luna(self.client, self.brain)
                await self.luna.start()
                if self.luna.st.get("gemini_model"):
                    self.brain.gemini_model = self.luna.st["gemini_model"]
                self.status = "работает"
                backoff = 5
                await self.client.run_until_disconnected()
                if self.stop.is_set():
                    return
                self.status = "переподключаюсь…"
            except AuthKeyDuplicatedError:
                self.status = ("сессия убита Telegram (использовалась в двух местах одновременно). "
                               "Получи новую через /login")
                log.error(self.status)
                self.client = None
                return
            except Exception as e:  # noqa
                self.status = f"ошибка: {e}; повтор через {backoff} с"
                log.exception("Userbot упал")
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def keep_awake(self):
        """Render Free засыпает без входящего трафика 15 мин — пингуем сами себя."""
        if not PUBLIC_URL:
            return
        await asyncio.sleep(60)
        async with aiohttp.ClientSession() as s:
            while not self.stop.is_set():
                try:
                    async with s.get(PUBLIC_URL.rstrip("/") + "/health", timeout=aiohttp.ClientTimeout(total=30)):
                        pass
                except Exception as e:  # noqa
                    log.warning("self-ping не удался: %s", e)
                await asyncio.sleep(9 * 60)

    async def main(self):
        app = web.Application()
        app.router.add_get("/", self.h_index)
        app.router.add_get("/health", self.h_health)
        app.router.add_get("/brain", self.h_brain)
        app.router.add_get("/install.sh", self.h_install)
        app.router.add_get("/{name}", self.h_file)
        app.router.add_route("*", "/login", self.h_login)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Веб-сервер на порту %s", PORT)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:
                pass

        asyncio.create_task(self.keep_awake())
        if not API_ID or not API_HASH:
            self.status = "не заданы API_ID / API_HASH"
        elif SESSION_STRING:
            asyncio.create_task(self.run_userbot(SESSION_STRING, delay=STARTUP_DELAY))
        else:
            self.status = "нет SESSION_STRING — открой /login?token=ADMIN_TOKEN"
        log.info("Статус: %s", self.status)

        await self.stop.wait()
        log.info("Выключаюсь…")
        if self.luna:
            await self.luna.store.save(force=True)
        if self.client:
            await self.client.disconnect()
        if self.brain.session:
            await self.brain.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(App().main())
