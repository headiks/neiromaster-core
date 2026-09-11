/*
 * Клиент уведомлений плана адаптации.
 *
 * Опрашивает инбокс (/api/my/messages), берёт доставленные, но не прочитанные
 * сообщения и показывает их ПО ОДНОМУ: всплывашка в углу + браузерное уведомление.
 * Если накопилось несколько — это очередь: первое показывается, затем через
 * задержку следующее. Когда всплывашка закрыта (кнопкой или сама по таймеру),
 * сообщение помечается прочитанным на сервере и очередь двигается дальше.
 *
 * Подключается на страницах кабинета и админки. window.nmTestNotification() —
 * ручная отправка тестового уведомления себе (кнопка в админке).
 */
(function () {
  "use strict";

  var POLL_MS = 20000; // как часто опрашивать инбокс
  var AUTO_MS = 8000;  // сколько держать всплывашку, если не закрыли вручную
  var GAP_MS = 900;    // пауза между сообщениями очереди

  var queue = [];          // сообщения, ждущие показа (FIFO)
  var seen = {};           // id -> true: уже в очереди или показаны (без дублей)
  var showing = false;     // сейчас на экране всплывашка

  function api(url, opts) {
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) { throw new Error("401"); }
      return r;
    });
  }

  // ---- Разрешение на браузерные уведомления ----
  function ensurePermission() {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      try { Notification.requestPermission(); } catch (e) { /* старый API */ }
    }
  }

  function browserNotify(title, body) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      new Notification(title || "НейроМастер", {
        body: (body || "").slice(0, 300),
        tag: "neiromaster-plan",
      });
    } catch (e) { /* некоторые браузеры требуют ServiceWorker — молча пропускаем */ }
  }

  // ---- Всплывашка в углу ----
  function ensureContainer() {
    var el = document.getElementById("nm-toast-wrap");
    if (el) return el;
    el = document.createElement("div");
    el.id = "nm-toast-wrap";
    el.style.cssText =
      "position:fixed;top:16px;right:16px;z-index:99999;display:flex;" +
      "flex-direction:column;gap:10px;max-width:360px;width:calc(100vw - 32px);" +
      "font-family:'Segoe UI',system-ui,-apple-system,sans-serif;";
    document.body.appendChild(el);
    return el;
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function showToast(msg, onClose) {
    var wrap = ensureContainer();
    var card = document.createElement("div");
    card.style.cssText =
      "background:#fff;color:#1e293b;border-radius:12px;padding:14px 16px;" +
      "box-shadow:0 10px 30px rgba(15,23,42,.22);border-left:4px solid #3b82f6;" +
      "opacity:0;transform:translateY(-8px);transition:opacity .2s,transform .2s;";
    card.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;">' +
      '  <strong style="font-size:14px;color:#0f172a;">' + esc(msg.title || "Новое сообщение") + "</strong>" +
      '  <button aria-label="Закрыть" style="border:none;background:none;cursor:pointer;color:#94a3b8;font-size:18px;line-height:1;">&times;</button>' +
      "</div>" +
      '<div style="white-space:pre-wrap;font-size:13px;color:#334155;margin-top:6px;max-height:180px;overflow:auto;">' +
      esc(msg.body || "") + "</div>" +
      '<div style="text-align:right;margin-top:10px;">' +
      '  <button style="border:1px solid #cbd5e1;background:#f8fafc;color:#334155;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;">Прочитано</button>' +
      "</div>";
    wrap.appendChild(card);
    requestAnimationFrame(function () {
      card.style.opacity = "1";
      card.style.transform = "translateY(0)";
    });

    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      card.style.opacity = "0";
      card.style.transform = "translateY(-8px)";
      setTimeout(function () { card.remove(); }, 200);
      onClose();
    }
    card.querySelector('button[aria-label="Закрыть"]').addEventListener("click", close);
    card.querySelectorAll("button")[1].addEventListener("click", close);
    var timer = setTimeout(close, AUTO_MS);
  }

  // ---- Очередь ----
  function pump() {
    if (showing || !queue.length) return;
    showing = true;
    var msg = queue.shift();
    browserNotify(msg.title, msg.body);
    showToast(msg, function () {
      // помечаем прочитанным на сервере (id содержит ':')
      api("/api/my/messages/" + encodeURIComponent(msg.id) + "/read", { method: "POST" })
        .catch(function () {});
      showing = false;
      setTimeout(pump, GAP_MS); // задержка перед следующим сообщением очереди
    });
  }

  function poll() {
    api("/api/my/messages")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        (d.messages || []).forEach(function (m) {
          if (m.status !== "delivered" || seen[m.id]) return;
          seen[m.id] = true;
          queue.push({ id: m.id, title: m.title, body: m.body });
        });
        pump();
      })
      .catch(function () {});
  }

  // Немедленный опрос инбокса (после отправки теста себе — чтобы всплыло сразу).
  window.nmPollNow = poll;

  // Ручной тест из админки: отправить себе и сразу опросить.
  window.nmTestNotification = function (title, body) {
    ensurePermission();
    return api("/api/my/messages/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title || "", body: body || "" }),
    }).then(function () { setTimeout(poll, 300); });
  };

  function start() {
    ensurePermission();
    // если разрешение ещё не дано — попросим при первом клике (жест пользователя)
    if ("Notification" in window && Notification.permission === "default") {
      document.addEventListener("click", ensurePermission, { once: true });
    }
    poll();
    setInterval(poll, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
