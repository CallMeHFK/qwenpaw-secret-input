import re
import unittest
import uuid

from tests.support import DUMMY

from backend.card_page import html_response, render_page


class CardPageTest(unittest.TestCase):
    def test_page_is_self_contained_and_has_no_cdn(self):
        html = render_page(uuid.uuid4().hex)
        self.assertIn('type="password"', html)
        self.assertNotIn("http://", html.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", html)
        self.assertIn("<script>", html)               # 内联，无 src

    def test_page_embeds_only_the_request_id_never_a_value(self):
        html = render_page("a" * 32)
        self.assertIn("a" * 32, html)
        self.assertNotIn(DUMMY, html)

    def test_request_id_is_html_escaped_before_embedding(self):
        evil = '"><script>alert(1)</script>'
        self.assertNotIn(evil, render_page(evil))     # 非 hex 的 id 必须被拒绝或转义
        self.assertNotIn("<script>alert(1)</script>", render_page(evil))

    def test_label_and_reason_are_set_by_textcontent_not_innerhtml(self):
        # 卡片文案由模型写，属不可信文本：只允许 textContent
        html = render_page("b" * 32)
        self.assertNotIn("innerhtml", html.lower())
        self.assertGreaterEqual(html.count("textContent"), 2)

    # --- 裁定 R20 交接的第二半：decline 的文案不得说成"提交"（spec 8.2 答者审计）---

    def test_declined_audit_line_does_not_claim_a_submission(self):
        html = render_page("c" * 32)
        # 计划正文那行把动词写死成"提交"；declined 卡显示"已由 x 于 y 提交"是假话。
        self.assertNotIn('+ " 提交"', html)
        self.assertIn('d.status === "declined" ? "拒绝" : "提交"', html)
        self.assertIn("已拒绝", html)                 # 保存/拒绝两条成功文案也在分流

    def test_page_never_prefers_the_meta_nonce_over_a_fetch(self):
        # nonce 只能由页面自己向 /card/{id}/meta 取（spec 8.1 的两步），模板里不许有第二个来源
        html = render_page("d" * 32)
        self.assertEqual(len(re.findall(r"[0-9a-f]{32}", html)), 1)   # 只有 id
        self.assertIn('fetch(BASE + ID + "/meta"', html)
        self.assertNotIn("nonce", html.split("<script>")[0])          # 文案之外没有 nonce

    def test_failure_reasons_are_mapped_and_unknown_ones_are_not_guessed(self):
        # 端点的 detail 是闭集固定串（见 router._ANSWER_MAP）；页面要有两套东西：
        # 认得出的翻成中文，认不出的原样显示 —— 不许猜一个"错的原因"给用户（R20 的同一条理由）。
        html = render_page("f" * 32)
        self.assertIn("FAIL_TEXT[d.detail] || d.detail", html)
        for reason in ("empty_value", "invalid_nonce", "already_answered", "expired", "not_found"):
            self.assertIn(reason + ":", html)

    def test_html_response_is_utf8_html(self):
        res = html_response("e" * 32)
        self.assertTrue(res.media_type.startswith("text/html"), res.media_type)
        self.assertIn(b"e" * 32, res.body)


if __name__ == "__main__":
    unittest.main()
