import base64
import binascii
import time
import unittest
from urllib.parse import quote

from tests.support import DUMMY

from backend.scrub import REDACT, Scrubber

# 第二个假明文（同样绝不是真密钥）。选它是因为 DUMMY 太"干净"：它的
# base64 == base64url、percent == raw，只靠 DUMMY 测不出那几条分支是不是活的。
NEEDLE = "DUMMY-SK-+/~"


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.s = Scrubber(ttl_seconds=3600, max_entries=64)
        self.s.register(DUMMY)

    def test_raw_replaced(self):
        self.assertEqual(self.s.replace(f"Bearer {DUMMY} done"), f"Bearer {REDACT} done")

    def test_all_five_variants_replaced(self):
        raw = DUMMY.encode()
        forms = [
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
            binascii.hexlify(raw).decode(),
            quote(DUMMY, safe=""),
        ]
        self.assertEqual(len(forms), 4)
        for form in forms:
            self.assertEqual(self.s.replace(f"x{form}y"), f"x{REDACT}y", msg=form)

    def test_unknown_text_untouched(self):
        self.assertEqual(self.s.replace("nothing to see"), "nothing to see")

    def test_empty_and_short_values_never_registered(self):
        s = Scrubber()
        s.register("")
        s.register("a")        # 1 字符值会把正常文本吃掉，必须拒绝登记
        self.assertEqual(s.replace("banana"), "banana")

    def test_ttl_expires_entry(self):
        s = Scrubber(ttl_seconds=1)
        s.register(DUMMY)
        s._entries[DUMMY] = time.monotonic() - 5
        self.assertEqual(s.sweep(), 1)
        self.assertEqual(s.replace(DUMMY), DUMMY)

    def test_max_entries_evicts_oldest(self):
        s = Scrubber(max_entries=2)
        s.register("DUMMY-SK-older0000")
        s.register(DUMMY)
        self.assertEqual(len(s), 2)
        s.register("DUMMY-SK-newest99")
        self.assertEqual(len(s), 2)
        self.assertNotIn("DUMMY-SK-older0000", s)

    def test_all_six_encodings_are_live_branches(self):
        # DUMMY 的 base64 与 base64url 相等、percent 编码等于原值，所以只用它测
        # 时那两条分支删掉也不会红。换一个六种写法两两不同的假值把每条都钉住。
        s = Scrubber()
        s.register(NEEDLE)
        raw = NEEDLE.encode()
        forms = [
            NEEDLE,
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
            binascii.hexlify(raw).decode(),
            binascii.hexlify(raw).decode().upper(),
            quote(NEEDLE, safe=""),
        ]
        self.assertEqual(len(set(forms)), 6, "六种写法必须两两不同，否则这条门是空的")
        for form in forms:
            self.assertEqual(s.replace(f"x{form}y"), f"x{REDACT}y", msg=form)

    def test_empty_registry_is_the_identity_function(self):
        # Task 5 依赖这条：集合为空时 replace 必须恒等返回（不复制、不改写、不抛），
        # 这样"没登记过任何明文"的正常工具结果一个字节都不会被动过。
        s = Scrubber()
        texts = ["", "plain tool output", f"already {REDACT} here", DUMMY,
                 base64.b64encode(DUMMY.encode()).decode(), NEEDLE]
        for text in texts:
            out = s.replace(text)
            self.assertEqual(out, text, msg=repr(text))
            self.assertIs(out, text, msg=repr(text))

    def test_marker_is_never_doubled_and_replace_is_idempotent(self):
        # 插进去的标记只有 3 个字符，而登记的值恒 ≥8 个字符，所以标记本身不可能
        # 再被任何形式命中：同一文本跑两次必须得到同一个串，*** 不会变成 ******。
        already = f"tool said {REDACT} and {REDACT} again"
        self.assertEqual(self.s.replace(already), already)
        once = self.s.replace(f"Bearer {DUMMY} and {DUMMY} done")
        self.assertEqual(once, f"Bearer {REDACT} and {REDACT} done")
        self.assertEqual(self.s.replace(once), once)

    def test_no_match_returns_the_same_object_not_a_copy(self):
        # Task 5 的中间件用 replacement != text 决定要不要重建块，所以"没命中"
        # 必须零拷贝。顺带钉住口径：原始值大小写不同即不命中（只有 hex 两种
        # 大小写都登记）——这是 spec 10.2 第 1 条承认的边界，不加模糊匹配。
        for text in ("plain output", f"already {REDACT} here", NEEDLE, DUMMY.lower()):
            self.assertIs(self.s.replace(text), text, msg=repr(text))

    def test_longer_value_replaced_before_its_own_substring(self):
        # 顺序是安全属性，不是风格问题：先换短值会把长值咬开——长值剩下的那截明文
        # 没处可换了，直接留在输出里。_live_values 按长度降序正是为此（Task 5 的评审点）。
        short = "DUMMY-SK-ab"                       # short 是 DUMMY 的前缀
        self.assertTrue(DUMMY.startswith(short))
        for order in ((short, DUMMY), (DUMMY, short)):   # 登记顺序不影响结果
            s = Scrubber()
            for value in order:
                s.register(value)
            self.assertEqual(s._live_values(), [DUMMY, short])
            self.assertEqual(s.replace(f"v={DUMMY}"), f"v={REDACT}")
            self.assertEqual(s.replace(f"v={short}"), f"v={REDACT}")

    def test_long_text_is_scrubbed_without_raising(self):
        # spec §7.2 把单条结果限到 32KB，这里给约 5 倍量（161KB）：
        # 长输入既不炸也不能把没命中的文本搅坏。
        padding = "x" * 2000
        text = (padding + DUMMY + padding + " readable tail") * 40
        out = self.s.replace(text)
        self.assertNotIn(DUMMY, out)
        self.assertEqual(out.count(REDACT), 40)
        self.assertEqual(out.count(padding), 80)
        self.assertEqual(out.count(" readable tail"), 40)

    def test_replace_and_register_never_raise_on_anything(self):
        # 非字符串进不来（None/bytes/int/容器原样退回），短值与非字符串值只拒收不抛。
        for weird in (None, 0, 42, b"bytes", ["a"], {"k": "v"}):
            self.assertIs(self.s.replace(weird), weird)
        s = Scrubber()
        with self.assertLogs("backend.scrub", level="INFO"):
            s.register(None)
            s.register(1234567890)
        self.assertEqual(len(s), 0)

    def test_refused_value_is_logged_without_the_value(self):
        # 禁令：明文绝不进日志行（spec §10 的"日志"通道）。
        short = "DUMMY-6"                    # 7 字符，低于 _MIN_VALUE_CHARS，必须拒收
        self.assertLess(len(short), 8)
        s = Scrubber()
        with self.assertLogs("backend.scrub", level="INFO") as caught:
            s.register(short)
        self.assertEqual(len(s), 0)
        self.assertNotIn(short, "\n".join(caught.output))

    def test_min_length_boundary_is_exclusive(self):
        # 规则是"短于 8 个字符拒收"，所以恰好 8 个字符必须收（Task 7/8 靠这条
        # 把短凭据也保住），7 个字符仍然只记一条不带值的 INFO 后丢掉。
        s = Scrubber()
        s.register("1234567")        # 7 -> 拒
        s.register("12345678")       # 8 -> 收
        self.assertEqual(len(s), 1)
        self.assertIn("12345678", s)
        self.assertEqual(s.replace("prefix 12345678 suffix"), "prefix *** suffix")
        self.assertEqual(s.replace("a 1234567 b"), "a 1234567 b")

    def test_reregistering_refreshes_eviction_order(self):
        # Task 8 每消费一次就重新登记同一个值：淘汰必须按"最久没再登记"，
        # 否则刚刷新的明文会被先挤出去，擦洗面静默变窄。
        s = Scrubber(max_entries=2)
        s.register("DUMMY-SK-older0000")
        s.register(DUMMY)
        s.register("DUMMY-SK-older0000")        # 再登记 -> 它不该是被淘汰的那个
        s.register("DUMMY-SK-newest99")
        self.assertEqual(len(s), 2)
        self.assertIn("DUMMY-SK-older0000", s)
        self.assertIn("DUMMY-SK-newest99", s)
        self.assertNotIn(DUMMY, s)

    def test_mask_function_is_not_used(self):
        # 守卫：宿主 mask_secret_value 保留首尾字符，绝不能进擦洗实现
        # 断言是查源码文本的，所以 backend/scrub.py 连提它的名字都不行——
        # 别把那个函数名"顺手"写回 docstring，这条会红，且红得对。
        import inspect

        from backend import scrub as mod

        src = inspect.getsource(mod)
        self.assertNotIn("mask_secret_value", src)
        self.assertNotIn("qwenpaw.security.secret_store", src)


if __name__ == "__main__":
    unittest.main()
