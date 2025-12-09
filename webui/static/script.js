let autoScroll = true;
let userScrolled = false;

function setServiceLinks() {
  const base = `${window.location.protocol}//${window.location.hostname}`;
  document.getElementById(
    "open-comfy"
  ).href = `${base}:8188/?__theme=dark&__8080redirect=true`;
  document.getElementById("open-jupyter").href = `${base}:8888`;
}

function renderCustomNodes(nodes) {
  const container = document.getElementById("custom-nodes");
  container.innerHTML = "";
  const list = document.createElement("ul");
  list.className = "node-list";
  if (!nodes || nodes.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No custom nodes detected yet.";
    list.appendChild(li);
  } else {
    nodes.forEach((node) => {
      const li = document.createElement("li");
      li.textContent = node;
      list.appendChild(li);
    });
  }
  container.appendChild(list);
  document.getElementById("custom-node-count").textContent = nodes.length;
}

function renderModels(models) {
  const container = document.getElementById("model-list");
  container.innerHTML = "";
  let total = 0;

  if (!models || Object.keys(models).length === 0) {
    const p = document.createElement("p");
    p.textContent = "No models found.";
    container.appendChild(p);
    document.getElementById("model-count").textContent = 0;
    return;
  }

  Object.entries(models).forEach(([category, files]) => {
    if (!files || files.length === 0) return;
    total += files.length;
    const name = document.createElement("div");
    name.className = "category-name";
    name.textContent = `${category} (${files.length})`;
    container.appendChild(name);

    const list = document.createElement("ul");
    list.className = "model-list";
    files.forEach((file) => {
      const li = document.createElement("li");
      li.textContent = file;
      list.appendChild(li);
    });
    container.appendChild(list);
  });

  document.getElementById("model-count").textContent = total;
}

async function refreshStatus() {
  try {
    const resp = await fetch("/api/status", { cache: "no-cache" });
    if (!resp.ok) throw new Error("Failed to load status");
    const data = await resp.json();
    renderCustomNodes(data.custom_nodes || []);
    renderModels(data.models || {});
  } catch (err) {
    console.error(err);
  }
}

function updateLogBox(logs) {
  const logBox = document.getElementById("log-box");
  const wasAtBottom =
    Math.abs(logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight) < 1 ||
    (autoScroll && !userScrolled);

  logBox.innerHTML = "";
  const lines = (logs || "").split("\n");
  lines.forEach((line) => {
    if (!line.trim()) return;
    const div = document.createElement("div");
    div.className = "log-line";
    div.textContent = line;
    logBox.appendChild(div);
  });

  if (wasAtBottom) {
    logBox.scrollTop = logBox.scrollHeight;
  }
}

async function refreshLogs() {
  try {
    const resp = await fetch("/logs", { cache: "no-cache" });
    if (!resp.ok) return;
    const data = await resp.json();
    updateLogBox(data.logs || "");
  } catch (err) {
    console.error("Failed to fetch logs", err);
  }
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  userScrolled = false;
  const logBox = document.getElementById("log-box");
  if (autoScroll) {
    logBox.scrollTop = logBox.scrollHeight;
  }
  localStorage.setItem("autoScroll", autoScroll ? "true" : "false");
}

function attachLogScrollListener() {
  const logBox = document.getElementById("log-box");
  logBox.addEventListener("scroll", function () {
    const atBottom =
      Math.abs(logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight) < 1;
    if (autoScroll && !atBottom) {
      userScrolled = true;
    }
    if (atBottom) {
      userScrolled = false;
    }
  });
}

function switchTab(tabName) {
  document.querySelectorAll(".downloader").forEach((downloader) => {
    downloader.classList.remove("active");
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.remove("active");
  });
  document.getElementById(`${tabName}-tab`).classList.add("active");
  document.getElementById(`${tabName}-downloader`).classList.add("active");
}

function setStatus(statusId, message, type) {
  const el = document.getElementById(statusId);
  el.textContent = message;
  el.style.display = "block";
  el.className = "status-message" + (type ? ` status-${type}` : "");
}

async function pollTask(taskId, buttonId, statusId) {
  const button = document.getElementById(buttonId);
  const interval = setInterval(async () => {
    try {
      const resp = await fetch(`/download/status?id=${encodeURIComponent(taskId)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.status === "success") {
        setStatus(statusId, "Download completed", "success");
        button.disabled = false;
        clearInterval(interval);
        refreshStatus();
      } else if (data.status === "failed") {
        setStatus(statusId, `Download failed: ${data.detail}`, "error");
        button.disabled = false;
        clearInterval(interval);
      }
    } catch (err) {
      console.error(err);
    }
  }, 2000);
}

async function startDownload(endpoint, payload, buttonId, statusId) {
  const button = document.getElementById(buttonId);
  button.disabled = true;
  setStatus(statusId, "Starting download...", "");

  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || "Request failed");
    }

    if (data.task_id) {
      setStatus(statusId, "Downloading...", "");
      pollTask(data.task_id, buttonId, statusId);
    } else {
      setStatus(statusId, "Unexpected response", "error");
      button.disabled = false;
    }
  } catch (err) {
    setStatus(statusId, err.message || "Error", "error");
    button.disabled = false;
  }
}

function downloadFromCivitai() {
  startDownload(
    "/download/civitai",
    {
      url: document.getElementById("modelUrl").value,
      api_key: document.getElementById("apiKey").value,
      model_type: document.getElementById("modelType").value,
    },
    "civitaibutton",
    "downloadStatus"
  );
}

function downloadFromHuggingFace() {
  startDownload(
    "/download/huggingface",
    {
      url: document.getElementById("hfUrl").value,
      model_type: document.getElementById("hfModelType").value,
    },
    "huggingfacebutton",
    "hfDownloadStatus"
  );
}

function downloadFromGoogleDrive() {
  let url = document.getElementById("gdUrl").value.trim();
  if (url && !url.startsWith("http")) {
    url = `https://drive.google.com/uc?export=download&id=${url}`;
  }

  startDownload(
    "/download/googledrive",
    {
      url: url,
      model_type: document.getElementById("gdModelType").value,
      filename: document.getElementById("gdFilename").value,
    },
    "gdrivebutton",
    "gdDownloadStatus"
  );
}

document.addEventListener("DOMContentLoaded", () => {
  setServiceLinks();
  const saved = localStorage.getItem("autoScroll");
  if (saved !== null) {
    autoScroll = saved === "true";
    document.getElementById("auto-scroll-toggle").checked = autoScroll;
  }

  attachLogScrollListener();
  refreshStatus();
  refreshLogs();

  setInterval(refreshStatus, 15000);
  setInterval(refreshLogs, 4000);

  switchTab("civitai");
});
