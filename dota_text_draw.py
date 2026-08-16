# -*- coding: utf-8 -*-
"""
Dota Text Draw
Рисует текст на миникарте Dota 2 движением мыши с зажатым Ctrl+ЛКМ.

Хоткеи (по умолчанию, меняются в настройках Ctrl+Alt+C):
  Ctrl+Alt+D  — открыть оверлей ввода текста (Enter — нарисовать, Esc — отмена)
  Ctrl+Alt+C  — открыть настройки (скорость и горячие клавиши)

Запуск:
  python dota_text_draw.py              # основная работа
  python dota_text_draw.py --test       # двигает мышь без кнопок (проверка геометрии)
  python dota_text_draw.py --selftest   # проверка конвертации текста в штрихи

Настройки HUD/позиции — в константах ниже.
"""

import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk

try:
    import freetype
except ImportError:
    freetype = None

try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.recordingPen import DecomposingRecordingPen
except ImportError:
    TTFont = None
    DecomposingRecordingPen = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
FONT_PATH = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'arial.ttf')

MAX_TEXT_WIDTH_FRAC = 0.8   # доля ширины миникарты, занимаемая текстом
STEP_PX = 2.0               # шаг интерполяции точек мыши, px (меньше = плотнее линия)
STEP_DELAY = 0.008          # пауза между точками, сек (больше = надёжнее, но медленнее)
PRESS_DELAY = 0.08          # пауза после нажатия/отпускания ЛКМ, сек
SIMPLIFY_EPS = 0.0          # >0 — упрощение прямых участков (быстрее, но может рвать линии);
                            # 0 — плотное рисование, максимум качества (медленнее)
MAX_JUMP_PX = 8.0           # максимум между соседними точками, чтобы не было разрывов (px)
MAX_CHARS = 10              # ограничение длины вводимого текста

# Пресеты скорости, выбираются в настройках (Ctrl+Alt+C) и хранятся в config.json
SPEED_PRESETS = {
    'slow': {'label': 'Максимальное качество (медленно)',
             'step_px': 2.0, 'step_delay': 0.008, 'press_delay': 0.08, 'simplify_eps': 0.0},
    'medium': {'label': 'Средняя',
               'step_px': 2.0, 'step_delay': 0.006, 'press_delay': 0.06, 'simplify_eps': 0.0},
    'fast': {'label': 'Максимальная скорость (текст чуть грубее)',
             'step_px': 2.0, 'step_delay': 0.003, 'press_delay': 0.05, 'simplify_eps': 0.8},
}
DEFAULT_SPEED = 'slow'


def apply_speed(name):
    """Применяет пресет скорости к глобальным настройкам рисования."""
    global STEP_PX, STEP_DELAY, PRESS_DELAY, SIMPLIFY_EPS
    p = SPEED_PRESETS.get(name, SPEED_PRESETS[DEFAULT_SPEED])
    STEP_PX = p['step_px']
    STEP_DELAY = p['step_delay']
    PRESS_DELAY = p['press_delay']
    SIMPLIFY_EPS = p['simplify_eps']

TEST_MODE = '--test' in sys.argv

# --------------------------------------------------------------------------
# Win32 API
# --------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = ctypes.c_bool
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
user32.AttachThreadInput.restype = ctypes.c_bool
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = ctypes.c_bool
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong, ctypes.c_uint]
user32.RegisterHotKey.restype = ctypes.c_bool
user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.UnregisterHotKey.restype = ctypes.c_bool
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint


class MSG(ctypes.Structure):
    _fields_ = [
        ('hwnd', ctypes.c_void_p),
        ('message', ctypes.c_uint),
        ('wParam', ctypes.c_size_t),
        ('lParam', ctypes.c_size_t),
        ('time', ctypes.c_ulong),
        ('pt', ctypes.c_long * 2),
    ]

user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
HOTKEY_DRAW_ID = 1
HOTKEY_SETTINGS_ID = 2

# Хоткеи по умолчанию; пользователь может переопределить в настройках (config.json)
DEFAULT_HOTKEYS = {
    'draw': {'mods': ['control', 'alt'], 'key': 'd'},
    'settings': {'mods': ['control', 'alt'], 'key': 'c'},
}

MOD_NAME_TO_FLAG = {'control': MOD_CONTROL, 'alt': MOD_ALT, 'shift': MOD_SHIFT}

_SPECIAL_VK = {
    'space': 0x20, 'tab': 0x09, 'return': 0x0D, 'escape': 0x1B,
    'backspace': 0x08, 'delete': 0x2E, 'home': 0x24, 'end': 0x23,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    'prior': 0x21, 'next': 0x22,
}


def _keysym_to_vk(keysym):
    """Преобразует keysym из tkinter в Windows virtual-key код."""
    s = str(keysym).lower()
    if len(s) == 1 and s.isalpha():
        return ord(s.upper())
    if len(s) == 1 and s.isdigit():
        return ord(s)
    if s.startswith('f') and s[1:].isdigit():
        n = int(s[1:])
        if 1 <= n <= 12:
            return 0x70 + n - 1
    return _SPECIAL_VK.get(s)


def _mods_to_flags(mods):
    flags = 0
    for m in mods or []:
        flags |= MOD_NAME_TO_FLAG.get(m, 0)
    return flags


def format_hotkey(hk):
    """'control+alt' + 'd' -> 'Ctrl+Alt+D'."""
    names = {'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}
    mods = '+'.join(names.get(m, m.title()) for m in (hk.get('mods') or []))
    key = hk.get('key', '')
    key = key.upper() if len(key) == 1 else key.title()
    return mods + '+' + key if mods else key

# Ввод мыши через SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx', ctypes.c_long),
        ('dy', ctypes.c_long),
        ('mouseData', ctypes.c_ulong),
        ('dwFlags', ctypes.c_ulong),
        ('time', ctypes.c_ulong),
        ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk', ctypes.c_ushort),
        ('wScan', ctypes.c_ushort),
        ('dwFlags', ctypes.c_ulong),
        ('time', ctypes.c_ulong),
        ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', ctypes.c_ulong), ('u', _INPUTUNION)]


def _send_input(inp):
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def mouse_down():
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    _send_input(inp)


def mouse_up():
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_LEFTUP
    _send_input(inp)


def ctrl_down():
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = VK_CONTROL
    _send_input(inp)


def ctrl_up():
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = VK_CONTROL
    inp.ki.dwFlags = KEYEVENTF_KEYUP
    _send_input(inp)


def _set_foreground(hwnd):
    """Best-effort возврат фокуса окну игры."""
    try:
        user32.ShowWindow(hwnd, 9)
        fg = user32.GetForegroundWindow()
        t1 = user32.GetWindowThreadProcessId(fg, None)
        t2 = kernel32.GetCurrentThreadId()
        attached = False
        if t1 and t1 != t2:
            attached = user32.AttachThreadInput(t2, t1, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(t2, t1, False)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Конфиг
# --------------------------------------------------------------------------
def _load_all():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_all(data):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    try:
        rect = _load_all()['minimap']
        return {k: int(v) for k, v in rect.items()}
    except (KeyError, TypeError, ValueError):
        return None


def save_config(rect):
    data = _load_all()
    data['minimap'] = rect
    _save_all(data)


def load_speed():
    try:
        return _load_all().get('speed', DEFAULT_SPEED)
    except (OSError, ValueError):
        return DEFAULT_SPEED


def save_speed(name):
    data = _load_all()
    data['speed'] = name
    _save_all(data)


def load_hotkeys():
    def norm(act, default):
        e = _load_all().get('hotkeys', {}).get(act)
        if (isinstance(e, dict) and isinstance(e.get('mods'), list)
                and isinstance(e.get('key'), str) and e['key']):
            return {'mods': list(e['mods']), 'key': e['key']}
        return dict(default)
    return {act: norm(act, default) for act, default in DEFAULT_HOTKEYS.items()}


def save_hotkeys(hotkeys):
    data = _load_all()
    data['hotkeys'] = {act: {'mods': hk['mods'], 'key': hk['key']}
                       for act, hk in hotkeys.items()}
    _save_all(data)


LOG_PATH = os.path.join(SCRIPT_DIR, 'dota_text_draw.log')


def _single_instance():
    try:
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        h = kernel32.CreateMutexW(None, False, 'DotaTextDraw_Mutex')
        if not h:
            return False
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _setup_streams():
    """Под pythonw (без консоли) перенаправляем вывод в лог-файл."""
    if sys.stdout is None:
        try:
            log = open(LOG_PATH, 'a', encoding='utf-8')
            sys.stdout = log
            sys.stderr = log
        except Exception:
            pass


def _tray_image():
    from PIL import ImageFont
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(41, 128, 185, 255))
    try:
        font = ImageFont.truetype(FONT_PATH, 44)
    except Exception:
        font = None
    d.text((16, 10), 'D', fill=(255, 255, 255, 255), font=font)
    return img


# --------------------------------------------------------------------------
# Текст -> штрихи (контуры букв шрифта)
# --------------------------------------------------------------------------
def _quad_flatten(out, p0, c, p1, n=12):
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0],
                    mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1]))


def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _point_in_poly(x, y, poly):
    """Луч из точки — точка внутри полигона? (алгоритм пересечения лучей)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def _drop_inner_contours(glyph_strokes):
    """Убирает внутренние контуры (дырки в буквах), оставляя только внешние.

    Так 'O' рисуется одним кольцом, а не двумя, 'A' — треугольником без
    внутреннего треугольника и т.д. Точки (точки у i/j/! — отдельные контуры,
    не лежат внутри других — сохраняются).
    """
    kept = []
    for a in glyph_strokes:
        if len(a) < 2:
            continue
        inside_other = False
        for b in glyph_strokes:
            if b is a or len(b) < 2:
                continue
            hits = sum(1 for (x, y) in a if _point_in_poly(x, y, b))
            if hits > len(a) // 2:
                inside_other = True
                break
        if not inside_other:
            kept.append(a)
    return kept


def _flatten_qcurve(cur, p0, offs, end):
    k = len(offs)
    if k == 1:
        _quad_flatten(cur, p0, offs[0], end)
    else:
        m = _mid(offs[0], offs[1])
        _quad_flatten(cur, p0, offs[0], m)
        for i in range(1, k - 1):
            m2 = _mid(offs[i], offs[i + 1])
            _quad_flatten(cur, m, offs[i], m2)
            m = m2
        _quad_flatten(cur, m, offs[-1], end)


def _cubic_flatten(out, p0, c1, c2, p1, n=16):
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))


def _strokes_from_freetype(text, max_height):
    face = freetype.Face(FONT_PATH)
    face.set_pixel_sizes(0, int(max_height))
    strokes = []
    x_cursor = 0.0

    for ch in text:
        try:
            face.load_char(ch, freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_NO_HINTING)
        except Exception:
            x_cursor += 8.0
            continue

        cur = []
        glyph_strokes = []

        def move_to(v, _ctx=None):
            nonlocal cur
            if cur:
                glyph_strokes.append(cur)
            cur = [(v.x / 64.0, v.y / 64.0)]

        def line_to(v, _ctx=None):
            cur.append((v.x / 64.0, v.y / 64.0))

        def conic_to(v0, v1, _ctx=None):
            p0 = cur[-1]
            _quad_flatten(cur, p0, (v0.x / 64.0, v0.y / 64.0), (v1.x / 64.0, v1.y / 64.0))

        def cubic_to(v0, v1, v2, _ctx=None):
            p0 = cur[-1]
            _cubic_flatten(cur, p0,
                           (v0.x / 64.0, v0.y / 64.0),
                           (v1.x / 64.0, v1.y / 64.0),
                           (v2.x / 64.0, v2.y / 64.0))

        try:
            face.glyph.outline.decompose(move_to=move_to, line_to=line_to,
                                         conic_to=conic_to, cubic_to=cubic_to)
        except Exception:
            pass
        if cur:
            glyph_strokes.append(cur)

        glyph_strokes = _drop_inner_contours(glyph_strokes)

        for pl in glyph_strokes:
            if len(pl) < 2:
                continue
            if math.hypot(pl[0][0] - pl[-1][0], pl[0][1] - pl[-1][1]) > 0.6:
                pl.append(pl[0])
            strokes.append([(x + x_cursor, y) for x, y in pl])

        x_cursor += face.glyph.advance.x / 64.0

    return strokes


def _strokes_from_fonttools(text, max_height):
    font = TTFont(FONT_PATH)
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    units = float(font['head'].unitsPerEm)
    scale = max_height / units
    strokes = []
    x_cursor = 0.0

    for ch in text:
        gname = cmap.get(ord(ch))
        if not gname:
            x_cursor += 0.6 * max_height
            continue
        pen = DecomposingRecordingPen(gs)
        try:
            gs[gname].draw(pen)
        except Exception:
            x_cursor += gs[gname].width * scale
            continue

        cur = []
        glyph_strokes = []

        def push(op, args):
            nonlocal cur
            if op == 'moveTo':
                if cur:
                    glyph_strokes.append(cur)
                cur = [(args[0][0] * scale, args[0][1] * scale)]
            elif op == 'lineTo':
                cur.append((args[0][0] * scale, args[0][1] * scale))
            elif op == 'qCurveTo':
                p0 = cur[-1]
                offs = [(a[0] * scale, a[1] * scale) for a in args if a is not None]
                implicit = len(offs) != len(args)
                if not offs:
                    return
                end = p0 if implicit else offs.pop()
                _flatten_qcurve(cur, p0, offs, end)
            elif op == 'curveTo':
                p0 = cur[-1]
                _cubic_flatten(cur, p0,
                               (args[0][0] * scale, args[0][1] * scale),
                               (args[1][0] * scale, args[1][1] * scale),
                               (args[2][0] * scale, args[2][1] * scale))
            elif op == 'closePath':
                if cur:
                    glyph_strokes.append(cur)
                    cur = []

        for op, args in pen.value:
            push(op, args)
        if cur:
            glyph_strokes.append(cur)

        for pl in _drop_inner_contours(glyph_strokes):
            if len(pl) >= 2:
                if math.hypot(pl[0][0] - pl[-1][0], pl[0][1] - pl[-1][1]) > 0.6:
                    pl.append(pl[0])
                strokes.append([(x + x_cursor, y) for x, y in pl])
        x_cursor += gs[gname].width * scale

    return strokes


def text_to_strokes(text, max_width, max_height):
    if freetype is not None:
        strokes = _strokes_from_freetype(text, max_height)
    elif TTFont is not None:
        strokes = _strokes_from_fonttools(text, max_height)
    else:
        raise RuntimeError('Нужен пакет freetype-py или fonttools')

    flat = [p for pl in strokes for p in pl]
    if not flat:
        return [], 0.0, 0.0

    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    w = max(xmax - xmin, 1e-6)
    h = max(ymax - ymin, 1e-6)
    scale = min(max_width / w, max_height / h)

    out = [[((p[0] - xmin) * scale, (h - (p[1] - ymin)) * scale) for p in pl] for pl in strokes]
    return out, w * scale, h * scale


# --------------------------------------------------------------------------
# Рисование мышью
# --------------------------------------------------------------------------
def _rdp(points, eps):
    """Ramer–Douglas–Peucker: сжимает прямые участки в отрезки, сохраняя изгибы."""
    if len(points) < 3:
        return points
    s, e = points[0], points[-1]
    sx, sy = s
    ex, ey = e
    dx, dy = ex - sx, ey - sy
    d2 = dx * dx + dy * dy
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if d2 == 0:
            dd = (px - sx) ** 2 + (py - sy) ** 2
        else:
            t = ((px - sx) * dx + (py - sy) * dy) / d2
            t = max(0.0, min(1.0, t))
            qx, qy = sx + t * dx, sy + t * dy
            dd = (px - qx) ** 2 + (py - qy) ** 2
        if dd > dmax:
            dmax, idx = dd, i
    if dmax > eps * eps:
        left = _rdp(points[:idx + 1], eps)
        right = _rdp(points[idx:], eps)
        return left[:-1] + right
    return [s, e]


def _iter_points(pl, start_x, start_y):
    """Интерполирует ломаную с шагом STEP_PX; точки в абсолютных координатах экрана."""
    prev = (start_x + pl[0][0], start_y + pl[0][1])
    yield prev
    for p in pl[1:]:
        tx, ty = start_x + p[0], start_y + p[1]
        dx, dy = tx - prev[0], ty - prev[1]
        dist = math.hypot(dx, dy)
        steps = max(1, int(round(dist / STEP_PX)))
        for s in range(1, steps + 1):
            f = s / steps
            yield (prev[0] + dx * f, prev[1] + dy * f)
        prev = (tx, ty)


def _prepared_stroke(pl, start_x, start_y):
    """Интерполирует штрих (при SIMPLIFY_EPS>0 прямые участки рисуются редкими точками)."""
    if SIMPLIFY_EPS <= 0:
        return list(_iter_points(pl, start_x, start_y))
    pts = _rdp(list(_iter_points(pl, start_x, start_y)), SIMPLIFY_EPS)
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        dist = math.hypot(dx, dy)
        n = int(math.ceil(dist / MAX_JUMP_PX))
        for i in range(1, n):
            f = i / n
            out.append((a[0] + dx * f, a[1] + dy * f))
        out.append(b)
    return out


def draw_strokes(strokes, start_x, start_y):
    SetCursorPos = user32.SetCursorPos
    ctrl_down()
    time.sleep(0.12)
    try:
        for pl in strokes:
            pts = _prepared_stroke(pl, start_x, start_y)
            if len(pts) < 2:
                continue
            SetCursorPos(int(pts[0][0]), int(pts[0][1]))
            time.sleep(PRESS_DELAY)
            mouse_down()
            time.sleep(PRESS_DELAY)
            for px, py in pts[1:]:
                SetCursorPos(int(px), int(py))
                time.sleep(STEP_DELAY)
            mouse_up()
            time.sleep(PRESS_DELAY)
    finally:
        mouse_up()
        ctrl_up()


def trace_path(strokes, start_x, start_y):
    """Режим --test: двигает мышь по траектории без нажатий."""
    for pl in strokes:
        for px, py in _prepared_stroke(pl, start_x, start_y):
            user32.SetCursorPos(int(px), int(py))
            time.sleep(STEP_DELAY)


# --------------------------------------------------------------------------
# Оверлей ввода текста
# --------------------------------------------------------------------------
class DrawOverlay:
    def __init__(self, root, on_submit, on_cancel):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        w, h = 380, 70
        sw = root.winfo_screenwidth()
        self.win.geometry('%dx%d+%d+40' % (w, h, sw // 2 - w // 2))
        self.win.configure(bg='#26262b')

        row = tk.Frame(self.win, bg='#26262b')
        row.pack(fill='both', padx=12, pady=(10, 2))

        vcmd = (root.register(self._limit), '%P')
        self.entry = tk.Entry(row, font=('Segoe UI', 16), width=24, justify='center',
                              bg='#1c1c20', fg='#ffffff', insertbackground='#ffffff',
                              relief='flat', highlightthickness=1, highlightbackground='#3d3d44',
                              validate='key', validatecommand=vcmd)
        self.entry.pack(side='left', ipady=3, expand=True, fill='x')

        self.entry.bind('<Return>', lambda e: on_submit(self.entry.get().strip()))
        self.entry.bind('<Escape>', lambda e: on_cancel())
        self.win.bind('<Escape>', lambda e: on_cancel())

        hint = tk.Label(self.win, text='Enter — нарисовать    Esc — отмена',
                        bg='#26262b', fg='#8a8a94', font=('Segoe UI', 9))
        hint.pack()

        self.win.update_idletasks()
        self.win.focus_force()
        self.entry.focus_set()

    def _limit(self, value):
        return len(value) <= MAX_CHARS


# --------------------------------------------------------------------------
# Калибровка миникарты
# --------------------------------------------------------------------------
class Calibration:
    def __init__(self, root, on_saved, on_cancel):
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.start = None
        self.rect = None
        self.rect_id = None

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.attributes('-alpha', 0.25)
        self.win.geometry('%dx%d+0+0' % (sw, sh))

        self.canvas = tk.Canvas(self.win, bg='black', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.create_text(sw // 2, 30,
                                text='Зажми ЛКМ и обведи миникарту прямоугольником',
                                fill='#ffffff', font=('Segoe UI', 16, 'bold'))
        self.canvas.create_text(sw // 2, 62,
                                text='Enter — сохранить    Esc — отменить',
                                fill='#cccccc', font=('Segoe UI', 12))

        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._drag)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.win.bind('<Return>', self._save)
        self.win.bind('<Escape>', self._cancel)

        self.win.update_idletasks()
        self.win.focus_force()
        self.win.grab_set()

    def _press(self, e):
        self.start = (e.x, e.y)
        self.rect = None

    def _drag(self, e):
        if not self.start:
            return
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        x0, y0 = self.start
        self.rect_id = self.canvas.create_rectangle(x0, y0, e.x, e.y,
                                                    outline='#00ff66', width=2,
                                                    fill='#00ff66', stipple='gray25')

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        self.rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.start = None

    def _save(self, _e):
        if not self.rect:
            return
        l, t, r, b = self.rect
        self.on_saved({'left': l, 'top': t, 'right': r, 'bottom': b})

    def _cancel(self, _e):
        self.on_cancel()


# --------------------------------------------------------------------------
# Окно настроек
# --------------------------------------------------------------------------
class SettingsWindow:
    def __init__(self, root, current_speed, current_hotkeys, on_save, on_cancel, on_calibrate):
        self.on_save = on_save
        self.on_cancel = on_cancel
        self.on_calibrate = on_calibrate
        self.speed = tk.StringVar(value=current_speed)
        self.hotkeys = {act: dict(hk) for act, hk in current_hotkeys.items()}
        self.recording = None
        self.hk_buttons = {}

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        w, h = 440, 400
        sw = root.winfo_screenwidth()
        self.win.geometry('%dx%d+%d+%d' % (w, h, sw // 2 - w // 2, 60))
        self.win.configure(bg='#26262b')
        self.win.resizable(False, False)

        tk.Label(self.win, text='Настройки', bg='#26262b', fg='#ffffff',
                 font=('Segoe UI', 14, 'bold')).pack(pady=(12, 4))

        tk.Label(self.win, text='Скорость рисования', bg='#26262b', fg='#8a8a94',
                 font=('Segoe UI', 10)).pack(pady=(2, 0))
        frame = tk.Frame(self.win, bg='#26262b')
        frame.pack(fill='both', padx=20, pady=2)
        for key, p in SPEED_PRESETS.items():
            tk.Radiobutton(frame, text=p['label'], value=key, variable=self.speed,
                           bg='#26262b', fg='#ffffff', selectcolor='#3d3d44',
                           activebackground='#26262b', activeforeground='#ffffff',
                           font=('Segoe UI', 11), anchor='w').pack(fill='x', pady=2)

        tk.Label(self.win, text='Горячие клавиши', bg='#26262b', fg='#8a8a94',
                 font=('Segoe UI', 10)).pack(pady=(8, 0))
        hk_frame = tk.Frame(self.win, bg='#26262b')
        hk_frame.pack(fill='x', padx=20, pady=2)
        for act, label in (('draw', 'Ввод текста:'), ('settings', 'Настройки:')):
            tk.Label(hk_frame, text=label, bg='#26262b', fg='#ffffff',
                     font=('Segoe UI', 11), width=14, anchor='w').pack(side='left')
            btn = tk.Button(hk_frame, text=format_hotkey(self.hotkeys[act]),
                            command=lambda a=act: self._record(a),
                            bg='#1c1c20', fg='#ffffff', relief='flat',
                            font=('Segoe UI', 11), width=16)
            btn.pack(side='left', fill='x', expand=True, padx=(0, 4))
            self.hk_buttons[act] = btn
            tk.Label(hk_frame, text='нажмите, чтобы изменить', bg='#26262b',
                     fg='#8a8a94', font=('Segoe UI', 9)).pack(side='right')
        tk.Label(self.win, text='Минимум один модификатор (Ctrl/Alt/Shift)',
                 bg='#26262b', fg='#66666e', font=('Segoe UI', 9)).pack(pady=(0, 4))

        tk.Button(self.win, text='Калибровка миникарты…', command=self._calibrate,
                  bg='#3d3d44', fg='#ffffff', relief='flat',
                  font=('Segoe UI', 10)).pack(pady=(8, 2))

        row = tk.Frame(self.win, bg='#26262b')
        row.pack(fill='x', padx=20, pady=(6, 12))
        tk.Button(row, text='Сохранить', command=self._save,
                  bg='#4a6ea9', fg='#ffffff', relief='flat',
                  font=('Segoe UI', 11), width=12).pack(side='right')
        tk.Button(row, text='Отмена', command=self._cancel,
                  bg='#3d3d44', fg='#ffffff', relief='flat',
                  font=('Segoe UI', 11), width=12).pack(side='right', padx=(0, 8))

        self.win.bind('<KeyPress>', self._on_key)
        self.win.bind('<Return>', lambda e: self._save())
        self.win.bind('<Escape>', lambda e: self._cancel())

        self.win.update_idletasks()
        self.win.focus_force()
        self.win.grab_set()

    def _record(self, act):
        self.recording = act
        self.hk_buttons[act].config(text='Нажмите комбинацию…')

    def _on_key(self, e):
        if self.recording is None:
            return None
        if e.keysym in ('Control_L', 'Control_R', 'Alt_L', 'Alt_R', 'Shift_L', 'Shift_R'):
            return 'break'
        if e.keysym == 'Escape':
            act = self.recording
            self.recording = None
            self.hk_buttons[act].config(text=format_hotkey(self.hotkeys[act]))
            return 'break'
        mods = []
        if e.state & 0x0001:
            mods.append('shift')
        if e.state & 0x0004:
            mods.append('control')
        if e.state & 0x0008:
            mods.append('alt')
        if not mods or _keysym_to_vk(e.keysym) is None:
            return 'break'
        act = self.recording
        self.hotkeys[act] = {'mods': mods, 'key': e.keysym}
        self.recording = None
        self.hk_buttons[act].config(text=format_hotkey(self.hotkeys[act]))
        return 'break'

    def _save(self):
        self.on_save(self.speed.get(), self.hotkeys)

    def _cancel(self):
        self.on_cancel()

    def _calibrate(self):
        self.on_calibrate()


# --------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------
class App:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.q = queue.Queue()
        self.overlay = None
        self.calibration = None
        self.settings = None
        self.speed = DEFAULT_SPEED
        self.hotkeys = {act: dict(hk) for act, hk in DEFAULT_HOTKEYS.items()}
        self._reload_hotkeys = True
        self.drawing = False
        self.prev_fg = None
        self.tray = None

    def start(self):
        self.speed = load_speed()
        apply_speed(self.speed)
        self.hotkeys = load_hotkeys()
        print('Скорость: %s' % SPEED_PRESETS[self.speed]['label'])
        print('Хоткеи: ввод — %s, настройки — %s'
              % (format_hotkey(self.hotkeys['draw']), format_hotkey(self.hotkeys['settings'])))
        if self.config is None:
            print('Первый запуск: обведи миникарту прямоугольником.')
            self.open_calibration()
        else:
            print('Миникарта загружена из config.json.')
        threading.Thread(target=self._hotkey_loop, daemon=True).start()
        self._start_tray()
        self.root.after(50, self._poll)

    def _start_tray(self):
        if pystray is None:
            print('pystray не установлен — трей недоступен (pip install pystray pillow).')
            return
        try:
            self.tray = pystray.Icon('dota_text_draw', _tray_image(),
                                     'Dota Text Draw', menu=self._tray_menu())
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as e:
            print('Не удалось запустить трей:', e)
            self.tray = None

    def _tray_menu(self):
        return pystray.Menu(
            pystray.MenuItem('Оверлей ввода (%s)' % format_hotkey(self.hotkeys['draw']),
                             lambda: self.q.put('draw')),
            pystray.MenuItem('Настройки (%s)' % format_hotkey(self.hotkeys['settings']),
                             lambda: self.q.put('settings')),
            pystray.MenuItem('Калибровка миникарты',
                             lambda: self.q.put('calibrate')),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Справка', lambda: self.q.put('info')),
            pystray.MenuItem('Выход', lambda: self.q.put('exit')),
        )

    def _tray_notify(self, text, title='Dota Text Draw'):
        if self.tray is not None:
            try:
                self.tray.notify(text, title)
            except Exception:
                pass

    def _register_hotkeys(self):
        """Регистрирует хоткеи из self.hotkeys; возвращает список ошибок."""
        errs = []
        user32.UnregisterHotKey(None, HOTKEY_DRAW_ID)
        user32.UnregisterHotKey(None, HOTKEY_SETTINGS_ID)
        combos = {}
        for act, act_id in (('draw', HOTKEY_DRAW_ID), ('settings', HOTKEY_SETTINGS_ID)):
            hk = self.hotkeys.get(act, DEFAULT_HOTKEYS[act])
            vk = _keysym_to_vk(hk.get('key', ''))
            mods = _mods_to_flags(hk.get('mods'))
            if vk is None:
                errs.append('%s: клавиша «%s» не поддерживается'
                            % (act, hk.get('key', '')))
                continue
            if mods == 0:
                errs.append('%s: нужен модификатор' % act)
                continue
            combo = (mods, vk)
            if combo in combos:
                errs.append('одинаковые комбинации: %s и %s'
                            % (combos[combo], act))
                continue
            combos[combo] = act
            if not user32.RegisterHotKey(None, act_id, mods | MOD_NOREPEAT, vk):
                errs.append('%s: %s (занято или нельзя)' % (act, format_hotkey(hk)))
        return errs

    def _hotkey_loop(self):
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        while True:
            if self._reload_hotkeys:
                self._reload_hotkeys = False
                errs = self._register_hotkeys()
                if errs:
                    print('Ошибки регистрации хоткеев: ' + '; '.join(errs))
                    self.q.put(('hk_error', errs))
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_QUIT:
                    return
                if msg.message == WM_HOTKEY:
                    if msg.wParam == HOTKEY_DRAW_ID:
                        self.q.put('draw')
                    elif msg.wParam == HOTKEY_SETTINGS_ID:
                        self.q.put('settings')
            time.sleep(0.05)

    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if isinstance(ev, tuple):
                    kind, payload = ev
                    if kind == 'hk_error':
                        self._tray_notify('Хоткей не работает: ' + '; '.join(payload))
                    continue
                if ev == 'draw':
                    self.toggle_draw_overlay()
                elif ev == 'settings':
                    self.open_settings()
                elif ev == 'calibrate':
                    self.open_calibration()
                elif ev == 'info':
                    self._show_info()
                elif ev == 'exit':
                    self._shutdown()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _show_info(self):
        import tkinter.messagebox as mb
        mb.showinfo(
            'Dota Text Draw',
            'Ввод текста — %s\nНастройки — %s\n'
            'Enter — нарисовать, Esc — отмена\n\n'
            'Хоткеи и скорость меняются в настройках (%s).\n'
            'Dota должна быть в режиме «Окно без рамки».'
            % (format_hotkey(self.hotkeys['draw']),
               format_hotkey(self.hotkeys['settings']),
               format_hotkey(self.hotkeys['settings'])),
        )

    def _shutdown(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---- оверлей ----
    def toggle_draw_overlay(self):
        if self.drawing or self.calibration is not None:
            return
        if self.overlay is not None:
            self.close_overlay()
        else:
            self.open_draw_overlay()

    def open_draw_overlay(self):
        self.prev_fg = user32.GetForegroundWindow()
        self.overlay = DrawOverlay(self.root, self.on_submit, self.on_cancel)

    def close_overlay(self):
        if self.overlay is not None:
            try:
                self.overlay.win.destroy()
            except Exception:
                pass
            self.overlay = None
        if self.prev_fg:
            _set_foreground(self.prev_fg)
            self.prev_fg = None

    def on_submit(self, text):
        self.close_overlay()
        if not text or self.drawing or self.config is None:
            return
        self.root.after(250, lambda: self._draw(text))

    def on_cancel(self):
        self.close_overlay()

    # ---- настройки ----
    def open_settings(self):
        if self.drawing or self.calibration is not None:
            return
        self.close_overlay()
        self.close_settings()
        self.settings = SettingsWindow(self.root, self.speed, self.hotkeys,
                                       self.on_settings_saved,
                                       self.on_settings_cancel,
                                       self.on_settings_calibrate)

    def on_settings_saved(self, name, hotkeys):
        self.speed = name
        apply_speed(name)
        save_speed(name)
        self.hotkeys = {act: dict(hk) for act, hk in hotkeys.items()}
        save_hotkeys(self.hotkeys)
        self._reload_hotkeys = True
        self.close_settings()
        print('Скорость: %s' % SPEED_PRESETS[name]['label'])
        print('Хоткеи: ввод — %s, настройки — %s'
              % (format_hotkey(self.hotkeys['draw']), format_hotkey(self.hotkeys['settings'])))
        self._tray_notify('Настройки сохранены. Хоткеи применены.')

    def on_settings_cancel(self):
        self.close_settings()

    def on_settings_calibrate(self):
        self.close_settings()
        self.open_calibration()

    def close_settings(self):
        if self.settings is not None:
            try:
                self.settings.win.grab_release()
                self.settings.win.destroy()
            except Exception:
                pass
            self.settings = None

    # ---- калибровка ----
    def open_calibration(self):
        if self.drawing or self.calibration is not None:
            return
        self.close_overlay()
        self.calibration = Calibration(self.root, self.on_calibrated, self.on_calib_cancel)

    def on_calibrated(self, rect):
        self.config = rect
        save_config(rect)
        self.close_calibration()
        print('Миникарта сохранена: left=%d top=%d right=%d bottom=%d'
              % (rect['left'], rect['top'], rect['right'], rect['bottom']))
        self._tray_notify('Миникарта сохранена. Ctrl+Alt+D — ввод текста.')

    def on_calib_cancel(self):
        self.close_calibration()

    def close_calibration(self):
        if self.calibration is not None:
            try:
                self.calibration.win.grab_release()
                self.calibration.win.destroy()
            except Exception:
                pass
            self.calibration = None

    # ---- рисование ----
    def _draw(self, text):
        if self.drawing or self.config is None:
            return
        self.drawing = True
        try:
            rect = self.config
            rw = rect['right'] - rect['left']
            rh = rect['bottom'] - rect['top']
            max_w = max(20.0, rw * MAX_TEXT_WIDTH_FRAC)
            max_h = max(12.0, rh * MAX_TEXT_WIDTH_FRAC)
            strokes, tw, th = text_to_strokes(text, max_w, max_h)
            if not strokes:
                print('Нет штрихов для рисования.')
                return
            start_x = rect['left'] + (rw - tw) / 2
            start_y = rect['top'] + (rh - th) / 2
            print('Рисую «%s» ...' % text)
            if TEST_MODE:
                trace_path(strokes, start_x, start_y)
            else:
                draw_strokes(strokes, start_x, start_y)
            print('Готово.')
        except Exception as e:
            print('Ошибка рисования:', e)
        finally:
            self.drawing = False


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------
def main():
    _setup_streams()
    if not _single_instance():
        print('Программа уже запущена (иконка в трее).')
        return
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(errors='replace')
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()
    app = App(root, load_config())
    app.start()
    root.mainloop()


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        _setup_streams()
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(errors='replace')
            except Exception:
                pass
        import traceback
        try:
            strokes, tw, th = text_to_strokes('AB', 300, 60)
            print('strokes=%d  width=%.1f  height=%.1f' % (len(strokes), tw, th))
            for i, pl in enumerate(strokes[:6]):
                print(' stroke %d: %d точек, первая=%s' % (i, len(pl), pl[0]))
        except Exception:
            traceback.print_exc()
        sys.exit(0)
    main()
