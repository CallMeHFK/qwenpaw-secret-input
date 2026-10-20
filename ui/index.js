/**
 * secret-input —— 会话内私密输入卡片（免构建，React.createElement）
 * 常驻 overlay.global，2s 轮询本会话的待答请求；值只 POST 回插件，
 * 绝不写进聊天、绝不进模型上下文。与 qwenpaw-file-browser 同一套宿主范式。
 */
(function () {
  "use strict";

  if (!window.QwenPaw || !window.QwenPaw.host) {
    console.error("[secret-input] QwenPaw host SDK not ready");
    return;
  }

  var QP = window.QwenPaw;
  var React = QP.host.React;
  var antd = QP.host.antd;
  var h = React.createElement;

  var PLUGIN_ID = "secret-input";
  // 不带 /api：宿主的 host.fetch 是 ei(e,t)=fetch(ce(e),…) 而 ce(e)="/api"+e
  // （console/assets/index-*.js，2.2.2 实测），无条件前缀。写成 "/api/secret-input"
  // 会发出 /api/api/secret-input/pending → 404，而 getPending 对非 2xx 一律回 null
  // 「保留上一帧」⇒ 卡片永远不显示，且控制台之外零征兆（R59）。
  var API = "/secret-input";
  var POLL_MS = 2000;
  var ENDED_MS = 30000;

  // 终态标题走本地闭集：服务端只提供 status，任何响应文本都不拼进卡片。
  var ENDED_TEXT = {
    saved: "已保存",
    declined: "已拒绝",
    timed_out: "已超时",
    cancelled: "已取消",
  };

  function sessionId() {
    try {
      if (typeof QP.host.getCurrentSessionId === "function") {
        return QP.host.getCurrentSessionId() || "";
      }
    } catch (e) { /* 宿主未就绪时静默，卡片下一轮再试 */ }
    return "";
  }

  // 2xx 但体不成形（宿主回 HTML、代理插页）也回 null：保留上一帧，别把卡片闪没。
  async function getPending(sid) {
    var url = API + "/pending?session_id=" + encodeURIComponent(sid);
    var resp = await QP.host.fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) return null;              // 这一轮没结果：保留上一帧，不闪没卡片
    var data;
    try {
      data = await resp.json();
    } catch (e) {
      return null;                          // JSON 坏掉不等于「没有待答请求」
    }
    if (!data || !Array.isArray(data.requests)) return null;
    return data.requests;
  }

  // 只回传终结状态与 HTTP 码；响应里的任何其他字段都不读，
  // 尤其没有任何「值」字段会在客户端被解析。
  async function postAnswer(requestId, body) {
    var resp = await QP.host.fetch(API + "/card/" + requestId + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    var data = await resp.json().catch(function () { return {}; });
    return { ok: resp.ok, status: resp.status, state: (data && data.status) || "" };
  }

  // nonce 只在提交这一刻现取现用，绝不从 /pending 缓存、绝不落到 state 里。
  // 回整个 meta 而不是单个 nonce：提交瞬间卡片可能已被他人答掉或已超时，
  // 卡片需要真实终态与答者审计来展示，而不是一句笼统的「请求已结束」。
  async function fetchMeta(requestId) {
    var resp = await QP.host.fetch(API + "/card/" + requestId + "/meta", {
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) return null;
    var meta;
    try {
      meta = await resp.json();
    } catch (e) {
      return null;
    }
    if (!meta || typeof meta !== "object") return null;
    return meta;
  }

  // 提交失败后再看一眼 meta：若是竞态（别人刚答完 / 刚超时），把真实终态交给 ended 卡。
  async function terminalMeta(requestId) {
    var meta;
    try {
      meta = await fetchMeta(requestId);
    } catch (e) {
      return null;
    }
    if (!meta || !meta.status || meta.status === "pending") return null;
    return meta;
  }

  function panelStyle(extra) {
    var base = {
      position: "fixed", right: 24, bottom: 96, width: 360, zIndex: 1080,
      background: "#161b22", color: "#e6edf3", border: "1px solid #30363d",
      borderRadius: 10, padding: 16, boxShadow: "0 8px 24px rgba(0,0,0,.4)",
    };
    for (var key in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, key)) base[key] = extra[key];
    }
    return base;
  }

  // 倒计时只消费 /pending 的 expires_in，服务端每轮重新给值；本地只向下走、下界 0。
  function clampSeconds(n) {
    var v = Number(n);
    if (!isFinite(v)) return null;
    return Math.max(0, Math.round(v));
  }

  function formatCountdown(total) {
    var s = Math.max(0, Math.round(Number(total) || 0));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + ":" + (r < 10 ? "0" : "") + r;
  }

  function useCountdown(item) {
    var state = React.useState(clampSeconds(item.expires_in));
    var remaining = state[0];
    var setRemaining = state[1];
    React.useEffect(function () {
      var base = clampSeconds(item.expires_in);
      setRemaining(base);
      if (base === null) { return undefined; }
      var timer = setInterval(function () {
        setRemaining(function (prev) {
          if (prev === null) { return null; }
          return Math.max(0, prev - 1);
        });
      }, 1000);
      return function () { clearInterval(timer); };
    }, [item.request_id, item.expires_in]);
    return remaining;
  }

  function SecretCard(props) {
    var item = props.item;
    var done = props.done;
    var input = React.useState("");
    var value = input[0];
    var setValue = input[1];
    var busy = React.useState(false);
    var isBusy = busy[0];
    var setBusy = busy[1];
    var err = React.useState("");
    var error = err[0];
    var setError = err[1];
    var remaining = useCountdown(item);

    async function save() {
      if (isBusy) { return; }
      if (!value) { setError("值为空"); return; }
      var requestId = item.request_id;
      var pendingValue = value;
      setValue("");            // 提交后立刻清掉内存里的输入
      setError("");
      setBusy(true);
      try {
        var meta = await fetchMeta(requestId);
        if (!meta) { setError("请求已结束"); return; }
        if (meta.status !== "pending") {
          // 轮询间隙已被答掉 / 已超时：显示真实终态与答者，不当成提交失败。
          if (meta.status) { done(meta.status, meta); return; }
          setError("请求已结束");
          return;
        }
        if (!meta.nonce) { setError("请求已结束"); return; }
        var res = await postAnswer(requestId, { nonce: meta.nonce, value: pendingValue });
        if (res.ok) { done(res.state === "declined" ? "declined" : "saved"); return; }
        var after = await terminalMeta(requestId);
        if (after) { done(after.status, after); return; }
        setError("提交失败，请重试");
      } catch (e) {
        setError("提交失败，请重试");
      } finally {
        setBusy(false);
      }
    }

    async function decline() {
      if (isBusy) { return; }
      var requestId = item.request_id;
      setValue("");            // 拒答同样不再保留已经敲进去的值
      setError("");
      setBusy(true);
      try {
        var meta = await fetchMeta(requestId);
        if (!meta) { setError("请求已结束"); return; }
        if (meta.status !== "pending") {
          if (meta.status) { done(meta.status, meta); return; }
          setError("请求已结束");
          return;
        }
        if (!meta.nonce) { setError("请求已结束"); return; }
        var res = await postAnswer(requestId, { nonce: meta.nonce, decline: true });
        if (res.ok) { done("declined"); return; }
        var after = await terminalMeta(requestId);
        if (after) { done(after.status, after); return; }
        setError("提交失败，请重试");
      } catch (e) {
        setError("提交失败，请重试");
      } finally {
        setBusy(false);
      }
    }

    return h(
      "div",
      { style: panelStyle({}) },
      h("div", { style: { fontSize: 15, fontWeight: 600 } }, item.label),
      h("div", { style: { fontSize: 12, color: "#9198a1", marginTop: 4 } }, item.reason),
      h("div", { style: { fontSize: 11, color: "#d29922", marginTop: 8 } },
        "值不会进入对话，模型看不到它。"),
      remaining === null
        ? null
        : h("div", { style: { fontSize: 11, color: "#9198a1", marginTop: 6 } },
            "倒计时 " + formatCountdown(remaining)),
      h(antd.Input.Password, {
        style: { marginTop: 10 },
        placeholder: item.placeholder || "粘贴密钥",
        value: value,
        onChange: function (e) { setValue(e.target.value); },
        onPressEnter: save,
        autoComplete: "new-password",
      }),
      error
        ? h("div", { style: { color: "#f85149", fontSize: 12, marginTop: 6 } }, error)
        : null,
      h(
        "div",
        { style: { marginTop: 10 } },
        h(antd.Button, {
          type: "primary", size: "small", loading: isBusy, disabled: isBusy,
          onClick: save,
        }, "保存"),
        h(antd.Button, {
          size: "small", style: { marginLeft: 8 }, disabled: isBusy,
          onClick: decline,
        }, "拒绝"),
      ),
    );
  }

  function EndedCard(props) {
    var info = props.info || {};
    var status = props.status || info.status || "saved";
    var declined = status === "declined";
    return h(
      "div",
      {
        style: panelStyle({
          border: "1px solid #238636", padding: 12, color: "#3fb950", fontSize: 13,
        }),
      },
      h("div", null, ENDED_TEXT[status] || "已结束"),
      info.answered_peer
        ? h("div", { style: { color: "#9198a1", fontSize: 11, marginTop: 4 } },
            "已由 " + info.answered_peer + " 于 " + (info.answered_at || "") +
              (declined ? " 拒绝" : " 提交"))
        : null,
    );
  }

  function Overlay() {
    var state = React.useState([]);
    var pending = state[0];
    var setPending = state[1];
    var ended = React.useState(null);
    var endedState = ended[0];
    var setEnded = ended[1];

    React.useEffect(function () {
      var stopped = false;
      var inFlight = false;
      var lastSid = null;
      async function tick() {
        // 一次只放一个请求在途：晚到的旧响应不可能盖掉新一帧。
        if (stopped || inFlight) { return; }
        var sid = sessionId();
        if (sid !== lastSid) {
          // 切会话：上一会话的待答与终态一律作废，避免把 A 的值递给 B。
          lastSid = sid;
          setPending([]);
          setEnded(null);
        }
        if (!sid) { return; }
        inFlight = true;
        try {
          var list = await getPending(sid);
          if (!stopped && sessionId() === sid && list) setPending(list);
        } catch (e) { /* 轮询失败保持上一帧，下一轮再试 */ }
        finally { inFlight = false; }
      }
      tick();
      var timer = setInterval(tick, POLL_MS);
      return function () { stopped = true; clearInterval(timer); };
    }, []);

    // 终态卡片留 30s，够人核对答者审计；下一张卡最多等这半分钟。
    React.useEffect(function () {
      if (!endedState) { return undefined; }
      var timer = setTimeout(function () { setEnded(null); }, ENDED_MS);
      return function () { clearTimeout(timer); };
    }, [endedState]);

    var current = pending[0];
    if (endedState) {
      return h(EndedCard, { status: endedState.status, info: endedState.info });
    }
    if (!current) { return null; }
    return h(SecretCard, {
      key: current.request_id,               // A→B 换卡必须重挂，输入/忙/错状态全部归零
      item: current,
      done: function (status, info) {
        var requestId = current.request_id;   // 这张卡自己的 id，绝不回读列表
        if (info) {
          setEnded({ status: status || info.status || "saved", info: info });
          return;
        }
        QP.host.fetch(API + "/card/" + requestId + "/meta", {
          headers: { Accept: "application/json" },
        })
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (m) { setEnded({ status: status, info: m || {} }); })
          .catch(function () { setEnded({ status: status, info: {} }); });
      },
    });
  }

  QP.slot.fill(PLUGIN_ID, "overlay.global", function () { return h(Overlay); });
  console.info("[secret-input] overlay card registered");
})();
