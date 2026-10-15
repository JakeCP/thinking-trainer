// Math rendering, live answer preview, and "busy" states for slow submits.
(function () {
  const delimiters = [
    { left: "$$", right: "$$", display: true },
    { left: "\\[", right: "\\]", display: true },
    { left: "\\(", right: "\\)", display: false },
    { left: "$", right: "$", display: false },
  ];
  const typeset = (el) => window.renderMathInElement && renderMathInElement(el, { delimiters, throwOnError: false });

  function escapeHtml(s) {
    return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  function setupPreview(textarea) {
    const target = document.getElementById(textarea.dataset.preview);
    if (!target) return;
    let timer;
    const update = () => {
      // Preview only when there's math to see; plain text needs no echo.
      if (!/\$|\\\(|\\\[/.test(textarea.value)) { target.innerHTML = ""; return; }
      target.innerHTML = escapeHtml(textarea.value).replace(/\n/g, "<br>");
      typeset(target);
    };
    textarea.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(update, 250); });
    update();
  }

  function setupBusy(form) {
    form.addEventListener("submit", () => {
      const label = form.dataset.busy;
      form.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      const text = form.querySelector("[data-busy-text]");
      if (text) text.textContent = label;
      const btn = form.querySelector("button.primary") || form.querySelector("button");
      if (btn) btn.textContent = label;
    });
  }

  window.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".math").forEach(typeset);
    document.querySelectorAll("textarea[data-preview]").forEach(setupPreview);
    document.querySelectorAll("form[data-busy]").forEach(setupBusy);
    const auto = document.querySelector("form[data-autosubmit]");
    if (auto) auto.requestSubmit();
  });
})();
