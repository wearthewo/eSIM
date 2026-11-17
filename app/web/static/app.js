/* Page behaviour. An external file, so the Content-Security-Policy needs no 'unsafe-inline'. */
(function () {
  "use strict";

  // The order being generated, or stopped part-way:
  //   { url, data: FormData, parts, rows, chunk, next, mode, folder, controller, renamed,
  //     state }
  // `mode` is one of:
  //   "single"  an order of one part, handed to the browser's download manager;
  //   "choose"  the page asks where to save the parts of an order of several parts;
  //   "folder"  each part is written into `folder` (a directory handle from the File
  //             System Access API), one after another;
  //   "manual"  the parts list: the user downloads each part with its own button.
  // `next` is the first part not yet saved (folder) or handed to the browser (single):
  // parts 1 to next - 1 are done. `controller` is set only while a part is being
  // requested or saved; there is never more than one. `renamed` lists the names that got
  // a number added because the folder already had a file of that name. `state[n]` is
  // part n's state in the parts list: "", "generating", "downloaded", "failed" or
  // "cancelled". `rows` and `chunk` (rows per part) label the parts list.
  var run = null;

  // How long a handed-off part keeps its blob URL. Chrome takes the blob when the link is
  // clicked; the pause leaves room for browsers that read it a little later. The next
  // part can be requested only after the URL is revoked, so at most one part is held here.
  var HAND_OFF_MS = 1000;

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

  // Show the progress line (single part and folder mode). While a request is running the
  // inputs are locked (the request data was captured when the download started) and
  // Cancel is offered; when stopped, `retryLabel` (if any) offers to continue from the
  // same part. `current` is the run, for the progress bar: parts done of all parts.
  function setProgress(running, message, retryLabel, current) {
    byId("progress").hidden = false;
    byId("generating").textContent = message;
    byId("cancel").hidden = !running;
    var retry = byId("retry");
    retry.hidden = !retryLabel;
    retry.textContent = retryLabel || "";
    showChooser(false);
    showPartList(false);
    lockInputs(running);
    showProgressBar(current);
    setStep(3);
  }

  // Before generating an order of several parts: offer to save the parts into a folder,
  // or to download them one by one. `note` (optional) says why the question is asked again.
  function askForFolder(current, note) {
    current.mode = "choose";
    byId("progress").hidden = false;
    byId("generating").textContent = (note ? note + " " : "") + "This order has " +
      current.parts + " parts. Choose a folder and each ZIP file is saved there as soon " +
      "as it is ready, without a Save dialog for each file.";
    byId("cancel").hidden = false;
    byId("retry").hidden = true;
    showChooser(true);
    showPartList(false);
    lockInputs(true);
    setLoading(byId("download"), false); // waiting for the user, not for the server
    showProgressBar(current);
    setStep(3);
  }

  // --- Display only: progress bar, step indicator, loading states. ---------------------

  function showChooser(visible) {
    ["choose-folder", "one-by-one"].forEach(function (id) {
      var button = byId(id);
      if (button) button.hidden = !visible;
    });
  }

  function showPartList(visible) {
    ["parts-manual", "next-part"].forEach(function (id) {
      var element = byId(id);
      if (element) element.hidden = !visible;
    });
  }

  function showProgressBar(current) {
    var bar = byId("progress-bar");
    if (!bar) return;
    bar.hidden = !current || current.mode === "choose";
    if (bar.hidden) return;
    bar.max = current.parts;
    bar.value = current.mode === "manual" ? downloadedCount(current) :
      Math.min(current.next - 1, current.parts);
    bar.setAttribute("aria-label", current.mode === "folder" ? "ZIP files saved" :
      current.mode === "manual" ? "ZIP files downloaded" : "ZIP files generated");
  }

  // Highlight step n (1 upload, 2 review, 3 download) of the step indicator.
  function setStep(n) {
    var steps = byId("steps");
    if (!steps) return;
    steps.querySelectorAll(".step").forEach(function (step) {
      var number = Number(step.dataset.step);
      step.classList.toggle("is-done", number < n);
      if (number === n) {
        step.setAttribute("aria-current", "step");
      } else {
        step.removeAttribute("aria-current");
      }
    });
  }

  function setLoading(button, loading) {
    if (!button) return;
    button.classList.toggle("is-loading", loading);
    if (loading) {
      button.setAttribute("aria-busy", "true");
    } else {
      button.removeAttribute("aria-busy");
    }
  }

  function lockInputs(locked) {
    var button = byId("download");
    if (button) button.disabled = locked;
    setLoading(button, locked);
    document.querySelectorAll("#generate-form fieldset").forEach(function (fieldset) {
      fieldset.disabled = locked;
    });
  }

  function reset() {
    var controller = run && run.controller;
    run = null;
    if (controller) controller.abort();
    byId("progress").hidden = true;
    showPartList(false);
    lockInputs(false);
  }

  function fileName(response) {
    var match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    return match ? match[1] : "esim_batch.zip";
  }

  // --- Texts of the progress line (single part and folder mode). -------------------------

  function partOf(current) { return "part " + current.next + " of " + current.parts; }

  function upTo(k) { return k === 1 ? "Part 1" : "Parts 1 to " + k; }

  // Saving into a folder (also while asked to choose it again part-way).
  function inFolder(current) { return Boolean(current.folder); }

  // The parts saved in the folder so far, as a sentence (empty before part 1).
  function doneSoFar(current) {
    var k = current.next - 1;
    if (k < 1) return "";
    return " " + upTo(k) + (k === 1 ? " is" : " are") + " saved in " + current.folder.name + ".";
  }

  function generatingText(current) {
    var k = current.next - 1;
    if (k < 1) return "Generating " + partOf(current) + "…";
    return "Saved part " + k + " of " + current.parts + " to " + current.folder.name + "." +
      " Generating " + partOf(current) + "…";
  }

  function doneText(current) {
    if (current.mode === "folder") {
      var renamed = current.renamed.length;
      return "Done: all " + current.parts + " ZIP files were saved to " + current.folder.name + "." +
        (renamed ? " " + renamed + (renamed === 1 ? " file was" : " files were") +
          " given a number, such as \"" + current.renamed[0] + "\", because the folder " +
          "already had a file of the same name." : "");
    }
    return "Done: the ZIP file was generated; your browser is saving it.";
  }

  function showCancelled(current) {
    var n = current.next;
    setProgress(false, "Cancelled before " + partOf(current) + " was " +
      (inFolder(current) ? "saved." : "generated.") +
      (n > 1 ? doneSoFar(current) :
        inFolder(current) ? " Nothing was saved." : " Nothing was handed to your browser."),
      n > 1 ? "Resume from part " + n : "Start again", current);
  }

  // Why a part failed, as a sentence.
  function whyFailed(reason, current) {
    if (reason && reason.failure && reason.message) return reason.message;
    if (reason && reason.saveFailed) {
      return "It could not be saved in " + current.folder.name +
        (reason.error && reason.error.message ? " (" + reason.error.message + ")." : ".");
    }
    if (current.mode === "folder") {
      return "The connection was lost, or the file could not be written to " +
        current.folder.name + ".";
    }
    return "The connection was lost or the server did not answer.";
  }

  // The file no longer passes validation: show the server's table, nothing to retry.
  function showValidationTable(table) {
    var results = byId("results");
    results.replaceChildren.apply(results,
      Array.prototype.map.call(table.childNodes, function (node) {
        return document.importNode(node, true);
      }));
  }

  // --- Requesting one part. ------------------------------------------------------------------

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

  // Request part n with the data captured when the download started. Resolves with the
  // response of a ZIP; rejects with { failure, message, table } when the server refused
  // the part, or { loggedOut } (after sending the page to the login page).
  function fetchPart(current, n, signal) {
    current.data.set("part", String(n));
    current.data.set("parts", String(current.parts));
    return fetch(current.url, {
      method: "POST",
      body: current.data,
      credentials: "same-origin",
      headers: { "X-CSRF-Token": String(current.data.get("csrf_token") || "") },
      signal: signal
    }).then(function (response) {
      // Logged out meanwhile (session expired): the server redirected to the login page.
      if (response.status === 401 || response.redirected) {
        window.location.assign("/login");
        throw { loggedOut: true };
      }
      if (!response.ok) {
        return readFailure(response).then(function (failure) { throw failure; });
      }
      return response;
    });
  }

  // Click a download link for the blob, then revoke its URL after HAND_OFF_MS. Resolves
  // after the revoke; rejects at once on Cancel (the URL is still revoked on time).
  // `handedOff` runs right after the click.
  function handOff(blob, name, signal, handedOff) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    handedOff();
    return new Promise(function (resolve, reject) {
      function cancelled() { reject(new DOMException("Cancelled", "AbortError")); }
      window.setTimeout(function () {
        URL.revokeObjectURL(url);
        signal.removeEventListener("abort", cancelled);
        resolve();
      }, HAND_OFF_MS);
      if (signal.aborted) {
        cancelled();
      } else {
        signal.addEventListener("abort", cancelled);
      }
    });
  }

  // --- Folder mode: write each part into a folder the user chose. -------------------------

  // A name not used yet in the folder: "name.zip", else "name (2).zip", "name (3).zip", …
  // An existing file is never overwritten.
  function freeName(folder, name) {
    var dot = name.lastIndexOf(".");
    var stem = dot > 0 ? name.slice(0, dot) : name;
    var extension = dot > 0 ? name.slice(dot) : "";
    function attempt(n) {
      if (n > 999) return Promise.reject(new Error("The folder has too many files named " + name));
      var candidate = n === 1 ? name : stem + " (" + n + ")" + extension;
      return folder.getFileHandle(candidate).then(function () {
        return attempt(n + 1);
      }, function (error) {
        if (error && error.name === "NotFoundError") return candidate;
        if (error && error.name === "TypeMismatchError") return attempt(n + 1); // a subfolder
        throw error;
      });
    }
    return attempt(1);
  }

  // Stream the response into a new file in the folder; resolves with the file's name once
  // the file is complete and closed. The part is never held in memory as a whole. On
  // failure or Cancel the file is removed again (always a new file: see freeName).
  // A failure before the file is open is reported as { saveFailed, error }.
  function writeToFolder(folder, name, response, signal) {
    var free = null;
    var opened = false;
    return freeName(folder, name).then(function (found) {
      free = found;
      return folder.getFileHandle(free, { create: true });
    }).then(function (handle) {
      return handle.createWritable();
    }).then(function (writable) {
      opened = true;
      if (!response.body) return writable.close();
      return response.body.pipeTo(writable, { signal: signal }); // aborts the write on Cancel
    }).then(function () {
      return free;
    }, function (reason) {
      if (!opened && response.body) response.body.cancel().catch(function () {});
      var removed = free === null ? Promise.resolve() :
        folder.removeEntry(free).catch(function () {});
      return removed.then(function () {
        throw opened ? reason : { saveFailed: true, error: reason };
      });
    });
  }

  // The folder may no longer be writable (permission is per page visit): ask again.
  function folderWritable(folder) {
    if (typeof folder.queryPermission !== "function") return Promise.resolve(true);
    var options = { mode: "readwrite" };
    return folder.queryPermission(options).then(function (state) {
      if (state === "granted") return true;
      return folder.requestPermission(options).then(function (answer) {
        return answer === "granted";
      });
    }).catch(function () { return false; });
  }

  // Retry or resume in folder mode: continue from the first part not saved.
  function resumeInFolder(current) {
    folderWritable(current.folder).then(function (writable) {
      if (run !== current) return;
      if (writable) {
        requestPart(current);
      } else {
        askForFolder(current, "The page may no longer save to " + current.folder.name + ".");
      }
    });
  }

  // Request part run.next and save it (folder mode) or hand it to the browser (an order
  // of one part); in folder mode, only then move on to the following part. Parts are
  // requested strictly one after another, never in parallel, and no part is kept once it
  // is saved or handed off.
  function requestPart(current) {
    var controller = new AbortController();
    var signal = controller.signal;
    var n = current.next;
    current.controller = controller;
    setProgress(true, generatingText(current), "", current);

    fetchPart(current, n, signal).then(function (response) {
      var name = fileName(response);
      if (current.mode === "folder") {
        if (run === current) {
          setProgress(true, "Saving part " + n + " of " + current.parts + " to " +
            current.folder.name + "…", "", current);
        }
        return writeToFolder(current.folder, name, response, signal).then(function (saved) {
          if (saved !== name) current.renamed.push(saved);
          current.next = n + 1;
        });
      }
      return response.blob().then(function (blob) {
        return handOff(blob, name, signal, function () { current.next = n + 1; });
      });
    }).then(function () {
      if (run !== current) return; // a new file was chosen meanwhile
      current.controller = null;
      if (current.next <= current.parts && !signal.aborted) {
        requestPart(current);
      } else if (current.next <= current.parts) {
        showCancelled(current); // Cancel arrived just as the part was saved
      } else {
        run = null;
        setProgress(false, doneText(current), "", current);
      }
    }).catch(function (reason) {
      if (run !== current) return;
      current.controller = null;
      if (reason && reason.loggedOut) {
        run = null;
        return;
      }
      if (signal.aborted && current.next > current.parts) {
        // Cancel during the pause after the last part was handed to the browser.
        run = null;
        setProgress(false, doneText(current), "", current);
      } else if (signal.aborted) {
        showCancelled(current);
      } else if (reason && reason.failure && reason.table) {
        run = null;
        showValidationTable(reason.table);
        setProgress(false, "Stopped at " + partOf(current) + ": the file did not pass validation.", "",
          current);
      } else {
        setProgress(false, "Part " + n + " of " + current.parts + " failed. " +
          whyFailed(reason, current) + doneSoFar(current), "Retry part " + n, current);
      }
    });
  }

  // "Choose a folder for the parts": must run in the click itself (user activation).
  function chooseFolder(current) {
    if (current.picking) return;
    current.picking = true;
    window.showDirectoryPicker({ id: "esim-parts", mode: "readwrite", startIn: "downloads" })
      .then(function (folder) {
        current.picking = false;
        if (run !== current) return;
        current.mode = "folder";
        current.folder = folder;
        current.renamed = [];
        requestPart(current);
      }, function () {
        // Dismissed, or write access refused. Ask again rather than falling back to one
        // Save dialog per part; "Download parts one by one" is the explicit fallback.
        current.picking = false;
        if (run === current) askForFolder(current, "No folder was chosen.");
      });
  }

  // --- Parts list: the user downloads each part with its own button. ----------------------
  // Used when the browser cannot save into a folder (Firefox, Safari), or when the user
  // chose "Download parts one by one". A part is generated only when its button is
  // clicked, one part at a time, and handed to the browser's download manager.

  var PART_STATUS = {
    "": "Not downloaded",
    generating: "Generating…",
    downloaded: "Downloaded",
    failed: "Failed",
    cancelled: "Cancelled"
  };

  // 1001 -> "1,001"
  function grouped(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }

  // "Part 3 · rows 1,001–1,500": the rows of the upload in part n.
  function partLabel(current, n) {
    var label = "Part " + n;
    if (!current.rows || !current.chunk) return label;
    var first = (n - 1) * current.chunk + 1;
    var last = Math.min(n * current.chunk, current.rows);
    return label + " · rows " + grouped(first) + "–" + grouped(last);
  }

  function downloadedCount(current) {
    var count = 0;
    for (var n = 1; n <= current.parts; n++) {
      if (current.state[n] === "downloaded") count++;
    }
    return count;
  }

  // The lowest-numbered part not downloaded yet (0 when all are).
  function nextPart(current) {
    for (var n = 1; n <= current.parts; n++) {
      if (current.state[n] !== "downloaded") return n;
    }
    return 0;
  }

  function buildPartList(current) {
    var rows = [];
    for (var n = 1; n <= current.parts; n++) {
      var row = document.createElement("tr");
      row.dataset.part = String(n);
      var label = document.createElement("td");
      label.textContent = partLabel(current, n);
      var status = document.createElement("td");
      var action = document.createElement("td");
      action.className = "actions";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm";
      button.dataset.downloadPart = String(n);
      action.appendChild(button);
      row.append(label, status, action);
      rows.push(row);
    }
    var body = byId("part-list").tBodies[0];
    body.replaceChildren.apply(body, rows);
  }

  // Show the list as it stands. While a part is generating every button is disabled.
  function renderPartList(current) {
    var busy = Boolean(current.controller);
    byId("part-list").querySelectorAll("tbody tr").forEach(function (row) {
      var n = Number(row.dataset.part);
      var state = current.state[n] || "";
      var status = row.cells[1];
      var badge = state === "downloaded" ? "badge-success" : state === "failed" ? "badge-error" : "";
      if (badge) {
        var span = document.createElement("span");
        span.className = "badge " + badge;
        span.textContent = PART_STATUS[state];
        status.replaceChildren(span);
        status.className = "";
      } else {
        status.textContent = PART_STATUS[state];
        status.className = "muted";
      }
      var button = row.querySelector("[data-download-part]");
      if (state === "downloaded") {
        button.textContent = "Download again";
        button.setAttribute("aria-label", "Download part " + n + " again");
      } else if (state === "failed") {
        button.textContent = "Try again";
        button.setAttribute("aria-label", "Try part " + n + " again");
      } else {
        button.textContent = "Download";
        button.setAttribute("aria-label", "Download part " + n);
      }
      button.disabled = busy;
      setLoading(button, state === "generating");
    });
    var next = byId("next-part");
    next.hidden = !nextPart(current);
    next.disabled = busy;
  }

  function showParts(current, message) {
    byId("progress").hidden = false;
    byId("generating").textContent = message;
    byId("cancel").hidden = !current.controller;
    byId("retry").hidden = true;
    showChooser(false);
    showPartList(true);
    lockInputs(Boolean(current.controller));
    showProgressBar(current);
    renderPartList(current);
    setStep(3);
  }

  function startPartList(current) {
    current.mode = "manual";
    current.state = {};
    buildPartList(current);
    showParts(current, "This order has " + current.parts + " parts. Download them one by " +
      "one: each part is generated when you click its button.");
    byId("next-part").focus(); // the button that was clicked may be hidden now
  }

  // Generate part n and hand it to the browser. Only one part at a time: every button is
  // disabled until this part's blob URL is revoked, and Cancel aborts the request.
  function downloadPart(current, n) {
    if (current.controller || current.mode !== "manual") return;
    var controller = new AbortController();
    var signal = controller.signal;
    var before = current.state[n] || "";
    var ofAll = " of " + current.parts;
    current.controller = controller;
    current.state[n] = "generating";
    showParts(current, "Generating part " + n + ofAll + "…");

    function handedOff() {
      current.state[n] = "downloaded";
      if (run !== current) return;
      byId("cancel").hidden = true; // nothing left to cancel, only the URL to revoke
      byId("generating").textContent = "Part " + n + ofAll + " was handed to your browser.";
    }

    function finished() {
      if (run !== current) return; // a new file was chosen meanwhile
      current.controller = null;
      var count = downloadedCount(current);
      showParts(current, count === current.parts ?
        "Done: all " + current.parts + " parts were downloaded." :
        "Part " + n + ofAll + " was downloaded. " + count + ofAll + " parts downloaded.");
    }

    fetchPart(current, n, signal).then(function (response) {
      var name = fileName(response);
      return response.blob().then(function (blob) {
        return handOff(blob, name, signal, handedOff);
      });
    }).then(finished, function (reason) {
      if (run !== current) return;
      if (current.state[n] === "downloaded") return finished(); // Cancel after the hand-off
      current.controller = null;
      if (reason && reason.loggedOut) {
        run = null;
      } else if (signal.aborted) {
        current.state[n] = before === "downloaded" ? before : "cancelled";
        showParts(current, "Cancelled: part " + n + ofAll + " was not downloaded" +
          (before === "downloaded" ? " again." : "."));
      } else if (reason && reason.failure && reason.table) {
        run = null;
        showValidationTable(reason.table);
        setProgress(false, "Stopped at part " + n + ofAll + ": the file did not pass validation.", "",
          current);
      } else {
        current.state[n] = before === "downloaded" ? before : "failed";
        showParts(current, "Part " + n + ofAll + " failed. " + whyFailed(reason, current));
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

  // "Download": capture the form once (file, branding, local time stamp); the same
  // captured data is sent for every part. One part: generate and download it. Several
  // parts: ask for a folder first when the browser can write to one, else show the
  // parts list.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.id !== "generate-form") return;
    event.preventDefault();
    if (run && (run.controller || run.mode === "choose")) return; // already running
    var button = byId("download");
    var sizes = button ? button.closest("[data-rows]") : null;
    var data = new FormData(form);
    data.set("client_stamp", localStamp(new Date()));
    run = {
      url: form.getAttribute("action"),
      data: data,
      parts: Math.max(1, parseInt(button ? button.dataset.parts : "1", 10) || 1),
      rows: sizes ? parseInt(sizes.dataset.rows, 10) || 0 : 0,
      chunk: sizes ? parseInt(sizes.dataset.chunkRows, 10) || 0 : 0,
      next: 1,
      mode: "single",
      folder: null,
      controller: null,
      renamed: [],
      state: {}
    };
    if (run.parts === 1) {
      requestPart(run);
    } else if (typeof window.showDirectoryPicker === "function") {
      askForFolder(run);
    } else {
      startPartList(run);
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("button") : null;
    if (!run || !button) return;
    var id = button.id;
    if (button.dataset.downloadPart) {
      downloadPart(run, Number(button.dataset.downloadPart));
    } else if (id === "next-part") {
      if (nextPart(run)) downloadPart(run, nextPart(run));
    } else if (id === "cancel") {
      if (run.controller) {
        run.controller.abort();
      } else if (run.mode === "choose" && run.next === 1) {
        reset(); // nothing generated yet: back to the review
      } else if (run.mode === "choose") {
        showCancelled(run); // folder access was lost part-way; Resume asks again
      }
    } else if (id === "retry") {
      // Continue from the part that failed or was cancelled; earlier parts are not redone.
      if (run.controller || run.mode === "manual") return;
      byId("retry").hidden = true;
      if (run.mode === "choose") {
        askForFolder(run);
      } else if (run.mode === "folder") {
        resumeInFolder(run);
      } else {
        requestPart(run);
      }
    } else if (id === "choose-folder" && run.mode === "choose") {
      chooseFolder(run);
    } else if (id === "one-by-one" && run.mode === "choose") {
      startPartList(run);
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

  // --- Display only below: none of these listeners prevents or changes a request. -----

  // Drop zone: highlight while a file is dragged over it. The transparent file input
  // covers the zone, so the browser itself puts a dropped file into the input.
  function dropzoneOf(event) {
    return event.target instanceof Element ? event.target.closest(".dropzone") : null;
  }
  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var zone = dropzoneOf(event);
      if (zone) zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop", "dragend"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var zone = dropzoneOf(event);
      if (!zone) return;
      if (type === "dragleave" && event.relatedTarget instanceof Node && zone.contains(event.relatedTarget)) return;
      zone.classList.remove("is-dragover");
    });
  });

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " bytes";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // Show the chosen data file's name and size in the drop zone; back to step 1.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || input.id !== "file") return;
    var file = input.files && input.files[0];
    var label = byId("file-name");
    if (label) label.textContent = file ? file.name + " (" + formatSize(file.size) + ")" : "";
    var zone = input.closest(".dropzone");
    if (zone) zone.classList.toggle("has-file", Boolean(file));
    setStep(1);
  });

  // The check result arrived: step 2 (review).
  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail && event.detail.target;
    if (target && target.id === "results" && target.childElementCount) setStep(2);
  });

  // Ordinary forms (login, admin actions, …): show a spinner on the button that was
  // pressed. The button is not disabled, so the submission is exactly as before.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.id === "generate-form") return;
    var button = event.submitter || form.querySelector("button[type=submit], button:not([type])");
    setLoading(button, true);
  });

  // Back/forward cache: clear spinners left from a submission that navigated away.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll(".is-loading").forEach(function (button) {
      if (button.id !== "download" || !run) setLoading(button, false);
    });
  });

  // A page rendered with results (e.g. after a failed download) starts at step 2.
  var initialResults = byId("results");
  if (initialResults && initialResults.childElementCount) setStep(2);

  // A page restored from the back/forward cache must not show a stale state.
  window.addEventListener("pageshow", function () {
    var progress = byId("progress");
    if (!run && progress) {
      progress.hidden = true;
      lockInputs(false);
    }
  });
})();
