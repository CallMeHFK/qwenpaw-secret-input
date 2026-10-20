"""Self-contained answer page. No CDN, no build step, no secret in the HTML.

The URL carries only the unguessable request id; the nonce is fetched by the
page itself (``GET /card/{id}/meta``) so it never lands in a log line, a shell
history entry or a ``Referer``. That is spec 8.1's deliberate two-step, and it
is why this file has no ``nonce`` in the markup — only in the script that goes
to fetch one.

Label and reason are model-authored text (spec 10.2 item 4), so every write of
them goes through ``textContent``; there is no ``innerHTML`` anywhere here.
"""

from __future__ import annotations

import re

from fastapi import Response

_HEX = re.compile(r"^[0-9a-f]{32}$")

_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>私密输入 · Secret Input</title>
<style>
body{font:14px/1.6 system-ui,sans-serif;margin:0;padding:24px;background:#0d1117;color:#e6edf3}
main{max-width:420px;margin:0 auto;padding:20px;border:1px solid #30363d;border-radius:10px}
h1{font-size:16px;margin:0 0 4px}p.reason{color:#9198a1;margin:0 0 16px;font-size:13px}
label{display:block;margin-bottom:6px;font-size:12px;color:#9198a1}
input{width:100%;padding:8px;box-sizing:border-box;border:1px solid #30363d;border-radius:6px;
background:#161b22;color:#e6edf3;font-family:monospace}
button{margin-top:12px;padding:8px 14px;border-radius:6px;border:1px solid #30363d;
background:#238636;color:#fff;cursor:pointer}
button.ghost{background:transparent;color:#9198a1;margin-left:8px}
#status{margin-top:12px;font-size:13px;min-height:20px}
.done{color:#3fb950}.err{color:#f85149}
</style></head><body><main>
<h1 id="label">载入中…</h1><p class="reason" id="reason"></p>
<form id="f" autocomplete="off">
<label for="v">密钥（不会进入对话，也不会给模型看到）</label>
<input id="v" type="password" autocomplete="new-password" placeholder="…">
<div><button id="save" type="submit">保存</button>
<button id="no" type="button" class="ghost">拒绝</button></div>
</form><div id="status"></div>
<script>
const ID = "%s";
const BASE = "/api/secret-input/card/";
// 端点回出的 detail 是闭集里的固定串（绝不含提交内容），这里翻成人话；
// 认不出的串原样显示 —— 宁可显示一个英文 token，也不猜一个错的原因（裁定 R20 的同一条理由）。
const FAIL_TEXT = {empty_value: "值为空", invalid_nonce: "凭据不对，请重新打开答页",
  already_answered: "这张卡已经结束了", expired: "这张卡已超时", not_found: "请求不存在",
  invalid_json: "提交格式不对", invalid_body: "提交格式不对", value_too_long: "值太长了",
  invalid_nonce_field: "凭据缺失", invalid_value_field: "值格式不对",
  invalid_decline_field: "拒绝标记不对", body_too_large: "提交太大",
  unreadable_body: "没能读到提交内容"};
let nonce = "";
const el = (id) => document.getElementById(id);
async function load() {
  const r = await fetch(BASE + ID + "/meta", {headers: {Accept: "application/json"}});
  const d = await r.json().catch(() => ({}));      // 宿主回一页 HTML 时也不许整段脚本挂掉
  // 死链支路（修复轮 1 的 Important）：卡片超时后从日志点进来，meta 回 404 —— 这时
  // d.status 是 undefined，旧代码会掉到正常支路，把 undefined 当文案打上屏幕、表单留着、
  // nonce 赋成 undefined。"没有可渲染的卡片"与"卡片已终结"是同一个终态，缺 status 就
  // 必须走终态，不看 r.ok 也别看 d.status 的真值。
  if (!r.ok || !d.status) {
    el("label").textContent = "该请求已结束";
    el("reason").textContent = r.status === 404 ? "请求不存在或已结束" : "没能载入这张卡";
    el("f").style.display = "none";
    return;
  }
  if (d.status !== "pending") {
    el("label").textContent = "该请求已结束";
    el("reason").textContent = d.status_text || "";
    el("f").style.display = "none";
    if (d.answered_peer) {
      // 答者审计（spec 8.2）：动词按终态分流，拒答卡不许说"提交"（裁定 R20 后半）。
      el("status").textContent = "已由 " + d.answered_peer + " 于 " + d.answered_at + " " +
        (d.status === "declined" ? "拒绝" : "提交");
    }
    return;
  }
  el("label").textContent = d.label; el("reason").textContent = d.reason;
  if (d.placeholder) { el("v").placeholder = d.placeholder; }
  nonce = d.nonce;
}
async function send(body) {
  const r = await fetch(BASE + ID + "/answer", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) {
    // 终态即弃凭据：这张卡已经结了，nonce 留在闭包里没有任何用处（终审 Minor 18）。
    nonce = "";
    el("status").textContent = d.status === "declined" ? "已拒绝" : "已保存，值不会出现在对话里";
    el("status").className = "done"; el("f").style.display = "none";
  } else {
    el("status").textContent = "失败：" + (FAIL_TEXT[d.detail] || d.detail || r.status);
    el("status").className = "err";
  }
}
el("f").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = el("v").value;
  // 空框在本地就拦下，别让它去撞端点的 422（裁定 R20 的用户可见那一半）。
  if (!value || !nonce) { el("status").textContent = "值为空或未载入"; return; }
  send({nonce: nonce, value: value}); el("v").value = "";
});
el("no").addEventListener("click", () => nonce && send({nonce: nonce, decline: true}));
load();
</script></body></html>
"""

_INVALID = (
    "<!doctype html><meta charset=utf-8><title>无效请求</title>"
    "<p>这个私密输入链接不可用（请求 id 不合法）。</p>"
)


def render_page(request_id: str) -> str:
    """Return the HTML, or an error page if the id is not a 32-hex token."""
    if not _HEX.match(str(request_id or "")):
        return _INVALID
    # 只用 replace，不用 % 格式化：JS 与 CSS 里的 % 会和格式化打架（Step 2 的说明）。
    return _TEMPLATE.replace("%s", str(request_id), 1)


def html_response(request_id: str) -> Response:
    return Response(content=render_page(request_id), media_type="text/html; charset=utf-8")
