// Клиентское логирование кликов. Просмотры страниц пишет сервер (middleware в app.py) —
// здесь только клики, чтобы не дублировать. Подключение опционально:
//   <script src="/static/activity.js" defer></script>
// Логируются клики по кнопкам, ссылкам и любым элементам с атрибутом data-log.
(function () {
  "use strict";

  function describe(el) {
    var t = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
    return {
      tag: el.tagName ? el.tagName.toLowerCase() : "",
      id: el.id || null,
      name: el.getAttribute && el.getAttribute("data-log") || null,
      text: t ? t.slice(0, 80) : null,
    };
  }

  function send(detail) {
    try {
      fetch("/api/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        keepalive: true,
        body: JSON.stringify({ type: "click", path: location.pathname, detail: detail }),
      }).catch(function () {});
    } catch (e) { /* логирование не должно мешать интерфейсу */ }
  }

  document.addEventListener("click", function (ev) {
    var el = ev.target && ev.target.closest
      ? ev.target.closest("button, a, [data-log], input[type=submit], input[type=button]")
      : null;
    if (el) send(describe(el));
  }, true);
})();
