"""
test_ccfolia_map_controller.py — CCFoliaMapController のユニットテスト

Selenium WebDriver は MagicMock で差し替える。
DOM 操作・pyautogui は呼ばれないのでテスト環境でも安全。

テスト対象:
  - get_board_state()
  - move_piece()
  - _parse_xy()
  - _hash()
"""
from unittest.mock import patch

import pytest

from core.ccfolia_map_controller import GRID_SIZE, CCFoliaMapController


@pytest.fixture
def controller(mock_driver) -> CCFoliaMapController:
    return CCFoliaMapController(driver=mock_driver)


# ──────────────────────────────────────────
# _parse_xy
# ──────────────────────────────────────────

class TestParseXY:
    def test_normal_translate(self, controller):
        assert controller._parse_xy("translate(96px, 192px)") == (96, 192)

    def test_negative_values(self, controller):
        assert controller._parse_xy("translate(-48px, -96px)") == (-48, -96)

    def test_float_values_truncated(self, controller):
        x, y = controller._parse_xy("translate(96.5px, 192.8px)")
        assert x == 96
        assert y == 192

    def test_invalid_returns_zero(self, controller):
        assert controller._parse_xy("no-transform") == (0, 0)

    def test_none_returns_zero(self, controller):
        assert controller._parse_xy(None) == (0, 0)


# ──────────────────────────────────────────
# _hash
# ──────────────────────────────────────────

class TestHash:
    def test_extracts_8_chars_from_shared(self, controller):
        url = "https://ccfolia.com/shared/abcdef1234567890/img.png"
        assert controller._hash(url) == "abcdef12"

    def test_extracts_from_files_path(self, controller):
        url = "https://ccfolia.com/files/deadbeef99999999/piece.png"
        assert controller._hash(url) == "deadbeef"

    def test_returns_empty_on_no_match(self, controller):
        assert controller._hash("https://example.com/img.png") == ""

    def test_none_url_returns_empty(self, controller):
        assert controller._hash(None) == ""


# ──────────────────────────────────────────
# get_board_state
# ──────────────────────────────────────────

class TestGetBoardState:
    def test_returns_list(self, controller):
        state = controller.get_board_state()
        assert isinstance(state, list)

    def test_piece_has_required_keys(self, controller):
        state = controller.get_board_state()
        assert len(state) > 0
        piece = state[0]
        for key in ("index", "img_hash", "img_url", "px_x", "px_y", "grid_x", "grid_y", "vx", "vy"):
            assert key in piece, f"キー '{key}' が欠けています"

    def test_grid_calculated_from_px(self, controller):
        """px / GRID_SIZE が grid 座標になっているか"""
        state = controller.get_board_state()
        piece = state[0]
        assert piece["grid_x"] == round(piece["px_x"] / GRID_SIZE)
        assert piece["grid_y"] == round(piece["px_y"] / GRID_SIZE)

    def test_img_hash_extracted(self, controller):
        state = controller.get_board_state()
        piece = state[0]
        assert piece["img_hash"] == "abcdef12"

    def test_empty_board(self, mock_driver):
        mock_driver.execute_script.return_value = []
        ctrl = CCFoliaMapController(driver=mock_driver)
        assert ctrl.get_board_state() == []


# ──────────────────────────────────────────
# move_piece
# ──────────────────────────────────────────

class TestMovePiece:
    def test_returns_false_when_piece_not_found(self, controller, capsys):
        result = controller.move_piece("nonexistent", 3, 4)
        assert result is False
        captured = capsys.readouterr()
        assert "見つかりません" in captured.out

    def test_calls_drag_when_piece_found(self, controller):
        """img_hash が一致する駒がある場合、_drag が呼ばれること"""
        with patch.object(controller, "_drag", return_value=True) as mock_drag:
            result = controller.move_piece("abcdef12", 2, 3)
        mock_drag.assert_called_once()
        assert result is True

    def test_target_grid_passed_to_drag(self, controller):
        """move_piece に渡した grid 座標が _drag の pixel 座標に変換されること"""
        with patch.object(controller, "_drag", return_value=True) as mock_drag:
            controller.move_piece("abcdef12", 5, 7)
        # _drag(pieces, target_piece, dest_px_x, dest_px_y) の引数を確認
        args = mock_drag.call_args[0]
        assert args[2] == 5 * GRID_SIZE  # dest_px_x
        assert args[3] == 7 * GRID_SIZE  # dest_px_y
