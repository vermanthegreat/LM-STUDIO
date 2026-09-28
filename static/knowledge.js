// Knowledge drag-and-drop upload and search. All model/user text is rendered via textContent.
(function () {
  "use strict";

  var dropzone = document.getElementById("dropzone");
  var input = document.getElementById("file-input");
  var projectInput = document.getElementById("project");
  var table = document.getElementById("upload-table");
  var tbody = table.querySelector("tbody");
  var queue = [];
  var active = 0;
  var MAX_PARALLEL = 2;

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  }

  function addRow(file) {
    table.hidden = false;
    var tr = el("tr");
    tr.appendChild(el("td", file.name));
    var status = el("td");
    var badge = el("span", "queued", "badge");
    status.appendChild(badge);
    var progress = el("progress");
    progress.max = 100;
    progress.value = 0;
    status.appendChild(progress);
    tr.appendChild(status);
    var details = el("td", "");
    tr.appendChild(details);
    tbody.appendChild(tr);
    return { badge: badge, progress: progress, details: details };
  }

  function renderResult(row, result) {
    row.progress.remove();
    row.badge.textContent = result.status;
    row.badge.className = "badge" + (result.status === "ingested" ? "" : " warn");
    row.details.textContent = "";
    if (result.status === "ingested") {
      var link = el("a", "K" + result.item_id);
      link.href = "/knowledge/items/" + result.item_id;
      row.details.appendChild(link);
      var p = result.preview || {};
      var bits = [];
      if (p.category) bits.push(p.category);
      if (p.project) bits.push("project: " + p.project);
      if (p.topics && p.topics.length) bits.push("topics: " + p.topics.join(", "));
      if (p.entities && p.entities.length) bits.push("entities: " + p.entities.join(", "));
      bits.push("extraction: " + result.extraction_status);
      if (result.vision_status && result.vision_status !== "not_applicable") bits.push("vision: " + result.vision_status);
      bits.push("classification: " + result.classification_status);
      row.details.appendChild(el("div", bits.join(" · ")));
      if (p.summary) row.details.appendChild(el("div", p.summary));
      else if (p.text_excerpt) row.details.appendChild(el("div", p.text_excerpt.slice(0, 200), "muted"));
    } else if (result.status === "duplicate") {
      row.details.appendChild(el("span", result.message + " "));
      var dup = el("a", "Open K" + result.existing_item_id);
      dup.href = "/knowledge/items/" + result.existing_item_id;
      row.details.appendChild(dup);
    } else {
      row.details.textContent = (result.error_code ? result.error_code + ": " : "") + (result.message || "");
    }
  }

  function upload(job) {
    active += 1;
    job.row.badge.textContent = "uploading";
    var form = new FormData();
    form.append("files", job.file, job.file.name);
    form.append("project", projectInput.value || "");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/knowledge/ingest");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        job.row.progress.value = Math.round((e.loaded / e.total) * 100);
        if (e.loaded === e.total) job.row.badge.textContent = "processing";
      }
    };
    xhr.onload = function () {
      var body = null;
      try { body = JSON.parse(xhr.responseText); } catch (err) { body = null; }
      if (body && body.results && body.results.length) {
        renderResult(job.row, body.results[0]);
      } else {
        renderResult(job.row, {
          status: "failed",
          error_code: body && body.error_code ? body.error_code : "http_" + xhr.status,
          message: body && body.message ? body.message : "Upload failed.",
        });
      }
      done();
    };
    xhr.onerror = function () {
      renderResult(job.row, { status: "failed", error_code: "network_error", message: "Upload failed." });
      done();
    };
    xhr.send(form);
  }

  function done() {
    active -= 1;
    pump();
  }

  function pump() {
    while (active < MAX_PARALLEL && queue.length) upload(queue.shift());
  }

  function enqueue(fileList) {
    for (var i = 0; i < fileList.length; i++) {
      queue.push({ file: fileList[i], row: addRow(fileList[i]) });
    }
    pump();
  }

  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files) enqueue(e.dataTransfer.files);
  });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", function () {
    enqueue(input.files);
    input.value = "";
  });

  // ---------------------------------------------------------------- search
  var form = document.getElementById("search-form");
  var results = document.getElementById("search-results");

  function runSearch() {
    var params = new URLSearchParams();
    new FormData(form).forEach(function (value, key) {
      if (String(value).trim()) params.append(key, value);
    });
    results.textContent = "Searching…";
    fetch("/api/knowledge/search?" + params.toString())
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        results.textContent = "";
        if (!res.ok) {
          results.appendChild(el("p", res.body.message || "Search failed."));
          return;
        }
        var b = res.body;
        var summary = b.count + " result(s) — " + b.engine + ", " + b.match_mode;
        if (b.mode_used) summary += " · mode " + b.mode_used;
        if (b.semantic && b.semantic.status === "error") summary += " (semantic unavailable: " + b.semantic.error_code + ", lexical fallback)";
        results.appendChild(el("p", summary));
        b.hits.forEach(function (hit) {
          var div = el("div", null, "hit");
          var a = el("a", "K" + hit.id + " " + hit.original_filename);
          a.href = "/knowledge/items/" + hit.id;
          div.appendChild(a);
          var meta = ["captured " + String(hit.captured_at).slice(0, 10)];
          if (hit.event_date) meta.push("event " + hit.event_date);
          if (hit.project) meta.push(hit.project);
          if (hit.category) meta.push(hit.category);
          if (hit.ranking && hit.ranking.score !== null) meta.push("score " + hit.ranking.score);
          if (hit.retrieval && hit.retrieval.evidence) meta.push("evidence " + hit.retrieval.evidence.join("+"));
          div.appendChild(el("div", meta.join(" · "), "muted"));
          if (hit.summary) div.appendChild(el("div", hit.summary));
          if (hit.snippet) div.appendChild(el("div", hit.snippet, "snippet"));
          results.appendChild(div);
        });
      })
      .catch(function () { results.textContent = "Search failed."; });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    runSearch();
  });

  document.querySelectorAll("[data-filter]").forEach(function (link) {
    link.addEventListener("click", function (e) {
      e.preventDefault();
      form.reset();
      var field = form.querySelector('[name="' + link.getAttribute("data-filter") + '"]');
      if (field) field.value = link.getAttribute("data-value");
      runSearch();
    });
  });
})();
