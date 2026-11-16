/* Page behaviour. An external file, so the Content-Security-Policy needs no 'unsafe-inline'. */
(function () {
  "use strict";

  var COOKIE = "esim_download";

  function tooLarge(input) {
    var max = Number(input.dataset.maxBytes || 0);
    var file = input.files && input.files[0];
    return Boolean(max && file && file.size > max);
  }

  function showError(text) {
    var message = document.createElement("p");
    message.className = "error";
    message.setAttribute("role", "alert");
    message.textContent = text;
    document.getElementById("results").replaceChildren(message);
  }

  function randomToken() {
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
  }

  function resetGenerating() {
    var status = document.getElementById("generating");
    var button = document.getElementById("download");
    if (status) status.hidden = true;
    if (button) button.disabled = false;
  }

  // Mirror the server's size caps so an oversize file is caught before upload.
  // Capture phase: runs before htmx's own change listener on the input.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
    input.setCustomValidity(tooLarge(input) ? input.dataset.tooLarge : "");
    if (input.id === "file") {
      document.getElementById("results").replaceChildren(); // results of the previous file
    } else {
      input.reportValidity();
    }
  }, true);

  // Skip the validation request when no file is chosen or it is too large.
  document.addEventListener("htmx:beforeRequest", function (event) {
    var input = event.detail.elt;
    if (input.id !== "file") return;
    if (!input.files || !input.files.length) {
      event.preventDefault();
    } else if (tooLarge(input)) {
      event.preventDefault();
      showError(input.dataset.tooLarge);
    }
  });

  // Show "Generating..." until the server answers. A ZIP download does not leave
  // the page, so the server echoes a random token as a short-lived cookie when the
  // ZIP is sent; seeing it means the download has started.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.id !== "generate-form") return;
    var token = randomToken();
    var expected = COOKIE + "=" + token;
    form.elements.namedItem("download_token").value = token;
    document.getElementById("generating").hidden = false;
    var button = document.getElementById("download");
    if (button) button.disabled = true;
    var timer = window.setInterval(function () {
      if (document.cookie.split("; ").indexOf(expected) === -1) return;
      window.clearInterval(timer);
      document.cookie = COOKIE + "=; Max-Age=0; Path=/; SameSite=Strict";
      resetGenerating();
    }, 500);
  });

  // A page restored from the back/forward cache must not show a stale message.
  window.addEventListener("pageshow", resetGenerating);
})();
