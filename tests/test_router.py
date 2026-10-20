import ast
import asyncio
import inspect
import json
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import router as router_mod
from backend.card_page import render_page
from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.router import _ANSWER_MAP, build_router
from backend.vault import Vault
from tests.support import DUMMY

API = "/api/secret-input"

# 裁定集（brief）：这个模块能落到线上的状态码只有这五个。
RATIFIED = {200, 403, 404, 410, 422}


def _codes_in_router_source() -> set[int]:
    """router 源码里所有可能上线的状态码 —— 按源码扫，不是按那张表读。

    修复轮 1 · R37：门只读 `_ANSWER_MAP` 的话，`guard` 的 403 与 `meta` 的 404 这两个
    字面量在表外，漂成 401 也不会红。两类出处都要扫：
      1) 任何 `status_code=<int>` 实参（JSONResponse / Response / 路由装饰器）；
      2) 任何 `(固定串, <int>)` 二元组字面量（`_ANSWER_MAP` 的表体与 `_code_for` 的兜底）。
    """
    tree = ast.parse(inspect.getsource(router_mod))
    codes: set[int] = set()

    def _int_of(node: ast.AST) -> int | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            return node.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "status_code":
                    found = _int_of(kw.value)
                    if found is not None:
                        codes.add(found)
        elif isinstance(node, ast.Tuple) and len(node.elts) == 2:
            detail, code = node.elts
            if isinstance(detail, ast.Constant) and isinstance(detail.value, str):
                found = _int_of(code)
                if found is not None:
                    codes.add(found)
    return codes


DEFAULT_CONFIG = PluginConfig()


def make_client(config=DEFAULT_CONFIG):
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    app = FastAPI()
    app.include_router(build_router(registry=registry, config=config, vault=vault),
                       prefix="/api/secret-input")
    # TestClient 默认 peer 是 "testclient"（非 loopback），必须显式伪装成 loopback
    return TestClient(app, client=("127.0.0.1", 51000)), registry


def make_remote_client(config=DEFAULT_CONFIG, as_user=None):
    """同一个 router，但 peer 是局域网地址；as_user 模拟宿主 AuthMiddleware 已认出身份。"""
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    app = FastAPI()
    if as_user is not None:
        @app.middleware("http")
        async def fake_auth(request, call_next):
            request.state.user = as_user
            return await call_next(request)
    app.include_router(build_router(registry=registry, config=config, vault=vault),
                       prefix="/api/secret-input")
    return TestClient(app, client=("10.0.0.9", 1)), registry


def _public(payload: dict) -> dict:
    """断言消息专用：失败时把整个 payload 打进 stderr 是常事，但里面可能躺着一个活 nonce
    （答这张卡的凭据）。records 的 __repr__ 为此砍掉了 nonce，这一层同理。"""
    return {k: v for k, v in payload.items() if k != "nonce"}


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.client, self.registry = make_client()

    def new_request(self, session_id="s1"):
        return asyncio.run(self.registry.create(
            label="GitHub token", reason="webhook signing", placeholder="ghp_...",
            session_id=session_id, agent_id="a1", user_id="u1", channel="console",
        ))

    def test_pending_lists_session_request_without_nonce(self):
        req = self.new_request()
        body = self.client.get(f"{API}/pending", params={"session_id": "s1"}).json()
        self.assertEqual(len(body["requests"]), 1)
        item = body["requests"][0]
        self.assertEqual(item["label"], "GitHub token")
        self.assertNotIn("nonce", item)
        self.assertIn(f"/card/{req.request_id}", item["answer_url"])

    def test_pending_without_session_requires_loopback(self):
        app = FastAPI()
        app.include_router(build_router(registry=self.registry, config=PluginConfig(),
                                        vault=Vault()), prefix="/api/secret-input")
        # assertLogs 兼有两个作用：钉住"拒绝要留痕"，并让这条 WARNING 不污染别的断言读数。
        lan = TestClient(app, client=("10.0.0.9", 1))
        with self.assertLogs("backend.router", level="WARNING") as logged:
            self.assertEqual(lan.get(f"{API}/pending").status_code, 403)
        self.assertIn("10.0.0.9", "\n".join(logged.output))

    def test_meta_exposes_nonce_only_to_allowed_caller(self):
        req = self.new_request()
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["nonce"], req.nonce)
        self.assertEqual(meta["status"], "pending")

    def test_answer_requires_correct_nonce(self):
        req = self.new_request()
        bad = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": "wrong", "value": DUMMY})
        self.assertEqual(bad.status_code, 403)
        good = self.client.post(f"{API}/card/{req.request_id}/answer",
                                json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["status"], "saved")

    def test_answer_response_body_contains_no_value(self):
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "value": DUMMY})
        self.assertNotIn(DUMMY, res.text)

    def test_decline_path(self):
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "decline": True})
        self.assertEqual(res.json()["status"], "declined")

    def test_second_answer_is_gone_with_410(self):
        req = self.new_request()
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "value": DUMMY})
        again = self.client.post(f"{API}/card/{req.request_id}/answer",
                                 json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(again.status_code, 410)

    def test_unknown_request_id_is_404_on_answer_404_on_meta(self):
        self.assertEqual(self.client.post(f"{API}/card/{'f' * 32}/answer",
                                          json={"nonce": "x", "value": DUMMY}).status_code, 404)
        self.assertEqual(self.client.get(f"{API}/card/{'f' * 32}/meta").status_code, 404)

    def test_answer_page_is_html_and_shows_audit_after_end(self):
        req = self.new_request()
        page = self.client.get(f"{API}/card/{req.request_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "value": DUMMY})
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["status"], "saved")
        self.assertEqual(meta["answered_peer"], "127.0.0.1")   # 抢答可发现的审计

    def test_allow_remote_true_lets_a_non_loopback_peer_in(self):
        app = FastAPI()
        app.include_router(build_router(registry=self.registry,
                                        config=PluginConfig(allow_remote=True),
                                        vault=Vault()), prefix="/api/secret-input")
        remote = TestClient(app, client=("10.0.0.9", 1))
        self.assertEqual(remote.get(f"{API}/pending", params={"session_id": "s1"}).status_code, 200)

    # --- 裁定 R20 交接：空值在端点前置成 422，绝不进 registry.answer ---

    def test_empty_value_is_422_and_never_reaches_the_registry(self):
        req = self.new_request()
        seen = []
        original = self.registry.answer

        async def spy(request_id, **kwargs):
            seen.append((request_id, kwargs.get("value")))
            return await original(request_id, **kwargs)

        self.registry.answer = spy
        url = f"{API}/card/{req.request_id}/answer"
        for body in ({"nonce": req.nonce, "value": ""}, {"nonce": req.nonce}):
            res = self.client.post(url, json=body)
            self.assertEqual(res.status_code, 422, res.text)
            self.assertEqual(res.json()["detail"], "empty_value")
        self.assertEqual(seen, [])                              # registry 一次都没被叫
        self.assertIsNotNone(self.registry.get(req.request_id))  # 卡还 pending，没被误结算
        # 拒答同样空 value，但它不是"空值"：422 那道门不许把它挡在外面
        self.assertEqual(self.client.post(url, json={"nonce": req.nonce,
                                                     "decline": True}).status_code, 200)
        self.assertEqual(len(seen), 1)                          # 绊线自己是活的（非空门）

    def test_empty_value_422_is_not_a_leak_of_the_card_state(self):
        # 空值 + 未知 id 与空值 + 已知 id 必须回同一个 422：这道门先于查表，不透露 id 是否存在
        req = self.new_request()
        known = self.client.post(f"{API}/card/{req.request_id}/answer",
                                 json={"nonce": req.nonce, "value": ""})
        unknown = self.client.post(f"{API}/card/{'a' * 32}/answer",
                                   json={"nonce": "x", "value": ""})
        self.assertEqual((known.status_code, unknown.status_code), (422, 422))
        self.assertEqual(known.text, unknown.text)

    def test_wrong_nonce_with_empty_value_is_still_422(self):
        # 顺序可辩护：422 说的是"这个 body 不成话"，与 nonce 对不对无关，也不结算任何东西
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": "wrong", "value": ""})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.client.get(f"{API}/card/{req.request_id}/meta").json()["status"],
                         "pending")

    # --- spec 8.2 答者审计与 decline 文案（R16 + R20 后半）---

    def test_declined_card_reports_refusal_not_submission(self):
        req = self.new_request()
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "decline": True})
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["status"], "declined")
        self.assertEqual(meta["status_text"], "已拒绝")
        self.assertEqual(meta["answered_peer"], "127.0.0.1")
        self.assertTrue(meta["answered_at"])
        self.assertNotIn("提交", meta["status_text"])
        self.assertNotIn("nonce", meta)                       # 已终结的卡不再发 nonce

    def test_timed_out_card_answers_410_expired_and_meta_has_no_nonce(self):
        req = self.new_request()
        req.expires_at = time.monotonic() - 1                  # 不调 sleep，直接把墙拨过去
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(res.status_code, 410)
        self.assertEqual(res.json()["detail"], "expired")
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["status"], "timed_out")
        self.assertNotIn("nonce", meta)

    def test_cancelled_card_answers_410(self):
        req = self.new_request()
        asyncio.run(self.registry.cancel(req.request_id, "run_ended"))
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(res.status_code, 410)
        self.assertEqual(res.json()["detail"], "already_answered")

    def test_saved_card_stops_listing_in_pending(self):
        req = self.new_request()
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(self.client.get(f"{API}/pending",
                                         params={"session_id": "s1"}).json()["requests"], [])

    def test_no_endpoint_ever_hands_out_a_value_or_a_ref(self):
        # spec 3 I2：值是 POST 的一次性载荷，任何响应体里都不许再现（含 ref）
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(res.status_code, 200)
        surfaces = {
            "answer": res.text,
            "meta": self.client.get(f"{API}/card/{req.request_id}/meta").text,
            "pending": self.client.get(f"{API}/pending").text,
            "card": self.client.get(f"{API}/card/{req.request_id}").text,
        }
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                self.assertNotIn(DUMMY, text)
                self.assertNotIn(req.ref or "sr_none", text)
        self.assertNotIn("ref", res.json())

    def test_remote_caller_without_identity_is_403_on_every_route(self):
        req = self.new_request()
        remote, _ = make_remote_client()
        with self.assertLogs("backend.router", level="WARNING") as logged:
            self.assertEqual(remote.get(f"{API}/pending",
                                        params={"session_id": "s1"}).status_code, 403)
            self.assertEqual(remote.get(f"{API}/card/{req.request_id}").status_code, 403)
            self.assertEqual(remote.get(f"{API}/card/{req.request_id}/meta").status_code, 403)
            answered = remote.post(f"{API}/card/{req.request_id}/answer",
                                   json={"nonce": req.nonce, "value": DUMMY})
            self.assertEqual(answered.status_code, 403)
        denied = "\n".join(logged.output)
        self.assertEqual(denied.count("access denied"), 4)     # 四条路都留痕
        # 拒绝日志本身也是 I1 的一条通道：值与 nonce 都不许出现在里面
        self.assertNotIn(DUMMY, denied)
        self.assertNotIn(req.nonce, denied)

    def test_authenticated_remote_caller_is_allowed_except_the_bare_list(self):
        # 门禁口径（spec 8.2）：loopback TCP peer 或 request.state.user 任一即可，
        # 但"省略 session_id 的全量清单"仍然只给 loopback。
        remote, registry = make_remote_client(as_user="leo")
        req = asyncio.run(registry.create(
            label="GitHub token", reason="webhook signing", placeholder="ghp_...",
            session_id="s1", agent_id="a1", user_id="u1", channel="console",
        ))
        self.assertEqual(remote.get(f"{API}/pending", params={"session_id": "s1"}).status_code, 200)
        self.assertEqual(remote.get(f"{API}/card/{req.request_id}/meta").json()["nonce"], req.nonce)
        with self.assertLogs("backend.router", level="WARNING"):
            self.assertEqual(remote.get(f"{API}/pending").status_code, 403)

    def test_app_state_allow_remote_hook_is_honoured(self):
        # guard 里除 config.allow_remote 之外还认 app.state.secret_input_allow_remote（brief
        # 原句）。它存在的意义是"装配后运行时再放开"，那就必须有人测：不测的门等于没有门。
        app = FastAPI()
        app.include_router(build_router(registry=self.registry, config=PluginConfig(),
                                        vault=Vault()), prefix="/api/secret-input")
        app.state.secret_input_allow_remote = True
        remote = TestClient(app, client=("10.0.0.9", 1))
        req = self.new_request()
        self.assertEqual(remote.get(f"{API}/pending", params={"session_id": "s1"}).status_code, 200)
        self.assertEqual(remote.get(f"{API}/card/{req.request_id}/meta").json()["nonce"], req.nonce)
        # 全量清单那一支照旧只给 loopback：这个钩子买不到它。
        with self.assertLogs("backend.router", level="WARNING"):
            self.assertEqual(remote.get(f"{API}/pending").status_code, 403)

    # --- 自审发现（brief 没有）：框架的 body 校验会回显 value ⇒ /answer 自己读 body ---

    def test_malformed_answer_bodies_neither_echo_nor_settle_the_card(self):
        # 实测过 FastAPI 默认校验的三种形状（text/plain 装 JSON、JSON 数组、JSON 字符串）
        # 都把提交的值原样打进 422 响应体，二进制 body 还会把 RequestValidationError
        # 顶成 UnicodeDecodeError 抛到宿主异常面（spec 3：异常报文也不许含明文）。这些
        # 形状在这里必须是"固定 detail + 不结算 + 不回显"。
        def js(obj):
            return json.dumps(obj).encode()

        shapes = {
            "json array body": ("application/json", lambda n: js([DUMMY])),
            "json string body": ("application/json", lambda n: js(DUMMY)),
            "truncated json": ("application/json",
                               lambda n: b'{"nonce":"' + n.encode() + b'","v":"' + DUMMY.encode()),
            "binary body": ("application/octet-stream", lambda n: b"\xff\xfe" + DUMMY.encode()),
            "empty body": ("application/json", lambda n: b""),
            "value as list": ("application/json", lambda n: js({"nonce": n, "value": [DUMMY]})),
            "value over the cap": ("application/json",
                                   lambda n: js({"nonce": n, "value": DUMMY + "z" * 5000})),
            "body over the read cap": ("application/json",
                                       lambda n: js({"nonce": n, "value": "q" * 70000})),
            "nonce missing": ("application/json", lambda n: js({"value": DUMMY})),
            "nonce not a string": ("application/json",
                                   lambda n: js({"nonce": 12345, "value": DUMMY})),
            "decline not a bool": ("application/json",
                                   lambda n: js({"nonce": n, "decline": "false", "value": DUMMY})),
        }
        for name, (ctype, build) in shapes.items():
            req = self.new_request()
            res = self.client.post(f"{API}/card/{req.request_id}/answer",
                                   content=build(req.nonce), headers={"Content-Type": ctype})
            with self.subTest(shape=name):
                self.assertNotIn(DUMMY, res.text)
                self.assertNotIn("q" * 64, res.text)
                self.assertTrue(400 <= res.status_code < 500, res.text)
                # detail 是固定字符串，不是 pydantic 那个带 input 的数组
                self.assertIsInstance(res.json().get("detail"), str, res.text)
                meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
                self.assertEqual(meta["status"], "pending")   # 畸形请求不许结算卡片

    def test_well_formed_json_in_a_text_plain_body_still_saves(self):
        # 口径存档：Content-Type 不参与判定（这道面的凭据是 nonce 与 loopback，不是请求头；
        # spec 10.2 第 6 条早已把"同机 agent 能抢答"列为不防）。同时钉住"合法提交走
        # text/plain 也不回显"。
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               content=json.dumps({"nonce": req.nonce, "value": DUMMY}).encode(),
                               headers={"Content-Type": "text/plain"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "saved"})
        self.assertNotIn(DUMMY, res.text)

    def test_router_only_emits_the_ratified_status_codes(self):
        # brief 把码集钉死成 200/403/404/410/422。这条门管住 T7/T9 往表里塞新 code 的冲动
        # （本次实现里 413/400 就是被它劝回 422 的，区分交给 detail 串），也管住表外那两个字
        # 面量（R37）：扫源码，不读表。
        from_source = _codes_in_router_source()
        self.assertEqual(from_source, {403, 404, 410, 422}, from_source)
        # 200 没有字面量可扫：四条路的成功响应都是 FastAPI 的默认码，那就现场量一次。
        req = self.new_request()
        observed = {
            self.client.get(f"{API}/pending").status_code,
            self.client.get(f"{API}/card/{req.request_id}").status_code,
            self.client.get(f"{API}/card/{req.request_id}/meta").status_code,
            self.client.post(f"{API}/card/{req.request_id}/answer",
                             json={"nonce": req.nonce, "value": DUMMY}).status_code,
        }
        self.assertEqual(observed, {200}, observed)
        self.assertEqual(from_source | observed, RATIFIED, (from_source, observed))

    def test_every_detail_the_router_emits_has_an_answer_page_copy(self):
        # 闭集漂移门：router 里多一个 reason、答页少一句文案，这条当场红。
        # （detail 必须是固定串这件事由 _ANSWER_MAP 的形状保证：value 从不进表。）
        page = render_page("a" * 32)
        for reason in _ANSWER_MAP:
            with self.subTest(reason=reason):
                self.assertIn(reason + ":", page)

    def test_expires_in_is_clamped_at_the_response_boundary(self):
        # 修复轮 1 · R38：expires_in 是要画到答页/卡片倒计时上的，负数就是"已超时 -12 秒"
        # 这种假话。records.seconds_left() 今天自己夹了 0，但那是下层的内部口径、不是这条
        # 响应的契约 —— 下层一改（或新增一个使用者）负数就直接上线。所以响应面自己夹，
        # 这里用"下层回负数"的桩来钉这一层，而不是钉那一层。
        def past_expiry(req):
            req.seconds_left = lambda: -12.3       # 桩：不夹 0 的 seconds_left

        # 1) 还 pending 的 meta
        req = self.new_request()
        past_expiry(req)
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["status"], "pending")
        self.assertEqual(meta["expires_in"], 0.0, _public(meta))

        # 2) 已终结的 meta —— R38 点名的那一支（超时后从墓地读出来的卡）
        req = self.new_request()
        req.expires_at = time.monotonic() - 12.3
        past_expiry(req)
        self.assertEqual(self.client.post(f"{API}/card/{req.request_id}/answer",
                                          json={"nonce": req.nonce, "value": DUMMY}).status_code,
                         410)
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["status"], "timed_out")
        self.assertNotIn("nonce", meta)
        self.assertEqual(meta["expires_in"], 0.0, _public(meta))

        # 3) /pending 里同一个字段同一个口径（这条只改桩、不把 expires_at 拨过去，
        #    否则 sweep 会先把它结算掉，列表里就没有这张卡可测了）
        req = self.new_request()
        past_expiry(req)
        item = self.client.get(f"{API}/pending",
                               params={"session_id": "s1"}).json()["requests"][0]
        self.assertEqual(item["expires_in"], 0.0, item)

    def test_pending_body_never_contains_a_live_nonce(self):
        # 两步取 nonce 是 spec 8.1 钉死的：/pending 高频且按会话过滤，它一旦带上 nonce，
        # "只有真正打开过答页的一侧能提交"这条前提就废了。这里直接对原文找 nonce。
        req = self.new_request()
        for url in (f"{API}/pending", f"{API}/pending?session_id=s1"):
            text = self.client.get(url).text
            with self.subTest(url=url):
                self.assertNotIn(req.nonce, text)
                self.assertNotIn("nonce", text)


class NoStoreAndLoopbackSetTest(unittest.TestCase):
    """终审 Important 3 + Minor 9。

    Important 3：`/card/{id}/meta` 是全仓库唯一发 nonce 的响应面，而 nonce 是持有型凭据
    —— 配上 request_id（就在 INFO 日志与答页 URL 里），谁拿到都能替人提交一个模型会信的值。
    答页设计上要在新标签页打开，`answer_url` 又按 Host 头推导（远程控制台要能点）⇒ 中间
    可能有代理。所以答页与 meta 两个响应都必须 `no-store`，别在浏览器或中间缓存里留下比
    pending 窗口更长寿的副本。

    Minor 9：`_LOOPBACK` 里原有的 `"localhost"` 是死条目 —— `request.client.host` 是套接字
    地址，永远不等于这个字符串；而宿主哪天挂上 ProxyHeadersMiddleware，它就变成可用
    Host/XFF 伪造的放行口。删掉它，并把 README 一直承诺、却从没有测试的那半边（`127.0.0.2`
    不算 loopback）钉成用例。
    """

    def setUp(self):
        self.client, self.registry = make_client()

    def new_request(self, session_id="s1"):
        return asyncio.run(self.registry.create(
            label="GitHub token", reason="webhook signing", placeholder="",
            session_id=session_id, agent_id="a1", user_id="u1", channel="console",
        ))

    def _client_with_peer(self, peer):
        vault = Vault(ttl_seconds=300)
        registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
        app = FastAPI()
        app.include_router(build_router(registry=registry, config=DEFAULT_CONFIG, vault=vault),
                           prefix="/api/secret-input")
        return TestClient(app, client=(peer, 1)), registry

    def test_answer_page_and_meta_are_no_store_in_every_state(self):
        req = self.new_request()
        for url in (f"{API}/card/{req.request_id}", f"{API}/card/{req.request_id}/meta"):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.headers["cache-control"], "no-store")
                self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        # 头没把载荷改坏：活卡的 meta 仍然发 nonce（全仓库唯一一处）
        self.assertTrue(self.client.get(f"{API}/card/{req.request_id}/meta").json()["nonce"])

        # 终态也要 no-store：终态 meta 带答者审计，且答页会停在这个状态上。
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "value": DUMMY})
        ended = self.client.get(f"{API}/card/{req.request_id}/meta")
        self.assertEqual(ended.json()["status"], "saved")
        self.assertEqual(ended.headers["cache-control"], "no-store")
        self.assertNotIn("nonce", ended.json())

    def test_loopback_matching_is_literal_and_fails_closed(self):
        from backend import router as router_mod

        self.assertNotIn("localhost", router_mod._LOOPBACK)
        self.assertEqual(router_mod._LOOPBACK, {"127.0.0.1", "::1"})
        for peer, expected in (("127.0.0.1", 200), ("::1", 200),
                               ("127.0.0.2", 403), ("::ffff:127.0.0.1", 403),
                               ("localhost", 403), ("10.0.0.9", 403)):
            client, _registry = self._client_with_peer(peer)
            with self.subTest(peer=peer):
                self.assertEqual(client.get(f"{API}/pending").status_code, expected)


if __name__ == "__main__":
    unittest.main()
