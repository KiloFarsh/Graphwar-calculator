"""
Бот для классического Graphwar (окно 800x600, режим обычной функции).

В этом режиме игра сама сдвигает график по вертикали так, чтобы он прошёл
через вашего солдата. Бот строит интерполяционную формулу через несколько
вражеских солдат и проверяет путь на круги и союзников. Если общей безопасной
кривой нет, он выбирает выстрел с максимальным числом целей во взрыве.

Оси на поле: x от -25 до 25. По вертикали тот же масштаб, что и по
горизонтали (50 единиц на ширину поля), поэтому y примерно от -14.61 до 14.61,
а не от -15 до 15.

F3 — только расчёт и картинка last_shot.png
F4 — расчёт и выстрел
Esc — выход
"""

import math
import os
import sys
import threading
import time
from ctypes import wintypes

import ctypes
import cv2
import mss
import numpy as np
import pyautogui
import pyperclip

try:
    import keyboard
except ImportError:
    keyboard = None

# Клиент Graphwar: GraphServer.Constants и rsc/GameScreen.txt
CLIENT_W = 800
CLIENT_H = 600
PLANE_X = 15
PLANE_Y = 15
PLANE_W = 770
PLANE_H = 450
GAME_LENGTH = 50.0  # x = -25..25, и тот же масштаб по y
FIELD_CX = 195 + 275 / 2  # центр поля ввода функции
FIELD_CY = 480 + 25 / 2

SOLDIER_RADIUS_PX = 7
EXPLOSION_RADIUS_PX = 12

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.02

_busy = threading.Lock()
_script_dir = os.path.dirname(os.path.abspath(__file__))


def _dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def num(value):
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def px_to_math(px, py, width):
    """Пиксели поля -> координаты Graphwar. Масштаб y считается по ширине."""
    mx = (px / width) * GAME_LENGTH - GAME_LENGTH / 2
    my = (PLANE_H / 2 - py) * GAME_LENGTH / PLANE_W
    # Если поле на экране растянуто, ширина и высота уже в пикселях кадра,
    # а эталонные 770x450 заменены фактическими размерами вызывающим кодом.
    return mx, my


def math_to_px(mx, my, width, height):
    px = (mx + GAME_LENGTH / 2) / GAME_LENGTH * width
    py = height / 2 - my * width / GAME_LENGTH
    return px, py


def y_limit(width, height):
    return (height / 2) * GAME_LENGTH / width


def frame_to_math(px, py, width, height):
    mx = (px / width) * GAME_LENGTH - GAME_LENGTH / 2
    my = (height / 2 - py) * GAME_LENGTH / width
    return mx, my


def find_graphwar_window():
    user32 = ctypes.windll.user32
    matches = []

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if "graphwar" in title.lower():
            matches.append((hwnd, title))
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    if not matches:
        return None, None
    for hwnd, title in matches:
        if title.lower().strip() == "graphwar":
            return hwnd, title
    return matches[0]


def client_rect_on_screen(hwnd):
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    return int(origin.x), int(origin.y), width, height


def grab_region(left, top, width, height):
    with mss.mss() as sct:
        shot = np.array(sct.grab({"left": left, "top": top, "width": width, "height": height}))
    return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)


def capture_screen():
    """Возвращает кадр, экранные координаты его левого верхнего угла и hwnd."""
    hwnd, title = find_graphwar_window()
    if hwnd:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.25)
        left, top, width, height = client_rect_on_screen(hwnd)
        if width > 50 and height > 50:
            return grab_region(left, top, width, height), left, top, hwnd, title

    with mss.mss() as sct:
        monitor = sct.monitors[1]
    frame = grab_region(monitor["left"], monitor["top"], monitor["width"], monitor["height"])
    return frame, monitor["left"], monitor["top"], None, None


def _plane_from_layout(frame):
    height, width = frame.shape[:2]
    aspect = width / max(height, 1)
    if not (1.15 < aspect < 1.55):
        return None
    x = int(round(PLANE_X * width / CLIENT_W))
    y = int(round(PLANE_Y * height / CLIENT_H))
    w = int(round(PLANE_W * width / CLIENT_W))
    h = int(round(PLANE_H * height / CLIENT_H))
    if w < 80 or h < 50 or x + w > width or y + h > height:
        return None
    gray = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    # Поле почти целиком белое и чёрное. Зелёная рамка окна такой не бывает.
    pure = np.mean((gray > 245) | (gray < 20))
    if pure < 0.45:
        return None
    return x, y, w, h


def _plane_from_white(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    white = (gray > 210).astype(np.uint8) * 255
    kernel = max(21, frame.shape[1] // 25)
    if kernel % 2 == 0:
        kernel += 1
    closed = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    target_aspect = PLANE_W / PLANE_H
    best = None
    best_score = 0
    frame_h, frame_w = frame.shape[:2]
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < frame_w * 0.35 or h < frame_h * 0.25:
            continue
        aspect = w / max(h, 1)
        if not (1.45 < aspect < 2.05):
            continue
        score = (w * h) / (1 + abs(aspect - target_aspect) * 4)
        if score > best_score:
            best_score = score
            best = (x, y, w, h)
    return best


def locate_plane(frame):
    return _plane_from_layout(frame) or _plane_from_white(frame)


def obstacle_mask(board):
    """Чёрные круги. Оси и подписи тоньше 3 пикселей и в маску не попадают."""
    gray = cv2.cvtColor(board, cv2.COLOR_BGR2GRAY)
    dark = (gray < 80).astype(np.uint8) * 255
    opened = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return cv2.dilate(opened, np.ones((3, 3), np.uint8), iterations=1)


def find_soldiers(board):
    height, width = board.shape[:2]
    scale = width / PLANE_W
    hsv = cv2.cvtColor(board, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    colored = ((sat > 55) & (val > 45)).astype(np.uint8) * 255
    close = max(3, int(round(5 * scale)))
    if close % 2 == 0:
        close += 1
    colored = cv2.morphologyEx(colored, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    colored = cv2.morphologyEx(colored, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))

    contours, _ = cv2.findContours(colored, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = 30 * scale * scale
    max_area = 2800 * scale * scale
    centers = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not (min_area < area < max_area):
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if h == 0 or w == 0:
            continue
        ratio = w / h
        # Плашка ника широкая и низкая, солдат и маркер ближе к квадрату или кругу.
        if ratio > 2.4 or ratio < 0.35:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] <= 0:
            continue
        cx = int(moments["m10"] / moments["m00"])
        cy = int(moments["m01"] / moments["m00"])
        centers.append((cx, cy))

    merge_dist = 24 * scale
    merged = []
    for cx, cy in centers:
        for index, (mx, my, count) in enumerate(merged):
            if math.hypot(cx - mx, cy - my) <= merge_dist:
                merged[index] = ((mx * count + cx) / (count + 1), (my * count + cy) / (count + 1), count + 1)
                break
        else:
            merged.append((cx, cy, 1))
    return [(int(round(x)), int(round(y))) for x, y, _ in merged], hue, sat, val, scale


def pick_player(soldiers, hue, sat, val, scale):
    if not soldiers:
        return None
    height, width = hue.shape
    radius = max(8, int(round(18 * scale)))
    best = None
    best_score = 0
    yy, xx = np.ogrid[:height, :width]
    warm = ((hue <= 30) | (hue >= 170)) & (sat > 70) & (val > 60)
    for cx, cy in soldiers:
        disk = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius * radius
        score = int(np.count_nonzero(warm & disk))
        if score > best_score:
            best_score = score
            best = (cx, cy)
    # Маркер текущего солдата — красное кольцо. Без него ход не наш.
    if best_score < max(12, int(25 * scale * scale)):
        return None
    return best


def classify_targets(player, soldiers, width, height):
    px, py = frame_to_math(*player, width, height)
    allies = []
    enemies = []
    for soldier in soldiers:
        if math.hypot(soldier[0] - player[0], soldier[1] - player[1]) < 18 * (width / PLANE_W):
            continue
        sx, sy = frame_to_math(*soldier, width, height)
        # Команды стоят по разные стороны от оси x = 0.
        if px * sx >= 0:
            allies.append((sx, sy, soldier))
        else:
            enemies.append((sx, sy, soldier))
    enemies.sort(key=lambda item: math.hypot(item[0] - px, item[1] - py))
    return (px, py), allies, enemies


def trajectory_y(xp, yp, xt, yt, amp, freq, x):
    dx = xt - xp
    k = (yt - yp) / dx
    t = (x - xp) / dx
    return yp + k * (x - xp) + amp * math.sin(freq * math.pi * t)


def path_is_clear(xp, yp, xt, yt, amp, freq, blocked, width, height):
    dx = xt - xp
    if abs(dx) < 1e-6:
        return False
    limit = y_limit(width, height) - 0.35
    steps = max(50, int(abs(dx) / 0.04))
    start_px = math_to_px(xp, yp, width, height)
    target_px = math_to_px(xt, yt, width, height)
    safe_start = (SOLDIER_RADIUS_PX + 6) * (width / PLANE_W)
    safe_target = (EXPLOSION_RADIUS_PX + 4) * (width / PLANE_W)

    for index in range(steps + 1):
        t = index / steps
        x = xp + dx * t
        y = trajectory_y(xp, yp, xt, yt, amp, freq, x)
        if y > limit or y < -limit:
            return False
        px, py = math_to_px(x, y, width, height)
        ix = int(round(px))
        iy = int(round(py))
        if ix < 1 or iy < 1 or ix >= width - 1 or iy >= height - 1:
            return False
        if math.hypot(px - start_px[0], py - start_px[1]) <= safe_start:
            continue
        if math.hypot(px - target_px[0], py - target_px[1]) <= safe_target:
            continue
        if blocked[iy - 1:iy + 2, ix - 1:ix + 2].any():
            return False
    return True


def interpolate_relative(knots, x):
    values = []
    for index, (xi, yi) in enumerate(knots):
        value = yi
        for other_index in range(len(knots)):
            if other_index != index:
                xj = knots[other_index][0]
                value *= (x - xj) / (xi - xj)
        values.append(value)
    return sum(values)


def build_interpolated_formula(knots):
    terms = []
    for index, (xi, yi) in enumerate(knots):
        if abs(yi) < 1e-9:
            continue
        factors = []
        for other_index in range(len(knots)):
            if other_index == index:
                continue
            xj = knots[other_index][0]
            factors.append(f"((x-({num(xj)}))/({num(xi - xj)}))")
        terms.append(f"({num(yi)})*({'*'.join(factors)})")
    return "+".join(terms) or "0"


def path_is_clear_curve(player, targets, knots, allies, blocked, width, height):
    xp, yp = player
    xt = targets[-1][0]
    dx = xt - xp
    if abs(dx) < 1e-6:
        return False

    scale = width / PLANE_W
    limit = y_limit(width, height) - 0.35
    steps = max(50, int(abs(dx) / 0.04))
    start_px = math_to_px(xp, yp, width, height)
    safe_start = (SOLDIER_RADIUS_PX + 6) * scale
    safe_target = (EXPLOSION_RADIUS_PX + 4) * scale
    target_pixels = [item[2] for item in targets]
    ally_mask = np.zeros_like(blocked)
    avoid_r = int(round((SOLDIER_RADIUS_PX + 8) * scale))
    for ax, ay, _ in allies:
        apx, apy = math_to_px(ax, ay, width, height)
        cv2.circle(ally_mask, (int(round(apx)), int(round(apy))), avoid_r, 255, -1)

    for index in range(steps + 1):
        t = index / steps
        x = xp + dx * t
        y = yp + interpolate_relative(knots, x)
        if not math.isfinite(y) or y > limit or y < -limit:
            return False
        px, py = math_to_px(x, y, width, height)
        ix = int(round(px))
        iy = int(round(py))
        if ix < 1 or iy < 1 or ix >= width - 1 or iy >= height - 1:
            return False
        if math.hypot(px - start_px[0], py - start_px[1]) <= safe_start:
            continue
        if ally_mask[iy - 1:iy + 2, ix - 1:ix + 2].any():
            return False
        near_target = any(
            math.hypot(px - target_px[0], py - target_px[1]) <= safe_target
            for target_px in target_pixels
        )
        if not near_target and blocked[iy - 1:iy + 2, ix - 1:ix + 2].any():
            return False
    return True


def plan_multi_curve(player, enemies, allies, blocked, width, height):
    """Builds one interpolated formula through as many reachable enemies as possible."""
    xp = player[0]
    direction = 1 if enemies[0][0] >= xp else -1
    ordered = sorted(enemies, key=lambda item: direction * (item[0] - xp))
    selected = []
    for enemy in ordered:
        if abs(enemy[0] - xp) < 0.35:
            continue
        if any(abs(enemy[0] - other[0]) < 0.35 for other in selected):
            continue
        selected.append(enemy)

    for count in range(len(selected), 1, -1):
        targets = selected[:count]
        knots = [(xp, 0.0)]
        for enemy in targets:
            knots.append((enemy[0], enemy[1] - player[1]))
        if path_is_clear_curve(player, targets, knots, allies, blocked, width, height):
            return build_interpolated_formula(knots), targets, knots
    return None


def build_formula(xp, xt, yt_minus_yp, dx, amp, freq):
    k = yt_minus_yp / dx
    if abs(amp) < 0.05:
        return f"{num(k)}*x"
    return (
        f"{num(k)}*x+({num(amp)})*sin({num(freq * math.pi)}*(x-({num(xp)}))/({num(dx)}))"
    )


def plan_shot(player, enemies, allies, blocked, width, height):
    """Выбирает безопасную траекторию с максимальным числом целей во взрыве."""
    xp, yp = player
    ally_mask = blocked.copy()
    scale = width / PLANE_W
    avoid_r = int(round((SOLDIER_RADIUS_PX + 8) * scale))
    for ax, ay, _pixel in allies:
        apx, apy = math_to_px(ax, ay, width, height)
        cv2.circle(ally_mask, (int(round(apx)), int(round(apy))), avoid_r, 255, -1)

    limit = y_limit(width, height) - 0.5
    amps = [0.0]
    for magnitude in np.linspace(0.35, max(2.0, limit * 0.95), 28):
        amps.append(float(magnitude))
        amps.append(float(-magnitude))

    best = None
    blast_radius = (EXPLOSION_RADIUS_PX + SOLDIER_RADIUS_PX) * scale
    for xt, yt, pixel in enemies:
        dx = xt - xp
        dy = yt - yp
        if abs(dx) < 0.35:
            continue
        killed = sum(
            math.hypot(pixel[0] - enemy_pixel[0], pixel[1] - enemy_pixel[1]) <= blast_radius
            for _ex, _ey, enemy_pixel in enemies
        )
        for freq in (1, 2):
            for amp in amps:
                if not path_is_clear(xp, yp, xt, yt, amp, freq, ally_mask, width, height):
                    continue
                score = (-killed, abs(amp), freq, math.hypot(dx, dy))
                if best is None or score < best[0]:
                    formula = build_formula(xp, xt, dy, dx, amp, freq)
                    best = (score, formula, (xt, yt), pixel, amp, freq, killed)
    return best


def formula_point(xp, yp, xt, yt, amp, freq, x):
    return trajectory_y(xp, yp, xt, yt, amp, freq, x)


def draw_debug(board, player_px, allies, enemies, chosen, amp, freq, plane_box, trajectory=None):
    canvas = board.copy()
    overlay = canvas.copy()
    mask = obstacle_mask(board)
    overlay[mask > 0] = (40, 40, 40)
    canvas = cv2.addWeighted(overlay, 0.35, canvas, 0.65, 0)
    height, width = board.shape[:2]

    if chosen is not None:
        xp, yp = frame_to_math(*player_px, width, height)
        xt, yt = chosen
        dx = xt - xp
        pts = []
        for index in range(80):
            x = xp + dx * index / 79
            y = trajectory(x) if trajectory is not None else formula_point(xp, yp, xt, yt, amp, freq, x)
            px, py = math_to_px(x, y, width, height)
            pts.append((int(round(px)), int(round(py))))
        cv2.polylines(canvas, [np.array(pts, np.int32)], False, (0, 220, 255), 2)

    cv2.circle(canvas, player_px, 10, (0, 220, 0), 2)
    for _mx, _my, pixel in allies:
        cv2.circle(canvas, pixel, 8, (255, 180, 0), 2)
    for _mx, _my, pixel in enemies:
        color = (0, 0, 255)
        if chosen is not None:
            cx, cy = frame_to_math(*pixel, width, height)
            if abs(cx - chosen[0]) < 0.05 and abs(cy - chosen[1]) < 0.05:
                color = (0, 0, 255)
                cv2.circle(canvas, pixel, 12, (0, 0, 255), 2)
                continue
        cv2.circle(canvas, pixel, 8, color, 1)

    path = os.path.join(_script_dir, "last_shot.png")
    cv2.imwrite(path, canvas)
    return path


def field_click(origin, plane, frame_shape):
    """Центр поля y= в экранных координатах, по сетке из GameScreen."""
    frame_h, frame_w = frame_shape[:2]
    left, top = origin
    if plane is not None and abs(frame_w / frame_h - CLIENT_W / CLIENT_H) > 0.12:
        px, py, pw, ph = plane
        click_x = left + px + (FIELD_CX - PLANE_X) / PLANE_W * pw
        click_y = top + py + (FIELD_CY - PLANE_Y) / PLANE_H * ph
        return int(round(click_x)), int(round(click_y))
    scale_x = frame_w / CLIENT_W
    scale_y = frame_h / CLIENT_H
    return int(round(left + FIELD_CX * scale_x)), int(round(top + FIELD_CY * scale_y))


def focus_window(hwnd):
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)


def type_formula(formula, click):
    pyperclip.copy(formula)
    pyautogui.click(click[0], click[1])
    time.sleep(0.08)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("backspace")
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    pyautogui.press("enter")


def analyze(fire):
    if not _busy.acquire(blocking=False):
        print("[INFO] Предыдущий расчёт ещё идёт.")
        return
    try:
        _analyze(fire)
    except Exception as error:
        print(f"[Ошибка] {error}")
    finally:
        _busy.release()


def _analyze(fire):
    print("\n[INFO] Смотрю на поле...")
    frame, left, top, hwnd, title = capture_screen()
    if title:
        print(f"[INFO] Окно: {title}")
    else:
        print("[INFO] Окно Graphwar не найдено, ищу поле на всём экране.")

    plane = locate_plane(frame)
    if plane is None:
        print("[Ошибка] Белое поле 770x450 не найдено. Откройте экран боя.")
        cv2.imwrite(os.path.join(_script_dir, "last_shot.png"), frame)
        return

    x, y, w, h = plane
    board = frame[y:y + h, x:x + w]
    soldiers, hue, sat, val, scale = find_soldiers(board)
    player_px = pick_player(soldiers, hue, sat, val, scale)
    if player_px is None:
        print(f"[Ошибка] Не вижу красный маркер вашего солдата. Найдено цветных точек: {len(soldiers)}.")
        cv2.imwrite(os.path.join(_script_dir, "last_shot.png"), board)
        return

    player, allies, enemies = classify_targets(player_px, soldiers, w, h)
    if not enemies:
        print(f"[Ошибка] Солдат на чужой половине нет. Своих рядом: {len(allies)}, всего точек: {len(soldiers)}.")
        draw_debug(board, player_px, allies, enemies, None, 0, 1, plane)
        return

    blocked = obstacle_mask(board)
    # Тёмные спрайты солдат не должны считаться стеной, иначе выстрел до цели не дойдёт.
    clear_r = int(round((EXPLOSION_RADIUS_PX + 2) * (w / PLANE_W)))
    for soldier in soldiers:
        cv2.circle(blocked, soldier, clear_r, 0, -1)

    multi_plan = plan_multi_curve(player, enemies, allies, blocked, w, h)
    if multi_plan is not None:
        formula, targets, knots = multi_plan
        target = (targets[-1][0], targets[-1][1])
        trajectory = lambda x: player[1] + interpolate_relative(knots, x)
        path = draw_debug(board, player_px, allies, enemies, target, 0, 1, plane, trajectory)
        print(f"[Успех] Одна кривая проходит через {len(targets)} из {len(enemies)} противников.")
        print("[Важно] Если игра останавливает снаряд при первом попадании, следующие цели не будут поражены.")
        if len(targets) < len(enemies):
            print("[Ограничение] Некоторые цели имеют слишком близкие координаты x для одной функции y=f(x).")
    else:
        print("[INFO] Общая кривая небезопасна; ищу выстрел с максимальным числом целей во взрыве.")
        planned = plan_shot(player, enemies, allies, blocked, w, h)
        if planned is None:
            print("[Ошибка] До врагов не добраться: дуга упирается в круги или в край поля.")
            draw_debug(board, player_px, allies, enemies, None, 0, 1, plane)
            return

        _score, formula, target, _pixel, amp, freq, killed = planned
        path = draw_debug(board, player_px, allies, enemies, target, amp, freq, plane)
        kind = "прямая" if abs(amp) < 0.05 else f"дуга {amp:+.2f}, частота {freq}"
        print(f"[Успех] Солдат ({player[0]:.2f}, {player[1]:.2f}) -> цель ({target[0]:.2f}, {target[1]:.2f}), {kind}")
        print(f"[Оценка] Один взрыв накроет примерно {killed} из {len(enemies)} противников.")
        if killed < len(enemies):
            print("[Ограничение] Противники вне радиуса этого взрыва; одна формула не гарантирует уничтожение всех.")

    print(f"[Формула] {formula}")
    print(f"[Картинка] {path}")

    if not fire:
        print("[F3] В игру ничего не отправлено.")
        return

    focus_window(hwnd)
    click = field_click((left, top), plane, frame.shape)
    type_formula(formula, click)
    print("[Готово] Формула вставлена и отправлена.")


def self_test():
    width, height = PLANE_W, PLANE_H
    # Углы поля совпадают с convertX/convertY из GraphPlane.java
    for mx, my in ((-25, 0), (25, 0), (0, 0), (10, 10), (-20, -14)):
        px, py = math_to_px(mx, my, width, height)
        rx, ry = frame_to_math(px, py, width, height)
        assert abs(rx - mx) < 1e-6, (mx, rx)
        assert abs(ry - my) < 1e-6, (my, ry)

    top_y = frame_to_math(0, 0, width, height)[1]
    assert abs(top_y - (height / 2) * GAME_LENGTH / width) < 1e-9
    assert abs(top_y - 14.61038961038961) < 1e-9

    # После вертикального сдвига игры кривая проходит через оба солдата.
    xp, yp, xt, yt = -12.0, 3.5, 14.0, -2.0
    for amp, freq in ((0, 1), (4.5, 1), (-3, 2)):
        for x, expected in ((xp, yp), (xt, yt)):
            raw = (yt - yp) / (xt - xp) * x + amp * math.sin(freq * math.pi * (x - xp) / (xt - xp))
            shift = yp - ((yt - yp) / (xt - xp) * xp)
            got = raw + shift
            assert abs(got - expected) < 1e-6, (amp, freq, x, got, expected)

    board = np.full((height, width, 3), 255, np.uint8)
    cv2.line(board, (0, height // 2), (width, height // 2), (0, 0, 0), 1)
    cv2.line(board, (width // 2, 0), (width // 2, height), (0, 0, 0), 1)
    mask = obstacle_mask(board)
    assert int(mask.sum()) == 0, "оси не должны быть препятствием"

    # Круг на прямой между солдатами. Прямая обязана отвергаться, дуга — проходить.
    p = math_to_px(-15, 0, width, height)
    t = math_to_px(15, 0, width, height)
    mid = math_to_px(0, 0, width, height)
    cv2.circle(board, (int(mid[0]), int(mid[1])), 36, (0, 0, 0), -1)
    blocked = obstacle_mask(board)
    assert not path_is_clear(-15, 0, 15, 0, 0, 1, blocked, width, height)
    assert path_is_clear(-15, 0, 15, 0, 6, 1, blocked, width, height)

    enemies = [(15.0, 0.0, (int(t[0]), int(t[1])))]
    planned = plan_shot((-15.0, 0.0), enemies, [], blocked, width, height)
    assert planned is not None
    assert abs(planned[4]) > 0.2
    assert "sin(" in planned[1]

    clear = np.zeros_like(blocked)
    planned_clear = plan_shot((-15.0, 0.0), enemies, [], clear, width, height)
    assert planned_clear is not None
    assert "sin(" not in planned_clear[1]
    clustered = [
        (10.0, 0.0, (int(math_to_px(10.0, 0.0, width, height)[0]), int(math_to_px(10.0, 0.0, width, height)[1]))),
        (10.5, 0.0, (int(math_to_px(10.5, 0.0, width, height)[0]), int(math_to_px(10.5, 0.0, width, height)[1]))),
        (18.0, 0.0, (int(math_to_px(18.0, 0.0, width, height)[0]), int(math_to_px(18.0, 0.0, width, height)[1]))),
    ]
    clustered_plan = plan_shot((-15.0, 0.0), clustered, [], clear, width, height)
    assert clustered_plan is not None
    assert clustered_plan[6] == 2, clustered_plan[6]

    multi_enemies = [
        (10.0, 2.5, tuple(int(round(value)) for value in math_to_px(10.0, 2.5, width, height))),
        (13.0, 2.8, tuple(int(round(value)) for value in math_to_px(13.0, 2.8, width, height))),
        (16.0, 3.1, tuple(int(round(value)) for value in math_to_px(16.0, 3.1, width, height))),
    ]
    multi_plan = plan_multi_curve((-15.0, 0.0), multi_enemies, [], clear, width, height)
    assert multi_plan is not None
    formula, targets, knots = multi_plan
    assert len(targets) == len(multi_enemies), len(targets)
    for tx, ty, _ in targets:
        assert abs(interpolate_relative(knots, tx) - ty) < 1e-6
        assert abs(eval(formula, {"__builtins__": {}}, {"x": tx}) - ty) < 1e-4
    assert "sin(" not in formula

    ally_px = tuple(int(round(value)) for value in math_to_px(0.0, 1.5, width, height))
    ally_on_path = [(0.0, 1.5, ally_px)]
    assert plan_multi_curve((-15.0, 0.0), multi_enemies, ally_on_path, clear, width, height) is None

    print("[TEST] координаты, сдвиг формулы, оси и обход круга — ок")
    print("[TEST] выбор цели с максимальным числом противников во взрыве — ок")
    print("[TEST] одна формула интерполирует несколько целей — ок")
    print("[TEST] солдаты для проверки:", p, t)


def main():
    if "--test" in sys.argv:
        self_test()
        return

    _dpi_aware()
    if keyboard is None:
        print("Нужен пакет keyboard: pip install keyboard")
        return

    print("=========================================")
    print("  Graphwar: точный выстрел")
    print("=========================================")
    print(" Режим игры: обычная функция (y = ...).")
    print(" Угол в этом режиме на траекторию не влияет.")
    print(" F3  — посчитать формулу, не стрелять")
    print(" F4  — посчитать и выстрелить")
    print(" Esc — выход")
    print(" Курсор в левый верхний угол экрана — аварийная остановка клика.")
    print("=========================================")

    keyboard.add_hotkey("f3", lambda: analyze(False))
    keyboard.add_hotkey("f4", lambda: analyze(True))
    keyboard.wait("esc")
    print("Выход.")


if __name__ == "__main__":
    main()
