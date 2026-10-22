# -*- coding: utf-8 -*-
"""
relay.py — «мозг» Луны на телефоне (Termux + Ollama).

Телефон сам подключается к твоему сервису на Render по WebSocket, получает вопросы,
отвечает локальной моделью Ollama и отправляет ответ обратно. Белый IP, туннели и
проброс портов не нужны. Только стандартная библиотека Python, pip не нужен.

Запуск:  bash start_phone.sh        (или python relay.py)
Сменить модель:  python relay.py --model qwen3.5:9b
Перенастроить:   python relay.py --setup
"""
import base64
import json
import os
import queue
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "relay.json")
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = "qwen3.5:4b"


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ───────────── настройки ─────────────
def load_cfg():
    cfg = {}
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    args = sys.argv[1:]
    if "--model" in args:
        cfg["model"] = args[args.index("--model") + 1]
    setup = "--setup" in args
    if setup or not cfg.get("url") or not cfg.get("token"):
        print("\n🌙 Настройка связи телефон ↔ Render\n")
    if setup or not cfg.get("url"):
        url = input("Адрес сервиса на Render (например https://userbot-xxxx.onrender.com): ").strip()
        if url and not url.startswith("http"):
            url = "https://" + url
        cfg["url"] = url.rstrip("/") or cfg.get("url", "")
    if setup or not cfg.get("token"):
        tok = input("ADMIN_TOKEN из Render → Environment: ").strip()
        cfg["token"] = tok or cfg.get("token", "")
    if setup:
        m = input(f"Модель [{cfg.get('model', DEFAULT_MODEL)}]: ").strip()
        if m:
            cfg["model"] = m
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("num_ctx", 6144)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return cfg


# ───────────── Ollama ─────────────
def http_json(path, body=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ollama_up():
    try:
        http_json("/api/tags", timeout=3)
        return True
    except Exception:
        return False


def ensure_ollama(model):
    if not ollama_up():
        if not shutil.which("ollama"):
            sys.exit("Ollama не установлена. Выполни: pkg install ollama -y")
        say("Запускаю ollama serve…")
        log = open(os.path.join(HERE, "ollama.log"), "a")
        subprocess.Popen(["ollama", "serve"], stdout=log, stderr=log, start_new_session=True)
        for _ in range(60):
            time.sleep(1)
            if ollama_up():
                break
        else:
            sys.exit("Ollama не запустилась, смотри ollama.log")
    names = [m.get("name", "") for m in http_json("/api/tags").get("models", [])]
    if not any(n == model or n == model + ":latest" for n in names):
        say(f"Скачиваю модель {model} (один раз, может занять время)…")
        req = urllib.request.Request(OLLAMA + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        last = -1
        with urllib.request.urlopen(req, timeout=None) as r:
            for line in r:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    sys.exit("Ошибка скачивания: " + d["error"])
                if d.get("total") and d.get("completed"):
                    pct = int(d["completed"] * 100 / d["total"])
                    if pct != last and pct % 5 == 0:
                        say(f"  {pct}%")
                        last = pct
        say("Модель скачана.")
    say(f"Прогреваю {model}…")
    try:
        http_json("/api/generate", {"model": model, "prompt": "", "keep_alive": -1}, timeout=600)
    except Exception as e:
        say("Прогрев не удался (не страшно):", e)


THINK_OK = [True]


def ask_ollama(cfg, system, turns, max_tokens):
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}] + turns,
        "stream": False,
        "keep_alive": -1,
        "options": {"num_ctx": int(cfg.get("num_ctx", 6144)), "num_predict": int(max_tokens), "temperature": 0.9},
    }
    if THINK_OK[0]:
        body["think"] = False
    try:
        d = http_json("/api/chat", body)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="ignore")
        if e.code == 400 and "think" in msg.lower() and THINK_OK[0]:
            THINK_OK[0] = False
            body.pop("think", None)
            d = http_json("/api/chat", body)
        else:
            raise RuntimeError(f"Ollama HTTP {e.code}: {msg[:200]}")
    text = d.get("message", {}).get("content", "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    return text


# ───────────── минимальный WebSocket-клиент (RFC 6455) ─────────────
class WS:
    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        secure = u.scheme in ("wss", "https")
        host, port = u.hostname, u.port or (443 if secure else 80)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        raw = socket.create_connection((host, port), timeout=30)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if secure else raw
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\nUser-Agent: luna-relay\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            ch = self.sock.recv(1)
            if not ch:
                raise ConnectionError("сервер закрыл соединение")
            head += ch
            if len(head) > 16384:
                raise ConnectionError("странный ответ сервера")
        status = head.split(b"\r\n", 1)[0].decode(errors="ignore")
        if " 101 " not in status + " ":
            if " 403" in status:
                raise PermissionError("неверный токен (BRAIN_TOKEN/ADMIN_TOKEN)")
            raise ConnectionError(f"не WebSocket: {status}")
        self.sock.settimeout(120)   # сервер шлёт ping каждые 30 с
        self.lock = threading.Lock()

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            ch = self.sock.recv(n - len(buf))
            if not ch:
                raise ConnectionError("соединение закрыто")
            buf += ch
        return buf

    def send(self, payload, opcode=1):
        if isinstance(payload, str):
            payload = payload.encode()
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            self.sock.sendall(hdr + mask + body)

    def send_json(self, obj):
        self.send(json.dumps(obj, ensure_ascii=False))

    def recv(self):
        """Возвращает текст сообщения; ping/pong обрабатывает сам."""
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n) if n else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:
                self.send(data, opcode=10)
                continue
            if op == 10:
                continue
            if op == 8:
                raise ConnectionError("сервер закрыл соединение")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self):
        try:
            self.send(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ───────────── основной цикл ─────────────
def worker(cfg, jobs, get_ws):
    while True:
        job, got_at = jobs.get()
        if time.time() - got_at > job.get("timeout", 240) - 5:
            continue  # Render уже не ждёт — пропускаем
        t0 = time.time()
        try:
            text = ask_ollama(cfg, job["system"], job["turns"], job.get("max_tokens", 900))
            res = {"type": "result", "id": job["id"], "text": text, "model": cfg["model"]}
            say(f"✓ ответила за {time.time() - t0:.0f} с ({len(text)} симв.)")
        except Exception as e:
            res = {"type": "result", "id": job["id"], "error": str(e)[:300]}
            say("✗ ошибка модели:", e)
        ws = get_ws()
        if ws:
            try:
                ws.send_json(res)
            except Exception as e:
                say("не смогла отправить ответ:", e)


def main():
    cfg = load_cfg()
    ensure_ollama(cfg["model"])
    base = cfg["url"].replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base}/brain?token={urllib.parse.quote(cfg['token'])}&model={urllib.parse.quote(cfg['model'])}"
    jobs = queue.Queue()
    current = {"ws": None}
    threading.Thread(target=worker, args=(cfg, jobs, lambda: current["ws"]), daemon=True).start()

    def pinger():
        while True:
            time.sleep(50)
            ws = current["ws"]
            if ws:
                try:
                    ws.send_json({"type": "ping"})   # заодно не даёт Render уснуть
                except Exception:
                    pass
    threading.Thread(target=pinger, daemon=True).start()

    backoff = 3
    while True:
        try:
            say(f"Подключаюсь к {cfg['url']} … (если Render спал — до минуты)")
            ws = WS(url)
            current["ws"] = ws
            ws.send_json({"type": "hello", "model": cfg["model"]})
            say(f"🌙 Связь есть! Луна думает на телефоне моделью {cfg['model']}. Не закрывай Termux.")
            backoff = 3
            while True:
                d = json.loads(ws.recv())
                if d.get("type") == "job":
                    say(f"→ вопрос (в очереди {jobs.qsize()})")
                    jobs.put((d, time.time()))
        except PermissionError as e:
            sys.exit(f"Ошибка: {e}. Запусти: python relay.py --setup")
        except KeyboardInterrupt:
            say("Выход.")
            return
        except Exception as e:
            say(f"Связь потеряна: {e}. Повтор через {backoff} с")
        finally:
            if current["ws"]:
                current["ws"].close()
                current["ws"] = None
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
