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
  var API = "/api/secret-input";
  var POLL_MS = 2000;
  var ENDED_MS = 8000;

  function sessionId() {
    try {
      if (typeof QP.host.getCurrentSessionId === "function") {
        return QP.host.getCurrentSessionId() || "";
      }
    } catch (e) { /* 宿主未就绪时静默，卡片下一轮再试 */ }
    return "";
  }

  async function getPending(sid) {
    var url = API + "/pending?session_id=" + encodeURIComponent(sid);
    var resp = await QP.host.fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) return null;              // 这一轮没结果：保留上一帧，不闪没卡片
    var data = await resp.json().catch(function () { return {}; });
    return (data && data.requests) || [];
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
  async function fetchNonce(requestId) {
    var resp = await QP.host.fetch(API + "/card/" + requestId + "/meta", {
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) return null;
    var meta = await resp.json().catch(function () { return {}; });
    // 卡片可能在轮询间隙已经终结：终态记录不再回 nonce，这时不提交。
    if (!meta || meta.status !== "pending" || !meta.nonce) return null;
    return meta.nonce;
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

    async function save() {
      if (!value) { setError("值为空"); return; }
      var requestId = item.request_id;
      var pendingValue = value;
      setValue("");            // 提交后立刻清掉内存里的输入
      setError("");
      setBusy(true);
      try {
        var nonce = await fetchNonce(requestId);
        if (!nonce) { setError("请求已结束"); return; }
        var res = await postAnswer(requestId, { nonce: nonce, value: pendingValue });
        if (res.ok) { done(res.state === "declined" ? "declined" : "saved"); }
        else { setError("提交失败，请重试"); }
      } catch (e) {
        setError("提交失败，请重试");
      } finally {
        setBusy(false);
      }
    }

    async function decline() {
      var requestId = item.request_id;
      setError("");
      setBusy(true);
      try {
        var nonce = await fetchNonce(requestId);
        if (!nonce) { setError("请求已结束"); return; }
        var res = await postAnswer(requestId, { nonce: nonce, decline: true });
        if (res.ok) { done("declined"); }
        else { setError("提交失败，请重试"); }
      } catch (e) {
        setError("提交失败，请重试");
      } finally {
        setBusy(false);
      }
    }

    return h(
      "div",
      { style: panelStyle({}) },
      h("div", { style: { fontSize: 15, fontWeight: 600 }, textContent: item.label }),
      h("div", {
        style: { fontSize: 12, color: "#9198a1", marginTop: 4 },
        textContent: item.reason,
      }),
      h("div", {
        style: { fontSize: 11, color: "#d29922", marginTop: 8 },
        textContent: "值不会进入对话，模型看不到它。",
      }),
      h(antd.Input.Password, {
        style: { marginTop: 10 },
        placeholder: item.placeholder || "粘贴密钥",
        value: value,
        onChange: function (e) { setValue(e.target.value); },
        onPressEnter: save,
        autoComplete: "new-password",
      }),
      error
        ? h("div", {
            style: { color: "#f85149", fontSize: 12, marginTop: 6 },
            textContent: error,
          })
        : null,
      h(
        "div",
        { style: { marginTop: 10 } },
        h(antd.Button, {
          type: "primary", size: "small", loading: isBusy, onClick: save, children: "保存",
        }),
        h(antd.Button, {
          size: "small", style: { marginLeft: 8 }, onClick: decline, children: "拒绝",
        }),
      ),
    );
  }

  function EndedCard(props) {
    var info = props.info || {};
    var declined = props.status === "declined";
    return h(
      "div",
      {
        style: panelStyle({
          border: "1px solid #238636", padding: 12, color: "#3fb950", fontSize: 13,
        }),
      },
      h("div", { textContent: declined ? "已拒绝" : "已保存" }),
      info.answered_peer
        ? h("div", {
            style: { color: "#9198a1", fontSize: 11, marginTop: 4 },
            textContent: "已由 " + info.answered_peer + " 于 " + (info.answered_at || "") +
              (declined ? " 拒绝" : " 提交"),
          })
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
      async function tick() {
        var sid = sessionId();
        if (!sid) return;
        try {
          var list = await getPending(sid);
          if (!stopped && list) setPending(list);
        } catch (e) { /* 轮询失败保持上一帧，下一轮再试 */ }
      }
      tick();
      var timer = setInterval(tick, POLL_MS);
      return function () { stopped = true; clearInterval(timer); };
    }, []);

    // 终态卡片只停一会儿，免得同一会话里的下一张卡被它挡到会话结束。
    React.useEffect(function () {
      if (!endedState) { return undefined; }
      var timer = setTimeout(function () { setEnded(null); }, ENDED_MS);
      return function () { clearTimeout(timer); };
    }, [endedState]);

    var current = pending[0];
    if (endedState) { return h(EndedCard, endedState); }
    if (!current) { return null; }
    return h(SecretCard, {
      item: current,
      done: function (status) {
        var requestId = current.request_id;   // 这张卡自己的 id，绝不回读列表
        QP.host.fetch(API + "/card/" + requestId + "/meta", {
          headers: { Accept: "application/json" },
        })
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (info) { setEnded({ status: status, info: info || {} }); })
          .catch(function () { setEnded({ status: status, info: {} }); });
      },
    });
  }

  QP.slot.fill(PLUGIN_ID, "overlay.global", function () { return h(Overlay); });
  console.info("[secret-input] overlay card registered");
})();
