/* Page behaviour. An external file, so the Content-Security-Policy needs no 'unsafe-inline'. */
(function () {
  "use strict";

  // The download in progress or stopped part-way:
  //   { url, data: FormData, parts, next, controller }
  // `next` is the part to request next; `controller` is set only while a request is running.
  var run = null;

  function byId(id) { return document.getElementById(id); }

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
    byId("results").replaceChildren(message);
  }

  // The user's local date and time as DDMMYYYY_HHMM, for the ZIP file name.
  function localStamp(now) {
    function two(n) { return String(n).padStart(2, "0"); }
    return two(now.getDate()) + two(now.getMonth() + 1) + String(now.getFullYear()).padStart(4, "0") +
      "_" + two(now.getHours()) + two(now.getMinutes());
  }

  // Show the progress line. While a request is running the inputs are locked (the
  // request data was captured when the download started) and Cancel is offered;
  // when stopped, `retryLabel` (if any) offers to continue from the same part.
  function setProgress(running, message, retryLabel) {
    byId("progress").hidden = false;
    byId("generating").textContent = message;
    byId("cancel").hidden = !running;
    var retry = byId("retry");
    retry.hidden = !retryLabel;
    retry.textContent = retryLabel || "";
    lockInputs(running);
  }

  function lockInputs(locked) {
    var button = byId("download");
    if (button) button.disabled = locked;
    document.querySelectorAll("#generate-form fieldset").forEach(function (fieldset) {
      fieldset.disabled = locked;
    });
  }

  function reset() {
    var controller = run && run.controller;
    run = null;
    if (controller) controller.abort();
    byId("progress").hidden = true;
    lockInputs(false);
  }

  function partOf(current) { return "part " + current.next + " of " + current.parts; }

  function fileName(response) {
    var match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    return match ? match[1] : "esim_batch.zip";
  }

  function save(blob, name) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
  }

  // A failed part is answered with the full page; its #results block holds the reason:
  // either one error message, or the validation table.
  function readFailure(response) {
    return response.text().then(function (html) {
      var results = new DOMParser().parseFromString(html, "text/html").getElementById("results");
      var error = results && results.querySelector("p.error");
      return {
        failure: true,
        message: error ? error.textContent.trim() : "",
        table: results && results.querySelector("table") ? results : null
      };
    });
  }

  // Request run.next, download it, then move on to the following part. Parts are
  // requested strictly one after another, never in parallel.
  function requestPart(current) {
    current.controller = new AbortController();
    current.data.set("part", String(current.next));
    current.data.set("parts", String(current.parts));
    setProgress(true, "Generating " + partOf(current) + "…");

    fetch(current.url, {
      method: "POST",
      body: current.data,
      credentials: "same-origin",
      headers: { "X-CSRF-Token": String(current.data.get("csrf_token") || "") },
      signal: current.controller.signal
    }).then(function (response) {
      // Logged out meanwhile (session expired): the server redirected to the login page.
      if (response.status === 401 || response.redirected) {
        window.location.assign("/login");
        throw { loggedOut: true };
      }
      if (!response.ok) {
        return readFailure(response).then(function (failure) { throw failure; });
      }
      return response.blob().then(function (blob) {
        return { blob: blob, name: fileName(response) };
      });
    }).then(function (file) {
      if (run !== current) return; // a new file was chosen meanwhile
      current.controller = null;
      save(file.blob, file.name);
      current.next += 1;
      if (current.next <= current.parts) {
        requestPart(current);
        return;
      }
      run = null;
      setProgress(false, current.parts === 1 ?
        "Done: the ZIP file was downloaded." :
        "Done: all " + current.parts + " ZIP files were downloaded.", "");
    }).catch(function (reason) {
      if (run !== current) return;
      current.controller = null;
      if (reason && reason.loggedOut) {
        run = null;
        return;
      }
      var n = current.next;
      if (reason && reason.name === "AbortError") {
        setProgress(false, "Cancelled before " + partOf(current) + " was downloaded." +
          (n > 1 ? " Parts 1 to " + (n - 1) + " are already downloaded." : ""),
          n > 1 ? "Resume from part " + n : "Start again");
      } else if (reason && reason.failure && reason.table) {
        // The file no longer passes validation: show the server's table, nothing to retry.
        run = null;
        byId("results").replaceChildren.apply(byId("results"),
          Array.prototype.map.call(reason.table.childNodes, function (node) {
            return document.importNode(node, true);
          }));
        setProgress(false, "Stopped at " + partOf(current) + ": the file did not pass validation.", "");
      } else {
        var why = reason && reason.failure && reason.message ? reason.message :
          "The connection was lost or the server did not answer.";
        setProgress(false, "Part " + n + " of " + current.parts + " failed. " + why, "Retry part " + n);
      }
    });
  }

  // Mirror the server's size caps so an oversize file is caught before upload.
  // Capture phase: runs before htmx's own change listener on the input.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
    input.setCustomValidity(tooLarge(input) ? input.dataset.tooLarge : "");
    if (input.id === "file") {
      reset(); // a different file: forget any stopped download
      byId("results").replaceChildren(); // and the results of the previous file
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

  // "Download": capture the form once (file, branding, local time stamp) and
  // request the parts in order. The same captured data is sent for every part.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.id !== "generate-form") return;
    event.preventDefault();
    if (run && run.controller) return; // already running
    var button = byId("download");
    var data = new FormData(form);
    data.set("client_stamp", localStamp(new Date()));
    run = {
      url: form.getAttribute("action"),
      data: data,
      parts: Math.max(1, parseInt(button ? button.dataset.parts : "1", 10) || 1),
      next: 1,
      controller: null
    };
    requestPart(run);
  });

  document.addEventListener("click", function (event) {
    if (event.target.id === "cancel") {
      if (run && run.controller) run.controller.abort();
    } else if (event.target.id === "retry") {
      // Continue from the part that failed or was cancelled; earlier parts are not redone.
      if (run && !run.controller) requestPart(run);
    }
  });

  // Copy buttons (one-time links on the admin and team pages):
  // <button data-copy-target="field-id"> copies that field's value.
  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("[data-copy-target]") : null;
    if (!button) return;
    var field = byId(button.dataset.copyTarget);
    if (!field) return;
    field.select();
    function manual() { button.textContent = "Press Ctrl+C to copy"; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(field.value).then(function () {
        button.textContent = "Copied";
      }, manual);
    } else {
      manual();
    }
  });

  // A page restored from the back/forward cache must not show a stale state.
  window.addEventListener("pageshow", function () {
    var progress = byId("progress");
    if (!run && progress) {
      progress.hidden = true;
      lockInputs(false);
    }
  });
})();
