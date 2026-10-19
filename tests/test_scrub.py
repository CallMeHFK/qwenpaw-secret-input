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

# 第三个假明文：带一个孤立代理位（lone surrogate）。json.loads 会把 "\ud800"
# 这样的字面量解成孤立代理，卡片 POST 体里只要出现一次，值就会走到这里。
# 它同时满足 isinstance(str) 与 len>=8，所以挡在 register 门外的是不掉的。
LONE = "DUMMY-\ud800-SK"


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.s = Scrubber(ttl_seconds=3600, max_entries=64)
        self.s.register(DUMMY)

    def test_marker_constant_is_the_exact_three_star_string(self):
        # spec §6.1/§10 的硬约束：标记恒为 ***（不是等长星号、不是部分掩码）。
        # 这条把"***"这个字面量的归属权写在测试里，改 REDACT 必须同时改这里。
        self.assertEqual(REDACT, "***")

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
        self.assertEqual(len(forms), 4)   # 只是列出这 4 个表达式数得过来，不是门（见 §7 报告）
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

    # --- R22：孤立代理（lone surrogate）绝不能把 replace() 打死 ----------------

    def test_lone_surrogate_value_does_not_disable_replace(self):
        # 评审 Critical：值里一个 \ud800 就让 value.encode("utf-8") 严格模式抛，
        # 而这个值已经进了集合且是活的（sweep 清不掉）——此后每一次 replace()
        # 都抛，I4 对整个集合永久失效；异常文本还带出那个字符和它的位置
        # （spec §10.1 "异常报文"通道必须不含明文）。两个失败分支都不接受：
        # 不登记 = 拿保护换稳定；登记了但抛 = 工具链当场断。
        s = Scrubber()
        s.register(LONE)
        self.assertIn(LONE, s)                       # 值必须留在集合里
        self.assertEqual(len(s), 1)
        self.assertEqual(s.replace("harmless"), "harmless")   # 不抛、恒等
        self.assertEqual(s.replace(LONE), REDACT)             # 保护仍然生效
        self.assertEqual(s.replace(f"x {LONE} y"), f"x {REDACT} y")

    def test_lone_surrogate_value_scrubs_all_five_families(self):
        # surrogatepass 编出来的是该代理的 CESU/WTF-8 字节（本机 3.12.14 实测
        # RFVNTVkt7aCALVNL / DUMMY-%ED%A0%80-SK），照它编出的形态必须照样命中。
        s = Scrubber()
        s.register(LONE)
        raw = LONE.encode("utf-8", "surrogatepass")
        forms = [
            LONE,
            base64.b64encode(raw).decode("ascii"),
            base64.urlsafe_b64encode(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii").upper(),
            quote(LONE, safe="", errors="surrogatepass"),
        ]
        for form in forms:
            self.assertEqual(s.replace(f"x{form}y"), f"x{REDACT}y", msg=repr(form))

    def test_replace_never_raises_when_a_surrogate_value_is_registered(self):
        # "抛出来的通道不能带值"的最强形式：根本没有异常可带。逐个输入跑一遍，
        # 任何一次抛异常都直接让这条用例 ERROR（不需要读异常文本，也就不会把
        # 明文抄进报告）。
        s = Scrubber()
        s.register(LONE)
        s.register(DUMMY)
        for text in ("", "harmless", DUMMY, LONE, f"a{LONE}b",
                     LONE.encode("utf-8", "surrogatepass").decode("latin-1"),
                     "x" * 5000):
            self.assertIsInstance(s.replace(text), str)
        self.assertEqual(s.sweep(), 0)               # 清扫也不抛

    def test_backstop_skips_a_broken_variant_family_and_keeps_scrubbing(self):
        # 兜底：未来任何一族变体抛了（编码、长度、别的字符家族），只丢那一个值，
        # 不许把整趟擦洗打断，也不许把值名写进日志。
        other = "DUMMY-SK-other9"
        s = Scrubber()
        s.register(DUMMY)
        s.register(other)
        real = s.variants

        def boom(value):
            if value == other:
                raise ZeroDivisionError("simulated future variant family")
            return real(value)

        s.variants = boom
        with self.assertLogs("backend.scrub", level="INFO") as caught:
            out = s.replace(f"a {DUMMY} b {other} c")
        self.assertEqual(out, f"a {REDACT} b {other} c")   # 坏值放行、好值照擦
        joined = "\n".join(caught.output)
        self.assertNotIn(DUMMY, joined)
        self.assertNotIn(other, joined)


# --- R24：过期即从集合观测量里消失（机会式回收） --------------------------------


class ScrubTtlViewTest(unittest.TestCase):
    def test_expired_entries_leave_len_and_contains(self):
        # 评审 Important 2：__len__/__contains__ 读裸 _entries，而过期项只有
        # sweep() 才清 —— 计划里没有周期性 sweep（只有启动钩子和卸载），于是
        # status() 公布的 scrub_entries 虚高（报的是"正在防护的明文条数"），
        # 且过期明文一直躺在内存里，spec §10.2 第 3 条"TTL 只缩小窗口"落空。
        stale = "DUMMY-SK-stale000"
        s = Scrubber(ttl_seconds=60)
        s.register(stale)
        s.register(DUMMY)
        s.register(NEEDLE)
        s._entries[stale] = time.monotonic() - 999        # 只把时间戳拨旧，不调 sweep
        self.assertEqual(len(s), 2)
        self.assertNotIn(stale, s)
        self.assertIn(DUMMY, s)
        self.assertIn(NEEDLE, s)
        self.assertEqual(len(s._entries), 2)              # 读操作已经把 RAM 收回
        self.assertEqual(s.sweep(), 0)                    # 没有东西留给 sweep
        self.assertEqual(s.replace(stale), stale)         # 也不再防护它

    def test_len_reflects_only_live_entries_before_any_sweep(self):
        # Task 8 的 status() 直接 len(scrubber)。这里证明不需要任何新接线：
        # 老化一条之后 len 立刻变小，不必等启动钩子或卸载钩子。
        s = Scrubber(ttl_seconds=1)
        for i in range(4):
            s.register(f"DUMMY-SK-n{i:04d}xxxx")
        self.assertEqual(len(s), 4)
        keys = list(s._entries)
        s._entries[keys[0]] = time.monotonic() - 5
        s._entries[keys[1]] = time.monotonic() - 5
        self.assertEqual(len(s), 2)
        self.assertNotIn(keys[0], s)
        self.assertIn(keys[2], s)

    def test_reregistering_during_a_pass_does_not_break_iteration(self):
        # 评审 Minor：register() 会改 _entries（move_to_end / popitem）。读侧
        # 必须先把条目快照成 list 再迭代，否则"字典在迭代中改了大小"。这里让
        # 一次 replace 的中途真的落进 register。
        s = Scrubber()
        s.register(DUMMY)
        real = s.variants
        state = {"done": False}

        def reentrant(value):
            if not state["done"]:
                state["done"] = True
                s.register(DUMMY)                      # 再登记已存在的值
                s.register("DUMMY-SK-added-in-pass")
            return real(value)

        s.variants = reentrant
        self.assertEqual(s.replace(f"v={DUMMY}"), f"v={REDACT}")
        self.assertEqual(len(s), 2)          # DUMMY + 途中新登记的那条
        self.assertIn("DUMMY-SK-added-in-pass", s)


# --- R25：候选排序按 form 长度，不按 raw 值长度 ---------------------------------


class ScrubCandidateOrderTest(unittest.TestCase):
    LONGER_RAW_IN_SHORTER_B64 = "DUMMY-SK-Bbb2"        # 13 字符的假明文 B

    def _a_and_b(self):
        b = self.LONGER_RAW_IN_SHORTER_B64
        b64 = base64.b64encode(b.encode()).decode()    # 20 字符
        return b64[2:18], b, b64                       # A 比 B 长，且 A 是 B 的 b64 子串

    def test_longer_value_raw_inside_shorter_value_base64(self):
        # 危害发生在 form 层不在 raw 层：A 的 raw（16 字符）是 B 的 base64（20 字符）
        # 的中间一段。按"raw 值长度降序"排，A 先跑，把 B 的 base64 咬断，剩下
        # "RF" + "***" + "=" —— 六个属于 B 的明文字节形态留在输出里。
        a, b, b64b = self._a_and_b()
        self.assertTrue(len(a) > len(b) and a in b64b)
        s = Scrubber()
        s.register(a)
        s.register(b)
        self.assertEqual(s.replace(f"x{b64b}y"), f"x{REDACT}y")

    def test_equal_length_ties_do_not_depend_on_registration_order(self):
        # 同长度的两个值：A 仍是 B 的 base64 的子串，此时"raw 长度降序"排不出
        # 先后（相等），退化成登记顺序——登记 A 在前就漏，登记 B 在前就不漏。
        # 按 form 长度排就没有这个二义性：b64(B) 20 字符恒在 A 13 字符之前。
        b = self.LONGER_RAW_IN_SHORTER_B64
        b64b = base64.b64encode(b.encode()).decode()
        a = b64b[2:15]                                 # 13 字符，与 len(b) 相等
        self.assertEqual(len(a), len(b))
        self.assertIn(a, b64b)
        for order in ((a, b), (b, a)):
            s = Scrubber()
            for value in order:
                s.register(value)
            self.assertEqual(s.replace(f"x{b64b}y"), f"x{REDACT}y", msg=order)

    def test_candidates_are_deduped_and_sorted_by_form_length(self):
        # 结构断言：拍平成 (form, value) 之后去重、按 form 长度降序。
        # 摘掉 sort 或摘掉去重都会红；相等长度的不同 form 不可能是彼此的子串
        # （等长且互相包含即相等，已被去重），所以这个序是安全的。
        s = Scrubber()
        s.register(DUMMY)
        s.register(NEEDLE)
        s.register(self.LONGER_RAW_IN_SHORTER_B64)
        # 第二个值的 raw 恰好等于第一个值的 base64 —— 跨值去重只有这种配对才咬得住，
        # 不去重的写法必须让下面那条 set 断言红，否则它只是装饰。
        twin = base64.b64encode(DUMMY.encode()).decode("ascii")
        self.assertNotEqual(twin, DUMMY)
        s.register(twin)
        forms = s._candidate_forms()
        self.assertEqual(len(forms), len(set(forms)), "候选必须去重")
        lens = [len(f) for f in forms]
        self.assertEqual(lens, sorted(lens, reverse=True), "候选必须按 form 长度降序")
        self.assertTrue(forms, "集合非空时候选不能为空")
        self.assertIn(twin, forms)
        self.assertEqual(forms.count(twin), 1, "同一个形态在一次遍群里只能出现一次")


if __name__ == "__main__":
    unittest.main()
