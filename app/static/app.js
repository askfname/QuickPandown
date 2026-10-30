/* 快客盘 QuickPandown — 前端逻辑（原生 ES2020，无构建） */
"use strict";

/* ---------- 全局状态 ---------- */
const store = {
  shareId: null,
  passcode: "",
  title: "",
  rawInput: "", // 最近一次解析的原文（补提取码时要用）
  // 登录身份（由 /api/auth/me 填充）；夸克 Cookie 仍只存本浏览器 localStorage
  me: null,
  cookie: localStorage.getItem("qp_cookie") || "",
  // fid -> {fid, name, dir, size, loaded, children, expanded, checked}
  roots: [],
  filters: { type: "", minSizeMB: null },
  tasks: {}, // task_id -> 最新任务对象
  autoDone: new Set(), // 已自动触发过下载的任务（防 SSE 重复触发）
  taskBusy: new Set(), // "taskId:action" 在途守卫（取消/重试防连点）
  itemsRefresh: new Set(), // 明细为空占位任务的去重补拉（防 item 事件风暴）
  speedPrev: {}, // task_id -> {ts, done} 任务级差分基准
  speed: {}, // task_id -> {speed, eta}
};

function saveLocalCookie(cookie) {
  store.cookie = cookie || "";
  if (store.cookie) localStorage.setItem("qp_cookie", store.cookie);
  else localStorage.removeItem("qp_cookie");
}

const TYPE_EXT = {
  video: ["mp4", "mkv", "avi", "mov", "flv", "wmv", "ts", "webm"],
  audio: ["mp3", "flac", "wav", "aac", "m4a", "ogg"],
  image: ["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic"],
  doc: ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub", "md"],
  archive: ["zip", "rar", "7z", "tar", "gz"],
};
const STATUS_TEXT = {
  queued: "排队",
  transferring: "转存中",
  downloading: "下载中",
  cleaning: "清理中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};
const RUNNING_STATUSES = new Set(["queued", "transferring", "downloading", "cleaning"]);

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */
// 文件名等来自云端的数据一律用 textContent 渲染，天然防 XSS

function fmtSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  for (const u of units) {
    if (v < 1024 || u === "TB") {
      return `${u !== "B" && v >= 10 ? Math.round(v) : v.toFixed(1)} ${u}`;
    }
    v /= 1024;
  }
}

function fmtEta(seconds) {
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`;
  return `${Math.ceil(seconds / 3600)} 小时`;
}

function extOf(name) {
  const i = String(name || "").lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

function typeOf(node) {
  if (node.dir) return "dir";
  const ext = extOf(node.name);
  for (const [type, exts] of Object.entries(TYPE_EXT)) {
    if (exts.includes(ext)) return type;
  }
  return "other";
}

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

async function api(path, opts) {
  const o = opts || {};
  const headers = Object.assign({}, o.headers);
  // 登录态由会话 Cookie 自动携带；夸克 Cookie 仅在 auth 请求时放请求头
  if (o.auth && store.cookie) headers["X-Quark-Cookie"] = store.cookie;
  const resp = await fetch(path, Object.assign({}, o, { headers }));
  // 服务器轮换了用户 Cookie → 更新本地
  const rotated = resp.headers.get("X-Quark-Set-Cookie");
  if (rotated) saveLocalCookie(rotated);
  if (resp.status === 401) {
    location.href = "/login.html"; // 会话过期 → 回登录页（无扩展名路径会 404）
    throw new Error("请先登录");
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body && body.detail) detail = String(body.detail);
    } catch (_) { /* 非 JSON 错误体 */
    }
    throw new Error(detail);
  }
  return resp.json();
}

async function postJSON(path, payload, opts) {
  return api(path, Object.assign({}, opts, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, (opts && opts.headers) || {}),
    body: JSON.stringify(payload),
  }));
}

/* 带用户 Cookie 的请求选项：parse/list 走用户账号语义，避免游客链路触发限权提示 */
const AUTH = { auth: true };

/* ---------- 解析 ---------- */
async function parse() {
  const raw = $("url-input").value.trim();
  const msg = $("parse-message");
  if (!raw) {
    msg.textContent = "请先粘贴分享链接";
    return;
  }
  store.rawInput = raw;
  $("parse-btn").disabled = true;
  msg.textContent = "解析中…";
  try {
    const body = await postJSON("/api/parse", { url: raw }, AUTH);
    handleParseResult(body);
  } catch (err) {
    msg.textContent = `解析失败：${err.message}`;
  } finally {
    $("parse-btn").disabled = false;
  }
}

function handleParseResult(body) {
  const msg = $("parse-message");
  if (body.need_passcode) {
    $("passcode-row").classList.remove("hidden");
    $("passcode-input").focus();
    msg.textContent = body.message || "该分享需要提取码";
    return;
  }
  $("passcode-row").classList.add("hidden");
  msg.textContent = "";
  store.shareId = body.share_id;
  // 链接本身带提取码时后端不回显，这里从原文补齐，否则后续下载会报需要提取码
  store.passcode = (store.rawInput.match(/[?&](?:pwd|passcode)=([A-Za-z0-9]{1,16})/) || [])[1] || "";
  store.title = body.title || "";
  store.roots = body.files.map(makeNode);
  $("share-title").textContent = store.title ? `📦 ${store.title}` : "";
  renderTree();
  updateSelectedBar();
}

async function parseWithPasscode() {
  // 在途守卫：click + Enter 双入口都会进这里，重复触发先挡掉
  if ($("parse-btn").disabled) return;
  // 后端只从链接自身的 ?pwd=/?passcode= 取提取码，这里把它拼回链接再走 /api/parse
  const passcode = $("passcode-input").value.trim();
  const msg = $("parse-message");
  if (!passcode) {
    msg.textContent = "请输入提取码";
    return;
  }
  const url = store.rawInput.replace(
    /(pan\.quark\.cn\/s\/[a-f0-9]{8,})(\?\S*)?/,
    `$1?pwd=${encodeURIComponent(passcode)}`
  );
  // 必须回写：handleParseResult 按原文回填提取码，不回写会把已输入的提取码清空，
  // 之后 /api/list 与 /api/download 全带空提取码再报"需要提取码"
  store.rawInput = url;
  $("parse-btn").disabled = true;
  $("passcode-ok").disabled = true;
  msg.textContent = "解析中…";
  try {
    const body = await postJSON("/api/parse", { url }, AUTH);
    if (body.need_passcode) {
      msg.textContent = body.message || "提取码不正确";
      return;
    }
    store.passcode = passcode;
    handleParseResult(body);
  } catch (err) {
    msg.textContent = `解析失败：${err.message}`;
  } finally {
    $("parse-btn").disabled = false;
    $("passcode-ok").disabled = false;
  }
}

/* ---------- 文件树 ---------- */
function makeNode(f) {
  return {
    fid: f.fid,
    name: f.name,
    dir: !!f.dir,
    size: f.size || 0,
    loaded: !f.dir,
    children: [],
    expanded: false,
    checked: false,
    loading: false,
  };
}

async function toggleDir(node) {
  node.expanded = !node.expanded;
  if (node.expanded && !node.loaded) {
    node.loading = true;
    renderTree();
    try {
      const body = await postJSON("/api/list", {
        share_id: store.shareId,
        fid: node.fid,
        passcode: store.passcode,
      }, AUTH);
      if (body.need_passcode) {
        node.expanded = false;
        $("passcode-row").classList.remove("hidden");
        $("passcode-input").focus();
        toast(body.message || "需要提取码");
      } else {
        node.children = body.files.map(makeNode);
        // 叶子状态权威：目录已勾选时，新加载的子项继承勾选
        if (node.checked) node.children.forEach((c) => setChecked(c, true));
        node.loaded = true;
      }
    } catch (err) {
      node.expanded = false;
      toast(`加载目录失败：${err.message}`);
    } finally {
      node.loading = false;
    }
  }
  renderTree();
  updateSelectedBar();
}

function nodeMatchesFilter(node) {
  const { type, minSizeMB } = store.filters;
  if (!type && minSizeMB == null) return true;
  if (node.dir) return true; // 目录始终显示，方便展开
  if (type && typeOf(node) !== type) return false;
  if (minSizeMB != null && node.size < minSizeMB * 1024 * 1024) return false;
  return true;
}

function iconFor(type) {
  return { video: "🎬", audio: "🎵", image: "🖼️", doc: "📄", archive: "🗜️", other: "📎" }[type] || "📎";
}

function renderTreeNode(node, container) {
  if (!nodeMatchesFilter(node)) return;
  const row = document.createElement("div");
  row.className = `tree-row${node.dir ? " dir" : ""}`;

  const toggle = document.createElement("span");
  toggle.className = "toggle";
  toggle.textContent = node.dir ? (node.loading ? "…" : node.expanded ? "▾" : "▸") : "";
  row.appendChild(toggle);

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = node.checked;
  checkbox.addEventListener("click", (e) => e.stopPropagation());
  checkbox.addEventListener("change", () => {
    setChecked(node, checkbox.checked);
    renderTree();
    updateSelectedBar();
  });
  row.appendChild(checkbox);

  const icon = document.createElement("span");
  icon.textContent = node.dir ? "📁" : iconFor(typeOf(node));
  row.appendChild(icon);

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = node.name; // 云端数据，textContent 防注入
  name.title = node.name;
  row.appendChild(name);

  const size = document.createElement("span");
  size.className = "size";
  size.textContent = node.dir ? "" : fmtSize(node.size);
  row.appendChild(size);

  if (node.dir) {
    row.addEventListener("click", (e) => {
      if (e.target === checkbox) return;
      toggleDir(node);
    });
  }
  container.appendChild(row);

  if (node.dir && node.expanded) {
    const kids = document.createElement("div");
    kids.className = "tree-children";
    container.appendChild(kids);
    if (node.loaded && node.children.length === 0) {
      const empty = document.createElement("div");
      empty.className = "tree-loading";
      empty.textContent = "（空目录）";
      kids.appendChild(empty);
    }
    node.children.forEach((child) => renderTreeNode(child, kids));
  }
}

function setChecked(node, value) {
  node.checked = value;
  node.children.forEach((c) => setChecked(c, value));
}

function walkNodes(fn) {
  const stack = [...store.roots];
  while (stack.length) {
    const node = stack.pop();
    fn(node);
    stack.push(...node.children);
  }
}

function renderTree() {
  const tree = $("tree");
  tree.textContent = "";
  if (!store.roots.length) {
    const empty = document.createElement("div");
    empty.className = "tree-empty";
    empty.textContent = "解析后在此显示文件列表";
    tree.appendChild(empty);
    return;
  }
  store.roots.forEach((root) => renderTreeNode(root, tree));
}

/* ---------- 选择与筛选 ---------- */
function selectAll() {
  walkNodes((n) => {
    if (!n.dir && nodeMatchesFilter(n)) n.checked = true;
  });
  renderTree();
  updateSelectedBar();
}

function invertSelection() {
  walkNodes((n) => {
    if (!n.dir && nodeMatchesFilter(n)) n.checked = !n.checked;
  });
  renderTree();
  updateSelectedBar();
}

function applyFilters() {
  store.filters.type = $("type-filter").value;
  const v = $("size-filter").value.trim();
  const num = Number(v);
  store.filters.minSizeMB = v !== "" && Number.isFinite(num) ? num : null;
  renderTree();
  updateSelectedBar();
}

function updateSelectedBar() {
  // 与 collectDownloadItems 同一权威模型：整目录提交的计 1 项（目录 size 恒为 0，不展示体积），
  // 其余按勾选叶子文件的个数与体积计
  const nodes = store.roots.flatMap(selectionFor);
  $("selected-count").textContent = String(nodes.length);
  $("selected-size").textContent = fmtSize(
    nodes.reduce((sum, n) => sum + (n.dir ? 0 : n.size || 0), 0)
  );
}

/* ---------- 下载 ---------- */
// 叶子状态权威：目录仅当其全部已加载叶子后代都被勾选时才整目录提交(isDir:true)，
// 否则只提交其中勾选的叶子文件；未展开的已勾选目录按整目录提交（内容未知）
function selectionFor(node) {
  if (!node.dir || !node.loaded) return node.checked ? [node] : [];
  const childSel = node.children.flatMap(selectionFor);
  // 按叶子覆盖率判断：全部已加载叶子都被勾选（含整目录子项覆盖的）才整目录提交
  const leaves = loadedLeafCount(node);
  const covered = childSel.reduce((s, n) => s + (n.dir ? loadedLeafCount(n) : 1), 0);
  if (leaves > 0 && covered === leaves) return [node];
  return childSel;
}

function loadedLeafCount(node) {
  let n = 0;
  const walk = (x) => {
    if (!x.dir) n += 1;
    else x.children.forEach(walk);
  };
  node.children.forEach(walk);
  return n;
}

function loadedLeafSize(node) {
  let total = 0;
  const walk = (x) => {
    if (!x.dir) total += x.size || 0;
    else x.children.forEach(walk);
  };
  node.children.forEach(walk);
  return total;
}

function collectDownloadItems() {
  return store.roots.flatMap(selectionFor).map((n) => ({ fid: n.fid, isDir: n.dir }));
}

// 勾选项大小表：文件按自身 size，目录按已加载叶子累计（服务器转存前预检用）
function collectSizeMaps(items) {
  const byFid = new Map();
  const walkAll = (n) => {
    byFid.set(n.fid, n);
    (n.children || []).forEach(walkAll);
  };
  store.roots.forEach(walkAll);
  const sizes = {};
  const dirSizes = {};
  for (const it of items) {
    const node = byFid.get(it.fid);
    if (!node) continue;
    if (it.isDir) dirSizes[it.fid] = loadedLeafSize(node);
    else sizes[it.fid] = node.size || 0;
  }
  return { sizes, dirSizes };
}

async function submitDownload() {
  if (!store.shareId) {
    toast("请先解析分享链接");
    return;
  }
  const items = collectDownloadItems();
  if (!items.length) {
    toast("请先勾选要下载的文件");
    return;
  }
  if ($("download-btn").disabled) return; // 在途守卫：双击/连点不重复建任务
  $("download-btn").disabled = true;
  try {
    const { sizes, dirSizes } = collectSizeMaps(items);
    const body = await postJSON(
      "/api/download",
      {
        share_id: store.shareId,
        items,
        passcode: store.passcode || "",
        title: store.title || "",
        sizes,
        dir_sizes: dirSizes,
      },
      { auth: true }
    );
    if (body.ok) {
      toast("已加入下载队列");
      refreshTasks();
    } else if (body.need_cookie) {
      toast(body.message || "需要先设置 Cookie");
      openModal("cookie-modal");
    } else {
      toast(body.message || "提交失败");
    }
  } catch (err) {
    toast(`提交失败：${err.message}`);
  } finally {
    $("download-btn").disabled = false;
  }
}

/* ---------- 任务列表与 SSE ---------- */
async function refreshTasks() {
  try {
    const body = await api("/api/tasks");
    body.tasks.forEach((t) => {
      store.tasks[t.id] = t;
    });
    renderTasks();
  } catch (err) {
    console.error("刷新任务失败", err);
  }
}

function taskTotals(t) {
  let done = 0;
  let total = 0;
  (t.items || []).forEach((i) => {
    done += i.done_bytes || 0;
    total += i.size || 0;
  });
  return { done, total };
}

// 明细缺失时的全量补拉：同任务去重，防止 item 事件连发造成刷新风暴
function syncTaskDetails(taskId) {
  store.itemsRefresh.add(taskId);
  refreshTasks().finally(() => store.itemsRefresh.delete(taskId));
}

function handleEvent(ev) {
  if (ev.type === "task") {
    autoDownloadFinishedTask(ev); // 终态 done 即自动下载（内部带去重）
    const t = store.tasks[ev.task_id];
    if (t) {
      t.status = ev.status;
      if (ev.message != null) t.message = ev.message;
    }
    if (!RUNNING_STATUSES.has(ev.status)) {
      delete store.speed[ev.task_id];
      delete store.speedPrev[ev.task_id];
      refreshTasks(); // 终态后拉明细
      renderTasks();
      return;
    }
    if (!t) {
      refreshTasks(); // 见到的新任务先拉全量
    } else if (ev.status !== "queued" && !(t.items || []).length) {
      // 占位任务：明细已在后台补齐但本端不知道 → 拉全量（进度条渲染依赖明细）
      syncTaskDetails(ev.task_id);
    }
    renderTasks();
  } else if (ev.type === "item") {
    const t = store.tasks[ev.task_id];
    if (!t) {
      refreshTasks();
      return;
    }
    const item = (t.items || []).find((i) => i.id === ev.item_id);
    if (item) {
      item.done_bytes = ev.done_bytes;
      item.size = ev.total_bytes;
    } else if (!store.itemsRefresh.has(ev.task_id)) {
      // 明细尚未同步到本地（占位任务后台补明细）：拉一次全量，不能静默丢弃
      syncTaskDetails(ev.task_id);
      return;
    }
    diffSpeed(t);
    renderTasks();
  }
}

async function autoDownloadFinishedTask(ev) {
  // 任务完成后浏览器直接自动下载：拉一次任务明细，全部已完成条目用
  // 隐形 <a download> 逐个触发；每任务只自动一次（Set 去重），刷新/
  // 重启页面不重弹历史下载。明细拉取失败时用户仍可手点下载链接
  if (ev.type !== "task" || ev.status !== "done") return;
  if (store.autoDone.has(ev.task_id)) return;
  store.autoDone.add(ev.task_id);
  try {
    const body = await api("/api/tasks");
    const t = (body.tasks || []).find((x) => x.id === ev.task_id);
    if (!t) return;
    // 多文件任务：一个打包链接搞定（替代逐文件 400ms 触发，防浏览器拦截）
    if (t.zip_url) {
      const a = document.createElement("a");
      a.href = `${t.zip_url}?session=${encodeURIComponent(store.me.username)}`;
      a.download = `${t.title || "download"}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      return;
    }
    const doneItems = (t.items || []).filter((i) => i.status === "done" && i.file_url);
    doneItems.forEach((item, i) => {
      // 间隔 400ms 陆续下载：连续多文件可避免被浏览器一次性拦截
      setTimeout(() => {
        const a = document.createElement("a");
        a.href = `${item.file_url}?session=${encodeURIComponent(store.me.username)}`;
        a.download = item.name || "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      }, i * 400);
    });
  } catch (_) { /* 静默：用户仍可手动下载 */ }
}

function diffSpeed(t) {
  // 任务级差分：全部 item 的 done_bytes 求和后与上次采样比较，指数平滑
  const { done, total } = taskTotals(t);
  const now = performance.now();
  const prev = store.speedPrev[t.id];
  let speed = store.speed[t.id] ? store.speed[t.id].speed : 0;
  if (prev && now > prev.ts) {
    const rate = (done - prev.done) / ((now - prev.ts) / 1000);
    if (rate >= 0) speed = speed ? speed * 0.7 + rate * 0.3 : rate;
  }
  store.speedPrev[t.id] = { ts: now, done };
  const eta = speed > 0 && total > done ? (total - done) / speed : null;
  store.speed[t.id] = { speed, eta };
}

function renderTasks() {
  const wrap = $("tasks");
  wrap.textContent = "";
  const list = Object.values(store.tasks).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "tasks-empty";
    empty.textContent = "暂无任务";
    wrap.appendChild(empty);
    return;
  }
  list.forEach((t) => wrap.appendChild(renderTaskCard(t)));
}

function renderTaskCard(t) {
  const card = document.createElement("div");
  card.className = "task-card";

  const head = document.createElement("div");
  head.className = "task-head";
  const title = document.createElement("span");
  title.className = "task-title";
  title.textContent = t.title || "未命名分享";
  head.appendChild(title);
  const status = document.createElement("span");
  status.className = `task-status st-${t.status}`;
  status.textContent = STATUS_TEXT[t.status] || t.status;
  head.appendChild(status);
  card.appendChild(head);

  if (t.message && ["done", "failed", "cancelled", "queued"].includes(t.status)) {
    // queued 也显示：占位任务「正在解析目录…」要立即可见（下载已响应、解析后台进行）
    const msg = document.createElement("div");
    msg.className = "task-message";
    msg.textContent = t.message;
    card.appendChild(msg);
  }

  const items = t.items || [];
  if (items.length) {
    const { done, total } = taskTotals(t);
    const pct = total > 0 ? Math.min(100, (done / total) * 100) : t.status === "done" ? 100 : 0;
    const bar = document.createElement("div");
    bar.className = "task-progress";
    const fill = document.createElement("div");
    fill.style.width = `${pct}%`;
    bar.appendChild(fill);
    card.appendChild(bar);

    const stats = document.createElement("div");
    stats.className = "task-stats";
    const parts = [`已完成 ${Math.round(pct)}% · ${fmtSize(done)} / ${fmtSize(total)}`];
    const spd = store.speed[t.id];
    if (RUNNING_STATUSES.has(t.status) && spd) {
      if (spd.speed > 0) parts.push(fmtSize(spd.speed) + "/s");
      if (spd.eta != null && Number.isFinite(spd.eta)) parts.push(`剩余 ${fmtEta(spd.eta)}`);
    }
    const failedCount = items.filter((i) => i.status === "failed").length;
    if (failedCount) parts.push(`失败 ${failedCount} 项`);
    stats.textContent = parts.join(" · ");
    card.appendChild(stats);
  }

  const actions = document.createElement("div");
  actions.className = "task-actions";
  if (RUNNING_STATUSES.has(t.status)) {
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "取消";
    cancelBtn.addEventListener("click", () => taskAction(t.id, "cancel"));
    actions.appendChild(cancelBtn);
  } else if (t.status === "failed") {
    const retryBtn = document.createElement("button");
    retryBtn.textContent = "重试";
    retryBtn.addEventListener("click", () => taskAction(t.id, "retry"));
    actions.appendChild(retryBtn);
  }
  // 已完成项：浏览器直接从服务器拉取文件（token 校验绑定会话，须带 ?session=）
  const doneWithFile = (t.items || []).filter((i) => i.status === "done" && i.file_url);
  if (t.status === "done") {
    if (t.zip_url) {
      // 多文件任务：服务端发打包 token，一次拿整个 zip（不再逐文件弹下载）
      const a = document.createElement("a");
      a.className = "task-download-link";
      a.href = `${t.zip_url}?session=${encodeURIComponent(store.me.username)}`;
      a.download = `${t.title || "download"}.zip`;
      a.textContent = "⬇ 打包下载 ZIP";
      actions.appendChild(a);
    }
    doneWithFile.forEach((item) => {
      const a = document.createElement("a");
      a.className = "task-download-link";
      a.href = `${item.file_url}?session=${encodeURIComponent(store.me.username)}`;
      a.download = item.name || "";
      a.textContent = `⬇ ${item.name || "保存到本地"}`;
      actions.appendChild(a);
    });
  }
  if (actions.children.length) card.appendChild(actions);
  return card;
}

async function taskAction(taskId, action) {
  // 在途守卫：连点不重复发请求（曾因第二击踩在已切状态的任务上误报「任务不存在」）
  const key = taskId + ":" + action;
  if (store.taskBusy.has(key)) return;
  store.taskBusy.add(key);
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" });
    toast(action === "cancel" ? "已请求取消" : "已请求重试");
  } catch (err) {
    // 404=任务真不存在；400=状态不允许（如服务重启后的历史任务），按提示处理
    toast(`操作失败：${err.message}`);
  } finally {
    store.taskBusy.delete(key);
  }
  refreshTasks();
}

/* ---------- Cookie ---------- */
const COOKIE_BADGE = {
  none: ["st-none", "● Cookie 未设置"],
  ok: ["st-ok", "● Cookie 正常"],
  invalid: ["st-invalid", "● Cookie 无效"],
};

async function refreshCookieStatus() {
  const badge = $("cookie-badge");
  if (!store.cookie) {
    const [cls, text] = COOKIE_BADGE.none;
    badge.className = `badge ${cls}`;
    badge.textContent = text;
    openModal("cookie-modal"); // 首次使用引导
    return;
  }
  try {
    const body = await postJSON("/api/cookie/check", {}, { auth: true });
    if (body.ok) {
      badge.className = "badge st-ok";
      badge.textContent = COOKIE_BADGE.ok[1];
    } else {
      badge.className = "badge st-invalid";
      badge.textContent = COOKIE_BADGE.invalid[1];
      openModal("cookie-modal");
      toast(body.message || "Cookie 已失效，请重新粘贴");
    }
  } catch (err) {
    badge.className = "badge st-none";
    badge.textContent = "● Cookie 状态未知";
  }
}

async function saveCookie() {
  const cookie = $("cookie-input").value.trim();
  const msg = $("cookie-message");
  if (!cookie) {
    msg.textContent = "请先粘贴 Cookie";
    return;
  }
  $("cookie-save").disabled = true;
  msg.textContent = "校验中…";
  try {
    saveLocalCookie(cookie); // 先本地保存，校验用它
    const body = await postJSON("/api/cookie/check", {}, { auth: true });
    if (body.ok) {
      $("cookie-input").value = "";
      msg.textContent = "";
      closeModal("cookie-modal");
      toast("Cookie 已保存到本浏览器");
      $("cookie-badge").className = "badge st-ok";
      $("cookie-badge").textContent = COOKIE_BADGE.ok[1];
    } else {
      saveLocalCookie(""); // 无效则不保留
      msg.textContent = body.message || "Cookie 无效，请重新复制";
    }
  } catch (err) {
    msg.textContent = `校验失败：${err.message}`;
  } finally {
    $("cookie-save").disabled = false;
  }
}

/* ---------- 设置 ---------- */
async function openSettings() {
  try {
    const body = await api("/api/settings");
    $("setting-download-dir").value = body.download_dir || "";
    $("setting-concurrency").value = String(body.concurrency || 2);
    $("setting-retention").value = String(body.retention_minutes ?? 0);
    $("setting-max-download").value = String(body.max_download_mb ?? 0);
    $("settings-message").textContent = "";
    openModal("settings-modal");
  } catch (err) {
    toast(`读取设置失败：${err.message}`);
  }
}

async function saveSettings() {
  const msg = $("settings-message");
  const concurrency = Number($("setting-concurrency").value);
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
    msg.textContent = "并发数需为 1–10 的整数";
    return;
  }
  const maxDownload = Number($("setting-max-download").value);
  if (!Number.isInteger(maxDownload) || maxDownload < 0) {
    msg.textContent = "下载上限需为不小于 0 的整数（0 = 不限制）";
    return;
  }
  $("settings-save").disabled = true;
  try {
    await postJSON("/api/settings", {
      download_dir: $("setting-download-dir").value.trim() || "downloads",
      concurrency,
      retention_minutes: Number($("setting-retention").value) || 0,
      max_download_mb: maxDownload,
    });
    msg.textContent = "";
    closeModal("settings-modal");
    toast("设置已保存");
  } catch (err) {
    msg.textContent = `保存失败：${err.message}`;
  } finally {
    $("settings-save").disabled = false;
  }
}

/* ---------- 弹层 ---------- */
function openModal(id) {
  $(id).classList.remove("hidden");
}

function closeModal(id) {
  $(id).classList.add("hidden");
}

function initModals() {
  document.querySelectorAll(".modal").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeModal(modal.id);
    });
    const close = modal.querySelector(".modal-close");
    if (close) close.addEventListener("click", () => closeModal(modal.id));
  });
}

/* ---------- 管理员面板（仅 admin 渲染） ---------- */

async function loadAdminPanel() {
  // 普通用户绝不显示管理入口：非 admin 直接移除面板与顶部按钮
  if (!store.me || store.me.role !== "admin") {
    const panel = $("admin-panel");
    if (panel) panel.remove();
    const invBtn = $("invites-btn");
    if (invBtn) invBtn.remove();
    const settingsBtn = $("settings-btn");
    if (settingsBtn) settingsBtn.remove();
    return;
  }
  $("admin-panel").classList.remove("hidden");
  const invBtn = $("invites-btn");
  if (invBtn) invBtn.classList.remove("hidden");
  const settingsBtn = $("settings-btn");
  if (settingsBtn) settingsBtn.classList.remove("hidden");
  await refreshOnlineStats();
  // 在线统计 30 秒轮询
  setInterval(refreshOnlineStats, 30000);
}

async function refreshInvites() {
  try {
    const body = await api("/api/admin/invites");
    const wrap = $("invite-list");
    wrap.textContent = "";
    (body.invites || []).forEach((inv) => {
      const row = document.createElement("div");
      row.className = "invite-row";
      const code = document.createElement("code");
      code.textContent = inv.code;
      row.appendChild(code);
      const state = document.createElement("span");
      state.className = inv.used_by ? "invite-used" : "invite-free";
      state.textContent = inv.used_by ? `已使用（用户 ${inv.used_by}）` : "未使用";
      row.appendChild(state);
      const del = document.createElement("button");
      del.textContent = "删除";
      if (inv.used_by) del.disabled = true; // 已使用的不可删除
      del.addEventListener("click", () => deleteInvites([inv.code]));
      row.appendChild(del);
      wrap.appendChild(row);
    });
    if (!(body.invites || []).length) {
      wrap.textContent = "暂无邀请码";
    }
  } catch (err) {
    toast(`邀请码加载失败：${err.message}`);
  }
}

async function addInvites() {
  const raw = $("invite-input").value.trim();
  if (!raw) {
    toast("请输入邀请码（每行一个）");
    return;
  }
  const codes = raw.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const body = await api("/api/admin/invites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes }),
    });
    toast(`已添加 ${body.added} 个，重复跳过 ${body.duplicated} 个`);
    $("invite-input").value = "";
    refreshInvites();
  } catch (err) {
    toast(`添加失败：${err.message}`);
  }
}

async function deleteInvites(codes) {
  try {
    const body = await api("/api/admin/invites", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes }),
    });
    toast(`已删除 ${body.removed} 个（已用跳过 ${body.skipped_used} 个）`);
    refreshInvites();
  } catch (err) {
    toast(`删除失败：${err.message}`);
  }
}

async function refreshOnlineStats() {
  try {
    const body = await api("/api/admin/stats");
    $("stat-online").textContent = String(body.online_users ?? "-");
    $("stat-downloading").textContent = String(body.downloading_users ?? "-");
  } catch (_) { /* 统计失败不打扰 */ }
}

async function openInvitesModal() {
  // 邀请码管理常驻弹窗入口：点击才拉取并渲染列表（主页不挂长列表）
  openModal("invites-modal");
  await refreshInvites();
}

async function logoutAndGo() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) { /* 服务端清理失败也照常跳转 */ }
  location.href = "/login.html";
}

/* ---------- 初始化 ---------- */
async function init() {
  // 登录墙：先取身份，未登录跳登录页
  try {
    store.me = await api("/api/auth/me");
  } catch (_) {
    return; // api() 已跳转 /login
  }

  // 身份展示 + 登出
  const whoami = $("whoami");
  if (whoami) whoami.textContent = `${store.me.username}${store.me.role === "admin" ? "（管理员）" : ""}`;
  const logoutBtn = $("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", logoutAndGo);

  $("parse-btn").addEventListener("click", parse);
  $("url-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) parse();
  });
  $("passcode-ok").addEventListener("click", parseWithPasscode);
  $("passcode-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") parseWithPasscode();
  });
  $("select-all-btn").addEventListener("click", selectAll);
  $("invert-btn").addEventListener("click", invertSelection);
  $("type-filter").addEventListener("change", applyFilters);
  $("size-filter").addEventListener("change", applyFilters);
  $("download-btn").addEventListener("click", submitDownload);
  $("cookie-badge").addEventListener("click", () => openModal("cookie-modal"));
  $("settings-btn").addEventListener("click", openSettings);
  $("cookie-save").addEventListener("click", saveCookie);
  $("settings-save").addEventListener("click", saveSettings);
  initModals();

  // 管理面板 + 邀请码弹窗入口（普通用户节点会被移除）
  $("invite-add").addEventListener("click", addInvites);
  $("invites-btn").addEventListener("click", openInvitesModal);
  await loadAdminPanel();

  const es = new EventSource(`/api/events?session=${encodeURIComponent(store.me.username)}`);
  es.onmessage = (e) => {
    try {
      handleEvent(JSON.parse(e.data));
    } catch (_) { /* 忽略非法帧 */ }
  };
  // 断线后 EventSource 浏览器默认自动重连，无需手动处理

  refreshCookieStatus();
  refreshTasks();
}

document.addEventListener("DOMContentLoaded", init);
