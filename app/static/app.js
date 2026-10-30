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
  statsPending: new Set(), // 未展开目录子树统计请求去重（fid 集合）
  filters: { type: "", minSizeMB: null },
  tasks: {}, // task_id -> 最新任务对象
  autoDone: new Set(), // 已自动触发过下载的任务（防 SSE 重复触发）
  taskBusy: new Set(), // "taskId:action" 在途守卫（取消/重试防连点）
  itemsRefresh: new Set(), // 明细为空占位任务的去重补拉（防 item 事件风暴）
  speedPrev: {}, // task_id -> {ts, done} 任务级差分基准
  speed: {}, // task_id -> {speed, eta}
  limitMb: null, // 单次下载上限（/api/limits）；null=未知（预检跳过，服务端兜底）
};

// 管理员关闭磁盘不足横幅后，本次页面会话不再提示（刷新页面恢复）
let diskAlertDismissed = false;

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
// 提取码统一识别：链接内 ?pwd= 优先；其次「提取码: XXX / 密码：XXX」文本形态
// 后端契约只认链接内的 ?pwd=/?passcode=，文本式的必须由前端拼好再发
function passcodeFromRaw(raw) {
  const viaQuery = raw.match(/[?&](?:pwd|passcode)=([A-Za-z0-9]{1,16})/);
  if (viaQuery) return viaQuery[1];
  const viaText = raw.match(/(?:提取码|密码)\s*[:：]?\s*([A-Za-z0-9]{1,16})/);
  return viaText ? viaText[1] : "";
}

function urlWithPasscode(raw) {
  // 已有 ?pwd/?passcode 原样透传；文本式提取码拼接到第一条分享链接
  if (/[?&](?:pwd|passcode)=/.test(raw)) return raw;
  const passcode = passcodeFromRaw(raw);
  if (!passcode) return raw;
  return raw.replace(
    /(pan\.quark\.cn\/s\/[a-f0-9]{8,})(\?\S*)?/,
    `$1?pwd=${encodeURIComponent(passcode)}`
  );
}

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
    const body = await postJSON("/api/parse", { url: urlWithPasscode(raw) }, AUTH);
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
  // 提取码补齐：兼容链接 ?pwd= 与文本式「提取码: XXX」两种来源，
  // 缺它会导致后续 /api/list 与 /api/download 全部再报"需要提取码"
  store.passcode = passcodeFromRaw(store.rawInput);
  store.title = body.title || "";
  store.statsPending.clear(); // 新分享的子树统计请求与旧树无关，清空在途去重
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
    // 未展开目录的子树统计 {count, bytes}（/api/tree-size 异步回填；null=未就绪）
    stats: null,
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
  // 全选覆盖目录节点：未展开的文件夹也勾选（懒加载后新叶子经继承逻辑自动勾上），
  // 整目录分享才能「一键全选」直接可提交
  walkNodes((n) => {
    if (nodeMatchesFilter(n)) n.checked = true;
  });
  renderTree();
  updateSelectedBar();
}

function invertSelection() {
  // 反选同样作用于目录节点（已加载叶子按各自状态翻转；目录翻转经 cascade 传播）
  walkNodes((n) => {
    if (nodeMatchesFilter(n)) n.checked = !n.checked;
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
  // 与 collectDownloadItems 同一权威模型。整目录输出按准确性排序口径：
  // 1) node.stats（/api/tree-size 全子树统计，含未展开的子文件夹）= 精确值，优先；
  // 2) 本地已加载叶子 = 统计在途或不可用时的部分值兜底；
  // 3) 两者都没有 = 目录暂计 1 项并显示加载动画，后台请求见 scheduleTreeStats
  let count = 0;
  let size = 0;
  let waiting = false;
  for (const node of store.roots.flatMap(selectionFor)) {
    if (node.dir) {
      if (node.stats) {
        count += node.stats.count;
        size += node.stats.bytes;
      } else {
        const leaves = loadedLeafCount(node);
        if (leaves) {
          count += leaves;
          size += loadedLeafSize(node);
          // 已加载部分叶子但树下还有未展开的孙子目录：叶子累计只是部分值，
          // 后台补一次子树统计，回填后即切成精确口径
          if (hasUnloadedDirDescendant(node)) {
            waiting = true;
            scheduleTreeStats(node);
          }
        } else {
          count += 1; // 统计在途：目录本身暂计 1 项
          waiting = true;
          scheduleTreeStats(node);
        }
      }
    } else {
      count += 1;
      size += node.size || 0;
    }
  }
  $("selected-count").textContent = String(count);
  $("selected-size").textContent = fmtSize(size);
  // 统计在途:数字位换跳动的「…」占位,回填后即显示真实值
  if (waiting) {
    $("selected-count").innerHTML = '<span class="stats-dots">…</span>';
    $("selected-size").innerHTML = '<span class="stats-dots">…</span> B';
  }
}

// 子树下是否存在未加载的目录（用于判断已加载叶子累计是否完整）
function hasUnloadedDirDescendant(node) {
  return node.children.some((c) => !c.loaded || (c.dir && hasUnloadedDirDescendant(c)));
}

/* ---------- 未展开目录子树统计 ---------- */
// 勾选未展开目录时前后端都还不知道里面有多少文件/多大：调 /api/tree-size 让
// 后端遍历子树统计叶子数与字节合计，异步回填 node.stats 并刷新选择条
function scheduleTreeStats(node) {
  if (!node || !node.dir || node.stats || !store.shareId) return;
  if (store.statsPending.has(node.fid)) return; // 同一目录只发一次
  store.statsPending.add(node.fid);
  const release = () => store.statsPending.delete(node.fid);
  postJSON("/api/tree-size", {
    share_id: store.shareId,
    fid: node.fid,
    passcode: store.passcode,
  }, AUTH)
    .then((body) => {
      if (!body.need_passcode) {
        node.stats = { count: body.count || 0, bytes: body.bytes || 0 };
      }
    })
    .catch(() => {
      // 静默失败：选择条维持「1 项 / 0.0 B」回退口径，不阻断主流程
    })
    .finally(() => {
      release();
      updateSelectedBar(); // 回填（或清 pending）后重算，让其它未就绪目录排队
    });
}

/* ---------- 下载 ---------- */
// 叶子状态权威：目录仅当其全部已加载叶子后代都被勾选时才整目录提交(isDir:true)，
// 否则只提交其中勾选的叶子文件；未展开的已勾选目录按整目录提交（内容未知）
function selectionFor(node) {
  if (!node.dir || !node.loaded) return node.checked ? [node] : [];
  const outs = node.children.map((c) => ({ child: c, sel: selectionFor(c) }));
  // 空目录按自身勾选表态（没有可覆盖的叶子，塌缩判定不适用）
  if (outs.length === 0) return node.checked ? [node] : [];
  // 已加载目录：仅当每个直接子项的输出都是「整目录」（[child] / 文件本身）才塌缩为
  // [node]——归纳保证整棵子树全被勾选覆盖
  if (outs.every((o) => o.sel.length === 1 && o.sel[0] === o.child)) return [node];
  return outs.flatMap((o) => o.sel);
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

// 勾选项大小表：文件按自身 size，目录按已加载叶子累计；未加载目录回退
// 后端子树统计 stats.bytes（勾选未展开文件夹时预检也能有真实体积）
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
    if (it.isDir) {
      // 后端子树统计优先：含未展开子文件夹的精确体积；无统计才退回已加载叶子累计
      dirSizes[it.fid] = node.stats ? node.stats.bytes : loadedLeafSize(node);
    } else {
      sizes[it.fid] = node.size || 0;
    }
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
    // 前端预检：与选择条「已选 X 项，共 Y」同一口径（目录用后端子树统计），
    // 超限直接提示且不发请求；服务端还有权威目录统计与解析后真实树校验兜底
    if (store.limitMb > 0) {
      let totalBytes = 0;
      for (const it of items) {
        if (it.isDir) totalBytes += dirSizes[it.fid] || 0;
        else totalBytes += sizes[it.fid] || 0;
      }
      if (totalBytes > store.limitMb * 1024 * 1024) {
        toast(`所选文件共 ${fmtSize(totalBytes)}，超过单次下载上限 ${store.limitMb}MB，请减少勾选后重试`);
        return;
      }
    }
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
      if (ev.disk_low != null) t.disk_low = !!ev.disk_low;
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

// 链接文案附有效期（服务器签发时刻 + token TTL）；未知时原文返回
function expiryLabel(base, expiresAt) {
  if (!expiresAt) return base;
  // 跨天的到期（保留 24 小时/7 天）带上日期，当天只显示时刻
  const d = new Date(expiresAt * 1000);
  const hm = fmtClock(expiresAt);
  const when = d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  return `${base}（有效期至 ${when}）`;
}

function fmtClock(unixSeconds) {
  const d = new Date(unixSeconds * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 触发任务文件下载：先校验有效期，再 HEAD 探测服务端确有该文件，两者都过
// 才放行 <a download>。过期/已被清理都提示并刷新任务（重新签发 token），
// 绝不让浏览器把 4xx 错误响应体存成损坏的下载文件
async function openTaskFile(url, expiresAt, filename) {
  if (expiresAt && Date.now() >= expiresAt * 1000) {
    toast("文件已过期，请重新下载");
    refreshTasks();
    return;
  }
  let check = null;
  try {
    check = await fetch(url, { method: "HEAD" });
  } catch (_) { /* 网络异常按文件不可用处理 */ }
  if (!check || !check.ok) {
    const reason = !check || check.status === 404
      ? "文件不存在或链接已失效"
      : check.status === 410 ? "文件已过期" : "文件已被清理";
    toast(`${reason}，请重新下载`);
    refreshTasks();
    return;
  }
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function renderTaskCard(t) {
  const card = document.createElement("div");
  card.className = "task-card";
  if (t.disk_low) card.classList.add("disk-low");

  const head = document.createElement("div");
  head.className = "task-head";
  const title = document.createElement("span");
  title.className = "task-title";
  title.textContent = t.title || "未命名分享";
  head.appendChild(title);
  const status = document.createElement("span");
  status.className = `task-status st-${t.status}`;
  if (t.disk_low) status.classList.add("disk-low");
  status.textContent = STATUS_TEXT[t.status] || t.status;
  head.appendChild(status);
  card.appendChild(head);

  if (t.message && ["done", "failed", "cancelled", "queued"].includes(t.status)) {
    // queued 也显示：占位任务「正在解析目录…」要立即可见（下载已响应、解析后台进行）
    const msg = document.createElement("div");
    msg.className = "task-message";
    // 磁盘守门排队：警告符号 + 警示色突出显示，直到空间释放自动恢复
    msg.textContent = t.disk_low ? `⚠ ${t.message}` : t.message;
    if (t.disk_low) msg.classList.add("warn");
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
    // SSE 进度会频繁重建任务卡：渲染时从在途守卫恢复禁用态，
    // 否则请求期间的重复渲染会带出可点的全新按钮，连点因此重复提交
    cancelBtn.disabled = store.taskBusy.has(t.id + ":cancel");
    cancelBtn.addEventListener("click", () => taskAction(cancelBtn, t.id, "cancel"));
    actions.appendChild(cancelBtn);
  } else if (t.status === "failed") {
    const retryBtn = document.createElement("button");
    retryBtn.textContent = "重试";
    retryBtn.disabled = store.taskBusy.has(t.id + ":retry");
    retryBtn.addEventListener("click", () => taskAction(retryBtn, t.id, "retry"));
    actions.appendChild(retryBtn);
  }
  // 已完成项：浏览器直接从服务器拉取文件（token 校验绑定会话，须带 ?session=）；
  // 链接附服务器签发的有效期，过期点击直接提示而不发无效请求
  const doneWithFile = (t.items || []).filter((i) => i.status === "done" && i.file_url);
  if (t.status === "done") {
    if (t.zip_url) {
      // 多文件任务：服务端发打包 token，一次拿整个 zip（不再逐文件弹下载）
      const a = document.createElement("a");
      a.className = "task-download-link";
      a.href = `${t.zip_url}?session=${encodeURIComponent(store.me.username)}`;
      a.download = `${t.title || "download"}.zip`;
      a.textContent = expiryLabel("⬇ 打包下载 ZIP", t.expires_at);
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openTaskFile(a.href, t.expires_at, a.download);
      });
      actions.appendChild(a);
    }
    doneWithFile.forEach((item) => {
      const a = document.createElement("a");
      a.className = "task-download-link";
      a.href = `${item.file_url}?session=${encodeURIComponent(store.me.username)}`;
      a.download = item.name || "";
      a.textContent = expiryLabel(`⬇ ${item.name || "保存到本地"}`, t.expires_at);
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openTaskFile(a.href, t.expires_at, a.download);
      });
      actions.appendChild(a);
    });
  }
  if (actions.children.length) card.appendChild(actions);
  return card;
}

async function taskAction(btn, taskId, action) {
  // 在途/已点守卫：连点直接忽略；请求期间禁用按钮（服务端也会对非法重试
  // 返回 400），操作完成恢复并由 refreshTasks 重渲染出实际状态
  const key = taskId + ":" + action;
  if (store.taskBusy.has(key) || btn.disabled) return;
  btn.disabled = true;
  store.taskBusy.add(key);
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" });
    toast(action === "cancel" ? "已请求取消" : "已请求重试");
  } catch (err) {
    // 404=任务真不存在；400=状态不允许（如服务重启后的历史任务），按提示处理
    toast(`操作失败：${err.message}`);
  } finally {
    store.taskBusy.delete(key);
    btn.disabled = false;
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
    $("setting-retention").value = String(body.retention_minutes || 60);
    $("setting-max-download").value = String(body.max_download_mb ?? 0);
    $("setting-reserve-disk").value = String(body.reserve_disk_mb ?? 1024);
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
  const reserveDisk = Number($("setting-reserve-disk").value);
  if (!Number.isInteger(reserveDisk) || reserveDisk < 0) {
    msg.textContent = "预留磁盘空间需为不小于 0 的整数（0 = 关闭守门）";
    return;
  }
  const retention = Number($("setting-retention").value);
  if (!Number.isInteger(retention) || retention < 1) {
    msg.textContent = "保留时长需为正整数分钟（「不保留」选项已移除）";
    return;
  }
  $("settings-save").disabled = true;
  try {
    await postJSON("/api/settings", {
      download_dir: $("setting-download-dir").value.trim() || "downloads",
      concurrency,
      retention_minutes: retention,
      max_download_mb: maxDownload,
      reserve_disk_mb: reserveDisk,
    });
    msg.textContent = "";
    closeModal("settings-modal");
    toast("设置已保存");
    refreshDiskStats(); // 预留值变化后立即刷新管理员磁盘提示
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
  await Promise.all([refreshOnlineStats(), refreshDiskStats()]);
  // 在线统计与磁盘用量 30 秒轮询
  setInterval(() => {
    refreshOnlineStats();
    refreshDiskStats();
  }, 30000);
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

async function refreshDiskStats() {
  // 仅管理员面板调用：已用/剩余空间与空间不足警告横幅
  try {
    const body = await api("/api/admin/disk");
    $("stat-disk-used").textContent = fmtSize(body.used_bytes || 0);
    $("stat-disk-free").textContent = fmtSize(body.free_bytes || 0);
    const box = $("disk-free-box");
    if (box) box.classList.toggle("disk-low", !!body.low);
    const hint = $("disk-hint");
    if (!hint) return;
    if (!body.low) {
      hint.classList.add("hidden"); // 空间恢复即收起横幅
      return;
    }
    const text = $("disk-hint-text");
    if (text) {
      text.textContent =
        `⚠ 可用空间低于预留值（${fmtSize(body.reserve_bytes || 0)}），新下载将排队等待`;
    }
    if (!diskAlertDismissed) hint.classList.remove("hidden");
  } catch (_) { /* 磁盘统计失败不打扰 */ }
}

function dismissDiskAlert() {
  diskAlertDismissed = true;
  $("disk-hint").classList.add("hidden");
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
  $("disk-hint-close").addEventListener("click", dismissDiskAlert);
  await loadAdminPanel();

  const es = new EventSource(`/api/events?session=${encodeURIComponent(store.me.username)}`);
  es.onmessage = (e) => {
    try {
      handleEvent(JSON.parse(e.data));
    } catch (_) { /* 忽略非法帧 */ }
  };
  // 断线后 EventSource 浏览器默认自动重连，无需手动处理

  refreshCookieStatus();
  refreshLimits(); // 上限预检数据源（失败静默：服务端仍有权威校验兜底）
  refreshTasks();
}

// 单次下载上限（MB）：登录后拉取，submitDownload 用「已选」口径即时预检
async function refreshLimits() {
  try {
    const body = await api("/api/limits");
    store.limitMb = Number(body.max_download_mb) || 0;
  } catch (_) {
    store.limitMb = null; // 拉取失败不拦：服务端权威校验兜底
  }
}

document.addEventListener("DOMContentLoaded", init);
