import requests
import json
import re
import copy
from typing import Optional

class LMClient:
    def __init__(self, base_url: str = "http://localhost:1234", model: str = "local-model"):
        self.base_url = base_url
        self.model = model

    def is_server_running(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/v1/models", timeout=3)
            return response.status_code == 200
        except:
            return False

    def _clean_response(self, text: str) -> str:
        """AIの余計な独り言や思考プロセスを完全に削ぎ落とし、純粋なJSONだけを抽出する"""
        
        # 1. もし <think> タグが含まれていたら、その後ろだけを切り出す
        if "</think>" in text:
            text = text.split("</think>")[-1]
            
        # 2. 「思考プロセス：」などの日本語の独り言が含まれていた場合、
        #    最初の `{` が出現するまでの文字をすべてゴミとして切り捨てる
        first_brace_idx = text.find('{')
        if first_brace_idx != -1:
            # 最初の { から後ろを切り出す
            text = text[first_brace_idx:]
            
        # 3. 最後の `}` より後ろにあるゴミ（「出力完了しました」など）を切り捨てる
        last_brace_idx = text.rfind('}')
        if last_brace_idx != -1:
            # 最初の { から最後の } までを正確に抜き出す
            text = text[:last_brace_idx + 1]
            
        # 4. マークダウン（```json 〜 ```）が残っていたら綺麗に剥がす
        import re
        cb = chr(96) * 3
        pattern = cb + r'(?:json)?\s*(\{.*?\})\s*' + cb
        match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1)
            
        return text.strip()

    def generate_response(
        self, system_prompt: str, user_message: str, temperature: float = 0.75,
        max_tokens: int = 300, timeout: Optional[int] = 600,
        top_p: float = 0.9, top_k: int = 20, presence_penalty: float = 0.0, repetition_penalty: float = 1.0, min_p: float = 0.0,
        no_think: bool = False
    ):
        if not self.is_server_running():
            return None, None

        # Qwen3系などの思考モデルで思考を抑制する
        # chat_template_kwargs でllama.cpp/LM Studioに指示し、
        # システムプロンプト先頭の /no_think でモデルにも直接指示する
        effective_system = system_prompt
        if no_think:
            effective_system = "/no_think\n" + system_prompt

        messages = [
            {"role": "system", "content": effective_system},
            {"role": "user", "content": user_message},
        ]

        payload_messages = copy.deepcopy(messages)

        payload = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "top_k": top_k,
            "min_p": min_p,
            "presence_penalty": presence_penalty,
            "repetition_penalty": repetition_penalty,
            "stream": False,
        }
        if no_think:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        try:
            response = requests.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=timeout)
            if response.status_code == 200:
                result = response.json()
                message = result["choices"][0]["message"]
                
                # content を取得し、空の場合は reasoning_content にフォールバック
                # （no_think を指定してもモデルが思考トークンに全出力を入れてしまう場合の対策）
                raw_content = message.get("content") or ""
                finish_reason = result["choices"][0].get("finish_reason", "")

                if not raw_content.strip():
                    # finish_reason が "length" の場合、モデルが思考トークンで
                    # max_tokens を使い切っており、reasoning_content は不完全な
                    # 思考テキストなのでフォールバックしても意味がない
                    if finish_reason != "length":
                        raw_content = message.get("reasoning_content") or ""

                # ログを見ると、AIがJSONの中にさらに思考を書き込んでいる場合があるため、クリーン処理にかける
                content = self._clean_response(raw_content)
                tool_calls = message.get("tool_calls") or None

                return content, tool_calls
            return None, None
        except Exception as e:
            print(f"   ⚠️  LM-Studio通信エラー: {str(e)}")
            return None, None