"""钉住本机模型那条路：发去哪、发什么、空回答怎么办。

这条路的 bug 是**实测才发现的**，而且藏得很深：
思考型模型（qwen3.5）在 /v1 上传 `think:false` 会被**忽略**，答案全被挤进
`reasoning` 字段、`content` 是空的 —— 五道评测题四道"回答为空"，裁定却写成
"未达门槛 1/5"。所以这里三件事都要钉死：

  ① 必须发到 ollama 的**原生** /api/chat（不是 /v1）
  ② 必须带上 `think: false`（带上不代表对方认，所以①也得钉）
  ③ 空回答必须**抛错**，不许悄悄返回空字符串（上游会把"没测成"当成"答错了"）
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import freeanalyst                                            # noqa: E402


class FakeGuard:
    """替掉 guarded_request：把"发去哪、发了什么"记下来，然后按脚本回复。"""

    def __init__(self, reply: dict):
        self.reply = reply
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, data=None, headers=None, purpose=""):
        self.calls.append((url, json.loads(data.decode("utf-8"))))
        return json.dumps(self.reply).encode("utf-8")


class TestLocalCallShape(unittest.TestCase):
    def setUp(self):
        self._orig = freeanalyst.guarded_request

    def tearDown(self):
        freeanalyst.guarded_request = self._orig

    def _call(self, reply: dict, **kw):
        fake = FakeGuard(reply)
        freeanalyst.guarded_request = fake
        text = freeanalyst.call_model("qwen3.5:9b", "系统", "问题", **kw)
        return text, fake.calls

    def test_goes_to_ollama_native_endpoint(self):
        """① 必须走原生 /api/chat —— /v1 上关不掉思考。"""
        _, calls = self._call({"message": {"role": "assistant", "content": "答案"}})
        self.assertEqual(len(calls), 1)
        url, _ = calls[0]
        self.assertTrue(url.endswith("/api/chat"), url)
        self.assertNotIn("/v1/", url)

    def test_sends_think_false(self):
        """② 必须带 think:false，并且是 ollama 原生的字段位置。"""
        _, calls = self._call({"message": {"content": "答案"}})
        body = calls[0][1]
        self.assertIs(body.get("think"), False)
        self.assertNotIn("max_tokens", body)               # /v1 的写法不能混进来
        self.assertEqual(body["options"]["num_predict"], 4096)
        self.assertEqual(body["stream"], False)

    def test_empty_content_raises_not_silent(self):
        """③ 空回答必须抛错并说明原因 —— 返回 "" 等于把"没测成"伪装成"答错"。"""
        reply = {"message": {"role": "assistant", "content": "",
                             "thinking": "想了一千字"}}
        with self.assertRaises(RuntimeError) as cm:
            self._call(reply)
        msg = str(cm.exception)
        self.assertIn("空回答", msg)
        self.assertIn("思考", msg)                          # 点出思考占了预算

    def test_whitespace_only_content_also_raises(self):
        with self.assertRaises(RuntimeError):
            self._call({"message": {"content": "   \n  "}})

    def test_thinking_field_is_never_returned_as_the_answer(self):
        """思考内容绝不能当成答案递出去（那是内部草稿，不是结论）。"""
        reply = {"message": {"content": "正式答案", "thinking": "内部草稿"}}
        text, _ = self._call(reply)
        self.assertEqual(text, "正式答案")
        self.assertNotIn("草稿", text)


if __name__ == "__main__":
    unittest.main()
