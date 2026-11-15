"""
plugins/_whatsapp_core.py — the WhatsApp driver every WhatsApp plugin shares.

WHAT THIS FILE IS
    A driver, not a skill. The plugin loader skips files whose name starts with
    an underscore, so nothing here is ever handed to the model as a tool; the
    skills beside it (whatsapp_call.py) import it and drive WhatsApp with it.

TWO TRANSPORTS, ONE API
    Desktop   the WhatsApp app for Windows, read through its accessibility
              tree with pywinauto. No browser, real notifications, and the
              only transport that can answer an incoming call.
    Web       WhatsApp Web in a Playwright browser. Slower to start and it
              needs a signed-in session, but it works on a machine with no
              WhatsApp app.

    Both answer the same questions — open_chat, current_chat, press_call,
    hangup, send_message_to — so a skill never has to care which one it got.

WHY THE DESKTOP TRANSPORT IS THE DEFAULT
    The desktop app is where the phone is, where the notifications are, and
    where an incoming call actually arrives. Web is the fallback, not the
    first choice.

THE RULE THAT MATTERS MOST
    A call is a real person ringing. Before anything is pressed, the driver
    reads the conversation title out of the window and compares it with the
    name that was asked for. If they do not match, nothing is pressed and the
    skill says so. A wrong tap rings a stranger.

THE PERFORMANCE STORY
    The accessibility tree of the WhatsApp app is enormous — around twenty
    thousand elements — and Windows answers even a narrow question about it
    in roughly two seconds. That cost is the provider's, not ours: it is the
    same whether we ask for every element or for one name.

    So the driver asks as rarely as it can, and it does the rest with the
    keyboard, which costs milliseconds:

        1. find the window          win32, ~5 ms
        2. focus it                 win32, ~5 ms
        3. open the search box      Ctrl+K, ~5 ms
        4. type the name            clipboard paste, ~50 ms
        5. find and press the row   ONE accessibility query
        6. read the header          the SAME query, if the answer is cached
        7. press the call button    no query — the element is already known

    One query per action instead of five is the difference between a call
    that starts while the user is still speaking and one that starts after
    they have put the phone down.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

# ── Settings ──────────────────────────────────────────────────────────────────
# The namespace is shared: every WhatsApp plugin reads and writes the same
# settings, so there is one WHATSAPP section in the settings tab and not two.
NS = "whatsapp"

DEFAULTS = {
    "transport":      "auto",     # auto | desktop | web
    "app_name":       "WhatsApp",  # how the OS finds the app
    "verify_contact": True,        # refuse to dial if the wrong chat opened
    "launch_wait":    12.0,        # seconds to wait for the app to show a window
    "search_wait":    4.0,         # seconds to wait for the conversation to open
}


class Unsupported(Exception):
    """This transport cannot run on this machine, and says why."""


# ── The words WhatsApp puts on its buttons ───────────────────────────────────
# A driver that only knows English stops working the moment the app is in
# another language — and the app follows the language of the operating
# system, not the language of the user. These lists cover the languages this
# project has met so far. A word that is not on the list still has a chance:
# the driver also reads the layout, and "diagnose" writes down everything it
# can see so a missing word can be added in one line.
VOICE_NAMES = (
    "voice call", "audio call", "voice", "audio",
    "مكالمة صوتية", "اتصال صوتي",
    "sesli arama", "sesli ara",
    "llamada de voz", "llamada de audio",
    "appel vocal", "appel audio",
    "sprachanruf", "audioanruf",
    "chamada de voz", "chamada de audio",
    "chiamata vocale", "chiamata audio",
)
VIDEO_NAMES = (
    "video call", "video",
    "مكالمة فيديو", "فيديو",
    "görüntülü arama",
    "videollamada", "llamada de video",
    "appel vidéo", "appel video",
    "videoanruf", "videocall",
    "chamada de vídeo", "chamada de video",
    "videochiamata",
)
# The word on the button, when the button only says "call".
CALL_NAMES = (
    "call", "اتصال", "مكالمة", "llamar", "appeler", "anrufen", "ligar", "chiama",
)
# The entries of the header menu, for apps that put the call behind "more".
VOICE_MENU = (
    "voice call", "audio call", "llamada de voz", "chamada de voz",
    "sprachanruf", "appel vocal", "sesli arama", "chiamata vocale",
    "مكالمة صوتية",
)
VIDEO_MENU = (
    "video call", "videollamada", "llamada de video", "appel vidéo",
    "videoanruf", "görüntülü arama", "chamada de vídeo", "videochiamata",
    "مكالمة فيديو",
)
MENU_NAMES = (
    "more", "menu", "options", "⋮", "...", "مزيد", "menú", "menu",
)
HANGUP_NAMES = (
    "end call", "hang up", "hangup", "leave call", "end",
    "إنهاء المكالمة", "إنهاء",
    "aramayı bitir", "finalizar llamada", "colgar", "raccrocher",
    "anruf beenden", "encerrar chamada", "termina chiamata",
)
ANSWER_NAMES = (
    "accept", "answer", "قبول", "kabul et", "aceptar", "accepter",
    "annehmen", "atender", "accetta",
)
DECLINE_NAMES = (
    "decline", "reject", "رفض", "reddet", "rechazar", "refuser",
    "ablehnen", "recusar", "rifiuta",
)
SEARCH_WORDS = (
    "search", "بحث", "buscar", "rechercher", "suche", "pesquisar",
    "cerca", "ricerca", "ara", "find",
)
MESSAGE_WORDS = (
    "message", "type a message", "write a message", "nachricht", "nachricht schreiben",
    "mensaje", "escribir un mensaje", "mesaj", "meddelelse", "bericht",
    "messaggio", "scrivi un messaggio", "nhat ky", "رسالة",
)

BUTTON_TYPES = ("Button", "SplitButton", "MenuItem", "Hyperlink", "ListItem")
ROW_TYPES = ("DataItem", "ListItem", "Button", "Hyperlink", "Group", "Text", "Custom")


# ── Names ─────────────────────────────────────────────────────────────────────
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\uFE0F]")
_STATUS_TAIL = re.compile(r"\((?:online|typing[^)]*|last seen[^)]*)\)\s*$")
_PHONE_TAIL = re.compile(r"\+?\d[\d\s().-]{5,}$")


def fold(text) -> str:
    """Lower-case, drop the accents, collapse the whitespace.

    "Yusuf" and "Yûsuf" and "  yusuf " are the same person. Accents and case
    are noise here; the name is what the user said and what the app shows.
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().casefold()


def _clean_name(text) -> str:
    """A chat title reduced to the name of the person."""
    s = _EMOJI.sub(" ", fold(text))
    s = _STATUS_TAIL.sub("", s).strip()
    s = _PHONE_TAIL.sub("", s).strip()
    return s


def names_match(a, b) -> bool:
    """Are these two names the same person?

    "mum" and "Mum (online)" are the same. "Ahmed" and "Ahmed - NS" are the
    same — the app shows what the contact is called, the user says what they
    call them, and both are allowed to be shorter than the other.
    """
    na, nb = _clean_name(a), _clean_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    for short, long in ((na, nb), (nb, na)):
        # a whole word inside the longer name: "mum" in "mum's phone"
        if len(short) >= 2 and re.search(r"(?<!\w)" + re.escape(short) + r"(?!\w)", long):
            return True
        # a longer fragment inside the longer name: "ahmed" in "ahmed ns"
        if len(short) >= 4 and short in long:
            return True
    return False


def name_in_phrase(name, phrase) -> bool:
    """Did the user say this person's name in what they said?"""
    n, p = _clean_name(name), fold(phrase)
    if not n or not p:
        return False
    if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", p):
        return True
    return len(n) >= 4 and n in p


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def _as_float(value, default: float, low: float, high: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, out))


# ── Settings ──────────────────────────────────────────────────────────────────
def cfg() -> dict:
    """The WhatsApp settings, with the defaults filled in.

    Reads the settings tab (⚙ → WHATSAPP). A missing or broken config file
    must never stop the plugin from loading, so every read is guarded.
    """
    values = {}
    try:
        from memory.config_manager import get_plugin_config
        values = get_plugin_config(NS) or {}
    except Exception:
        values = {}
    if not isinstance(values, dict):
        values = {}

    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if values.get(key) is not None:
            out[key] = values[key]

    out["transport"] = str(out["transport"] or "auto").strip().lower()
    if out["transport"] not in ("auto", "desktop", "web"):
        out["transport"] = "auto"
    out["app_name"] = str(out["app_name"] or DEFAULTS["app_name"]).strip() or DEFAULTS["app_name"]
    out["verify_contact"] = _as_bool(out["verify_contact"])
    out["launch_wait"] = _as_float(out["launch_wait"], DEFAULTS["launch_wait"], 2.0, 120.0)
    out["search_wait"] = _as_float(out["search_wait"], DEFAULTS["search_wait"], 0.5, 60.0)
    return out


class Chat:
    """The conversation that is open, or that was just opened."""

    __slots__ = ("raw_title", "contact")

    def __init__(self, raw_title: str = "", contact: str = ""):
        self.raw_title = raw_title or ""
        self.contact = contact or ""

    def name(self, contact: str = None) -> str:
        """How this chat should be called in a message to the user."""
        return contact or self.contact or self.raw_title

    def __repr__(self) -> str:
        return f"Chat({self.raw_title!r})"


class _El:
    """One accessibility element, kept as plain values.

    The live objects are COM handles that go stale; what the driver needs
    afterwards is the name, the kind and the place.
    """

    __slots__ = ("info", "name", "ctype", "aid", "left", "top", "right", "bottom")

    def __init__(self, info, name, ctype, aid, left, top, right, bottom):
        self.info = info
        self.name = name
        self.ctype = ctype
        self.aid = aid
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2

    @property
    def cy(self) -> int:
        return (self.top + self.bottom) // 2

    def __repr__(self) -> str:
        return f"_El({self.ctype!r}, {self.name!r}, @({self.left},{self.top}))"


def _match(text: str, keywords) -> bool:
    """Does this text mean one of these words?"""
    n = fold(text)
    if not n:
        return False
    for word in keywords:
        w = fold(word)
        if not w:
            continue
        if len(w) <= 3:
            # a short word ("end") must match exactly, or "Send" would do
            if n == w:
                return True
        elif w in n:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Desktop transport — the WhatsApp app for Windows
# ─────────────────────────────────────────────────────────────────────────────
class _Win:
    """One WhatsApp window, with the fast way of asking it questions."""

    def __init__(self, handle: int, pid: int, rect):
        self.handle = handle
        self.pid = pid
        self.rect = rect
        self._wrap = None
        self._cache = (0.0, None, ())

    # -- the window -----------------------------------------------------------
    @property
    def wrapper(self):
        if self._wrap is None:
            from pywinauto import Desktop
            self._wrap = Desktop(backend="uia").window(handle=self.handle).wrapper_object()
        return self._wrap

    @property
    def left(self) -> int:
        return self.rect[0]

    @property
    def top(self) -> int:
        return self.rect[1]

    @property
    def right(self) -> int:
        return self.rect[2]

    @property
    def bottom(self) -> int:
        return self.rect[3]

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    # -- asking it ------------------------------------------------------------
    def elements(self, control_type: str = None, fresh: bool = False):
        """Elements of one kind, filtered by Windows itself.

        The filtering happens in the provider, so the answer comes back
        without dragging the whole tree across the COM boundary.
        """
        stamp, key, cached = self._cache
        now = time.monotonic()
        if not fresh and key == control_type and now - stamp < 0.4:
            return cached
        try:
            kwargs = {"control_type": control_type} if control_type else {}
            infos = self.wrapper.element_info.descendants(**kwargs)
        except Exception:
            infos = []
        out = []
        for info in infos:
            try:
                r = info.rectangle
                out.append(_El(info, info.name or "", info.control_type or "",
                               info.automation_id or "", r.left, r.top, r.right, r.bottom))
            except Exception:
                continue
        self._cache = (now, control_type, out)
        return out

    # -- geometry -------------------------------------------------------------
    def contains(self, el: _El) -> bool:
        """Is this element inside the window, where a person could see it?"""
        return (self.left - 4 <= el.left and el.top >= self.top - 4
                and el.right <= self.right + 4 and el.bottom <= self.bottom + 4)

    def in_top_band(self, el: _El, height: int = 260) -> bool:
        """Is this element in the strip across the top of the window?"""
        return self.contains(el) and (el.top - self.top) < height

    @property
    def list_edge(self) -> int:
        """Where the chat list ends and the conversation begins."""
        return self.left + int(self.width * 0.28)


class WindowsDesktopTransport:
    """Drives the WhatsApp app for Windows through its accessibility tree."""

    key = "desktop"
    label = "WhatsApp for Windows (the desktop app)"

    def __init__(self, conf: dict = None):
        self.conf = dict(conf or {})
        self._wins = (0.0, ())
        self._appx = None
        self._appx_at = 0.0

    # ── can this machine run this at all? ───────────────────────────────────
    def usable(self):
        if sys.platform != "win32":
            return False, "the WhatsApp desktop app only runs on Windows"
        missing = [m for m in ("pywinauto", "psutil")
                   if importlib.util.find_spec(m) is None]
        if missing:
            return False, "pip install " + " ".join(missing)
        return True, ""

    # ── finding the app ──────────────────────────────────────────────────────
    def _wa_pids(self) -> set:
        try:
            import psutil
        except Exception:
            return set()
        pids = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                pid = proc.info.get("pid")
            except Exception:
                continue
            if pid and "whatsapp" in name:
                pids.add(pid)
        return pids

    def _windows(self):
        """WhatsApp's windows, biggest first. Cached for a moment."""
        now = time.monotonic()
        stamp, cached = self._wins
        if cached and now - stamp < 0.3:
            return cached
        pids = self._wa_pids()
        found = []
        try:
            from pywinauto import findwindows
            tops = findwindows.find_elements(backend="win32", top_level_only=True,
                                             visible_only=True)
        except Exception:
            tops = []
        for w in tops:
            try:
                pid = w.process_id
                title = w.name or ""
                r = w.rectangle
            except Exception:
                continue
            if pid not in pids and "whatsapp" not in title.lower():
                continue
            if (r.right - r.left) < 250 or (r.bottom - r.top) < 180:
                continue
            found.append(_Win(w.handle, pid, (r.left, r.top, r.right, r.bottom)))
        found.sort(key=lambda win: win.width * win.height, reverse=True)
        self._wins = (now, tuple(found))
        return self._wins[1]

    def _appx_ids(self):
        """The Store package ids of the WhatsApp app, e.g. Family!App."""
        if self._appx is not None and time.monotonic() - self._appx_at < 300:
            return self._appx
        self._appx_at = time.monotonic()
        self._appx = []
        wanted = re.sub(r"[^A-Za-z0-9]", "", self.conf.get("app_name") or "WhatsApp")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-AppxPackage | Where-Object {{ $_.Name -like '*{wanted}*' }} | "
                 "ForEach-Object { $_.PackageFamilyName + '!App' }"],
                capture_output=True, text=True, timeout=20)
            self._appx = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
        except Exception:
            self._appx = []
        return self._appx

    def _launch(self) -> bool:
        """Start the WhatsApp app, whichever way this machine installed it.

        The Store build answers to its package id; the classic build answers
        to the whatsapp: protocol. Both are tried, because which one exists
        is a property of the machine, not of the code.
        """
        if os.name != "nt":
            return False
        for aumid in self._appx_ids():
            try:
                os.startfile(f"shell:AppsFolder\\{aumid}")
                return True
            except Exception:
                continue
        try:
            os.startfile("whatsapp:")
            return True
        except Exception:
            return False

    def _main_window(self, launch: bool = True):
        """The biggest WhatsApp window, launching the app if it is not running."""
        wins = self._windows()
        if wins:
            return wins[0]
        if not launch or not self._launch():
            return None
        deadline = time.monotonic() + float(self.conf.get("launch_wait") or 12.0)
        while time.monotonic() < deadline:
            time.sleep(0.3)
            wins = self._windows()
            if wins:
                return wins[0]
        return None

    def _focus(self, win: _Win) -> bool:
        """Bring the window forward — keystrokes go to whatever has focus."""
        try:
            import ctypes
            u = ctypes.windll.user32
            if u.IsIconic(win.handle):
                u.ShowWindow(win.handle, 9)      # SW_RESTORE
                time.sleep(0.25)
            try:
                u.SetForegroundWindow(win.handle)
            except Exception:
                pass
            if u.GetForegroundWindow() == win.handle:
                return True
        except Exception:
            pass
        try:
            win.wrapper.set_focus()
            return True
        except Exception:
            return False

    # ── pressing things ──────────────────────────────────────────────────────
    @staticmethod
    def _press(el: _El) -> bool:
        """Press a control: invoke it if it can be invoked, click it if not."""
        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            wrapper = UIAWrapper(el.info)
        except Exception:
            return False
        try:
            wrapper.invoke()
            return True
        except Exception:
            pass
        try:
            wrapper.click_input()
            return True
        except Exception:
            return False

    @staticmethod
    def _paste_text(text: str) -> bool:
        """Put text on the clipboard and paste it — faster and safer than typing."""
        try:
            import pyperclip
            from pywinauto.keyboard import send_keys
            pyperclip.copy(str(text or ""))
            send_keys("^v")
            return True
        except Exception:
            return False

    # ── finding the pieces of the window ─────────────────────────────────────
    def _search_box(self, win: _Win):
        """The search field in the chat list."""
        best = None
        for el in win.elements("Edit"):
            if not win.contains(el) or el.width < 40:
                continue
            if el.left > win.list_edge or (el.top - win.top) > 260:
                continue
            if _match(el.name, SEARCH_WORDS):
                return el
            if best is None or el.top < best.top:
                best = el
        return best

    def _rows(self, win: _Win, contact: str):
        """The chat-list rows whose name mentions this contact."""
        want = fold(contact)
        if not want:
            return []
        hits = []
        for el in win.elements("DataItem"):
            if not win.contains(el):
                continue
            name = fold(el.name)
            if not name or want not in name:
                continue
            hits.append(el)
        hits.sort(key=lambda el: (el.top, el.left))
        return hits

    def _best_row(self, win: _Win, contact: str):
        """The row that is most likely this person's conversation.

        A row that STARTS with the name beats a row that merely mentions it
        ("Ahmed - NS Sunday Missed voice call" beats "BC: Ahmed was here").
        """
        want = fold(contact)
        best, best_key = None, None
        for el in self._rows(win, contact):
            name = fold(el.name)
            starts = 0 if name.startswith(want) else 1
            key = (starts, el.top, el.left)
            if best_key is None or key < best_key:
                best, best_key = el, key
        return best

    def _header_title(self, win: _Win):
        """The name in the header of the open conversation, or ''."""
        best, best_width = "", 0
        for el in win.elements("Text"):
            name = (el.name or "").strip()
            if not name or not win.in_top_band(el):
                continue
            if el.left < win.list_edge:
                continue
            if el.width > best_width:
                best, best_width = name, el.width
        return best

    def _call_button(self, win: _Win, call_type: str):
        """The header button that starts a voice or video call."""
        specific = VOICE_NAMES if call_type == "voice" else VIDEO_NAMES
        other = VIDEO_NAMES if call_type == "voice" else VOICE_NAMES
        buttons = [el for el in win.elements("Button") if win.in_top_band(el)]

        def pick(words):
            for el in buttons:
                if el.left < win.list_edge:
                    continue
                if _match(el.name, words):
                    return el
            return None

        # 1. the button says exactly what we want
        found = pick(specific)
        if found is not None:
            return found
        # 2. the button only says "call" — fine, as long as the other kind is
        #    not on screen too, because then we would be guessing
        generic = pick(CALL_NAMES)
        if generic is not None and pick(other) is None:
            return generic
        return None

    # ── what the plugin asks for ─────────────────────────────────────────────
    def usable_transport(self) -> bool:
        ok, _ = self.usable()
        return ok

    def focus(self) -> bool:
        win = self._main_window()
        if win is None:
            return False
        return self._focus(win)

    def current_chat(self):
        """The conversation that is open right now."""
        win = self._main_window()
        if win is None:
            return None
        self._focus(win)
        title = self._header_title(win)
        return Chat(title, "") if title else None

    def open_chat(self, contact: str, conf: dict = None, before: str = ""):
        """Open somebody's conversation and return it, or None.

        The search is done with the keyboard — Ctrl+K puts the cursor in the
        search field — because that costs milliseconds where a query costs
        seconds. Only the row itself is looked up.
        """
        conf = dict(conf or self.conf)
        win = self._main_window()
        if win is None:
            return None
        self._focus(win)

        # the conversation may already be open
        title = self._header_title(win)
        if title and names_match(contact, title):
            return Chat(title, contact)

        try:
            from pywinauto.keyboard import send_keys
            send_keys("^k")                       # WhatsApp: jump to the search
            time.sleep(0.12)
            send_keys("^a{DELETE}")
            if not self._paste_text(contact):
                return None
        except Exception:
            return None

        # let the list filter itself
        time.sleep(0.45)

        deadline = time.monotonic() + float(conf.get("search_wait") or 4.0)
        row = self._best_row(win, contact)
        while row is None and time.monotonic() < deadline:
            time.sleep(0.15)
            row = self._best_row(win, contact)
        if row is None:
            return None

        if not self._press(row):
            return None
        time.sleep(0.6)

        title = self._header_title(win)
        if title and names_match(contact, title):
            return Chat(title, contact)
        # the title could not be read — say so rather than pretend
        return Chat(title or "", contact)

    def press_call(self, chat, call_type: str = "voice"):
        """Press the button that starts the call.

        Returns (ok, how) where how is "pressed", "menu" or "no-button".
        """
        win = self._main_window()
        if win is None:
            return False, "no-window"
        self._focus(win)
        button = self._call_button(win, call_type)
        if button is not None:
            return (True, "pressed") if self._press(button) else (False, "no-button")

        # some builds put the call behind the header menu
        menu = None
        for el in win.elements("Button"):
            if win.in_top_band(el) and el.left >= win.list_edge and _match(el.name, MENU_NAMES):
                menu = el
                break
        if menu is None or not self._press(menu):
            return False, "no-button"
        time.sleep(0.4)

        wanted = VOICE_MENU if call_type == "voice" else VIDEO_MENU
        for el in win.elements("MenuItem") + win.elements("ListItem"):
            if _match(el.name, wanted):
                if self._press(el):
                    return True, "menu"
                break
        return False, "no-button"

    def hangup(self) -> bool:
        """Press the button that ends the call, in whichever window shows it."""
        for win in self._windows():
            for el in win.elements("Button"):
                if _match(el.name, HANGUP_NAMES) and self._press(el):
                    return True
        return False

    def answer_call(self) -> bool:
        """Answer an incoming call."""
        for win in self._windows():
            for el in win.elements("Button"):
                if _match(el.name, ANSWER_NAMES) and self._press(el):
                    return True
        return False

    def decline_call(self) -> bool:
        """Decline an incoming call."""
        for win in self._windows():
            for el in win.elements("Button"):
                if _match(el.name, DECLINE_NAMES) and self._press(el):
                    return True
        return False

    def send_message_to(self, receiver: str, message: str):
        """Type a message into a conversation and confirm it left.

        Returns (sent, failure).
        """
        chat = self.open_chat(receiver, self.conf)
        if chat is None or not chat.raw_title or not names_match(receiver, chat.raw_title):
            return False, "could not open the conversation with that contact"
        return self.send_message_in_open_chat(receiver, message)

    def send_message_in_open_chat(self, receiver: str, message: str):
        """Send into the already-open, verified chat without searching twice."""
        if not str(message or "").strip():
            return False, "the message is empty"
        win = self._main_window()
        if win is None:
            return False, "the WhatsApp window disappeared"
        title = self._header_title(win)
        if not title or not names_match(receiver, title):
            return False, "the active conversation does not match the recipient"
        self._focus(win)

        field = self._message_box(win)
        if field is None:
            return False, "the message box was not found"
        expected = " ".join(str(message or "").split())
        before_count = sum(
            " ".join((el.name or "").split()) == expected
            for el in win.elements("Text", fresh=True)
        )
        if not self._press(field):
            return False, "could not click into the message box"
        if not self._paste_text(message):
            return False, "could not type the message"
        time.sleep(0.25)
        try:
            from pywinauto.keyboard import send_keys
            send_keys("{ENTER}")
        except Exception as exc:
            return False, f"could not press send ({type(exc).__name__})"

        # A send is verified against the visible conversation, not the Edit's
        # accessible name: WhatsApp often leaves that name empty before and
        # after sending, which made an empty field a false-positive.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            win2 = self._main_window()
            if win2 is None:
                return False, "the WhatsApp window disappeared while sending"
            title2 = self._header_title(win2)
            if not title2 or not names_match(receiver, title2):
                return False, "the active conversation changed while sending"
            after_count = sum(
                " ".join((el.name or "").split()) == expected
                for el in win2.elements("Text", fresh=True)
            )
            if after_count > before_count:
                return True, ""
        return False, "WhatsApp did not confirm the message in the active conversation"

    def _message_box(self, win: _Win):
        """The field the message is typed into."""
        floor = win.top + int(win.height * 0.6)
        best, best_width = None, 0
        for el in win.elements("Edit"):
            if not win.contains(el) or el.top < floor:
                continue
            if el.left < win.list_edge:
                continue
            if _match(el.name, MESSAGE_WORDS):
                return el
            if el.width > best_width:
                best, best_width = el, el.width
        return best

    # ── the check button in the settings tab ─────────────────────────────────
    def diagnose(self, conf: dict = None):
        """What can this machine see? Returns (ok, lines)."""
        conf = dict(conf or self.conf)
        lines = []
        try:
            wins = self._windows()
        except Exception as e:
            return False, [f"  ✗ the window lookup failed: {e}"]

        if not wins:
            lines.append("  ✗ no WhatsApp window is open")
            if self._launch():
                lines.append("  · launched WhatsApp — run the check again")
            else:
                lines.append("  · could not launch WhatsApp on this machine")
            return False, lines

        win = wins[0]
        lines.append(f"  · window {win.width}x{win.height} at ({win.left},{win.top})")
        try:
            self._focus(win)
        except Exception:
            pass

        try:
            els = win.elements("Button", fresh=True)
            buttons = [el for el in els if win.contains(el)]
            lines.append(f"  · {len(buttons)} buttons visible")
        except Exception as e:
            lines.append(f"  ✗ the button lookup failed: {e}")
            return False, lines

        names = {}
        for el in buttons:
            names[el.name] = names.get(el.name, 0) + 1
        interesting = sorted(n for n in names if n.strip())
        if interesting:
            lines.append("  · button names: " + ", ".join(repr(n) for n in interesting[:40]))
        else:
            lines.append("  · no button has a name the driver can read")

        ok = any(_match(n, VOICE_NAMES + VIDEO_NAMES + CALL_NAMES) for n in interesting)
        if not ok:
            lines.append("  · no call button found by name — a language may be missing; "
                         "the names above are what to add")
        return ok, lines


# ─────────────────────────────────────────────────────────────────────────────
#  Web transport — WhatsApp Web in a browser
# ─────────────────────────────────────────────────────────────────────────────
class WebTransport:
    """Drives WhatsApp Web in a Playwright browser."""

    key = "web"
    label = "WhatsApp Web (a browser window)"

    def __init__(self, conf: dict = None):
        self.conf = dict(conf or {})
        self._pw = None
        self._ctx = None
        self._page = None

    def usable(self):
        if importlib.util.find_spec("playwright") is None:
            return False, "pip install playwright"
        try:
            from pathlib import Path
            root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
            if not root.exists():
                root = Path.home() / ".cache" / "ms-playwright"
            if not root.exists() or not any(root.iterdir()):
                return False, "playwright install chromium"
        except Exception:
            pass
        return True, ""

    # -- the browser ----------------------------------------------------------
    def _page_or_open(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise Unsupported(f"playwright is not available ({e})")
        self._pw = sync_playwright().start()
        profile = Path.home() / ".jarvis_whatsapp_web"
        try:
            profile.mkdir(parents=True, exist_ok=True)
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(profile), headless=False, viewport={"width": 1280, "height": 800})
        except Exception as e:
            raise Unsupported(f"the browser could not be started ({e})")
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        self._page = page
        return page

    def _signed_in(self) -> bool:
        page = self._page_or_open()
        try:
            return page.locator("header, [role='banner']").count() > 0
        except Exception:
            return False

    def _require_signed_in(self):
        page = self._page_or_open()
        if not self._signed_in():
            raise Unsupported("WhatsApp Web is not signed in — scan the QR code in the "
                              "browser window that opened")
        return page

    # -- the shared API -------------------------------------------------------
    def focus(self) -> bool:
        try:
            page = self._page_or_open()
            page.bring_to_front()
            return True
        except Exception:
            return False

    def current_chat(self):
        try:
            page = self._require_signed_in()
        except Exception:
            return None
        try:
            title = page.locator("header span[title], header div[title]").first.inner_text()
            return Chat(title.strip(), "")
        except Exception:
            return None

    def open_chat(self, contact: str, conf: dict = None, before: str = ""):
        try:
            page = self._require_signed_in()
        except Exception as e:
            raise Unsupported(str(e))
        try:
            box = page.locator("div[contenteditable='true'][aria-label*='earch' i], "
                                "input[aria-label*='earch' i]").first
            box.click()
            box.fill("")
            box.type(str(contact), delay=30)
            page.wait_for_timeout(1200)
            page.locator(f"span[title^='{contact}'], div[title^='{contact}']").first.click()
            page.wait_for_timeout(900)
            title = ""
            try:
                title = page.locator("header span[title]").first.inner_text().strip()
            except Exception:
                pass
            return Chat(title or contact, contact)
        except Exception:
            return None

    def press_call(self, chat, call_type: str = "voice"):
        try:
            page = self._require_signed_in()
        except Exception as e:
            raise Unsupported(str(e))
        label = "Video call" if call_type == "video" else "Call"
        try:
            page.locator(f"button[aria-label='{label}'], button[title='{label}']").first.click()
            return True, "pressed"
        except Exception:
            return False, "no-button"

    def hangup(self) -> bool:
        try:
            page = self._page_or_open()
            page.locator("button[aria-label*='End call' i]").first.click()
            return True
        except Exception:
            return False

    def answer_call(self) -> bool:
        try:
            page = self._page_or_open()
            page.locator("button[aria-label*='Accept' i], button[aria-label*='Answer' i]").first.click()
            return True
        except Exception:
            return False

    def decline_call(self) -> bool:
        try:
            page = self._page_or_open()
            page.locator("button[aria-label*='Reject' i], button[aria-label*='Decline' i]").first.click()
            return True
        except Exception:
            return False

    def send_message_to(self, receiver: str, message: str):
        try:
            chat = self.open_chat(receiver, self.conf)
        except Exception as e:
            return False, str(e)
        if chat is None:
            return False, "could not open the conversation with that contact"
        try:
            page = self._page_or_open()
            box = page.locator("div[contenteditable='true'][aria-label*='essage' i]").first
            box.click()
            box.fill(str(message))
            box.press("Enter")
            page.wait_for_timeout(600)
            return True, ""
        except Exception as e:
            return False, f"could not send the message ({e})"

    def diagnose(self, conf: dict = None):
        lines = []
        try:
            page = self._page_or_open()
        except Exception as e:
            return False, [f"  ✗ {e}"]
        lines.append(f"  · browser page: {page.title()}")
        if self._signed_in():
            lines.append("  · WhatsApp Web is signed in")
            return True, lines
        lines.append("  ✗ WhatsApp Web is showing the QR code — scan it once and check again")
        return False, lines


# ─────────────────────────────────────────────────────────────────────────────
#  Picking a transport
# ─────────────────────────────────────────────────────────────────────────────
class _Candidate:
    """One transport, as the settings tab sees it."""

    def __init__(self, cls):
        self.cls = cls
        self.label = getattr(cls, "label", cls.__name__)

    def usable(self):
        try:
            return self.cls().usable()
        except Exception as e:
            return False, str(e)

    def __call__(self):
        return self.cls()


TRANSPORTS = (WindowsDesktopTransport, WebTransport)


def transports_for_diagnosis(conf: dict = None):
    """Every transport this system could use, for the check button."""
    return [_Candidate(cls) for cls in TRANSPORTS]


def choose(conf: dict = None):
    """The transport class to use, and why if there is none. (cls, why)"""
    conf = dict(conf or cfg())
    wanted = str(conf.get("transport") or "auto").lower()
    desktop = WindowsDesktopTransport
    web = WebTransport

    if wanted == "desktop":
        return desktop, ""
    if wanted == "web":
        return web, ""

    # auto: the desktop app first, the browser second
    ok, why = desktop().usable()
    if ok:
        return desktop, ""
    web_ok, web_why = web().usable()
    if web_ok:
        return web, ""
    return None, why or web_why or "no way to reach WhatsApp on this machine"


def get(conf: dict = None):
    """A transport to drive, and why if there is none. (transport, why)"""
    try:
        conf = dict(conf or cfg())
        cls, why = choose(conf)
    except Exception as e:
        return None, f"the WhatsApp settings could not be read ({e})"
    if cls is None:
        return None, why
    try:
        return cls(conf), ""
    except Exception as e:
        return None, str(e)
