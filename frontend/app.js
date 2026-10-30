/* Клиент чата: обёртка над /api backend-сервиса.
   Код, который выполняет модель, сюда не попадает: сервис отдаёт только текст,
   таблицы и картинки. */
(function () {
  "use strict";

  var API = (function () {
    // Адрес API можно переопределить: ?api=http://127.0.0.1:8000/api
    // (нужно, если интерфейс открыт без nginx-прокси).
    var match = /[?&]api=([^&]+)/.exec(window.location.search);
    return match ? decodeURIComponent(match[1]) : "/api";
  })();
  var state = {
    sessionId: null,
    config: null,
    files: [],
    attached: [],
    busy: false,
    uploading: false
  };

  function qs(id) {
    return document.getElementById(id);
  }

  var el = {
    messages: qs("messages"),
    welcome: qs("welcome"),
    fileList: qs("file-list"),
    fileInput: qs("file-input"),
    dropzone: qs("dropzone"),
    attachBtn: qs("attach-btn"),
    attachments: qs("attachments"),
    composer: qs("composer"),
    composerHint: qs("composer-hint"),
    input: qs("input"),
    sendBtn: qs("send-btn"),
    newSession: qs("new-session"),
    clearChat: qs("clear-chat"),
    status: qs("status-line"),
    modelBadge: qs("model-badge"),
    chatSub: qs("chat-sub"),
    lightbox: qs("lightbox"),
    lightboxImg: qs("lightbox-img"),
    viewer: qs("viewer"),
    viewerTitle: qs("viewer-title"),
    viewerBody: qs("viewer-body"),
    viewerDownload: qs("viewer-download"),
    viewerClose: qs("viewer-close")
  };

  /* ---------- утилиты ---------- */

  function api(path, options) {
    return fetch(API + path, options).then(function (response) {
      if (!response.ok) {
        return response.text().then(function (body) {
          var detail = body;
          try {
            detail = JSON.parse(body).detail || body;
          } catch (error) {
            /* тело не JSON — оставляем как есть */
          }
          throw new Error(detail || ("HTTP " + response.status));
        });
      }
      if (response.status === 204) {
        return null;
      }
      return response.json();
    });
  }

  function escapeHtml(text) {
    return window.HermesMarkdown.escapeHtml(text);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("ru-RU").format(value || 0);
  }

  function formatSize(bytes) {
    if (bytes < 1024) {
      return bytes + " Б";
    }
    if (bytes < 1024 * 1024) {
      return (bytes / 1024).toFixed(1) + " КБ";
    }
    return (bytes / 1024 / 1024).toFixed(1) + " МБ";
  }

  function setStatus(text, isError) {
    el.status.textContent = text;
    el.status.style.color = isError ? "#d14343" : "";
  }

  function syncComposerState() {
    var locked = state.busy || state.uploading;
    el.sendBtn.disabled = locked || !el.input.value.trim();
    el.attachBtn.disabled = locked;
    el.fileInput.disabled = locked;
    el.composer.classList.toggle("is-uploading", state.uploading);
    el.composer.classList.toggle("is-busy", state.busy);
    el.composer.setAttribute("aria-busy", locked ? "true" : "false");
    if (el.composerHint) {
      if (state.uploading) {
        el.composerHint.textContent = "Загружаю файлы…";
      } else if (state.busy) {
        el.composerHint.textContent = "Hermes выполняет задачу…";
      } else if (state.attached.length) {
        el.composerHint.textContent = "Прикреплено файлов: " + state.attached.length + ". Они попадут в следующий запрос.";
      } else {
        el.composerHint.textContent = "Введите задачу или добавьте файл";
      }
    }
  }

  function fileExtension(name) {
    var match = /(?:^|\.)([^.]+)$/.exec(String(name || ""));
    var value = match ? match[1].toUpperCase() : "FILE";
    return value.length > 4 ? value.slice(0, 4) : value;
  }

  function scrollToBottom() {
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  function resetMessages(html) {
    el.messages.innerHTML = "";
    var welcome = document.createElement("div");
    welcome.className = "welcome";
    welcome.innerHTML = html;
    el.messages.appendChild(welcome);
    el.welcome = welcome;
  }

  function hideWelcome() {
    if (el.welcome && el.welcome.parentNode) {
      el.welcome.parentNode.removeChild(el.welcome);
      el.welcome = null;
    }
  }

  /* ---------- сессия ---------- */

  function setSession(sessionState) {
    state.sessionId = sessionState.session_id;
    state.files = sessionState.files || [];
    state.attached = state.files.map(function (file) { return file.id; });
    renderFiles();
    renderAttachments();
  }

  function newSession() {
    if (state.busy) {
      return Promise.resolve();
    }
    setStatus("Создаю задачу…");
    var previousSession = state.sessionId;
    var cleanup = previousSession
      ? api("/sessions/" + previousSession, { method: "DELETE" }).catch(function () {})
      : Promise.resolve();
    return cleanup.then(function () {
      return api("/sessions", { method: "POST" });
    }).then(function (sessionState) {
      setSession(sessionState);
      resetMessages("<h1>Новая задача создана</h1>" +
        "<p>Опишите задачу и прикрепите нужные файлы.</p>");
      setStatus("Готово к работе.");
    }).catch(function (error) {
      setStatus("Не удалось создать задачу: " + error.message, true);
    });
  }

  /* ---------- файлы ---------- */

  function renderFiles() {
    el.fileList.innerHTML = "";
    if (!state.files.length) {
      el.fileList.innerHTML = '<p class="muted file-empty">Здесь появятся загруженные файлы</p>';
      return;
    }
    state.files.forEach(function (file) {
      var attached = state.attached.indexOf(file.id) !== -1;
      var card = document.createElement("div");
      card.className = "file-card" + (attached ? " is-attached" : "");
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      card.setAttribute("aria-pressed", attached ? "true" : "false");
      var details = file.columns && file.columns.length
        ? formatNumber(file.rows) + " строк · " + file.columns.length + " колонок"
        : "Файл для контекста";
      card.innerHTML =
        '<span class="file-badge">' + escapeHtml(fileExtension(file.name)) + '</span>' +
        '<div class="file-card-main">' +
          '<div class="file-name-row"><div class="file-name">' + escapeHtml(file.name) + '</div>' +
            '<span class="file-check" aria-hidden="true">✓</span></div>' +
          '<div class="file-meta">' + details + ' · ' + formatSize(file.size) +
            (file.encoding ? ' · ' + escapeHtml(file.encoding) : "") + '</div>' +
          '<button class="file-remove" type="button">Удалить файл</button>' +
        '</div>';
      card.querySelector(".file-remove").addEventListener("click", function (event) {
        event.stopPropagation();
        removeFile(file.id);
      });
      card.addEventListener("click", function (event) {
        if (!event.target.closest("button")) {
          toggleAttach(file.id);
        }
      });
      card.addEventListener("keydown", function (event) {
        if ((event.key === "Enter" || event.key === " ") && !event.target.closest("button")) {
          event.preventDefault();
          toggleAttach(file.id);
        }
      });
      el.fileList.appendChild(card);
    });
  }

  function toggleAttach(fileId) {
    var index = state.attached.indexOf(fileId);
    if (index === -1) {
      state.attached.push(fileId);
    } else {
      state.attached.splice(index, 1);
    }
    renderFiles();
    renderAttachments();
  }

  function renderAttachments() {
    el.attachments.innerHTML = "";
    state.attached.forEach(function (fileId) {
      var file = state.files.filter(function (item) { return item.id === fileId; })[0];
      if (!file) {
        return;
      }
      var chip = document.createElement("div");
      chip.className = "chip";
      chip.innerHTML =
        '<span class="chip-icon" aria-hidden="true">' + escapeHtml(fileExtension(file.name)) + '</span>' +
        '<span class="chip-copy"><span class="chip-name">' + escapeHtml(file.name) + '</span>' +
        '<span class="chip-meta">' + formatSize(file.size) +
          (file.columns && file.columns.length ? ' · ' + formatNumber(file.rows) + ' строк' : '') +
        '</span></span>' +
        '<button class="chip-remove" type="button" title="Убрать из запроса" aria-label="Убрать ' +
          escapeHtml(file.name) + ' из запроса">×</button>';
      chip.querySelector(".chip-remove").addEventListener("click", function () {
        toggleAttach(fileId);
      });
      el.attachments.appendChild(chip);
    });
  }

  function uploadFiles(fileList) {
    if (!state.sessionId || !fileList || !fileList.length) {
      return Promise.resolve();
    }
    if (state.busy || state.uploading) {
      setStatus("Дождитесь завершения текущей операции перед загрузкой файлов.", true);
      return Promise.resolve();
    }
    var queue = Array.prototype.slice.call(fileList);
    var total = queue.length;
    var uploaded = [];
    var failed = [];
    state.uploading = true;
    syncComposerState();
    el.dropzone.classList.remove("dragover");
    setStatus("Подготовка загрузки: " + total + " файл(ов)…");

    function next() {
      var file = queue.shift();
      if (!file) {
        if (uploaded.length) {
          state.files = state.files.concat(uploaded);
          uploaded.forEach(function (info) { state.attached.push(info.id); });
          renderFiles();
          renderAttachments();
        }
        state.uploading = false;
        syncComposerState();
        if (failed.length) {
          var message = "Загружено " + uploaded.length + " из " + total + " файлов. Не удалось: " + failed.join("; ");
          setStatus(message, true);
        } else if (uploaded.length) {
          setStatus("Загружено файлов: " + uploaded.length + ". Они прикреплены к следующему запросу.");
        }
        return Promise.resolve();
      }
      setStatus("Загружаю " + file.name + "… (" + (uploaded.length + failed.length + 1) + "/" + total + ")");
      var body = new FormData();
      body.append("file", file);
      return api("/sessions/" + state.sessionId + "/files", { method: "POST", body: body })
        .then(function (info) {
          uploaded.push(info);
          return next();
        })
        .catch(function (error) {
          failed.push(file.name + ": " + error.message);
          return next();
        });
    }

    return next().catch(function (error) {
      state.uploading = false;
      syncComposerState();
      throw error;
    });
  }

  function removeFile(fileId) {
    return api("/sessions/" + state.sessionId + "/files/" + fileId, { method: "DELETE" })
      .then(function () {
        state.files = state.files.filter(function (file) { return file.id !== fileId; });
        state.attached = state.attached.filter(function (id) { return id !== fileId; });
        renderFiles();
        renderAttachments();
        setStatus("Файл удалён.");
      })
      .catch(function (error) {
        setStatus("Не удалось удалить файл: " + error.message, true);
      });
  }

  /* ---------- отрисовка сообщений и артефактов ---------- */

  function messageNode(role, html) {
    hideWelcome();
    var node = document.createElement("div");
    node.className = "msg " + role;
    var avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "Вы" : "H";
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = html;
    node.appendChild(avatar);
    node.appendChild(bubble);
    el.messages.appendChild(node);
    scrollToBottom();
    return { node: node, bubble: bubble };
  }

  function tableHtml(table) {
    var columns = table.columns || [];
    var rows = table.rows || [];
    var html = ["<table><thead><tr>"];
    columns.forEach(function (column) {
      html.push("<th>" + escapeHtml(String(column)) + "</th>");
    });
    html.push("</tr></thead><tbody>");
    rows.forEach(function (row) {
      html.push("<tr>");
      columns.forEach(function (column, columnIndex) {
        var value = row[columnIndex];
        html.push("<td>" + escapeHtml(value === undefined || value === null ? "" : String(value)) + "</td>");
      });
      html.push("</tr>");
    });
    html.push("</tbody></table>");
    return html.join("");
  }

  /* ---------- длинный текстовый результат: панель рядом с чатом ---------- */

  var TEXT_PREVIEW_LIMIT = 1200;

  function isTextArtifact(artifact) {
    if (artifact.kind === "image" || artifact.kind === "table") {
      return false;
    }
    var mime = artifact.mime || "";
    return mime.indexOf("text/") === 0 || mime === "application/json";
  }

  function openViewer(url, title, mime) {
    el.viewer.hidden = false;
    el.viewerTitle.textContent = title || "Результат";
    el.viewerDownload.href = url;
    el.viewerBody.innerHTML = '<p class="muted">Открываю файл…</p>';
    fetch(url)
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.text();
      })
      .then(function (text) {
        var asMarkdown = mime === "text/markdown" || /\.md$/i.test(title || "");
        el.viewerBody.innerHTML = asMarkdown
          ? window.HermesMarkdown.render(text)
          : "<pre>" + escapeHtml(text) + "</pre>";
      })
      .catch(function (error) {
        el.viewerBody.innerHTML = '<div class="error-box">Не удалось открыть файл: ' +
          escapeHtml(error.message) + "</div>";
      });
  }

  function closeViewer() {
    el.viewer.hidden = true;
    el.viewerBody.innerHTML = "";
  }

  function artifactUrl(url) {
    // Артефакт приходит как /api/artifacts/... — если интерфейс открыт без nginx
    // (параметр ?api=…), подставляем префикс API, иначе запрос уйдёт не на тот сервер.
    if (API !== "/api" && String(url).indexOf("/api/") === 0) {
      return API.replace(/\/api$/, "") + url;
    }
    return url;
  }

  function artifactsHtml(artifacts) {
    if (!artifacts || !artifacts.length) {
      return "";
    }
    var blocks = [];
    artifacts.forEach(function (artifact) {
      if (artifact.kind === "image") {
        blocks.push('<div class="artifact-image"><div class="artifact-title">График: ' +
          escapeHtml(artifact.name) + '</div><img src="' + artifactUrl(artifact.url) +
          '" alt="' + escapeHtml(artifact.name) + '" data-zoom="1"></div>');
      } else if (artifact.kind === "table" && artifact.table) {
        blocks.push('<div class="artifact-title">Таблица: ' + escapeHtml(artifact.name) +
          '</div><div class="table-wrap">' + tableHtml(artifact.table) + "</div>" +
          '<div class="artifact-actions"><a href="' + artifactUrl(artifact.url) + '" download>Скачать файл</a></div>');
      } else if (isTextArtifact(artifact) && artifact.text) {
        var preview = artifact.text.length > TEXT_PREVIEW_LIMIT
          ? artifact.text.slice(0, TEXT_PREVIEW_LIMIT) + "\n…"
          : artifact.text;
        blocks.push('<div class="artifact-text"><div class="artifact-title">Текст: ' +
          escapeHtml(artifact.name) + " · " + formatSize(artifact.size) + "</div>" +
          '<pre class="artifact-pre">' + escapeHtml(preview) + "</pre>" +
          '<div class="artifact-actions">' +
          '<button type="button" class="artifact-open" data-viewer="' + artifactUrl(artifact.url) +
          '" data-viewer-title="' + escapeHtml(artifact.name) +
          '" data-viewer-mime="' + escapeHtml(artifact.mime || "") +
          '">Открыть рядом →</button>' +
          '<a href="' + artifactUrl(artifact.url) + '" download>Скачать</a></div></div>');
      } else {
        blocks.push('<div class="artifact-file"><a href="' + artifactUrl(artifact.url) +
          '" download>' + escapeHtml(artifact.name) + '</a> <span class="muted">' +
          formatSize(artifact.size) + "</span></div>");
      }
    });
    return '<div class="artifacts">' + blocks.join("") + "</div>";
  }

  function renderMessage(message) {
    var isUser = message.role === "user";
    var html = isUser
      ? "<p>" + escapeHtml(message.text).replace(/\n/g, "<br>") + "</p>"
      : window.HermesMarkdown.render(message.text);
    if (!isUser) {
      html += artifactsHtml(message.artifacts);
    }
    var created = messageNode(isUser ? "user" : "assistant", html);
    if (!isUser) {
      var meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = "время работы Hermes: " +
        ((message.duration_ms || 0) / 1000).toFixed(1) + " с";
      created.bubble.appendChild(meta);
    }
    return created.node;
  }

  function showStage(text) {
    removeStage();
    var node = document.createElement("div");
    node.className = "msg assistant";
    node.id = "stage-node";
    node.innerHTML = '<div class="avatar">H</div><div class="bubble"><div class="stage">' +
      '<span class="spinner"></span><span>' + escapeHtml(text) + "</span></div></div>";
    el.messages.appendChild(node);
    scrollToBottom();
  }

  function removeStage() {
    var node = qs("stage-node");
    if (node && node.parentNode) {
      node.parentNode.removeChild(node);
    }
  }

  function showError(text) {
    removeStage();
    messageNode("assistant", '<div class="error-box">' + escapeHtml(text) + "</div>");
  }

  /* ---------- диалог: SSE-поток ---------- */

  function handleFrame(frame) {
    var eventName = "message";
    var dataLines = [];
    frame.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) {
        eventName = line.slice(6).trim();
      } else if (line.indexOf("data:") === 0) {
        dataLines.push(line.slice(5).trim());
      }
    });
    if (!dataLines.length) {
      return;
    }
    var payload;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch (error) {
      return;
    }
    handleEvent(eventName, payload);
  }

  function handleEvent(name, payload) {
    if (name === "accepted") {
      renderMessage(payload.message);
      showStage("Hermes анализирует данные…");
    } else if (name === "stage") {
      showStage(payload.message || "Обрабатываю данные…");
    } else if (name === "result") {
      removeStage();
      renderMessage(payload.message);
    } else if (name === "error") {
      showError(payload.message || "Не удалось обработать запрос.");
    } else if (name === "done") {
      setStatus("Готово за " + ((payload.duration_ms || 0) / 1000).toFixed(1) + " с.");
    }
  }

  function readStream(reader) {
    var decoder = new TextDecoder("utf-8");
    var buffer = "";

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          if (buffer.trim()) {
            handleFrame(buffer);
          }
          return null;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        var frames = buffer.split("\n\n");
        buffer = frames.pop();
        frames.forEach(handleFrame);
        return pump();
      });
    }

    return pump();
  }

  function sendMessage() {
    var text = el.input.value.trim();
    if (!text || state.busy || !state.sessionId) {
      return;
    }
    state.busy = true;
    syncComposerState();
    el.newSession.disabled = true;
    el.input.value = "";
    autoResize();
    showStage("Отправляю запрос…");

    fetch(API + "/sessions/" + state.sessionId + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, file_ids: state.attached.slice() })
    })
      .then(function (response) {
        if (!response.ok || !response.body) {
          return response.text().then(function (body) {
            var detail = body;
            try {
              detail = JSON.parse(body).detail || body;
            } catch (error) {
              /* тело не JSON */
            }
            throw new Error(detail || ("HTTP " + response.status));
          });
        }
        return readStream(response.body.getReader());
      })
      .catch(function (error) {
        showError(error.message);
      })
      .then(function () {
        state.busy = false;
        el.newSession.disabled = false;
        syncComposerState();
        removeStage();
        el.input.focus();
      });
  }

  /* ---------- инициализация ---------- */

  function refreshHealth() {
    return api("/health")
      .then(function (health) {
        var hermes = health.hermes || {};
        var text = "Hermes: " + (hermes.ok ? "доступен" : "недоступен");
        if (hermes.model) {
          text += " · " + hermes.model;
        }
        setStatus(text, !hermes.ok);
      })
      .catch(function (error) {
        setStatus("Сервис недоступен: " + error.message, true);
      });
  }

  function autoResize() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 160) + "px";
  }

  function wire() {
    el.composer.addEventListener("submit", function (event) {
      event.preventDefault();
      sendMessage();
    });
    el.input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    el.input.addEventListener("input", function () {
      autoResize();
      syncComposerState();
    });
    el.attachBtn.addEventListener("click", function () {
      el.fileInput.click();
    });
    el.fileInput.addEventListener("change", function () {
      uploadFiles(el.fileInput.files);
      el.fileInput.value = "";
    });
    el.dropzone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        el.fileInput.click();
      }
    });
    function hasFiles(event) {
      var types = event.dataTransfer && event.dataTransfer.types;
      return !!types && Array.prototype.indexOf.call(types, "Files") !== -1;
    }
    ["dragenter", "dragover"].forEach(function (name) {
      el.composer.addEventListener(name, function (event) {
        if (hasFiles(event)) {
          event.preventDefault();
          if (!state.busy && !state.uploading) {
            el.composer.classList.add("file-drop-active");
          }
        }
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      el.composer.addEventListener(name, function (event) {
        if (hasFiles(event)) {
          event.preventDefault();
          el.composer.classList.remove("file-drop-active");
        }
      });
    });
    el.composer.addEventListener("drop", function (event) {
      if (event.dataTransfer && event.dataTransfer.files.length) {
        uploadFiles(event.dataTransfer.files);
      }
    });
    el.newSession.addEventListener("click", newSession);
    el.clearChat.addEventListener("click", function () {
      resetMessages("<h1>Экран очищен</h1>" +
        "<p>История диалога сохранена в сессии — задайте новый вопрос.</p>");
    });
    ["dragenter", "dragover"].forEach(function (name) {
      el.dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        el.dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      el.dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        el.dropzone.classList.remove("dragover");
      });
    });
    el.dropzone.addEventListener("drop", function (event) {
      if (event.dataTransfer && event.dataTransfer.files.length) {
        uploadFiles(event.dataTransfer.files);
      }
    });
    el.messages.addEventListener("click", function (event) {
      if (event.target.dataset && event.target.dataset.zoom) {
        el.lightboxImg.src = event.target.src;
        el.lightbox.hidden = false;
      }
    });
    el.messages.addEventListener("click", function (event) {
      var node = event.target.closest ? event.target.closest("[data-viewer]") : null;
      if (node) {
        openViewer(node.dataset.viewer, node.dataset.viewerTitle, node.dataset.viewerMime);
      }
    });
    el.viewerClose.addEventListener("click", closeViewer);
    el.viewer.addEventListener("click", function (event) {
      if (event.target === el.viewer) {
        closeViewer();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && el.viewer && !el.viewer.hidden) {
        closeViewer();
      }
    });
    el.lightbox.addEventListener("click", function () {
      el.lightbox.hidden = true;
      el.lightboxImg.src = "";
    });
    syncComposerState();
  }

  function boot() {
    wire();
    api("/config")
      .then(function (config) {
        state.config = config;
        el.modelBadge.textContent = "UI v10.0.0 · модель: " + config.hermes_model;
        el.chatSub.textContent = "Hermes Agent · штатные инструменты и навыки";
        return newSession();
      })
      .catch(function (error) {
        setStatus("Сервис недоступен: " + error.message, true);
      });
    refreshHealth();
    el.input.focus();
  }

  window.HermesApp = window.HermesApp || {};
  window.HermesApp.state = state;
  window.HermesApp.boot = boot;

  boot();
})();

