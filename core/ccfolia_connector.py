# mypy: ignore-errors
# ================================
# ファイル: core/ccfolia_connector.py
# CCFolia連携 - チャット監視 + 自動投稿 + セッション記録 + エージェント機能
# ================================

import json
import re
import sys
import threading
import time
from pathlib import Path

# Selenium
from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# 同階層モジュール
sys.path.insert(0, str(Path(__file__).parent))
from ccfolia_map_controller import MAP_TOOLS, CCFoliaMapController, execute_map_tool
from character_manager import CharacterManager
from lm_client import LMClient
from main import PromptManager
from session_manager import SessionManager

# ==========================================
# CCFolia CSSセレクタ定義
# ==========================================

class CCFoliaSelectors:
    CHAT_LIST         = "ul.MuiList-root"
    CHAT_MESSAGES     = "ul.MuiList-root > div, ul.MuiList-root > li"
    CHAT_INPUT        = "textarea"
    SEND_BUTTON       = "button[class*='MuiButton']"
    PIECE_SELECT      = "[class*='MuiBox']"
    PIECE_ITEM        = "li, [role='option'], [role='menuitem']"


# ==========================================
# エージェント専用 ツール定義
# ==========================================

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "post_chat",
            "description": "CCFoliaに発言や情景描写を投稿する。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "投稿するテキスト"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "手番を終了する。",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

# マップ操作ツールと結合
ALL_TOOLS = AGENT_TOOLS + MAP_TOOLS


# ==========================================
# キャラクター判定ロジック
# ==========================================

class CharacterDetector:
    def __init__(self, character_manager: CharacterManager, default_id: str = "meta_gm"):
        self.cm = character_manager
        self.default_id = default_id
        self._build_keyword_map()

    def _build_keyword_map(self):
        self.keyword_map = {}
        for char_id, char in self.cm.characters.items():
            if not char.get("is_ai") or not char.get("enabled"): continue
            keywords = char.get("keywords", []) or [char.get("name", ""), char_id]
            self.keyword_map[char_id] = [k for k in keywords if k]

    def detect(self, message: str) -> list[str]:
        matched_ids = []
        for char_id, keywords in self.keyword_map.items():
            for kw in keywords:
                if kw and kw in message:
                    if char_id not in matched_ids: matched_ids.append(char_id)
                    break
        return matched_ids

    def reload(self):
        self.cm.load_characters()
        self._build_keyword_map()


# ==========================================
# セッション文脈管理
# ==========================================

class SessionContext:
    _DICE_RE = re.compile(r'\d*[dDbB]\d+|b\d+', re.IGNORECASE)
    _PHASE_KEYWORDS = {
        'combat': ['戦闘開始', '戦闘スタート', 'エンカウント', '敵が現れ'],
        'mission': ['ミッション開始', 'ミッションフェイズ', '突入'],
        'assessment': ['査定フェイズ', '帰還'],
        'briefing': ['ブリーフィング']
    }
    # フェイズの進行度を定義（一度進んだら自動判定では戻らないようにする）
    _PHASE_ORDER = {'free': 0, 'briefing': 1, 'mission': 2, 'combat': 3, 'assessment': 4}

    def __init__(self):
        self.phase = 'free'
        self.history = []

    def update_phase(self, body: str, is_ai: bool = False):
        if is_ai: return
        new_phase = self.phase
        for phase, keywords in self._PHASE_KEYWORDS.items():
            if any(kw in body for kw in keywords):
                new_phase = phase
                break
        if self._PHASE_ORDER.get(new_phase, 0) > self._PHASE_ORDER.get(self.phase, 0):
            self.phase = new_phase

    def add_message(self, speaker: str, body: str, is_ai: bool = False):
        self.history.append({'speaker': speaker, 'body': body})
        self.history = self.history[-100:]
        self.update_phase(body, is_ai)

    def get_context_summary(self) -> str:
        lines = [f"[{m['speaker']}]: {m['body']}" for m in self.history[-25:]]
        return f"【フェイズ: {self.phase.upper()}】\n【直近の会話】\n" + '\n'.join(lines)


# ==========================================
# CCFolia コネクター本体
# ==========================================

class CCFoliaConnector:
    POLL_INTERVAL = 2.0
    POST_DELAY    = 1.0
    AI_PREFIX     = "[AI] "

    def __init__(
        self,
        room_url: str,
        default_character_id: str = "meta_gm",
        headless: bool = False,
        poll_interval: float = None,
    ):
        self.room_url = room_url
        self.poll_interval = poll_interval or self.POLL_INTERVAL
        self.headless = headless

        self.lm_client = LMClient()
        self.cm = CharacterManager()
        self.pm = PromptManager()
        self.detector = CharacterDetector(self.cm, default_id=default_character_id)
        self.ctx = SessionContext()
        self.sm = SessionManager(Path(__file__).parent.parent)
        self.world_setting = self._load_world_setting()

        self.driver: webdriver.Chrome | None = None
        self.map_ctrl: CCFoliaMapController | None = None
        self._known_messages: list[dict] = []
        self._sent_bodies: set[str] = set()
        self._running = False

    def _init_driver(self):
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--lang=ja")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option('useAutomationExtension', False)

        base = Path.home() / "AppData/Local/TacticalAI"
        base.mkdir(parents=True, exist_ok=True)
        profile_dir = base / "ChromeProfile_AI"
        profile_dir.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--user-data-dir={profile_dir}")

        opts.add_argument(f"--app={self.room_url}")

        print("⏳ AI専用のアプリウィンドウを自動起動しています...")
        self.driver = webdriver.Chrome(options=opts)

        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {
                  get: () => undefined
                })
            '''
        })

        if self.driver.current_url == "data:,":
             self.driver.get(self.room_url)

        print(f"✓ CCFoliaアプリに接続: {self.room_url}")

        try:
            WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, CCFoliaSelectors.CHAT_INPUT)))
            print("✓ チャット入力欄を確認")
        except TimeoutException:
            print("⚠️  チャット入力欄が見つかりません。（ログインや入室操作を行ってください）")

        self.map_ctrl = CCFoliaMapController(self.driver)

    def _close_driver(self):
        if self.driver:
            try: self.driver.quit()
            except Exception: pass
            finally: self.driver, self.map_ctrl = None, None

    def _close_alert(self):
        try:
            alert = self.driver.switch_to.alert
            alert.dismiss()
        except Exception: pass

    def _get_chat_messages(self) -> list[dict]:
        messages = []
        try:
            items = self.driver.find_elements(By.CSS_SELECTOR, "div.MuiListItemText-root")
            for el in items:
                lines = [l.strip() for l in el.text.strip().splitlines() if l.strip()]
                if len(lines) >= 2:
                    speaker, body = lines[0], " ".join(lines[1:])
                    if speaker not in ["メイン", "情報", "noname"] and "[AI]" not in speaker:
                        messages.append({"speaker": speaker, "body": body})
        except Exception: pass
        return messages

    # ★ 最強自動入力ロジック（JavaScriptによる瞬間ペースト）
    def _post_message(self, character_name: str, text: str) -> bool:
        import time

        from selenium.webdriver.common.by import By
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.2)

            # 駒選択
            try:
                self.driver.find_element(By.CSS_SELECTOR, CCFoliaSelectors.PIECE_SELECT).click()
                time.sleep(0.3)
                for item in self.driver.find_elements(By.CSS_SELECTOR, CCFoliaSelectors.PIECE_ITEM):
                    if character_name in item.text:
                        item.click()
                        time.sleep(0.2)
                        break
                else:
                    self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception: pass

            time.sleep(0.2)
            inputs = self.driver.find_elements(By.CSS_SELECTOR, CCFoliaSelectors.CHAT_INPUT)
            if len(inputs) >= 2:
                inputs[0].send_keys(Keys.CONTROL + "a", character_name)

            input_el = inputs[-1]
            input_el.click()
            input_el.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
            time.sleep(0.1)

            # ★【修正ポイント】改行(\n)が勝手に「送信」にならないよう、Shift+Enterで入力する
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if line:
                    input_el.send_keys(line)
                # 最後の行以外は、Shift+Enterで改行を入力
                if i < len(lines) - 1:
                    input_el.send_keys(Keys.SHIFT, Keys.RETURN)

            time.sleep(0.3)
            # 最後にEnterを1回だけ押して確実に送信する
            input_el.send_keys(Keys.RETURN)
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"❌ 送信エラー: {e}")
            return False

    def _post_system_message(self, character_name: str, text: str):
        tagged = self.AI_PREFIX + text
        ok = self._post_message(character_name, tagged)
        if ok:
            self._sent_bodies.add(tagged[:80])
            self.ctx.add_message(character_name, tagged, is_ai=True)

    def _run_agent_loop(self, target_char: dict, target_id: str, enriched_body: str):
        char_name = target_char["name"]
        prompt_tmpl = self.pm.get_template(target_char.get("prompt_id"))

        sys_prompt = (
            f"{self.world_setting}\n\n{prompt_tmpl['system'] if prompt_tmpl else ''}\n\n"
            "【GMアクション指示】\n"
            "あなたはGMです。画像とチャットを見て次に行うべきことを判断してください。\n"
            "1. まず `[思考]` と `[/思考]` のタグの中で、状況を分析してください。\n"
            "2. 分析が終わったら、必ず `post_chat` ツールを使ってプレイヤーに発言してください。\n"
            "3. 発言が終わったら `finish` ツールで終了してください。\n\n"
            "【ステータス管理の絶対ルール】\n"
            "敵やPCのHP・MPなどのステータスはあなたが頭の中で計算・管理してください。ダメージや回復など、ステータスに変動があった際は、発言の末尾に必ず「(現在HP: 敵A 5/10, 敵B 10/10)」のように明記してステータス管理を行ってください。"
        )

        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": enriched_body}]
        print(f"\n🤖 エージェントループ開始 (最大3手番): {char_name}")

        try:
            for _step in range(3):
                screenshot_b64 = None
                try: screenshot_b64 = self.driver.get_screenshot_as_base64()
                except Exception: pass

                content, tool_calls = self.lm_client.generate_with_tools(
                    messages, ALL_TOOLS, temperature=0.7, max_tokens=1500, image_base64=screenshot_b64
                )

                if content is None and tool_calls is None:
                    print("   ⚠️ APIからの応答がありませんでした。ループを中断します。")
                    self._post_system_message(char_name, "（システム: 思考処理がタイムアウトしました。処理をスキップします）")
                    break

                if content and not tool_calls:
                    text = self.lm_client._clean_response(content)
                    if text:
                        self._post_message(char_name, f"[AI] {text}")
                        self.ctx.add_message(char_name, text, is_ai=True)
                        print(f"   ✓ (自動投稿): {text[:40]}...")
                    else:
                        print("   ⚠️ 有効なテキストがありませんでした。思考ループによる自爆と判断し終了します。")
                        self._post_system_message(char_name, "（システム: AIが考え込んでフリーズしました。再度指示を出してあげてください）")
                    break

                if not tool_calls:
                    break

                messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

                finished = False
                for tc in tool_calls:
                    f_name = tc["function"]["name"]
                    f_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                    print(f"   🛠️ ツール実行: {f_name}")

                    if f_name == "finish":
                        finished = True
                    elif f_name == "post_chat":
                        text = f_args.get("text", "")
                        if text:
                            tagged = self.AI_PREFIX + text
                            ok = self._post_message(char_name, tagged)
                            if ok:
                                self._sent_bodies.add(tagged[:80])
                                self.ctx.add_message(char_name, tagged, is_ai=True)
                                print(f"      ✓ 発言: {text[:40]}...")
                    elif self.map_ctrl:
                        execute_map_tool(self.map_ctrl, f_name, f_args)

                if finished: break
            else:
                self._post_system_message(char_name, "（システム: 思考ループが上限に達したため処理を中断しました。別のアプローチで指示してください）")

        except Exception as e:
            print(f"   ❌ エージェントループ内で重大なエラーが発生しました: {str(e)}")
            self._post_system_message(char_name, "（システム: 予期せぬエラーが発生しました。GMの処理をスキップします）")

    def _load_world_setting(self) -> str:
        ws_path = self.sm.configs_dir / "world_setting.json"
        if ws_path.exists():
            try:
                with open(ws_path, encoding='utf-8') as f:
                    data = json.load(f)
                return "\n".join(v for k, v in data.items() if v)
            except Exception: pass
        return ""

    def _monitor_loop(self):
        print("👁️  チャット監視開始")
        time.sleep(2)
        self._known_messages = [f"{m['speaker']}|{m['body']}" for m in self._get_chat_messages()]

        while self._running:
            self._close_alert()
            current = self._get_chat_messages()
            new_msgs = [m for m in current if f"{m['speaker']}|{m['body']}" not in self._known_messages]

            if new_msgs:
                for msg in new_msgs:
                    speaker, body = msg["speaker"], msg["body"]
                    self._known_messages.append(f"{speaker}|{body}")
                    self.sm.log_message(speaker, body)
                    self.ctx.add_message(speaker, body)
                    print(f"\n📨 新着: [{speaker}] {body[:40]}")

                    if "＞" in body or any(k in body for k in self.detector.keyword_map.get("meta_gm", [])):
                        target_char = self.cm.get_character("meta_gm")
                        enriched = f"{self.ctx.get_context_summary()}\n\n【今回反応すべき発言】\n[{speaker}]: {body}"

                        if self.ctx.phase in ['combat', 'mission']:
                            self._run_agent_loop(target_char, "meta_gm", enriched)
                        else:
                            # ★ 修正ポイント: プロンプトIDから「中身」を正しく取得し、世界観データと結合する
                            prompt_tmpl = self.pm.get_template(target_char.get("prompt_id"))
                            sys_prompt = f"{self.world_setting}\n\n{prompt_tmpl.get('system', '') if prompt_tmpl else ''}\n※重要: 内部の思考プロセス（Thinking Process）は極力短く済ませ、プレイヤーへの返答テキストを直ちに出力してください。"

                            # ★ 修正ポイント: max_tokensを増やして息切れ（lengthエラー）を防止
                            res, _ = self.lm_client.generate_response(
                                system_prompt=sys_prompt,
                                user_message=enriched,
                                max_tokens=4096
                            )

                            if res:
                                self._post_message(target_char["name"], f"[AI] {res}")
                                self.ctx.add_message(target_char["name"], res, is_ai=True)
                                print(f"   ✓ 応答: {res[:40]}...")

            self._known_messages = self._known_messages[-300:]
            time.sleep(self.poll_interval)

    # ★ 追加: ランチャーからの「送信命令」を受け取るための監視ループ
    def _stdin_monitor_loop(self):
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try:
                data = json.loads(line)
                if data.get("type") == "chat":
                    text = data.get("text", "")
                    char_name = data.get("character", "GM")
                    print(f"📥 ランチャーから送信命令を受信: {text[:20]}...")
                    self._post_message(char_name, text)
                elif data.get("type") == "quit":
                    self._running = False
                    break
            except json.JSONDecodeError:
                pass
            except Exception as e:
                print(f"❌ 標準入力の処理エラー: {e}")

    def start(self):
        print("=" * 50 + "\nタクティカル祓魔師 CCFolia連携\n" + "=" * 50)
        self.sm.start_new_session("CCFoliaSession")
        self._init_driver()
        self._running = True

        # セッション監視と標準入力監視を同時に実行
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        threading.Thread(target=self._stdin_monitor_loop, daemon=True).start()

        try:
            while self._running: time.sleep(1)
        except KeyboardInterrupt:
            self._running = False
            print("終了します")
            self._close_driver()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", required=True)
    parser.add_argument("--default", default="meta_gm")
    args = parser.parse_args()
    CCFoliaConnector(args.room, args.default).start()
