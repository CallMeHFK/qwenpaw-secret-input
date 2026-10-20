import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid

from backend.card_page import html_response, render_page
from tests.support import DUMMY

# --- 答页行为的取证台 -------------------------------------------------------
# 答页的全部职责是"取不到卡片时也要好好落地"（spec 8.1 的兜底路径），而这句话只有把页面
# 里那段 <script> 真跑一遍才验得到。所以这里不比对源码字符串（那只会把实现再抄一遍、
# 改个写法就红），而是用 node 在最小 DOM/网络替身上执行**页面自己的脚本**，断言的是
# 执行完落到页面上的东西：标题文案、表单是否收起来、nonce 变量有没有被赋值。
# 桩只交出一张 404 的 meta 响应，别的什么都不做 —— 页面无路可走，只能走降级分支。

_NODE_HEAD = """
const __els = new Map();
const __calls = [];
const __el = (name) => {
  if (!__els.has(name)) {
    const e = {id: name, textContent: "", className: "", style: {}, value: "",
               placeholder: "", handlers: {},
               addEventListener: (type, fn) => { e.handlers[type] = fn; }};
    __els.set(name, e);
  }
  const e = __els.get(name);
  e.__touched = true;
  return e;
};
globalThis.document = {getElementById: __el};
globalThis.fetch = (url, opts) => {
  __calls.push(((opts && opts.method) || "GET") + " " + String(url));
  return Promise.resolve(__RESPONSE);
};
"""

_NODE_TAIL = """
await new Promise((done) => setTimeout(done, 0));      // 让 load() 的微任务走完
const __seen = (name) => {
  const e = __els.get(name);
  if (!e || !e.__touched) { return "__never_touched__"; }
  // undefined 会被 JSON.stringify 静默吃掉，所以先按字面量把它暴露出来
  return typeof e.textContent === "string" ? e.textContent : "__" + String(e.textContent) + "__";
};
console.log(JSON.stringify({
  label: __seen("label"), reason: __seen("reason"), status: __seen("status"),
  formHidden: !!(__els.get("f") && __els.get("f").style.display === "none"),
  nonce: typeof nonce === "string" ? nonce : "__" + String(nonce) + "__",
  fetched: __calls,
}));
"""

# meta 回 404 的两张脸：请求 id 未知/已被逐出（人从 INFO 日志里点开的死链），
# 以及宿主回了一页非 JSON 的错误（500）。两种都不许把 "undefined" 打到屏幕上。
_META_404 = """{ok: false, status: 404,
  json: () => Promise.resolve({detail: "This secret request no longer exists."})}"""
_META_NOT_JSON = """{ok: false, status: 500,
  json: () => Promise.reject(new TypeError("meta replied with html, not json"))}"""
# 桩用的假凭据，绝不是任何 registry 真发出去的 nonce（tests.support 的 DUMMY 同一条理由）。
STUB_NONCE = "STUB-NONCE-not-a-real-handle"
_META_PENDING = """{ok: true, status: 200,
  json: () => Promise.resolve({label: "GitHub token", reason: "webhook signing",
    placeholder: "ghp_...", status: "pending", status_text: "等待输入",
    nonce: "__NONCE__"})}""".replace("__NONCE__", STUB_NONCE)
_META_SAVED = """{ok: true, status: 200,
  json: () => Promise.resolve({label: "GitHub token", reason: "webhook signing",
    status: "saved", status_text: "已保存", answered_peer: "127.0.0.1",
    answered_at: "2026-10-19 12:00:00"})}"""


def _run_page_script(meta_response: str, request_id: str = "a" * 32) -> dict:
    """在 node 里跑答页脚本，返回脚本执行后页面上实际呈现的状态。"""
    node = shutil.which("node")
    if node is None:
        # 不许静默放行：这道门要靠 node（计划里 CI 也跑 `node --check ui/index.js`）。
        raise AssertionError("node not available for the answer-page behaviour gate")
    html = render_page(request_id)
    script = html.split("<script>")[1].split("</script>")[0]
    head = _NODE_HEAD.replace("__RESPONSE", meta_response)
    source = head + script + _NODE_TAIL
    fd, path = tempfile.mkstemp(suffix=".mjs", prefix="card_page_probe_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        # 答页抛出的未处理 rejection 就是"降级没做成"的读数本身（500 那一支），不是工具故障。
        raise AssertionError(
            f"answer-page script did not survive the stub: "
            f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _meta_call(request_id: str) -> str:
    """桩记下来的那一次请求：只许打 meta，一次。"""
    return "GET /api/secret-input/card/" + request_id + "/meta"


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

    # --- 修复轮 1 · Important：死链必须优雅落地 -------------------------------
    # 人从 INFO 日志里点开答 URL 时，卡片往往早已超时并离开登记表（甚至被墓地逐出），
    # 于是 /meta 回 404 而 d.status 是 undefined。旧实现只在 `d.status && d.status !==
    # "pending"` 上分流，这一支直接掉到正常支路：屏幕标题显示 "undefined"、表单还留着、
    # nonce 被赋成 undefined。答页唯一的职责就是给无前端的路径兜底，所以这是核心用例。

    def test_dead_link_renders_terminal_state_instead_of_undefined(self):
        request_id = "a" * 32
        for name, meta_response in (("meta 404", _META_404),
                                    ("meta 回了非 JSON", _META_NOT_JSON)):
            with self.subTest(meta=name):
                seen = _run_page_script(meta_response, request_id)
                # 终态文案与"已答过的卡"用同一句 —— 这就是这条支路的契约，
                # 断言等值而不是断言我新写的那个 if 条件（改写法不该红，改行为才该红）。
                self.assertEqual(seen["label"], "该请求已结束")
                self.assertIsInstance(seen["reason"], str)
                self.assertTrue(seen["reason"], seen)          # 空话也不许，得说一句
                for text in (seen["label"], seen["reason"], seen["status"]):
                    self.assertNotIn("undefined", str(text).lower(), seen)
                self.assertTrue(seen["formHidden"], seen)      # 表单收起来
                self.assertEqual(seen["nonce"], "", seen)      # 没取到 nonce 就不许赋值
                # 没有 nonce 就没有第二次点击：只许打过一次 meta，绝不 POST /answer
                self.assertEqual(seen["fetched"], [_meta_call(request_id)])

    # 取证台自己得先证明它读得到东西：上面那条"终态"断言如果是空门（页面无论怎么跑都
    # 显示终态），它就永远绿。所以这两支反着钉 —— 活卡必须可见、nonce 必须被取到，
    # 已答卡必须走终态并带上审计行。页面坏了、桩坏了、或断言写反了，都会在这里露出来。

    def test_probe_reads_both_the_live_and_the_settled_card(self):
        request_id = "a" * 32
        live = _run_page_script(_META_PENDING, request_id)
        self.assertEqual(live["label"], "GitHub token")        # 活卡把卡片文案打上标题
        self.assertEqual(live["reason"], "webhook signing")
        self.assertFalse(live["formHidden"], live)             # 表单留给用户
        self.assertEqual(live["nonce"], STUB_NONCE)            # nonce 确实取到了
        self.assertEqual(live["fetched"], [_meta_call(request_id)])

        settled = _run_page_script(_META_SAVED, request_id)
        self.assertEqual(settled["label"], "该请求已结束")
        self.assertEqual(settled["reason"], "已保存")
        self.assertTrue(settled["formHidden"], settled)
        self.assertEqual(settled["nonce"], "", settled)        # 终态卡不再发 nonce
        self.assertIn("127.0.0.1", settled["status"])          # 答者审计（spec 8.2）
        self.assertIn("提交", settled["status"])


if __name__ == "__main__":
    unittest.main()
