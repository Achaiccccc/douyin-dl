(function () {
  "use strict";

  var views = {
    login: document.getElementById("view-login"),
    main: document.getElementById("view-main"),
    result: document.getElementById("view-result"),
  };

  var els = {
    password: document.getElementById("password"),
    btnLogin: document.getElementById("btn-login"),
    loginError: document.getElementById("login-error"),
    shareText: document.getElementById("share-text"),
    btnParse: document.getElementById("btn-parse"),
    parseError: document.getElementById("parse-error"),
    resultSummary: document.getElementById("result-summary"),
    resultList: document.getElementById("result-list"),
    btnAgain: document.getElementById("btn-again"),
  };

  var currentResults = [];
  var parseTotal = 0;
  var parsing = false;

  function show(name) {
    Object.keys(views).forEach(function (key) {
      views[key].classList.toggle("hidden", key !== name);
    });
  }

  function showError(el, message) {
    el.textContent = message;
    el.classList.remove("hidden");
  }

  function hideError(el) {
    el.classList.add("hidden");
  }

  function setLoading(btn, loading, text) {
    if (loading) {
      btn.dataset.label = btn.textContent;
      btn.textContent = text || "处理中…";
      btn.disabled = true;
    } else {
      btn.textContent = btn.dataset.label || btn.textContent;
      btn.disabled = false;
    }
  }

  function request(path, options) {
    return fetch(path, options).then(function (resp) {
      if (resp.status === 401) {
        show("login");
        throw new Error("登录已过期，请重新输入密码");
      }
      return resp
        .json()
        .catch(function () {
          return null;
        })
        .then(function (data) {
          if (!resp.ok) {
            var detail = data && data.detail;
            if (Array.isArray(detail)) {
              detail = detail
                .map(function (d) {
                  return d.msg;
                })
                .join("；");
            }
            throw new Error(detail || "请求失败（" + resp.status + "）");
          }
          return data;
        });
    });
  }

  function postJson(path, payload) {
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function doLogin() {
    var password = els.password.value.trim();
    if (!password) {
      showError(els.loginError, "请输入密码");
      return;
    }
    hideError(els.loginError);
    setLoading(els.btnLogin, true, "验证中…");
    postJson("/api/login", { password: password })
      .then(function () {
        els.password.value = "";
        show("main");
        els.shareText.focus();
      })
      .catch(function (err) {
        showError(els.loginError, err.message);
      })
      .finally(function () {
        setLoading(els.btnLogin, false);
      });
  }

  function initPlaceholders(total) {
    parseTotal = total;
    currentResults = [];
    els.resultList.innerHTML = "";
    for (var i = 0; i < total; i++) {
      currentResults.push({ pending: true, url: "" });
      els.resultList.appendChild(buildItemCard(currentResults[i], i));
    }
    renderSummary();
  }

  function replaceCard(index) {
    var old = els.resultList.children[index];
    var neu = buildItemCard(currentResults[index], index);
    if (old) {
      els.resultList.replaceChild(neu, old);
    } else {
      els.resultList.appendChild(neu);
    }
    renderSummary();
  }

  function finishParse() {
    parsing = false;
    els.btnAgain.disabled = false;
    setLoading(els.btnParse, false);
    renderSummary();
  }

  function handleStreamEvent(evt) {
    if (evt.event === "start") {
      initPlaceholders(evt.total || 0);
      return;
    }
    if (evt.event === "item") {
      var index = evt.index;
      if (typeof index !== "number" || index < 0) return;
      currentResults[index] = evt;
      replaceCard(index);
      return;
    }
    if (evt.event === "done") {
      finishParse();
    }
  }

  function consumeNdjson(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          if (buf.trim()) {
            try {
              handleStreamEvent(JSON.parse(buf));
            } catch (e) {}
          }
          finishParse();
          return;
        }
        buf += decoder.decode(chunk.value, { stream: true });
        var lines = buf.split("\n");
        buf = lines.pop();
        lines.forEach(function (line) {
          line = line.trim();
          if (!line) return;
          try {
            handleStreamEvent(JSON.parse(line));
          } catch (e) {}
        });
        return pump();
      });
    }
    return pump();
  }

  function doParse() {
    var text = els.shareText.value.trim();
    if (!text) {
      showError(els.parseError, "请先粘贴分享文案或链接");
      return;
    }
    hideError(els.parseError);
    setLoading(els.btnParse, true, "解析中…");
    parsing = true;
    parseTotal = 0;
    currentResults = [];
    els.resultList.innerHTML = "";
    els.resultSummary.textContent = "正在识别链接…";
    els.btnAgain.disabled = true;
    show("result");

    fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    })
      .then(function (resp) {
        if (resp.status === 401) {
          show("login");
          throw new Error("登录已过期，请重新输入密码");
        }
        if (!resp.ok) {
          return resp.json().then(function (data) {
            var detail = data && data.detail;
            if (Array.isArray(detail)) {
              detail = detail
                .map(function (d) {
                  return d.msg;
                })
                .join("；");
            }
            throw new Error(detail || "请求失败（" + resp.status + "）");
          });
        }
        return consumeNdjson(resp);
      })
      .catch(function (err) {
        finishParse();
        show("main");
        showError(els.parseError, err.message);
      });
  }

  function renderSummary() {
    var total = parseTotal || currentResults.length;
    var pending = currentResults.filter(function (r) {
      return r && r.pending;
    }).length;
    var ok = currentResults.filter(function (r) {
      return r && r.ok;
    }).length;
    var failed = currentResults.filter(function (r) {
      return r && !r.pending && !r.ok;
    }).length;
    var done = ok + failed;
    if (parsing || pending) {
      els.resultSummary.textContent =
        "解析中 " + done + "/" + total + "，成功 " + ok + " 条" + (failed ? "，失败 " + failed + " 条" : "");
    } else {
      els.resultSummary.textContent =
        "共 " + total + " 条，成功 " + ok + " 条" + (failed ? "，失败 " + failed + " 条" : "");
    }
  }

  function buildItemCard(item, index) {
    var card = document.createElement("div");
    var pending = !!(item && item.pending);
    card.className =
      "card item" + (pending ? " item-pending" : item.ok ? "" : " item-failed");

    var indexEl = document.createElement("span");
    indexEl.className = "item-index";
    indexEl.textContent = index + 1 + "/" + (parseTotal || currentResults.length || 1);
    card.appendChild(indexEl);

    if (pending) {
      var wait = document.createElement("p");
      wait.className = "item-meta";
      wait.textContent = "解析中…";
      card.appendChild(wait);
      return card;
    }

    if (item.ok) {
      var isImage = item.type === "image";
      if (isImage) {
        card.classList.add("item-images");
      } else {
        card.classList.add("item-video");
        var cover = document.createElement("img");
        cover.className = "item-cover";
        cover.src = item.cover_url;
        cover.alt = "封面";
        cover.loading = "lazy";
        card.appendChild(cover);
      }

      var body = document.createElement("div");
      body.className = "item-body";

      var title = document.createElement("p");
      title.className = "item-title";
      title.textContent = item.title;
      body.appendChild(title);

      var meta = document.createElement("p");
      meta.className = "item-meta";
      if (isImage) {
        meta.textContent =
          (item.author || "未知作者") +
          (item.date ? " · " + item.date : "") +
          " · 图文 " +
          (item.image_count || 0) +
          " 张";
      } else {
        meta.textContent = (item.author || "未知作者") + (item.date ? " · " + item.date : "");
      }
      body.appendChild(meta);

      if (isImage && item.images && item.images.length) {
        var grid = document.createElement("div");
        grid.className = "img-grid";
        item.images.forEach(function (imgInfo, n) {
          var cell = document.createElement("div");
          cell.className = "img-cell";
          var photo = document.createElement("img");
          photo.src = imgInfo.url + (imgInfo.url.indexOf("?") >= 0 ? "&" : "?") + "preview=1";
          photo.alt = "图 " + (n + 1);
          photo.loading = "lazy";
          var dl = document.createElement("a");
          dl.className = "btn ghost small";
          dl.href = imgInfo.url;
          dl.setAttribute("download", "");
          dl.textContent = "下载 " + (n + 1) + "/" + item.images.length;
          cell.appendChild(photo);
          cell.appendChild(dl);
          grid.appendChild(cell);
        });
        body.appendChild(grid);

        var downloadAll = document.createElement("button");
        downloadAll.className = "btn primary btn-download-all";
        downloadAll.type = "button";
        downloadAll.textContent = "下载全部";
        downloadAll.addEventListener("click", function () {
          downloadAllImages(item.images, downloadAll);
        });
        body.appendChild(downloadAll);
      } else {
        var download = document.createElement("a");
        download.className = "btn primary small";
        download.href = item.download_url;
        download.textContent = "下载";
        body.appendChild(download);
      }

      card.appendChild(body);
    } else {
      var failBody = document.createElement("div");
      failBody.className = "item-body";

      var urlEl = document.createElement("p");
      urlEl.className = "item-meta";
      urlEl.textContent = item.url;
      failBody.appendChild(urlEl);

      var errEl = document.createElement("p");
      errEl.className = "error";
      errEl.textContent = item.error || "解析失败";
      failBody.appendChild(errEl);

      var retry = document.createElement("button");
      retry.className = "btn ghost small";
      retry.type = "button";
      retry.textContent = "重试此条";
      retry.addEventListener("click", function () {
        doRetry(index, retry);
      });
      failBody.appendChild(retry);

      card.appendChild(failBody);
    }
    return card;
  }

  function filenameFromDisposition(header, fallback) {
    if (!header) return fallback;
    var star = /filename\*=UTF-8''([^;]+)/i.exec(header);
    if (star) {
      try {
        return decodeURIComponent(star[1].trim());
      } catch (e) {}
    }
    var quoted = /filename="([^"]+)"/i.exec(header);
    if (quoted) return quoted[1];
    var plain = /filename=([^;]+)/i.exec(header);
    if (plain) return plain[1].trim().replace(/^["']|["']$/g, "");
    return fallback;
  }

  function downloadBlob(url, fallbackName) {
    return fetch(url, { credentials: "same-origin" }).then(function (resp) {
      if (!resp.ok) throw new Error("下载失败");
      var name = filenameFromDisposition(
        resp.headers.get("Content-Disposition"),
        fallbackName
      );
      return resp.blob().then(function (blob) {
        var objectUrl = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = objectUrl;
        a.download = name;
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () {
          URL.revokeObjectURL(objectUrl);
        }, 4000);
      });
    });
  }

  function downloadAllImages(images, btn) {
    if (!images || !images.length || btn.disabled) return;
    var i = 0;
    var failed = 0;
    btn.disabled = true;
    function next() {
      if (i >= images.length) {
        btn.disabled = false;
        btn.textContent = failed
          ? "下载全部（失败 " + failed + " 张）"
          : "下载全部";
        return;
      }
      btn.textContent = "下载中 " + (i + 1) + "/" + images.length;
      var fallback = "image-" + String(i + 1).padStart(2, "0");
      downloadBlob(images[i].url, fallback)
        .catch(function () {
          failed += 1;
        })
        .then(function () {
          i += 1;
          setTimeout(next, 500);
        });
    }
    next();
  }

  function doRetry(index, btn) {
    var item = currentResults[index];
    if (!item) return;
    setLoading(btn, true, "重试中…");
    postJson("/api/parse_one", { url: item.url })
      .then(function (newItem) {
        currentResults[index] = newItem;
        replaceCard(index);
      })
      .catch(function (err) {
        currentResults[index] = { ok: false, url: item.url, error: err.message };
        replaceCard(index);
      });
  }

  els.btnLogin.addEventListener("click", doLogin);
  els.password.addEventListener("keydown", function (e) {
    if (e.key === "Enter") doLogin();
  });
  els.btnParse.addEventListener("click", doParse);
  els.btnAgain.addEventListener("click", function () {
    if (parsing) return;
    els.shareText.value = "";
    currentResults = [];
    parseTotal = 0;
    hideError(els.parseError);
    show("main");
    els.shareText.focus();
  });

  request("/api/me")
    .then(function () {
      show("main");
    })
    .catch(function () {
      show("login");
    });
})();
