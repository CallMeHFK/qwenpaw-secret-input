"""Gates for the in-session overlay bundle (no browser, no build).

The bundle is served as-is to the host Console, so its safety properties are
checked by reading it: host SDK only, no HTML injection, no external origin,
no value ever read back from a response.  One test additionally executes the
bundle under a tiny DOM-less React stub and walks the returned element tree,
so "text is a child, not a prop" is asserted on the render result rather than
on source spelling.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1] / "ui" / "index.js"
SRC = BUNDLE.read_text(encoding="utf-8")


def _slice(start: str, end: str) -> str:
    begin = SRC.index(start)
    return SRC[begin:SRC.index(end, begin + len(start))]


SAVE_BODY = _slice("async function save() {", "async function decline() {")
DECLINE_BODY = _slice("async function decline() {", "  function EndedCard")
PENDING_BODY = _slice("async function getPending(sid)", "  // 只回传终结状态")
FORMAT_BODY = _slice("function formatCountdown(total)", "  function useCountdown")
POLL_BODY = _slice("var inFlight = false;", "    }, []);")

SETERROR_RE = re.compile(r"setError\(\s*([^)]*)\)")

# Executes ui/index.js with a stub host; prints the rendered text children and
# any forbidden props of SecretCard / EndedCard as the last stdout line.
RENDER_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");
let hookState = [];
let hookIndex = 0;
function createElement(type, props) {
  return { type: type, props: props || {}, children: Array.prototype.slice.call(arguments, 2) };
}
const React = {
  createElement: createElement,
  useState: function (initial) {
    const idx = hookIndex++;
    return [idx < hookState.length ? hookState[idx] : initial, function () {}];
  },
  useEffect: function () {},
};
const antd = {
  Input: { Password: function Password() {} },
  Button: function Button() {},
};
let factory = null;
const window = {
  QwenPaw: {
    host: {
      React: React,
      antd: antd,
      fetch: function () { throw new Error("fetch must not run in static render"); },
      getCurrentSessionId: function () { return "s1"; },
    },
    slot: { fill: function (id, slot, f) { factory = f; } },
  },
};
const sandbox = {
  window: window, console: console,
  setInterval: setInterval, clearInterval: clearInterval,
  setTimeout: setTimeout, clearTimeout: clearTimeout,
  encodeURIComponent: encodeURIComponent,
  JSON: JSON, Promise: Promise, Number: Number, isFinite: isFinite,
  Math: Math, Array: Array, Object: Object, Error: Error,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
if (typeof factory !== "function") { throw new Error("bundle did not call slot.fill"); }
function walk(node, acc) {
  if (node === null || node === undefined || node === false) return;
  if (typeof node === "string" || typeof node === "number") { acc.text.push(String(node)); return; }
  if (Array.isArray(node)) { node.forEach(function (n) { walk(n, acc); }); return; }
  if (typeof node !== "object") return;
  if (node.props) {
    if (Object.prototype.hasOwnProperty.call(node.props, "textContent")) acc.badProps.push("textContent");
    if (Object.prototype.hasOwnProperty.call(node.props, "dangerouslySetInnerHTML")) acc.badProps.push("dangerouslySetInnerHTML");
  }
  (node.children || []).forEach(function (n) { walk(n, acc); });
}
function render(component, props) {
  hookIndex = 0;
  const acc = { text: [], badProps: [] };
  walk(component(props), acc);
  return acc;
}
const item = { request_id: "req-a", label: "LABEL-A", reason: "REASON-A", placeholder: "PH", expires_in: 95 };
hookState = [[item], null];
hookIndex = 0;
const overlayEl = factory();
if (!overlayEl || overlayEl.type.name !== "Overlay") { throw new Error("factory did not return Overlay"); }
const cardEl = overlayEl.type();
if (!cardEl || cardEl.type.name !== "SecretCard") { throw new Error("Overlay did not render SecretCard"); }
if (!cardEl.props.key) { throw new Error("SecretCard element has no key"); }
hookState = ["secret-in-state", false, "ERROR-TEXT", 95];
const card = render(cardEl.type, cardEl.props);
hookState = [];
const cardNegative = render(cardEl.type, Object.assign({}, cardEl.props, {
  item: Object.assign({}, item, { expires_in: -12 }),
}));
hookState = [[], { status: "declined", info: { answered_peer: "PEER-X", answered_at: "TIME-X" } }];
hookIndex = 0;
const endedEl = factory().type();
if (!endedEl || endedEl.type.name !== "EndedCard") { throw new Error("Overlay did not render EndedCard"); }
const ended = render(endedEl.type, endedEl.props);
const endedTimeout = render(endedEl.type, { status: "timed_out", info: {} });
const endedSaved = render(endedEl.type, { status: "saved", info: { answered_peer: "PEER-Y", answered_at: "TIME-Y" } });
console.log(JSON.stringify({ card: card, cardNegative: cardNegative, ended: ended, endedTimeout: endedTimeout, endedSaved: endedSaved, cardKey: cardEl.props.key }));
"""


class BundleTest(unittest.TestCase):
    def test_syntax_parses_under_node(self):
        proc = subprocess.run(["node", "--check", str(BUNDLE)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_uses_the_host_sdk_only(self):
        self.assertIn("window.QwenPaw", SRC)
        self.assertIn("overlay.global", SRC)
        self.assertIn("getCurrentSessionId", SRC)
        self.assertIn("host.fetch", SRC)          # 自动带 Bearer + X-Agent-Id

    def test_polls_pending_and_never_reads_a_value_field(self):
        self.assertIn("/pending", SRC)
        for forbidden in ("item.value", "rec.value", "d.value", "requests[0].value"):
            self.assertNotIn(forbidden, SRC)     # 响应对象上不能读出任何值字段（输入框的 e.target.value 不在此列）
        self.assertIn("Array.isArray(data.requests)", PENDING_BODY)
        self.assertNotIn("nonce", PENDING_BODY.lower())   # /pending 从不给 nonce，卡片也不许在这里找

    def test_error_text_is_fixed_and_never_echoes_the_server(self):
        # setError 的实参必须是本地字面量闭集；任何响应变量（res/meta/data）拼进去都要红。
        args = SETERROR_RE.findall(SRC)
        self.assertGreaterEqual(len(args), 4)
        self.assertEqual(len(args), SRC.count("setError("), "非字面量的 setError(...) 调用")
        for arg in args:
            self.assertRegex(arg.strip(), r'^"[^"]*"$', arg)
            for response_var in ("res.", "data.", "meta.", "d."):
                self.assertNotIn(response_var, arg)
        messages = {a.strip().strip('"') for a in args} - {""}   # "" 只用于清空错误
        self.assertEqual({"值为空", "请求已结束", "提交失败，请重试"}, messages)
        self.assertNotIn(".detail", SRC)
        for forbidden in ('"失败："', "+ res.", "+ data.", "+ meta.", "+ d.", "res.data"):
            self.assertNotIn(forbidden, SRC)

    def test_renders_text_as_children_and_never_as_html(self):
        low = SRC.lower()
        self.assertNotIn("textcontent", low)          # React 18 忽略这个 DOM 属性，文本必须走 children
        self.assertNotIn("innerhtml", low)
        self.assertNotIn("dangerouslysetinnerhtml", low)
        self.assertIn("item.label)", SRC)
        self.assertIn("item.reason)", SRC)
        self.assertIn("}, error)", SRC)
        self.assertIn('"值不会进入对话，模型看不到它。"', SRC)
        self.assertIn("ENDED_TEXT[status]", SRC)
        self.assertIn("info.answered_peer", SRC)

    def test_react_tree_renders_labels_and_audit_as_children(self):
        proc = subprocess.run(
            ["node", "-e", RENDER_HARNESS, str(BUNDLE)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(data["card"]["badProps"], [])
        self.assertEqual(data["ended"]["badProps"], [])
        self.assertEqual(data["cardKey"], "req-a")
        card_text = data["card"]["text"]
        for expected in (
            "LABEL-A", "REASON-A", "值不会进入对话，模型看不到它。",
            "倒计时 1:35", "ERROR-TEXT", "保存", "拒绝",
        ):
            self.assertIn(expected, card_text)
        self.assertIn("已拒绝", data["ended"]["text"])
        self.assertIn("已由 PEER-X 于 TIME-X 拒绝", data["ended"]["text"])
        self.assertIn("已超时", data["endedTimeout"]["text"])
        self.assertIn("已由 PEER-Y 于 TIME-Y 提交", data["endedSaved"]["text"])
        # 负的 expires_in 只许显示 0:00，不能出现 "-0:12" 之类的假话。
        self.assertIn("倒计时 0:00", data["cardNegative"]["text"])

    def test_no_external_origin(self):
        self.assertNotIn("http://", SRC)
        self.assertNotIn("https://", SRC)
        # 协议相对 URL（"//host"）没有 scheme，单查 https:// 抓不到。
        self.assertIsNone(re.search(r"""["'`]\s*//[A-Za-z0-9]""", SRC), "protocol-relative URL")
        self.assertNotIn("cdn", SRC.lower())

    def test_uses_password_input_and_clears_it(self):
        # 依据：宿主 SDK 只给 antd（docs/plugins.en.md:487），密码框即 antd.Input.Password
        self.assertIn("Input.Password", SRC)
        self.assertIn('autoComplete: "new-password"', SRC)
        self.assertIn('setValue("")', SAVE_BODY)      # 提交后立刻清掉内存里的输入
        self.assertIn('setValue("")', DECLINE_BODY)   # 拒答也不留已经敲进去的值

    def test_save_and_decline_are_disabled_while_busy(self):
        self.assertGreaterEqual(SRC.count("disabled: isBusy"), 2)
        self.assertIn("loading: isBusy", SRC)
        self.assertIn("if (isBusy) { return; }", SAVE_BODY)
        self.assertIn("if (isBusy) { return; }", DECLINE_BODY)

    def test_shows_the_who_answered_audit(self):
        self.assertIn("answered_peer", SRC)      # 抢答可发现（spec §10.2 第 6 条）
        self.assertIn("answered_at", SRC)
        for status in ("saved", "declined", "timed_out", "cancelled"):
            self.assertIn(status + ":", SRC)     # 终态标题是本地闭集，不是一句泛化文案

    def test_done_callback_uses_its_own_request_id(self):
        # 终态回读 meta 时必须用卡片自己的 id；回读 pending[0] 会串到下一张卡。
        self.assertNotIn("pending[0].request_id", SRC)
        self.assertIn("var requestId = current.request_id;", SRC)

    def test_secret_card_is_keyed_by_request_id(self):
        # key 决定 A→B 换卡时 React 重挂组件：输入/忙/错状态不跨请求存活。
        self.assertRegex(SRC, r"h\(SecretCard,\s*\{\s*key:\s*current\.request_id,")

    def test_terminal_meta_becomes_an_ended_card_not_a_generic_error(self):
        for body in (SAVE_BODY, DECLINE_BODY):
            self.assertIn('meta.status !== "pending"', body)
            self.assertIn("done(meta.status, meta);", body)
            self.assertIn("done(after.status, after);", body)
            self.assertIn("terminalMeta(requestId)", body)
        self.assertIn("var status = props.status || info.status", SRC)

    def test_countdown_uses_pending_expires_in_and_clamps_at_zero(self):
        self.assertIn("expires_in", SRC)
        self.assertIn("useCountdown(item)", SRC)
        self.assertIn("[item.request_id, item.expires_in]", SRC)   # 每轮轮询重新对齐
        self.assertIn("Math.max(0, Math.round(v))", SRC)
        self.assertIn("Math.max(0, prev - 1)", SRC)
        self.assertIn("Math.max(0, Math.round(Number(total) || 0))", FORMAT_BODY)
        self.assertIn('"倒计时 " + formatCountdown(remaining)', SRC)
        self.assertNotIn('formatCountdown(-', SRC)

    def test_polling_is_serialized_and_session_change_clears_state(self):
        self.assertIn("var inFlight = false;", POLL_BODY)
        self.assertIn("if (stopped || inFlight) { return; }", POLL_BODY)
        self.assertIn("setPending([]);", POLL_BODY)
        self.assertIn("setEnded(null);", POLL_BODY)
        self.assertIn("sessionId() === sid", POLL_BODY)   # 旧会话的晚到响应不许落地
        # 2xx + 坏 JSON 要保持上一帧，不能回 {} 把卡片清空。
        self.assertIn("catch (e) {\n      return null;", PENDING_BODY)
        self.assertNotIn("return {}", PENDING_BODY)

    def test_ended_card_lasts_thirty_seconds(self):
        self.assertIn("ENDED_MS = 30000;", SRC)
        self.assertIn("setEnded(null); }, ENDED_MS);", SRC)
        self.assertNotIn("ENDED_MS = 8000", SRC)

    def test_requires_the_plugin_id_guard_before_registering(self):
        guard = SRC.index('if (!window.QwenPaw || !window.QwenPaw.host)')
        fill = SRC.index("QP.slot.fill(")
        self.assertLess(guard, fill)
        self.assertIn("return;", SRC[guard:fill])       # 守卫必须早退，不能带半截 SDK 去注册
        self.assertIn('"overlay.global"', SRC)

    def test_nonce_is_fetched_at_submit_and_never_cached(self):
        for forbidden in ("item.nonce", "current.nonce", "pending.nonce", "requests[0].nonce"):
            self.assertNotIn(forbidden, SRC)
        self.assertIn("await fetchMeta(requestId)", SAVE_BODY)
        self.assertIn("await fetchMeta(requestId)", DECLINE_BODY)
        self.assertIn("postAnswer(requestId, { nonce: meta.nonce, value: pendingValue })", SAVE_BODY)
        self.assertIn("postAnswer(requestId, { nonce: meta.nonce, decline: true })", DECLINE_BODY)
        self.assertNotIn("setNonce", SRC)
        self.assertNotIn("useState(meta.nonce)", SRC)


if __name__ == "__main__":
    unittest.main()
