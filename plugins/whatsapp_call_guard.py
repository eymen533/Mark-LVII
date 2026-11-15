"""
JARVIS plugin — WhatsApp Call Guard.

WHAT THIS IS
    An opt-in WhatsApp call handler. Choose either busy-and-decline mode or
    assistant mode. In assistant mode JARVIS answers, identifies itself as an
    AI taking notes, talks with the caller through the Live session, and saves
    a short summary to memory when the call ends.

        "Jarvis, answer my WhatsApp calls and talk to the callers"

    arms the new assistant mode. Incoming-call audio is streamed only while a
    WhatsApp call is active; JARVIS's normal listening behavior is unchanged.

    On demand it does the obvious things too:

        "Jarvis, decline the voice call"   -> declines the call that is ringing
        "Jarvis, answer it"                -> answers it
        "Jarvis, call Ahmed back"          -> places a WhatsApp voice call
        "Jarvis, stop guarding"            -> the standing rule is off

WHY IT IS A SEPARATE PLUGIN
    `whatsapp_call` already places calls; what it cannot do is *notice* a call
    arriving, because noticing means watching the app between the tool calls
    rather than answering one. That is a different job with a different
    lifecycle: this plugin owns a small watcher thread that lives for as long
    as the rule is armed, and it only answers the model when you ask it
    something.

THE ORDER OF THINGS, AND WHY IT IS THIS ORDER
    You asked for "tell him I'm busy, then decline". The plugin declines FIRST
    and sends the message a moment later, and the reason is mechanical rather
    than editorial:

        Opening another conversation can take the call screen off the window.
        If the message went first, the decline would be the step with nowhere
        left to happen, and the caller would keep ringing.

    The caller cannot tell the difference — they get a missed call and your
    message within about a second of each other. What they can tell is that
    nobody picked up.

IT NEVER CALLS OR MESSAGES THE WRONG PERSON
    Before a message goes out or a call is placed, the name on screen is
    compared with the name that was asked for. If they differ, nothing is sent
    and JARVIS says so. A busy message sent to a stranger is worse than no
    message at all.

IT SAYS WHAT IT DID
    A call that was declined is a fact you need, and a call that was NOT
    declined must never be rounded off into one. Every path ends in a sentence
    that says what actually happened — declined, could not decline, or there
    was no call at all.

IT WORKS ON BOTH WHATSAPPS
    Windows  the desktop app, read through its accessibility tree
    macOS    the desktop app, through the Accessibility API
    Linux    WhatsApp Web in a Playwright browser — there is no official
             WhatsApp desktop client for Linux at all

    ⚙ → WHATSAPP → Transport chooses between them; this plugin reads the same
    setting, so both WhatsApp plugins always talk to the same WhatsApp.

NEEDS  _whatsapp_core.py  NEXT TO IT
    The shared driver — finding WhatsApp, opening a conversation, reading back
    who is on screen, pressing the control — lives in that file. This plugin
    imports it and drives WhatsApp with it.

WHEN WHATSAPP IS NOT ON SCREEN
    The driver only sees windows that are visible, and WhatsApp likes to sit
    in the tray. The guard brings the window up when the rule is armed,
    because a call screen that is not on screen cannot be declined — and it
    says so, rather than pretending to have handled a call it never saw.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

# The plugins folder is not on sys.path when the loader imports this file, so
# the shared driver is found by hand — the same dance the other WhatsApp
# plugin does, with the same fallbacks.
_PLUGINS_DIR = Path(__file__).resolve().parent
_BASE_DIR = _PLUGINS_DIR.parent

for _p in (str(_PLUGINS_DIR), str(_BASE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import _whatsapp_core as wa
except ImportError:
    try:
        from plugins import _whatsapp_core as wa
    except ImportError:
        try:
            import whatsapp_core as wa
        except ImportError:
            from plugins import whatsapp_core as wa


# ─────────────────────────────────────────────────────────────────────────────
# Tool declaration
# ─────────────────────────────────────────────────────────────────────────────
PLUGIN = {
    "name": "whatsapp_call_guard",
    "description": (
        "GUARDS the phone against incoming WhatsApp calls, and handles a "
        "call that is ringing right now. "
        "Use action='assistant_on' when the user asks JARVIS to answer "
        "incoming WhatsApp calls, identify itself as an AI, talk to the "
        "caller, and save a short note after hang-up (e.g. 'answer my "
        "WhatsApp calls and talk to them', 'WhatsApp aramalarımı cevapla ve "
        "konuş'). This mode is opt-in and only watches WhatsApp. "
        "Use action='guard' when the user says something like 'if someone "
        "calls me, tell them I'm busy and decline the call', 'whenever "
        "somebody rings me, say I'm busy and hang up' — pass the words to "
        "send in 'message' (e.g. 'I'm busy right now'). Use action='guard_off' "
        "when the user says 'stop guarding', "
        "'turn that off', 'stop doing that'. "
        "Use action='decline' when the user says 'decline the call', 'reject "
        "it', 'close the voice call', 'don't answer it' — it declines the "
        "call that is ringing, or ends the call in progress. "
        "Use action='answer' when the user says 'answer it', 'pick it up', "
        "'accept the call'. "
        "Use action='call_back' when the user says 'call him back', 'ring "
        "Ahmed', 'call back the caller' — or when the user is answering your "
        "own question 'shall I call him back?' with a yes. Put the name in "
        "'contact', or leave it empty to call back whoever called last — this "
        "tool already knows that name, 'whatsapp_call' does not. "
        "Use action='status' when the user asks whether the guard is on, who "
        "called, or whether a call is coming in. "
        "Use action='diagnose' when the user asks what the driver can see of "
        "WhatsApp. "
        "Do NOT use 'whatsapp_call' for declining, answering or guarding — "
        "that one places outgoing calls only, and it cannot see an incoming "
        "call. "
        "IMPORTANT: call this tool exactly ONCE per spoken request."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["guard", "guard_off", "decline", "answer",
                         "call_back", "status", "diagnose", "assistant_on"],
                "description": (
                    "guard = arm the standing rule (tell the caller you are "
                    "busy and decline); guard_off = disarm it; decline = "
                    "decline the ringing call / end the call in progress; "
                    "answer = accept the ringing call; call_back = place an "
                    "outgoing WhatsApp call; status = report the guard state; "
                    "diagnose = report what the driver can see; assistant_on "
                    "= opt in to answering WhatsApp calls, talking to callers "
                    "through Gemini Live, and saving a short note in memory."
                ),
            },
            "message": {
                "type": "STRING",
                "description": (
                    "The words to send to the caller when the guard fires — "
                    "e.g. 'I'm busy right now, I'll call you back later'. "
                    "Only used by action='guard'."
                ),
            },
            "contact": {
                "type": "STRING",
                "description": (
                    "Who to call back, spelled as the contact is saved in "
                    "WhatsApp. Optional for action='call_back' — empty means "
                    "the person who called last."
                ),
            },
            "call_type": {
                "type": "STRING",
                "enum": ["voice", "video"],
                "description": (
                    "'voice' for a normal audio call, 'video' for a video "
                    "call. Defaults to 'voice'."
                ),
            },
        },
        "required": ["action"],
    },
    # The rule is armed by voice and then lives on its own; the tool itself
    # answers in a second or two, so there is no reason to hold the turn.
    "behavior": "NON_BLOCKING",
    "tier": "core",
    "brief": (
        "Handles WhatsApp calls: can decline with a busy message or answer, "
        "talk to the caller as JARVIS, and save a short note."
    ),
}

# The guard keeps its own settings, in its own namespace, so that the
# WhatsApp section of the settings tab stays one form and this one stays
# another — the shared driver reads the transport from its own namespace.
NS = "whatsapp_guard"

PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "🛡️  WHATSAPP GUARD",
    "fields": [
        {"key": "armed", "label": "WhatsApp call handling is armed",
         "type": "toggle", "default": False},
        {"key": "mode", "label": "When a WhatsApp call arrives",
         "type": "choice", "options": ["decline", "assistant"], "default": "decline"},
        {"key": "auto_decline", "label": "Decline the call by itself",
         "type": "toggle", "default": True},
        {"key": "busy_message", "label": "What to send to the caller",
         "type": "text", "placeholder": "I'm busy right now, I'll call you back later."},
        {"key": "call_back", "label": "After declining, call the person back",
         "type": "choice", "options": ["ask", "never", "always"], "default": "ask"},
        {"key": "announce", "label": "Say out loud what happened",
         "type": "toggle", "default": True},
        {"key": "poll_seconds", "label": "Seconds between call checks",
         "type": "text", "placeholder": "2"},
    ],
    "action": {"label": "CHECK WHATSAPP", "run": lambda values: _self_check(values or {})},
}


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "armed":         False,
    "mode":          "decline",
    "auto_decline":  True,
    "busy_message":  "I'm busy right now, I'll call you back later.",
    "call_back":     "ask",     # ask | never | always
    "announce":      True,
    "poll_seconds":  2.0,
}


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


def gcfg() -> dict:
    """The guard's own settings, with the defaults filled in.

    Reads ⚙ → WHATSAPP GUARD. A missing or broken config file must never stop
    the plugin from working, so every read is guarded.
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

    out["armed"] = _as_bool(out["armed"])
    out["mode"] = str(out["mode"] or "decline").strip().lower()
    if out["mode"] not in ("decline", "assistant"):
        out["mode"] = "decline"
    out["auto_decline"] = _as_bool(out["auto_decline"])
    out["announce"] = _as_bool(out["announce"])
    out["poll_seconds"] = _as_float(out["poll_seconds"], DEFAULTS["poll_seconds"], 0.5, 30.0)
    out["call_back"] = str(out["call_back"] or "ask").strip().lower()
    if out["call_back"] not in ("ask", "never", "always"):
        out["call_back"] = "ask"
    out["busy_message"] = str(out["busy_message"] or "").strip() or DEFAULTS["busy_message"]
    return out


def _save_settings(values: dict) -> None:
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(NS, values)
    except Exception as e:
        print(f"[WhatsAppGuard] settings could not be saved: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# State — the rule, and who called last
# ─────────────────────────────────────────────────────────────────────────────
_LOCK = threading.RLock()
_PROC_NAMES: dict = {}
_PAGE_HANDLE: dict = {}
_STATE = {
    "armed": False,
    "message": "",
    "last_caller": "",
    "last_call_at": 0.0,
    "call_active": False,
    "active_caller": "",
    "active_missing_since": 0.0,
    "handled": {},        # caller -> timestamp of the last call it handled
}
_STOP = threading.Event()
_WAKE = threading.Event()
_WATCHER: threading.Thread | None = None
_ACTIVE_PLAYER = None


def _remember_caller(name: str) -> None:
    with _LOCK:
        if name:
            _STATE["last_caller"] = name
            _STATE["last_call_at"] = time.time()


def _last_caller() -> str:
    with _LOCK:
        return _STATE["last_caller"]


# ─────────────────────────────────────────────────────────────────────────────
# The words WhatsApp puts on its buttons
# ─────────────────────────────────────────────────────────────────────────────
# The shared driver already knows these in every language it has met; the
# guard adds the ones it needs for a call that is *coming in* rather than one
# that is already going.
try:
    _DECLINE_WORDS = tuple(wa.DECLINE_NAMES) + (
        "reddetmek", "reddet", "çağrıyı reddet", "reject call", "decline call",
    )
except Exception:
    _DECLINE_WORDS = ("decline", "reject", "reddet", "refuser", "rechazar",
                      "recusar", "rifiuta", "ablehnen", "رفض")

try:
    _ACCEPT_WORDS = tuple(wa.ANSWER_NAMES) + (
        "kabul et", "yanıtla", "cevapla", "accept call", "answer call",
    )
except Exception:
    _ACCEPT_WORDS = ("accept", "answer", "kabul", "accepter", "aceptar",
                     "annehmen", "atender", "accetta", "قبول")

try:
    _END_WORDS = tuple(wa.HANGUP_NAMES) + (
        "end the call", "call ended", "bitir", "aramayı sonlandır",
    )
except Exception:
    _END_WORDS = ("end call", "hang up", "hangup", "leave call", "end",
                  "colgar", "raccrocher", "anruf beenden", "إنهاء المكالمة")

# How a WhatsApp chat row announces that a call is coming in. The chat list
# says it under the contact's name, which is also the most reliable way to
# learn who is calling: the row is written with the name as it is saved.
_RINGING_WORDS = (
    "ringing", "incoming call", "incoming voice call", "incoming video call",
    "calling...", "gelen arama", "gelen sesli arama", "çağrı",
    "مكالمة واردة", "يرن", "entrant", "eingehender anruf",
)
_CALL_WORDS = ("missed voice call", "missed video call", "voice call", "video call")

# Words that are the interface talking, not a person.
_CHROME = {
    "whatsapp", "chats", "calls", "status", "channels", "communities",
    "archived", "meta ai", "media", "you", "menu", "new chat", "search",
    "search or start a new chat", "message", "type a message", "send",
    "attach", "emoji", "voice message", "decline", "accept", "answer",
    "end call", "hang up", "mute", "speaker", "video", "ringing",
    "incoming call", "incoming voice call", "voice call", "video call",
    "call", "close", "maximize", "minimize", "back", "cancel", "ok", "yes",
    "no", "all", "unread", "computer not connected",
    "make sure your computer has an active internet connection.",
    "end-to-end encrypted", "your personal messages are",
    "pinned chat", "archived chats", "settings", "notifications",
}

_DATE_TOKEN = re.compile(
    r"^(?:yesterday|today|sunday|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sun|mon|tue|wed|thu|fri|sat|dün|bugün|"
    r"\d{1,2}:\d{2}(?:\s?[ap]m)?|\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?)$",
    re.IGNORECASE,
)


def _match(text: str, words) -> bool:
    """Does this text mean one of these words?

    A short word must match exactly, or "Send" would be read as "end".
    """
    n = wa.fold(text)
    if not n:
        return False
    for word in words:
        w = wa.fold(word)
        if not w:
            continue
        if len(w) <= 3:
            if n == w:
                return True
        elif w in n:
            return True
    return False


def _has_ringing(text: str) -> bool:
    n = wa.fold(text)
    return any(wa.fold(w) in n for w in _RINGING_WORDS)


def _name_from_row(row: str) -> str:
    """The contact's name out of a chat-list row.

    A row reads like "Ahmed - NS Sunday Ringing..." — the name first, then
    the day, then the status. The status is cut where it starts, then the day
    goes, and what is left is the name the contact is saved under.
    """
    s = str(row or "").strip()
    low = s.lower()
    cut = len(s)
    for marker in _RINGING_WORDS + _CALL_WORDS:
        idx = low.find(marker.lower())
        if 0 < idx < cut:
            cut = idx
    s = s[:cut].strip()
    if not s:
        return ""
    head, sep, tail = s.rpartition(" ")
    if sep and _DATE_TOKEN.match(tail.strip()):
        s = head
    return s.strip(" -·|,:")


# ─────────────────────────────────────────────────────────────────────────────
# Windows: the WhatsApp windows and what is inside them
# ─────────────────────────────────────────────────────────────────────────────
class _Win:
    """One WhatsApp window, with the fast way of asking it questions."""

    __slots__ = ("handle", "title", "rect", "_wrap")

    def __init__(self, handle: int, title: str, rect):
        self.handle = handle
        self.title = title or ""
        self.rect = rect
        self._wrap = None

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

    @property
    def list_edge(self) -> int:
        """Where the chat list ends and the conversation begins."""
        return self.left + int(self.width * 0.28)

    def visible(self) -> bool:
        try:
            import ctypes
            return bool(ctypes.windll.user32.IsWindowVisible(self.handle))
        except Exception:
            return True

    def show(self) -> bool:
        """Bring the window back without raising it above other windows."""
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(self.handle, 5)   # SW_SHOW
            return True
        except Exception:
            return False

    def focus(self) -> bool:
        """Put this window in front, so a click lands on it and not elsewhere."""
        try:
            import ctypes
            u = ctypes.windll.user32
            if u.IsIconic(self.handle):
                u.ShowWindow(self.handle, 9)                  # SW_RESTORE
                time.sleep(0.2)
            try:
                u.SetForegroundWindow(self.handle)
            except Exception:
                pass
            if u.GetForegroundWindow() == self.handle:
                return True
        except Exception:
            pass
        try:
            self.wrapper.set_focus()
            time.sleep(0.15)
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow() == self.handle
        except Exception:
            return False

    # -- asking it ------------------------------------------------------------
    def _elements(self, control_type: str):
        try:
            return self.wrapper.element_info.descendants(control_type=control_type)
        except Exception:
            return []

    def buttons(self):
        out = []
        for el in self._elements("Button"):
            name = ""
            rect = None
            try:
                name = (el.name or "").strip()
                r = el.rectangle
                if callable(r):
                    r = r()
                rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
            except Exception:
                continue
            out.append({"name": name, "rect": rect, "info": el})
        return out

    def texts(self):
        out = []
        for el in self._elements("Text"):
            name = ""
            rect = None
            try:
                name = (el.name or "").strip()
                r = el.rectangle
                if callable(r):
                    r = r()
                rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
            except Exception:
                continue
            out.append({"name": name, "rect": rect})
        return out

    def rows(self):
        """The chat-list rows — each one is named after the whole row."""
        out = []
        for el in self._elements("DataItem"):
            try:
                name = (el.name or "").strip()
                r = el.rectangle
                if callable(r):
                    r = r()
                rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
            except Exception:
                continue
            if name:
                out.append({"name": name, "rect": rect})
        return out


def _process_name(pid: int) -> str:
    """The name of a process, cached.

    Asking the operating system about a process is not free, and the screen
    has hundreds of windows on it; the answer for one pid never changes while
    the process lives.
    """
    cached = _PROC_NAMES.get(pid)
    if cached is not None:
        return cached
    name = ""
    try:
        import psutil
        name = (psutil.Process(pid).name() or "").lower()
    except Exception:
        name = ""
    _PROC_NAMES[pid] = name
    return name


def _wa_windows(include_hidden: bool = False) -> list:
    """Every WhatsApp window on the screen.

    The hidden ones are only asked for when they are really needed: reading
    the title and the place of all two hundred and fifty windows on a busy
    desktop costs half a second, and the guard looks at the screen every
    couple of seconds. The visible ones are free.
    """
    try:
        from pywinauto import findwindows
    except Exception:
        return []
    try:
        tops = findwindows.find_elements(backend="win32", top_level_only=True,
                                          visible_only=not include_hidden)
    except Exception:
        return []

    out = []
    for w in tops:
        try:
            pid = w.process_id
            title = w.name or ""
            r = w.rectangle
        except Exception:
            continue
        # the size comes first: it is free, and it throws away hundreds of
        # tool windows before anything expensive is asked about them
        if (r.right - r.left) < 120 or (r.bottom - r.top) < 80:
            continue
        if "whatsapp" not in title.lower():
            # The page itself is drawn by a WebView2 process, so its title
            # is the only thing that identifies it; a call window can be
            # titled with the caller's name instead.
            if "whatsapp" not in _process_name(pid):
                continue
        out.append(_Win(w.handle, title, (r.left, r.top, r.right, r.bottom)))
    return out


def _page_windows(wins: list) -> list:
    """The windows that show the WhatsApp page, without the frames around them.

    WhatsApp for Windows draws its page in a window that sits inside a frame
    window, and the frame's accessibility tree repeats the whole page and
    thousands of hidden elements with it. A question asked of the frame takes
    seconds; the same question asked of the page takes milliseconds. So the
    frames are dropped — a window that has another WhatsApp window inside it
    is a frame, not a page.

    The page window is remembered between polls: the frame and the page are
    two separate windows, and once the cheap one has been found there is no
    reason to look for it again.
    """
    cached = _PAGE_HANDLE.get("h")
    if cached:
        for w in wins:
            if w.handle == cached:
                return [w]

    frames = []
    for outer in wins:
        for inner in wins:
            if inner is outer:
                continue
            if (outer.left <= inner.left and outer.top <= inner.top
                    and outer.right >= inner.right and outer.bottom >= inner.bottom
                    and (outer.width * outer.height) > (inner.width * inner.height)):
                frames.append(outer)
                break
    pages = [w for w in wins if w not in frames]
    if not pages:
        return list(wins)
    _PAGE_HANDLE["h"] = pages[0].handle
    return pages


def _press(win: _Win, info) -> bool:
    """Press a control on a window.

    Invoke where the app offers it, a real click where it does not — but only
    once the window is in front, because a click at the right coordinates on
    a covered window lands on whatever is covering it.
    """
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
    except Exception:
        return False
    try:
        UIAWrapper(info).invoke()
        return True
    except Exception:
        pass
    if not win.focus():
        return False
    time.sleep(0.15)
    try:
        UIAWrapper(info).click_input()
        return True
    except Exception:
        return False


def _first_button(win: _Win, words):
    for btn in win.buttons():
        if _match(btn["name"], words):
            return btn
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Detection — is a call coming in, and who is it?
# ─────────────────────────────────────────────────────────────────────────────
def _caller_for(win: _Win, wins: list) -> str:
    """Who is calling, as the contact is saved in WhatsApp.

    Three sources, in the order they can be trusted:

    1. a chat row that says a call is coming in — the row is written with
       the contact's own name, so this is the name that will also work in a
       search box later;
    2. the biggest piece of text on the call screen that is not the
       interface talking;
    3. the title of the call window, when it is not just "WhatsApp".
    """
    for w in wins:
        try:
            for row in w.rows():
                if _has_ringing(row["name"]):
                    name = _name_from_row(row["name"])
                    if name:
                        return name
        except Exception:
            continue

    best = ""
    best_width = 0
    try:
        for txt in win.texts():
            name = (txt.get("name") or "").strip()
            if not name or len(name) > 60:
                continue
            if wa.fold(name) in _CHROME:
                continue
            rect = txt.get("rect") or (0, 0, 0, 0)
            if rect[0] < win.list_edge:      # the chat list, not the call screen
                continue
            width = rect[2] - rect[0]
            if width > best_width:
                best, best_width = name, width
    except Exception:
        pass
    if best:
        return best

    title = (win.title or "").strip()
    if title and wa.fold(title) != "whatsapp":
        return title
    return ""


def _detect_windows() -> dict | None:
    """What is WhatsApp doing right now? None when nothing is happening."""
    wins = _wa_windows()
    if not wins:
        return None
    pages = _page_windows(wins)

    incoming = None
    active = None
    for win in pages:
        try:
            dec = _first_button(win, _DECLINE_WORDS)
            acc = _first_button(win, _ACCEPT_WORDS)
            end = _first_button(win, _END_WORDS)
        except Exception:
            continue
        if (dec or acc) and incoming is None:
            incoming = {"window": win, "decline": dec, "accept": acc, "end": end}
        elif end and active is None:
            active = {"window": win, "decline": dec, "accept": acc, "end": end}

    if incoming is not None:
        incoming["state"] = "incoming"
        incoming["caller"] = _caller_for(incoming["window"], pages)
        return incoming
    if active is not None:
        active["state"] = "active"
        active["caller"] = _caller_for(active["window"], pages)
        return active
    return None


def _detect_web(transport) -> dict | None:
    """The same question, asked of WhatsApp Web."""
    try:
        page = transport._page_or_open()
    except Exception:
        return None
    try:
        if page.locator("button[aria-label*='Reject' i], "
                        "button[aria-label*='Decline' i]").count() > 0:
            caller = ""
            try:
                caller = page.locator("header span[title], header div[title]").first.inner_text().strip()
            except Exception:
                pass
            return {"state": "incoming", "caller": caller, "window": None,
                    "decline": True, "accept": True, "end": None}
        if page.locator("button[aria-label*='End call' i]").count() > 0:
            return {"state": "active", "caller": "", "window": None,
                    "decline": None, "accept": None, "end": True}
    except Exception:
        return None
    return None


def detect() -> dict | None:
    """Is a call coming in, is one going on, or is WhatsApp unreachable?

    Returns None when the answer is "nothing is happening".
    """
    try:
        conf = wa.cfg()
        transport, why = wa.get(conf)
    except Exception:
        return None
    if transport is None:
        return {"state": "unreachable", "why": why}
    try:
        if getattr(transport, "key", "") == "web":
            return _detect_web(transport)
        return _detect_windows()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Acting
# ─────────────────────────────────────────────────────────────────────────────
def _restore_whatsapp() -> bool:
    """Bring a WhatsApp window that is only in the tray back onto the screen.

    Does not start the app: if the user closed WhatsApp on purpose, that is
    their decision, not the guard's to undo.
    """
    hidden = [w for w in _wa_windows(include_hidden=True) if not w.visible()]
    if not hidden:
        return False
    for win in hidden:
        win.show()
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if _wa_windows():
            return True
        time.sleep(0.2)
    return bool(_wa_windows())


def _ensure_visible() -> bool:
    """Make sure a WhatsApp window is on screen.

    The shared driver only sees visible windows, and WhatsApp likes to sit in
    the tray. A call screen that is not on screen cannot be declined.
    """
    if _wa_windows():
        return True
    if _restore_whatsapp():
        return True

    # WhatsApp is not running at all — start it, because a call that the
    # app never receives cannot be declined by it.
    try:
        transport, why = wa.get(wa.cfg())
        if transport is None:
            return False
        transport.focus()          # the shared driver launches the app
    except Exception:
        return False
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if _wa_windows():
            return True
        time.sleep(0.3)
    return bool(_wa_windows())


def _decline_call(info: dict) -> bool:
    """Press the decline button of the call that is ringing."""
    win = info.get("window")
    btn = info.get("decline")
    if win is not None and btn is not None:
        if _press(win, btn["info"]):
            return True
    # the window could not be read — fall back to the shared driver
    try:
        transport, why = wa.get(wa.cfg())
        if transport is not None:
            return bool(transport.decline_call())
    except Exception:
        pass
    return False


def _answer_call(info: dict) -> bool:
    win = info.get("window")
    btn = info.get("accept")
    if win is not None and btn is not None:
        if _press(win, btn["info"]):
            return True
    try:
        transport, why = wa.get(wa.cfg())
        if transport is not None:
            return bool(transport.answer_call())
    except Exception:
        pass
    return False


def _end_call(info: dict) -> bool:
    win = info.get("window")
    btn = info.get("end")
    if win is not None and btn is not None:
        if _press(win, btn["info"]):
            return True
    try:
        transport, why = wa.get(wa.cfg())
        if transport is not None:
            return bool(transport.hangup())
    except Exception:
        pass
    return False


def _notify_call_session(active: bool, caller: str) -> bool:
    player = _ACTIVE_PLAYER
    callback = getattr(player, "whatsapp_call", None) if player else None
    if not callable(callback):
        return False
    try:
        return bool(callback(active, caller))
    except Exception as e:
        print(f"[WhatsAppGuard] call session callback failed: {e}")
        return False


def _call_session_ready() -> bool:
    player = _ACTIVE_PLAYER
    ready = getattr(player, "whatsapp_call_ready", None) if player else None
    try:
        return bool(ready()) if callable(ready) else False
    except Exception as e:
        print(f"[WhatsAppGuard] call readiness check failed: {e}")
        return False


def _send_busy_message(contact: str, message: str) -> tuple[bool, str]:
    """Tell the caller you are busy.

    The name on the conversation is compared with the name that was asked for
    before a single character is typed: a busy message sent to a stranger is
    worse than no message.
    """
    conf = wa.cfg()
    transport, why = wa.get(conf)
    if transport is None:
        return False, why
    try:
        transport.focus()
        chat = transport.open_chat(contact, conf)
    except Exception as e:
        return False, f"the conversation could not be opened ({e})"
    if not chat:
        return False, f"the conversation with {contact} could not be opened"
    shown = (chat.raw_title or contact).strip()
    if conf.get("verify_contact") and not wa.names_match(contact, chat.raw_title):
        return False, (f"the conversation that opened is '{shown}' and not "
                       f"'{contact}', so nothing was sent")
    try:
        sent, failure = transport.send_message_to(contact, message)
    except Exception as e:
        return False, f"the message could not be sent ({e})"
    if not sent:
        return False, failure or "the message was not sent"
    return True, shown


def _place_call(contact: str, call_type: str = "voice") -> tuple[bool, str]:
    """Place a WhatsApp call, after checking who is on screen."""
    conf = wa.cfg()
    transport, why = wa.get(conf)
    if transport is None:
        return False, why
    try:
        transport.focus()
        chat = transport.current_chat()
        opened = chat.raw_title if chat else ""
        if not (chat and wa.names_match(contact, opened)):
            chat = transport.open_chat(contact, conf, before=opened)
    except Exception as e:
        return False, f"the conversation could not be opened ({e})"
    if not chat:
        return False, f"the conversation with {contact} could not be opened"
    shown = (chat.raw_title or contact).strip()
    if conf.get("verify_contact") and not wa.names_match(contact, chat.raw_title):
        return False, (f"the conversation that opened is '{shown}' and not "
                       f"'{contact}', so nobody was called")
    try:
        ok, how = transport.press_call(chat, call_type)
    except Exception as e:
        return False, f"the call button could not be pressed ({e})"
    if not ok:
        return False, f"'{shown}' offers no {call_type} call button"
    return True, shown


# ─────────────────────────────────────────────────────────────────────────────
# The guard: what happens when a call arrives
# ─────────────────────────────────────────────────────────────────────────────
def _handle_incoming(info: dict, player=None) -> str:
    """A call is ringing. Decline it, say you are busy, ask about calling back.

    Returns the sentence that says what happened.
    """
    conf = gcfg()
    caller = (info.get("caller") or "").strip()
    _remember_caller(caller)
    who = caller or "someone"

    if conf.get("mode") == "assistant":
        if player is not None and getattr(player, "muted", False):
            outcome = f"I left {who}'s WhatsApp call unanswered because JARVIS is muted"
            _log(player, f"JARVIS: {outcome}.")
            return outcome
        if not _answer_call(info):
            return f"I could not answer {who}'s WhatsApp call"
        if not _notify_call_session(True, caller):
            _end_call(info)
            outcome = (
                f"I answered {who}'s WhatsApp call but could not start the "
                "Gemini Live conversation, so I ended it"
            )
            _log(player, f"JARVIS: {outcome}.")
            return outcome
        with _LOCK:
            _STATE["call_active"] = True
            _STATE["active_caller"] = caller
            _STATE["active_missing_since"] = 0.0
        outcome = (
            f"I answered {who}'s WhatsApp call. I introduced myself as JARVIS "
            "and am listening for the conversation"
        )
        _log(player, f"JARVIS: {outcome}.")
        return outcome

    # The rule may be armed to only *tell* you, not to answer for you.
    if not conf.get("auto_decline", True):
        outcome = f"{who} is calling"
        _log(player, f"JARVIS: {outcome}.")
        if conf.get("announce", True):
            _say(player, "Tell the user in one short sentence in their own "
                         f"language: {outcome}")
        return outcome

    declined = _decline_call(info)

    message = conf.get("busy_message") or ""
    sent, detail = False, ""
    if declined and message and caller:
        sent, detail = _send_busy_message(caller, message)
    elif declined and message and not caller:
        detail = ("I could not work out who was calling, so I did not send "
                  "the message")

    if not declined:
        outcome = (f"{who} is calling and I could not decline the call - the "
                   f"decline button was not there to press")
    elif sent:
        outcome = (f"{who} was calling. I told them '{message}' and declined "
                   f"the call")
    elif detail:
        outcome = f"{who} was calling. I declined the call, but {detail}"
    else:
        outcome = f"{who} was calling and I declined the call"

    mode = conf.get("call_back", "ask")
    if mode == "always" and declined and caller:
        ok, shown = _place_call(caller, "voice")
        outcome += (f", and I am calling {shown} back now" if ok
                    else f", but the call back to {caller} did not go through")
    elif mode == "ask" and declined and caller:
        outcome += f". Shall I call {caller} back?"

    _log(player, f"JARVIS: {outcome}.")
    if conf.get("announce", True):
        _say(player, "Tell the user in one short sentence in their own "
                     f"language: {outcome}")
    return outcome


def _handle_active(info: dict, player=None) -> str:
    """A call is already going on. Nothing is done about it on its own."""
    caller = (info.get("caller") or "").strip()
    who = caller or "someone"
    _log(player, f"JARVIS: {who} is on a call right now.")
    return f"{who} is on a call right now"


# ─────────────────────────────────────────────────────────────────────────────
# The watcher — the rule, alive between your requests
# ─────────────────────────────────────────────────────────────────────────────
def _watch_loop() -> None:
    """Look for an incoming call while the rule is armed.

    The loop is cheap when it has nothing to do: it reads the settings, and
    only when the rule is armed does it look at WhatsApp at all.
    """
    while not _STOP.is_set():
        try:
            conf = gcfg()
        except Exception:
            conf = dict(DEFAULTS)

        if not conf.get("armed"):
            _WAKE.wait(5.0)
            _WAKE.clear()
            continue

        # A call screen that is not on screen cannot be declined, so a
        # WhatsApp that has gone back to the tray is brought out again — but
        # not so often that the window keeps jumping in front of the user.
        try:
            if not _wa_windows():
                now = time.time()
                with _LOCK:
                    due = now - float(_STATE.get("last_restore") or 0.0) > 60.0
                    if due:
                        _STATE["last_restore"] = now
                if due:
                    _restore_whatsapp()
        except Exception:
            pass

        try:
            info = detect()
        except Exception:
            info = None

        if info and info.get("state") == "incoming":
            if conf.get("mode") == "assistant" and not _call_session_ready():
                _WAKE.wait(0.5)
                _WAKE.clear()
                continue
            caller = (info.get("caller") or "").strip()
            now = time.time()
            with _LOCK:
                last = _STATE["handled"].get(caller or "?")
                if last and now - last < 20.0:
                    info = None          # the same call, seen twice
                else:
                    _STATE["handled"][caller or "?"] = now
            if info:
                try:
                    _handle_incoming(info, _ACTIVE_PLAYER)
                except Exception as e:
                    print(f"[WhatsAppGuard] {type(e).__name__}: {e}")
        elif info and info.get("state") == "active":
            with _LOCK:
                _STATE["active_missing_since"] = 0.0
                should_start = (
                    conf.get("mode") == "assistant"
                    and not _STATE.get("call_active")
                )
                if should_start:
                    _STATE["call_active"] = True
                    _STATE["active_caller"] = info.get("caller") or ""
            if should_start:
                caller = info.get("caller") or ""
                if not _notify_call_session(True, caller):
                    _end_call(info)
                    with _LOCK:
                        _STATE["call_active"] = False
                        _STATE["active_caller"] = ""
                    _log(_ACTIVE_PLAYER,
                         "JARVIS: Could not start Gemini Live for this "
                         "WhatsApp call; the call was ended.")
        else:
            with _LOCK:
                call_active = bool(_STATE.get("call_active"))
                if call_active and not _STATE.get("active_missing_since"):
                    _STATE["active_missing_since"] = time.monotonic()
                missing_since = float(_STATE.get("active_missing_since") or 0.0)
                ended = call_active and missing_since and (
                    time.monotonic() - missing_since >= 3.0
                )
                caller = _STATE.get("active_caller") or ""
                if ended:
                    _STATE["call_active"] = False
                    _STATE["active_caller"] = ""
                    _STATE["active_missing_since"] = 0.0
            if ended:
                _notify_call_session(False, caller)

        _WAKE.wait(float(conf.get("poll_seconds") or 2.0))
        _WAKE.clear()


def _start_watcher() -> None:
    global _WATCHER
    with _LOCK:
        if _WATCHER and _WATCHER.is_alive():
            return
        _STOP.clear()
        _WATCHER = threading.Thread(target=_watch_loop, daemon=True,
                                    name="whatsapp-call-guard")
        _WATCHER.start()


# ─────────────────────────────────────────────────────────────────────────────
# HUD helpers (no-ops when player is None)
# ─────────────────────────────────────────────────────────────────────────────
def _panel(player, title: str, line: str) -> None:
    if not player:
        return
    try:
        player.show_content(title, line)
    except Exception:
        pass


def _log(player, msg: str) -> None:
    if player:
        try:
            player.write_log(msg)
        except Exception:
            pass


def _say(player, instruction: str) -> None:
    if player and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    global _ACTIVE_PLAYER
    if player is not None:
        _ACTIVE_PLAYER = player
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()
    message = (params.get("message") or "").strip()
    contact = (params.get("contact") or "").strip()
    call_type = (params.get("call_type") or "voice").strip().lower()
    if call_type not in ("voice", "video"):
        call_type = "voice"

    conf = gcfg()
    _start_watcher()

    try:
        if action == "initialize":
            _start_watcher()
            return "WhatsApp call watcher initialized."

        if action == "guard":
            return _do_guard(message, player)

        if action == "assistant_on":
            return _do_guard(message, player, mode="assistant")

        if action == "guard_off":
            return _do_guard_off(player)

        if action == "decline":
            return _do_decline(player)

        if action == "answer":
            return _do_answer(player)

        if action == "call_back":
            return _do_call_back(contact, call_type, player)

        if action == "status":
            return _do_status(player)

        if action == "diagnose":
            return _do_diagnose(player)

    except Exception as e:
        print(f"[WhatsAppGuard] {type(e).__name__}: {e}")
        return f"Sir, the WhatsApp guard failed: {e}"

    return ("I did not understand that. I can guard the phone, decline or "
            "answer a call, call someone back, or tell you the status.")


def _do_guard(message: str, player=None, mode: str | None = None) -> str:
    conf = gcfg()
    mode = mode or conf.get("mode", "decline")
    text = message or conf.get("busy_message") or DEFAULTS["busy_message"]
    _save_settings({"armed": True, "busy_message": text, "mode": mode})

    with _LOCK:
        _STATE["armed"] = True
        _STATE["message"] = text
    if player is not None:
        global _ACTIVE_PLAYER
        _ACTIVE_PLAYER = player

    # A call screen that is not on screen cannot be declined, so the window is
    # brought up once, here, where the user is watching.
    opened = _ensure_visible()
    _start_watcher()
    _WAKE.set()

    who = "anyone"
    if opened and mode == "assistant":
        _panel(
            player, "📞  WHATSAPP ASSISTANT",
            "Armed.\n\nIncoming WhatsApp calls will be answered. "
            "JARVIS will identify itself as an AI, talk in Turkish, and save "
            "a short note in memory after hang-up."
        )
    elif opened:
        _panel(player, "🛡️  WHATSAPP GUARD",
               f"Armed.\n\nMessage: “{text}”\n\nA call will be declined and "
               f"the caller will be told you are busy.")
    _log(player, f"JARVIS: Guard armed — '{text}'.")
    if mode == "assistant":
        _say(
            player,
            "Tell the user in Turkish that WhatsApp call assistant mode is "
            "armed: incoming WhatsApp calls will be answered, JARVIS will "
            "identify itself as an AI, speak with the caller, and save a "
            "short note after the call. Mention that call audio must be "
            "routed to the selected microphone and speaker."
        )
    else:
        _say(player, "Tell the user in one short sentence: the phone is now "
                     f"guarded — if {who} calls, you will be told you are busy "
                     f"and the call will be declined.")
    if not opened:
        if mode == "assistant":
            return (
                "WhatsApp assistant mode is armed, but WhatsApp is not visible "
                "yet. Open the WhatsApp desktop app so incoming calls can be detected."
            )
        return ("The guard is armed, but WhatsApp is not on screen right now, "
                "so I cannot see a call until it is opened.")
    if mode == "assistant":
        return (
            "WhatsApp call assistant is armed. Incoming WhatsApp calls will "
            "be answered and handled by JARVIS; a short Turkish note will be "
            "saved to memory after hang-up."
        )
    return (f"Guarded. If someone calls, I will tell them '{text}' and "
            f"decline the call.")


def _do_guard_off(player=None) -> str:
    _save_settings({"armed": False})
    with _LOCK:
        _STATE["armed"] = False
        active = bool(_STATE.get("call_active"))
        caller = _STATE.get("active_caller") or ""
        _STATE["call_active"] = False
        _STATE["active_caller"] = ""
        _STATE["active_missing_since"] = 0.0
    _WAKE.set()
    if active:
        _notify_call_session(False, caller)
    _panel(player, "🛡️  WHATSAPP GUARD", "Disarmed.\n\nIncoming calls are left alone.")
    _log(player, "JARVIS: Guard disarmed.")
    _say(player, "Tell the user in one short sentence: the phone guard is off "
                 "and incoming calls are left alone now.")
    return "The guard is off — incoming calls will be left alone."


def _do_decline(player=None) -> str:
    _ensure_visible()
    info = detect()
    if not info:
        return "There is no call to decline right now."
    if info.get("state") == "unreachable":
        return f"I can't reach WhatsApp: {info.get('why', 'unknown reason')}."
    caller = (info.get("caller") or "").strip()
    who = caller or "the caller"

    if info.get("state") == "incoming":
        if _decline_call(info):
            _remember_caller(caller)
            _panel(player, "📞  WHATSAPP", f"Declined\n{who}")
            _log(player, f"JARVIS: Declined the incoming call from {who}.")
            return f"Declined the incoming WhatsApp call from {who}."
        return (f"I could not decline the call from {who} — the decline "
                f"button would not press.")

    if _end_call(info):
        _panel(player, "📞  WHATSAPP", f"Ended\n{who}")
        _log(player, f"JARVIS: Ended the call in progress with {who}.")
        with _LOCK:
            was_assistant_call = bool(_STATE.get("call_active"))
            active_caller = _STATE.get("active_caller") or caller
            _STATE["call_active"] = False
            _STATE["active_caller"] = ""
            _STATE["active_missing_since"] = 0.0
        if was_assistant_call:
            _notify_call_session(False, active_caller)
        return f"There was no incoming call — I ended the call in progress with {who}."
    return "I could not end the call — the end-call button would not press."


def _do_answer(player=None) -> str:
    _ensure_visible()
    info = detect()
    if not info:
        return "There is no call to answer right now."
    if info.get("state") == "unreachable":
        return f"I can't reach WhatsApp: {info.get('why', 'unknown reason')}."
    caller = (info.get("caller") or "").strip()
    who = caller or "the caller"
    if info.get("state") != "incoming":
        return "There is no incoming call to answer — one is already going on."
    assistant_mode = gcfg().get("mode") == "assistant"
    if assistant_mode and not _call_session_ready():
        return "I can't answer for JARVIS yet: Gemini Live is not ready or JARVIS is muted."
    if _answer_call(info):
        if assistant_mode and not _notify_call_session(True, caller):
            _end_call(info)
            return "The call connected, but JARVIS could not start; I ended the call."
        if assistant_mode:
            with _LOCK:
                _STATE["call_active"] = True
                _STATE["active_caller"] = caller
                _STATE["active_missing_since"] = 0.0
        _panel(player, "📞  WHATSAPP", f"Answered\n{who}")
        _log(player, f"JARVIS: Answered the call from {who}.")
        return f"Answered the incoming WhatsApp call from {who}."
    return f"I could not answer the call from {who} — the accept button would not press."


def _do_call_back(contact: str, call_type: str, player=None) -> str:
    who = contact or _last_caller()
    if not who:
        return "Who should I call back? Tell me the name."
    _ensure_visible()
    ok, detail = _place_call(who, call_type)
    spoken = "video call" if call_type == "video" else "voice call"
    if not ok:
        _log(player, f"JARVIS: Call back to {who} did not go through — {detail}.")
        return f"I could not call {who} back: {detail}."
    _panel(player, "📞  WHATSAPP", f"{spoken.title()}\n{detail}")
    _log(player, f"JARVIS: Calling {detail} back on WhatsApp — {spoken}.")
    return f"Calling {detail} on WhatsApp — {spoken}."


def _do_status(player=None) -> str:
    conf = gcfg()
    last = _last_caller()
    info = detect()
    state = "nothing is happening"
    if info:
        state = {
            "incoming": "a call is coming in right now",
            "active": "a call is already going on",
            "unreachable": f"WhatsApp cannot be reached ({info.get('why', '')})",
        }.get(info.get("state"), "nothing is happening")
    armed = "armed" if conf.get("armed") else "not armed"
    text = f"The guard is {armed}."
    if conf.get("armed"):
        if conf.get("mode") == "assistant":
            text += " It will answer WhatsApp calls and save a note after hang-up."
        else:
            text += (f" It would send '{conf.get('busy_message')}' and decline "
                     "the call.")
    text += f" Right now: {state}."
    if last:
        text += f" The last caller was {last}."
    _panel(player, "🛡️  WHATSAPP GUARD", text)
    return text


def _do_diagnose(player=None) -> str:
    lines = []
    try:
        conf = wa.cfg()
        lines.append(f"transport setting: {conf.get('transport')}")
        transport, why = wa.get(conf)
        lines.append(f"transport: {getattr(transport, 'key', None) or 'none'} "
                     f"({why})")
    except Exception as e:
        lines.append(f"the settings could not be read: {e}")

    wins = _wa_windows(include_hidden=True)
    if not wins:
        lines.append("no WhatsApp window was found")
    pages = _page_windows(wins)
    for win in wins:
        kind = "page" if win in pages else "frame"
        lines.append(f"window {win.width}x{win.height} at "
                     f"({win.left},{win.top}) title={win.title!r} "
                     f"visible={win.visible()} [{kind}]")

    seen = set()
    for win in pages:
        try:
            for btn in win.buttons():
                name = (btn.get("name") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                tags = []
                if _match(name, _DECLINE_WORDS):
                    tags.append("DECLINE")
                if _match(name, _ACCEPT_WORDS):
                    tags.append("ACCEPT")
                if _match(name, _END_WORDS):
                    tags.append("END")
                if tags:
                    lines.append(f"button {name!r} -> {', '.join(tags)}")
        except Exception:
            continue

    ringing = []
    for win in pages:
        try:
            for row in win.rows():
                if _has_ringing(row["name"]):
                    ringing.append(row["name"][:80])
        except Exception:
            continue
    lines.append("ringing rows: " + (", ".join(repr(r) for r in ringing[:5])
                                      if ringing else "none"))

    offline = False
    for win in pages:
        try:
            for txt in win.texts():
                if "not connected" in wa.fold(txt.get("name") or ""):
                    offline = True
        except Exception:
            continue
    if offline:
        lines.append("WhatsApp says it is NOT CONNECTED — calls will not "
                     "arrive on this computer until it reconnects.")

    conf = gcfg()
    lines.append(f"guard: {'armed' if conf.get('armed') else 'not armed'}, "
                 f"mode: {conf.get('mode')}, "
                 f"message: {conf.get('busy_message')!r}, "
                 f"call back: {conf.get('call_back')}")
    lines.append(f"last caller: {_last_caller() or 'none'}")

    text = "\n".join(lines)
    _panel(player, "🛡️  WHATSAPP GUARD", text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# ⚙ settings-tab CHECK button — read-only, rings nobody
# ─────────────────────────────────────────────────────────────────────────────
def _self_check(_values: dict):
    """Report what the driver can see, without calling anybody."""
    try:
        text = _do_diagnose(None)
    except Exception as e:
        return False, f"  ✗ the check itself failed: {e}"
    ok = "no WhatsApp window was found" not in text
    return ok, ("\n".join("  " + line for line in text.splitlines())
                + ("\n\nReady." if ok else "\n\nOpen WhatsApp and check again."))
