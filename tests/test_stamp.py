"""版本戳的测试。

★ 钉住三件事：

    ① **改一个字符，指纹就变** —— 这是整道闸的基础
    ② **判据文件变了也要变** —— 只哈希壳子等于没有闸（量测脚本常常只是壳子）
    ③ **缺文件不许静默跳过** —— 少算一个文件的指纹，结果看起来完全正常

不钉「戳里有什么字段」—— 那些会长。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench import stamp


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        self._d = tempfile.TemporaryDirectory()
        self.root = Path(self._d.name)
        self.script = self.root / "run.py"
        self.script.write_text("print('v1')\n")
        self.crit = self.root / "crit.py"
        self.crit.write_text("OK = True\n")

    def tearDown(self) -> None:
        self._d.cleanup()


class TestFingerprint(Temp):
    def test_same_content_same_fingerprint(self):
        a = stamp.fingerprint([self.script])
        b = stamp.fingerprint([self.script])
        self.assertEqual(a, b)

    def test_one_char_change_changes_it(self):
        """★ 整道闸的基础：文件内容一变，指纹就变。"""
        before = stamp.fingerprint([self.script])
        self.script.write_text("print('v2')\n")
        self.assertNotEqual(before, stamp.fingerprint([self.script]))

    def test_adding_a_dep_changes_it(self):
        only = stamp.fingerprint([self.script])
        with_dep = stamp.fingerprint([self.script, self.crit])
        self.assertNotEqual(only, with_dep)

    def test_dep_content_change_also_changes_it(self):
        """★ 判据文件改了也必须变 —— 否则壳子没改、闸就漏了。"""
        before = stamp.fingerprint([self.script, self.crit])
        self.crit.write_text("OK = False\n")
        self.assertNotEqual(before, stamp.fingerprint([self.script, self.crit]))

    def test_missing_file_raises(self):
        """★ 缺文件不许静默跳过。少算一个文件的指纹，结果看起来完全正常。"""
        with self.assertRaises(FileNotFoundError):
            stamp.fingerprint([self.script, self.root / "nope.py"])

    def test_missing_file_error_names_it(self):
        with self.assertRaises(FileNotFoundError) as cm:
            stamp.fingerprint([self.script, self.root / "nope.py"])
        self.assertIn("nope.py", str(cm.exception))

    def test_order_does_not_matter(self):
        a = stamp.fingerprint([self.script, self.crit])
        b = stamp.fingerprint([self.crit, self.script])
        self.assertEqual(a, b)


class TestMakeAndAttach(Temp):
    def test_make_records_what_was_hashed(self):
        st = stamp.make(self.script, [self.crit], pages=7)
        self.assertEqual(st["script"], "run.py")
        self.assertEqual(st["hashed"], ["crit.py", "run.py"])
        self.assertEqual(st["params"], {"pages": 7})

    def test_attach_puts_stamp_on_result(self):
        r = stamp.attach({"rows": []}, self.script, [self.crit], pages=7)
        self.assertIn(stamp.STAMP, r)
        self.assertEqual(r["rows"], [])

    def test_stamp_survives_json(self):
        """★ 戳必须能落盘再读回来 —— 迟到通知只给得到文本。"""
        r = stamp.attach({"rows": [1]}, self.script, [self.crit])
        back = json.loads(json.dumps(r))
        self.assertEqual(back[stamp.STAMP]["fingerprint"],
                         r[stamp.STAMP]["fingerprint"])


class TestCheck(Temp):
    def test_fresh_result_passes(self):
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        ok, why = stamp.check(r, self.script, [self.crit])
        self.assertTrue(ok, why)

    def test_no_stamp_fails(self):
        """没有戳 = 说不清是哪一版 → **不许默认通过**。"""
        ok, why = stamp.check({"x": 1}, self.script, [self.crit])
        self.assertFalse(ok)
        self.assertIn("版本戳", why)

    def test_script_changed_fails(self):
        """★ 用户真踩到的那件事：脚本改过、成绩单还是旧的。"""
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        self.script.write_text("print('改过了')\n")
        ok, why = stamp.check(r, self.script, [self.crit])
        self.assertFalse(ok)
        self.assertIn("作废", why)

    def test_dep_changed_fails(self):
        """判据改了也算过期 —— 「算法旧了」比「报错了」更危险。"""
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        self.crit.write_text("OK = False\n")
        ok, why = stamp.check(r, self.script, [self.crit])
        self.assertFalse(ok)
        self.assertIn("作废", why)

    def test_why_says_which_kind_of_mismatch(self):
        """不一致要说清是**哪一类**不同，不能只说「不一致」。"""
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        self.script.write_text("print('v2')\n")
        _, changed = stamp.check(r, self.script, [self.crit])
        _, added = stamp.check(r, self.script)          # 少哈希了 deps
        self.assertIn("内容变了", changed)
        self.assertIn("文件不一样", added)

    def test_show_without_stamp_says_so(self):
        self.assertIn("没有版本戳", stamp.show({}))

    def test_show_with_stamp_is_informative(self):
        r = stamp.attach({}, self.script, [self.crit], pages=7)
        text = stamp.show(r)
        self.assertIn("run.py", text)
        self.assertIn("pages=7", text)

    def test_check_with_stamp_but_no_script_does_not_claim_current(self):
        """有戳但没拿当前脚本对 → 可以返回 True，但**措辞不能声称是当前版**。"""
        r = stamp.attach({}, self.script, [self.crit])
        ok, why = stamp.check(r)
        self.assertTrue(ok)
        self.assertIn("没拿当前脚本对", why)


class TestCli(Temp):
    def test_check_returns_nonzero_when_stale(self):
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        p = self.root / "r.json"
        p.write_text(json.dumps(r))
        self.script.write_text("print('v2')\n")
        self.assertEqual(stamp._cli(["check", str(p), str(self.script)]), 1)

    def test_check_returns_zero_when_fresh(self):
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        p = self.root / "r.json"
        p.write_text(json.dumps(r))
        self.assertEqual(
            stamp._cli(["check", str(p), str(self.script), "--deps", str(self.crit)]), 0)

    def test_list_payload_uses_first(self):
        r = stamp.attach({"x": 1}, self.script, [self.crit])
        p = self.root / "r.json"
        p.write_text(json.dumps([r, r]))
        self.assertEqual(stamp._cli(["show", str(p)]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
