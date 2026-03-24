"""
ccfolia_map_controller.py — 最小版
pyautogui ドラッグのみ。スケールは駒座標から実測。DPI補正あり。
"""

import logging
import re
import time

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

logger = logging.getLogger(__name__)
GRID_SIZE = 96


def _dpi_scale():
    """Windowsの画面拡大率を取得（例: 150% → 1.5）"""
    try:
        import ctypes

        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class CCFoliaMapController:
    def __init__(self, driver):
        self.driver = driver

    def _parse_xy(self, style):
        m = re.search(r"translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)", style or "")
        return (int(float(m.group(1))), int(float(m.group(2)))) if m else (0, 0)

    def _hash(self, url):
        m = re.search(r"/(?:shared|files)/([a-f0-9]+)", url or "")
        return m.group(1)[:8] if m else ""

    # ──────────────────────────────────────────
    # 公開API
    # ──────────────────────────────────────────

    def get_board_state(self):
        raw = self.driver.execute_script("""
            const out = [];
            document.querySelectorAll('.movable').forEach((el, i) => {
                const t = el.style.transform;
                if (!t || !t.includes('translate(')) return;
                const img = el.querySelector('img');
                const r = el.getBoundingClientRect();
                out.push({
                    index: i, transform: t,
                    imgSrc: img ? img.src : '',
                    vx: r.left + r.width/2,
                    vy: r.top  + r.height/2
                });
            });
            return out;
        """)
        result = []
        for p in raw:
            px, py = self._parse_xy(p["transform"])
            result.append(
                {
                    "index": p["index"],
                    "img_hash": self._hash(p["imgSrc"]),
                    "img_url": p["imgSrc"],
                    "px_x": px,
                    "px_y": py,
                    "grid_x": round(px / GRID_SIZE),
                    "grid_y": round(py / GRID_SIZE),
                    "vx": p["vx"],
                    "vy": p["vy"],  # ビューポート座標（デバッグ用）
                }
            )
        return result

    def move_piece(self, img_hash, grid_x, grid_y):
        pieces = self.get_board_state()
        targets = [p for p in pieces if img_hash in p["img_url"]]
        if not targets:
            print(f"   ❌ 駒が見つかりません: {img_hash}")
            return False
        if len(targets) > 1:
            print(f"   同じ画像が{len(targets)}個。最初の1つを操作します。")
        t = targets[0]
        return self._drag(pieces, t, grid_x * GRID_SIZE, grid_y * GRID_SIZE)

    def move_piece_by_current_pos(self, cur_grid_x, cur_grid_y, dst_grid_x, dst_grid_y):
        pieces = self.get_board_state()
        targets = [p for p in pieces if p["grid_x"] == cur_grid_x and p["grid_y"] == cur_grid_y]
        if not targets:
            print(f"   ❌ 座標({cur_grid_x},{cur_grid_y})に駒なし")
            return False
        return self._drag(pieces, targets[0], dst_grid_x * GRID_SIZE, dst_grid_y * GRID_SIZE)

    def pan_map(self, direction, grid_amount=1):
        key = {
            "up": Keys.ARROW_UP,
            "down": Keys.ARROW_DOWN,
            "left": Keys.ARROW_LEFT,
            "right": Keys.ARROW_RIGHT,
        }.get(direction)
        if not key:
            return
        try:
            self.driver.find_element(
                By.CSS_SELECTOR, '[class*="map"],[class*="board"],[class*="field"]'
            ).click()
        except Exception:
            self.driver.find_element(By.TAG_NAME, "body").click()
        ac = ActionChains(self.driver)
        for _ in range(grid_amount):
            ac.send_keys(key).pause(0.03)
        ac.perform()

    # ──────────────────────────────────────────
    # ドラッグ本体
    # ──────────────────────────────────────────

    def _drag(self, pieces, target, tgt_px_x, tgt_px_y):
        try:
            import pyautogui
        except ImportError:
            print("❌ pip install pyautogui が必要です")
            return False

        # ── スケール実測（最も離れた2駒のビューポート距離÷マップ距離）──
        scale = self._measure_scale(pieces)

        # ── Chromeウィンドウのスクリーン上の位置 ──
        wx = self.driver.execute_script("return window.screenX ?? window.screenLeft ?? 0")
        wy = self.driver.execute_script("return window.screenY ?? window.screenTop  ?? 0")
        ih = self.driver.execute_script("return window.innerHeight")
        oh = self.driver.execute_script("return window.outerHeight")
        ui_h = max(oh - ih, 40)

        # ── 駒の開始スクリーン座標 ──
        dpi = _dpi_scale()
        src_x = (wx + target["vx"]) / dpi
        src_y = (wy + ui_h + target["vy"]) / dpi

        # ── 移動先スクリーン座標 ──
        dx = (tgt_px_x - target["px_x"]) * scale / dpi
        dy = (tgt_px_y - target["px_y"]) * scale / dpi
        dst_x = src_x + dx
        dst_y = src_y + dy

        print(
            f"   🖱️  scale={scale:.4f}  dpi={dpi:.2f}"
            f"  src=({src_x:.0f},{src_y:.0f}) dst=({dst_x:.0f},{dst_y:.0f})"
            f"  Δmap=({tgt_px_x - target['px_x']},{tgt_px_y - target['px_y']})"
        )

        pyautogui.PAUSE = 0.0
        pyautogui.moveTo(int(src_x), int(src_y), duration=0.15)
        time.sleep(0.25)
        pyautogui.mouseDown(button="left")
        time.sleep(0.3)
        pyautogui.moveTo(int(dst_x), int(dst_y), duration=0.45)
        time.sleep(0.15)
        pyautogui.mouseUp(button="left")
        time.sleep(0.5)
        return True

    def _measure_scale(self, pieces):
        """最も離れた2駒のビューポート距離÷マップ距離"""
        best_scale, best_dist = 1.0, 0.0
        n = min(len(pieces), 30)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = pieces[i], pieces[j]
                map_d = ((b["px_x"] - a["px_x"]) ** 2 + (b["px_y"] - a["px_y"]) ** 2) ** 0.5
                view_d = ((b["vx"] - a["vx"]) ** 2 + (b["vy"] - a["vy"]) ** 2) ** 0.5
                if map_d > best_dist and map_d > 0:
                    best_dist = map_d
                    best_scale = view_d / map_d
        return best_scale


# ── エージェント用 ────────────────────────────────

MAP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_board_state",
            "description": "全駒の位置情報を取得",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_piece",
            "description": "img_hashで駒を特定してグリッド座標に移動",
            "parameters": {
                "type": "object",
                "properties": {
                    "img_hash": {"type": "string"},
                    "grid_x": {"type": "integer"},
                    "grid_y": {"type": "integer"},
                },
                "required": ["img_hash", "grid_x", "grid_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_piece_by_current_pos",
            "description": "現在グリッド座標で駒を特定して移動",
            "parameters": {
                "type": "object",
                "properties": {
                    "cur_grid_x": {"type": "integer"},
                    "cur_grid_y": {"type": "integer"},
                    "dst_grid_x": {"type": "integer"},
                    "dst_grid_y": {"type": "integer"},
                },
                "required": ["cur_grid_x", "cur_grid_y", "dst_grid_x", "dst_grid_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pan_map",
            "description": "マップをスクロール",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "grid_amount": {"type": "integer"},
                },
                "required": ["direction"],
            },
        },
    },
]


def execute_map_tool(ctrl, tool_name, args):
    if tool_name == "get_board_state":
        return ctrl.get_board_state()
    if tool_name == "move_piece":
        return ctrl.move_piece(**args)
    if tool_name == "move_piece_by_current_pos":
        return ctrl.move_piece_by_current_pos(**args)
    if tool_name == "pan_map":
        return ctrl.pan_map(**args)
    return {"error": f"未知: {tool_name}"}
