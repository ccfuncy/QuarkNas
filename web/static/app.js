(() => {
  const $ = (id) => document.getElementById(id);

  const SESSION_KEY = "quarknas.ui.v1";
  const SPEED_KEY = "quarknas.speed.v1";
  const HISTORY_MS = 10 * 60 * 1000;
  const SAMPLE_MS = 2000;
  const ACTIVE_STATUSES = ["pending", "resolving", "downloading", "verifying"];
  const SPEED_STALE_MS = 15000;

  const state = {
    stack: [{ fid: "0", name: "根目录" }],
    entries: [],
    selected: new Map(),
    page: 1,
    size: 50,
    total: 0,
    filter: "",
    sort: { field: "time", order: "desc" },
    taskSearch: "",
    taskStatus: "all",
    mode: "local",
    keyword: "",
    tasks: new Map(),
    collapsed: new Set(),
    speeds: new Map(),
    samples: [],
    cfg: null,
    dragId: null,
  };

  // ------------------------------------------------------------ 工具

  function fmtSize(bytes) {
    if (!bytes) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = Number(bytes);
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
  }

  // 已下载 / 总大小：单位相同时只写一次，列宽才收得住，也不会被省略号截断
  function fmtSizePair(done, total) {
    if (!total) return fmtSize(done);
    if (done <= 0) return `0 / ${fmtSize(total)}`;
    const units = ["B", "KB", "MB", "GB", "TB"];
    const split = (bytes) => {
      let value = Number(bytes) || 0;
      let index = 0;
      while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
      }
      return { text: value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1), unit: units[index] };
    };
    const a = split(done);
    const b = split(total);
    return a.unit === b.unit ? `${a.text} / ${b.text} ${b.unit}` : `${fmtSize(done)} / ${fmtSize(total)}`;
  }

  function fmtTime(value) {
    if (!value) return "-";
    const date = typeof value === "number" && value < 1e12 ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return "-";
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function fmtDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "-";
    if (seconds < 60) return `${Math.round(seconds)} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
    return `${Math.floor(seconds / 3600)} 小时 ${Math.round((seconds % 3600) / 60)} 分`;
  }

  // 列表里的剩余时间只用一档单位，保证数字列宽度稳定、跨行对齐
  function fmtEta(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    if (seconds < 60) return `${Math.round(seconds)} 秒`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} 分`;
    return `${Math.round(seconds / 3600)} 小时`;
  }

  function fmtPercent(percent) {
    if (!Number.isFinite(percent) || percent <= 0) return "";
    return `${percent.toFixed(1).replace(/\.0$/, "")}%`;
  }

  function numCell(text, className = "") {
    const cell = document.createElement("span");
    cell.className = `num${className ? ` ${className}` : ""}`;
    cell.textContent = text || "";
    return cell;
  }

  // 任务列表的一行分两层：名字与状态在上，数字在下。
  // 分组标题和组内文件走同一套列宽，所以大小/速度/剩余/百分比会落在同一条竖线上。
  function taskBody({ lead = null, icon = null, name, count = null, status, actions = null, size, speed, eta, pct }) {
    const body = document.createElement("div");
    body.className = "task-body";

    const row = document.createElement("div");
    row.className = "task-row";
    const leadCell = document.createElement("div");
    leadCell.className = "row-lead";
    if (lead) leadCell.appendChild(lead);
    row.appendChild(leadCell);

    const main = document.createElement("div");
    main.className = "task-main";
    if (icon) main.appendChild(icon);
    main.appendChild(name);
    if (count) main.appendChild(count);
    row.appendChild(main);
    row.appendChild(status);
    if (actions) row.appendChild(actions);
    body.appendChild(row);

    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.appendChild(numCell(size, "m-size"));
    meta.appendChild(numCell(speed, "m-speed"));
    meta.appendChild(numCell(eta, "m-eta"));
    meta.appendChild(numCell(pct, "m-pct"));
    body.appendChild(meta);

    return body;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  function currentPath() {
    return "/" + state.stack.slice(1).map((item) => item.name).join("/");
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!response.ok) {
      let message = `请求失败 (${response.status})`;
      try {
        const body = await response.json();
        if (body && body.detail) message = body.detail;
      } catch (_) {
        /* 忽略非 JSON 错误体 */
      }
      throw new Error(message);
    }
    return response.json();
  }

  let toastTimer = null;
  function showToast(text, tone = "") {
    const el = $("toast");
    if (!el) return;
    el.textContent = text;
    el.className = `toast${tone ? ` ${tone}` : ""}`;
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.hidden = true;
    }, 2800);
  }

  // ------------------------------------------------------------ 状态持久化

  function saveSession() {
    try {
      const payload = {
        stack: state.stack,
        page: state.page,
        filter: state.filter,
        sort: state.sort,
        taskSearch: state.taskSearch,
        taskStatus: state.taskStatus,
        mode: state.mode,
        keyword: state.keyword,
        selected: Array.from(state.selected.values()).map((entry) => ({
          fid: entry.fid,
          name: entry.name,
          dir: entry.dir,
          size: entry.size,
        })),
      };
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(payload));
    } catch (_) {
      /* 隐私模式下可能不可写 */
    }
  }

  function restoreSession() {
    let saved;
    try {
      const raw = sessionStorage.getItem(SESSION_KEY);
      if (!raw) return false;
      saved = JSON.parse(raw);
    } catch (_) {
      return false;
    }
    if (!saved || typeof saved !== "object") return false;
    if (Array.isArray(saved.stack) && saved.stack.length) {
      state.stack = saved.stack.filter((node) => node && node.fid);
    }
    if (Number.isInteger(saved.page) && saved.page > 0) state.page = saved.page;
    if (typeof saved.filter === "string") state.filter = saved.filter;
    if (saved.sort && ["name", "size", "time"].includes(saved.sort.field)) {
      state.sort = { field: saved.sort.field, order: saved.sort.order === "asc" ? "asc" : "desc" };
    }
    if (typeof saved.taskSearch === "string") state.taskSearch = saved.taskSearch;
    if (typeof saved.taskStatus === "string") state.taskStatus = saved.taskStatus;
    if (saved.mode === "cloud" || saved.mode === "local") state.mode = saved.mode;
    if (typeof saved.keyword === "string") state.keyword = saved.keyword;
    if (Array.isArray(saved.selected)) {
      saved.selected.forEach((entry) => {
        if (entry && entry.fid) state.selected.set(entry.fid, entry);
      });
    }
    return true;
  }

  // ------------------------------------------------------------ 账号

  let accountValid = false;
  // 账号徽标悬停卡片的数据源：登录状态来自 /api/account，容量来自 /api/account/storage
  let accountInfo = { configured: false, valid: false, nickname: "", updated_at: null };
  let storageInfo = null;

  // 夸克 member_type 原始值 → 展示名；未知类型直接显示原值
  const MEMBER_TYPES = {
    NORMAL: "普通用户",
    VIP: "会员",
    SVIP: "超级会员",
    SUPER_VIP: "超级会员",
    EXP_SVIP: "超级会员（体验）",
    EXP_VIP: "会员（体验）",
    Z_VIP: "尊享会员",
    MINI_VIP: "迷你会员",
  };

  function memberLabel(raw) {
    const type = String(raw || "").toUpperCase();
    return MEMBER_TYPES[type] || type;
  }

  function renderAccountCard() {
    const set = (id, text) => {
      const el = $(id);
      if (el) el.textContent = text;
    };
    set("acc-nick", accountInfo.nickname || "-");
    const stateEl = $("acc-state");
    if (stateEl) {
      const ok = accountInfo.configured && accountInfo.valid;
      stateEl.textContent = !accountInfo.configured ? "未登录" : accountInfo.valid ? "正常" : "已失效";
      stateEl.className = `acc-val ${ok ? "ok" : "bad"}`;
    }
    if (storageInfo && (storageInfo.total || storageInfo.used)) {
      const raw = storageInfo.member_type || "";
      set("acc-vip", memberLabel(raw) || (storageInfo.vip ? "会员" : "普通用户"));
      set("acc-raw", raw || "-");
      set("acc-exp", storageInfo.expires_at ? fmtTime(storageInfo.expires_at) : "-");
      set("acc-used", fmtSize(storageInfo.used));
      set("acc-total", fmtSize(storageInfo.total));
      const free = storageInfo.total && storageInfo.used != null
        ? Math.max(0, storageInfo.total - storageInfo.used)
        : null;
      set("acc-free", free == null ? "-" : fmtSize(free));
      const pct = storageInfo.total
        ? Math.min(100, Math.round(((storageInfo.used || 0) / storageInfo.total) * 1000) / 10)
        : 0;
      const fill = $("acc-bar-fill");
      if (fill) fill.style.width = `${pct}%`;
    } else {
      ["acc-vip", "acc-raw", "acc-exp", "acc-used", "acc-total", "acc-free"].forEach((id) =>
        set(id, "-")
      );
      const fill = $("acc-bar-fill");
      if (fill) fill.style.width = "0%";
    }
    set(
      "acc-updated",
      accountInfo.updated_at ? `账号更新于 ${fmtTime(accountInfo.updated_at)}` : "尚未配置账号"
    );
  }

  async function loadAccount() {
    try {
      const info = await api("/api/account");
      accountInfo = info;
      const chip = $("account-chip");
      accountValid = !!info.valid;
      if (!info.configured) {
        chip.textContent = "未登录";
        chip.className = "chip bad";
        hideCapacity();
        maybeAutoLogin();
      } else if (info.valid) {
        chip.textContent = `${info.nickname || "已登录"}`;
        chip.className = "chip ok";
        loadCapacity();
      } else {
        chip.textContent = "登录已失效";
        chip.className = "chip bad";
        hideCapacity();
        maybeAutoLogin();
      }
    } catch (error) {
      $("account-chip").textContent = error.message;
      $("account-chip").className = "chip bad";
      hideCapacity();
    }
    renderAccountCard();
  }

  function hideCapacity() {
    const chip = $("capacity-chip");
    if (chip) chip.hidden = true;
  }

  async function loadCapacity() {
    try {
      const info = await api("/api/account/storage");
      storageInfo = info;
      const chip = $("capacity-chip");
      if (!chip) return;
      if (!info || (!info.total && !info.used)) {
        chip.hidden = true;
        renderAccountCard();
        return;
      }
      chip.textContent = `已用 ${fmtSize(info.used)} / ${fmtSize(info.total)}`;
      chip.title = info.vip ? memberLabel(info.member_type) || "会员" : "";
      chip.hidden = false;
    } catch (_) {
      hideCapacity();
    }
    renderAccountCard();
  }

  let qrSid = null;
  let qrTimer = null;
  let qrTick = null;
  let qrFailCount = 0;
  let qrDeadline = 0;
  let autoOpenedLogin = false;

  function maybeAutoLogin() {
    if (autoOpenedLogin) return;
    autoOpenedLogin = true;
    openLoginDialog();
  }

  function renderCountdown() {
    const el = $("qr-count");
    if (!el) return;
    if (!qrSid || !qrDeadline) {
      el.textContent = "";
      return;
    }
    const left = Math.max(0, Math.round((qrDeadline - Date.now()) / 1000));
    el.textContent = left > 0 ? `${left}s 后失效` : "已失效";
  }

  function stopQrPoll() {
    if (qrTimer) {
      clearInterval(qrTimer);
      qrTimer = null;
    }
    if (qrTick) {
      clearInterval(qrTick);
      qrTick = null;
    }
    qrSid = null;
    qrDeadline = 0;
    renderCountdown();
  }

  function setQrStatus(text, tone) {
    const el = $("qr-status");
    el.textContent = text;
    el.className = `qr-status${tone ? ` ${tone}` : ""}`;
  }

  async function startQrLogin() {
    stopQrPoll();
    const img = $("qr-img");
    const placeholder = $("qr-placeholder");
    img.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "二维码准备中…";
    setQrStatus("正在生成二维码…");
    try {
      const info = await api("/api/account/qr", { method: "POST" });
      qrSid = info.sid;
      img.onload = () => {
        img.hidden = false;
        placeholder.hidden = true;
      };
      img.onerror = () => {
        qrFailCount += 1;
        if (qrFailCount <= 3) {
          setQrStatus("二维码加载失败，正在重新获取…", "bad");
          setTimeout(startQrLogin, 800);
        } else {
          setQrStatus("二维码加载失败，请稍后重试", "bad");
        }
      };
      img.src = `${info.image_url}?t=${Date.now()}`;
      if (img.complete && img.naturalWidth > 0) {
        img.hidden = false;
        placeholder.hidden = true;
      }
      qrFailCount = 0;
      qrDeadline = Date.now() + (info.expires_in || 180) * 1000;
      renderCountdown();
      qrTick = setInterval(renderCountdown, 1000);
      setQrStatus("请用手机夸克 App 扫码，并在手机上点击确认登录");
      qrTimer = setInterval(pollQr, 2000);
    } catch (error) {
      setQrStatus(error.message, "bad");
    }
  }

  async function pollQr() {
    if (!qrSid) return;
    try {
      const result = await api(`/api/account/qr/${qrSid}`);
      if (result.status === "waiting") {
        setQrStatus(result.message || "等待扫码…");
        return;
      }
      if (result.status === "scanned") {
        setQrStatus("已扫码，请在手机上点击「确认登录」");
        return;
      }
      stopQrPoll();
      if (result.status === "confirmed") {
        setQrStatus(`登录成功：${result.nickname || "已连接"}`, "ok");
        $("qr-img").hidden = true;
        $("qr-placeholder").hidden = false;
        $("qr-placeholder").textContent = "已登录";
        autoOpenedLogin = true;
        await loadAccount();
        await loadFiles();
        setTimeout(() => $("cookie-dialog").close(), 900);
      } else if (result.status === "expired") {
        setQrStatus("二维码已失效，正在重新生成…", "bad");
        setTimeout(startQrLogin, 1000);
      } else {
        setQrStatus(result.message || "登录失败，请重新获取", "bad");
      }
    } catch (error) {
      stopQrPoll();
      setQrStatus(error.message, "bad");
    }
  }

  $("btn-qr-new").addEventListener("click", startQrLogin);
  $("btn-dialog-close").addEventListener("click", () => $("cookie-dialog").close());
  $("cookie-dialog").addEventListener("close", stopQrPoll);

  $("btn-account").addEventListener("click", openLoginDialog);

  function openLoginDialog() {
    if (!$("cookie-dialog").open) $("cookie-dialog").showModal();
    startQrLogin();
  }

  $("btn-check").addEventListener("click", async () => {
    const chip = $("account-chip");
    chip.textContent = "检测中…";
    try {
      const result = await api("/api/account/check", { method: "POST" });
      chip.textContent = result.valid ? result.nickname || "已登录" : result.message || "Cookie 无效";
      chip.className = `chip ${result.valid ? "ok" : "bad"}`;
      accountInfo = {
        ...accountInfo,
        configured: true,
        valid: !!result.valid,
        nickname: result.valid ? result.nickname || accountInfo.nickname : accountInfo.nickname,
      };
      renderAccountCard();
      if (result.valid) {
        accountValid = true;
        loadCapacity();
      } else {
        hideCapacity();
      }
    } catch (error) {
      chip.textContent = error.message;
      chip.className = "chip bad";
      accountInfo = { ...accountInfo, valid: false };
      renderAccountCard();
    }
  });

  // ------------------------------------------------------------ 文件列表

  function renderCrumbs() {
    const crumbs = $("crumbs");
    crumbs.innerHTML = "";

    if (state.mode === "cloud") {
      const button = document.createElement("button");
      button.className = "current";
      button.textContent = state.keyword ? `搜索「${state.keyword}」` : "全盘搜索";
      button.addEventListener("click", () => {
        state.mode = "local";
        state.keyword = "";
        state.page = 1;
        state.selected.clear();
        syncSearchUi();
        loadFiles();
      });
      crumbs.appendChild(button);
      return;
    }

    state.stack.forEach((node, index) => {
      if (index > 0) {
        const separator = document.createElement("span");
        separator.className = "sep";
        separator.textContent = "/";
        crumbs.appendChild(separator);
      }
      const button = document.createElement("button");
      button.textContent = node.name;
      if (index === state.stack.length - 1) button.classList.add("current");
      button.addEventListener("click", () => {
        state.stack = state.stack.slice(0, index + 1);
        state.page = 1;
        state.selected.clear();
        loadFiles();
      });
      crumbs.appendChild(button);
    });
  }

  function visibleEntries() {
    if (state.mode === "cloud" || !state.filter) return state.entries;
    const keyword = state.filter.toLowerCase();
    return state.entries.filter((entry) => entry.name.toLowerCase().includes(keyword));
  }

  // ------------------------------------------------------------ 排序

  function sortQuery() {
    return `&sort=${state.sort.field}&order=${state.sort.order}`;
  }

  function syncSortHeaders() {
    document.querySelectorAll(".files-table th[data-sort]").forEach((th) => {
      const active = th.dataset.sort === state.sort.field;
      th.classList.toggle("sort-asc", active && state.sort.order === "asc");
      th.classList.toggle("sort-desc", active && state.sort.order === "desc");
      th.setAttribute(
        "aria-sort",
        active ? (state.sort.order === "asc" ? "ascending" : "descending") : "none"
      );
    });
  }

  function toggleSort(field) {
    if (state.sort.field === field) {
      state.sort = { field, order: state.sort.order === "asc" ? "desc" : "asc" };
    } else {
      // 名称默认升序，大小/时间默认降序，符合找东西的习惯
      state.sort = { field, order: field === "name" ? "asc" : "desc" };
    }
    state.page = 1;
    saveSession();
    reloadCurrent();
  }

  document.querySelectorAll(".files-table th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => toggleSort(th.dataset.sort));
  });

  // ------------------------------------------------------------ 目录大小

  // 夸克不返回目录大小，只能由后端递归累加；结果按 fid 缓存在会话里。
  const dirSizes = new Map();
  const dirSizeQueue = new Set();
  let dirSizeTimer = null;

  function dirSizeLabel(fid) {
    const hit = dirSizes.get(fid);
    if (!hit) return "…";
    if (hit.size === null || hit.size === undefined) return "-";
    return hit.complete ? fmtSize(hit.size) : `≈ ${fmtSize(hit.size)}`;
  }

  function refreshDirSizeCells(fid) {
    const hit = dirSizes.get(fid);
    document.querySelectorAll(`[data-dir-size="${fid}"]`).forEach((cell) => {
      cell.textContent = dirSizeLabel(fid);
      if (!hit) cell.removeAttribute("title");
      else if (hit.size === null || hit.size === undefined) cell.title = "目录大小统计失败";
      else if (!hit.complete) cell.title = "目录较大，仅统计了一部分";
      else cell.title = "目录内文件大小合计";
    });
  }

  function requestDirSize(fid) {
    if (dirSizes.has(fid) || dirSizeQueue.has(fid)) return;
    dirSizeQueue.add(fid);
    if (!dirSizeTimer) dirSizeTimer = setTimeout(flushDirSizes, 150);
  }

  async function flushDirSizes() {
    dirSizeTimer = null;
    const fids = Array.from(dirSizeQueue);
    dirSizeQueue.clear();
    // 后端一次最多统计 12 个目录，这里小批发送，逐批回填结果。
    for (let index = 0; index < fids.length; index += 6) {
      const part = fids.slice(index, index + 6);
      const query = part.map(encodeURIComponent).join(",");
      try {
        const data = await api(`/api/files/sizes?fids=${query}`);
        part.forEach((fid) => {
          const item = data.sizes && data.sizes[fid];
          dirSizes.set(fid, item && item.size !== null ? item : { size: null, complete: false });
          refreshDirSizeCells(fid);
        });
      } catch (_) {
        part.forEach((fid) => {
          dirSizes.set(fid, { size: null, complete: false });
          refreshDirSizeCells(fid);
        });
      }
    }
  }

  // ------------------------------------------------------------ 文件图标

  // 按后缀归类，不同类型用不同形状与颜色，和网盘一样扫一眼就能分辨
  const FILE_TYPES = [
    { key: "image", test: /\.(png|jpe?g|gif|webp|bmp|svg|ico|heic|heif|avif|tiff?)$/i },
    { key: "video", test: /\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|rmvb|rm|mpg|mpeg|3gp)$/i },
    { key: "audio", test: /\.(mp3|flac|wav|aac|ogg|m4a|wma|ape|opus)$/i },
    { key: "pdf", test: /\.pdf$/i },
    { key: "doc", test: /\.(docx?|rtf|odt|pages|wps)$/i },
    { key: "sheet", test: /\.(xlsx?|csv|ods|numbers|et)$/i },
    { key: "slide", test: /\.(pptx?|odp|key|dps)$/i },
    { key: "code", test: /\.(txt|md|markdown|json|ya?ml|xml|html?|css|js|jsx|ts|tsx|py|java|c|cc|cpp|h|hpp|go|rs|sh|bat|ps1|log|ini|conf|toml|sql)$/i },
    { key: "archive", test: /\.(zip|rar|7z|tar|gz|tgz|bz2|xz|zst|iso)$/i },
    { key: "app", test: /\.(apk|exe|msi|dmg|pkg|deb|rpm|appimage)$/i },
  ];

  const ICON_PATHS = {
    folder: '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l1.8 2h9.2A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z"/>',
    image:
      '<rect x="3" y="4.5" width="18" height="15" rx="2"/><circle cx="8.5" cy="9.5" r="1.5" fill="currentColor" stroke="none"/><path d="M4.5 17.5 9 13l3.5 3.5L15 14l4.5 3.5"/>',
    video:
      '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10.5 9.5 15 12l-4.5 2.5z" fill="currentColor" stroke="none"/>',
    audio:
      '<path d="M9 17.5V6.5l9-2v11"/><circle cx="6.8" cy="17.6" r="2.2"/><circle cx="15.8" cy="15.6" r="2.2"/>',
    pdf:
      '<path d="M7 3.5h6.5L19 9v11.5H7z"/><path d="M13.2 3.6V9H19"/><path d="M9.6 12.6h2.2a1.6 1.6 0 0 1 0 3.2H9.6v-3.2Zm0 3.2v2.6"/>',
    doc:
      '<path d="M7 3.5h6.5L19 9v11.5H7z"/><path d="M13.2 3.6V9H19"/><path d="M9.8 12.6h5M9.8 15.6h5M9.8 18h3"/>',
    sheet:
      '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9.5h16M4 15h16M10 4v16M15 4v16"/>',
    slide:
      '<rect x="3.5" y="4.5" width="17" height="12" rx="1.8"/><path d="M9 20.5h6M12 16.5v4"/><path d="M7.5 13v-3M11 13V8M14.5 13v-2M18 13V9.5"/>',
    code: '<path d="M7 3.5h6.5L19 9v11.5H7z"/><path d="M13.2 3.6V9H19"/><path d="m10.4 12.4-2 2 2 2M14 12.4l2 2-2 2"/>',
    archive:
      '<path d="M4 8.5h16V19H4z"/><path d="M3 5h18v3.5H3z"/><path d="M10.6 5v2M10.6 8.5v2M10.6 12h2.8v3h-2.8z"/>',
    app: '<path d="M5 8.5 12 5l7 3.5v7L12 19l-7-3.5z"/><path d="M12 12v7M5 8.5l7 3.5 7-3.5"/>',
    file: '<path d="M7 3.5h6.5L19 9v11.5H7z"/><path d="M13.2 3.6V9H19"/>',
  };

  function fileTypeKey(entry) {
    if (entry.dir) return "folder";
    const name = entry.name || "";
    const hit = FILE_TYPES.find((item) => item.test.test(name));
    return hit ? hit.key : "file";
  }

  function fileIcon(entry, className = "icon") {
    const key = fileTypeKey(entry);
    const span = document.createElement("span");
    span.className = `${className} type-${key}`;
    span.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[key] || ICON_PATHS.file}</svg>`;
    return span;
  }

  function renderFiles() {
    const tbody = $("file-rows");
    tbody.innerHTML = "";
    syncSortHeaders();
    const entries = visibleEntries();
    const empty = $("files-empty");
    empty.hidden = entries.length > 0;
    if (entries.length === 0) {
      if (state.mode === "cloud" && state.keyword) {
        empty.textContent = "没有匹配的文件";
      } else if (state.filter) {
        empty.textContent = "当前目录没有匹配项";
      } else {
        empty.textContent = "这里还没有内容";
      }
    }

    entries.forEach((entry) => {
      const tr = document.createElement("tr");
      if (state.selected.has(entry.fid)) tr.classList.add("selected");

      const checkCell = document.createElement("td");
      checkCell.className = "col-check";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selected.has(entry.fid);
      checkbox.addEventListener("change", () => {
        toggleSelection(entry, checkbox.checked);
        tr.classList.toggle("selected", checkbox.checked);
      });
      checkCell.appendChild(checkbox);
      tr.appendChild(checkCell);

      const nameCell = document.createElement("td");
      const wrapper = document.createElement("div");
      wrapper.className = "name-cell";
      const icon = fileIcon(entry);
      wrapper.appendChild(icon);
      const label = document.createElement("button");
      label.className = "link";
      label.textContent = entry.name;
      if (entry.dir) {
        label.addEventListener("click", () => enterFolder(entry));
      } else {
        label.addEventListener("click", () => {
          const box = checkCell.querySelector("input");
          box.checked = !box.checked;
          toggleSelection(entry, box.checked);
          tr.classList.toggle("selected", box.checked);
        });
      }
      wrapper.appendChild(label);
      nameCell.appendChild(wrapper);
      tr.appendChild(nameCell);

      const sizeCell = document.createElement("td");
      sizeCell.className = "col-size";
      if (entry.dir) {
        sizeCell.dataset.dirSize = entry.fid;
        sizeCell.textContent = dirSizeLabel(entry.fid);
        requestDirSize(entry.fid);
      } else {
        sizeCell.textContent = fmtSize(entry.size);
      }
      tr.appendChild(sizeCell);

      const timeCell = document.createElement("td");
      timeCell.className = "col-time";
      timeCell.textContent = fmtTime(entry.updated_at);
      tr.appendChild(timeCell);

      if (entry.dir) {
        tr.addEventListener("dblclick", () => enterFolder(entry));
      }

      tr.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        openContextMenu(event, entry);
      });

      tbody.appendChild(tr);
    });

    const visible = visibleEntries();
    $("check-all").checked = visible.length > 0 && visible.every((e) => state.selected.has(e.fid));
    updateSelectionBadge();
  }

  function enterFolder(entry) {
    if (state.mode === "cloud") {
      // 从搜索结果进入目录时切回本地浏览，路径才准确。
      state.mode = "local";
      state.keyword = "";
      state.stack = [{ fid: "0", name: "根目录" }, { fid: entry.fid, name: entry.name }];
      syncSearchUi();
    } else {
      state.stack.push({ fid: entry.fid, name: entry.name });
    }
    state.page = 1;
    state.filter = "";
    state.selected.clear();
    syncSearchUi();
    loadFiles();
  }

  // ------------------------------------------------------------ 右键菜单

  const ctxMenu = document.createElement("div");
  ctxMenu.className = "ctx-menu";
  ctxMenu.hidden = true;
  ctxMenu.setAttribute("role", "menu");
  document.body.appendChild(ctxMenu);

  let ctxEntry = null;

  function closeContextMenu() {
    ctxMenu.hidden = true;
    ctxEntry = null;
  }

  function ctxItem(label, action, extraClass = "") {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `ctx-item${extraClass ? ` ${extraClass}` : ""}`;
    item.setAttribute("role", "menuitem");
    item.textContent = label;
    item.addEventListener("click", () => {
      const target = ctxEntry;
      closeContextMenu();
      if (target) action(target);
    });
    ctxMenu.appendChild(item);
  }

  function ctxDivider() {
    const line = document.createElement("div");
    line.className = "ctx-divider";
    ctxMenu.appendChild(line);
  }

  function openContextMenu(event, entry) {
    ctxEntry = entry;
    ctxMenu.innerHTML = "";
    ctxItem(entry.dir ? "重命名文件夹" : "重命名文件", openRenameDialog);
    ctxItem("移动到…", openMoveDialog);
    if (!entry.dir) ctxItem("下载到本地", enqueueDownload);
    ctxDivider();
    ctxItem("属性", openPropsDialog);
    ctxItem("删除", openDeleteDialog, "danger");

    // 先显示再量尺寸，贴边时向内收，避免菜单被裁掉
    ctxMenu.hidden = false;
    const rect = ctxMenu.getBoundingClientRect();
    const left = Math.min(event.clientX, window.innerWidth - rect.width - 8);
    const top = Math.min(event.clientY, window.innerHeight - rect.height - 8);
    ctxMenu.style.left = `${Math.max(8, left)}px`;
    ctxMenu.style.top = `${Math.max(8, top)}px`;
  }

  document.addEventListener("click", (event) => {
    if (!ctxMenu.hidden && !ctxMenu.contains(event.target)) closeContextMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeContextMenu();
  });
  window.addEventListener("blur", closeContextMenu);
  window.addEventListener("resize", closeContextMenu);
  document.addEventListener("scroll", closeContextMenu, true);

  // ------------------------------------------------------------ 重命名

  let renameEntry = null;

  function openRenameDialog(entry) {
    const dialog = $("rename-dialog");
    if (!dialog) return;
    renameEntry = entry;
    $("rename-title").textContent = entry.dir ? "重命名文件夹" : "重命名文件";
    const input = $("rename-input");
    input.value = entry.name;
    const message = $("rename-msg");
    message.textContent = "";
    message.className = "dialog-msg";
    if (!dialog.open) dialog.showModal();
    input.focus();
    // 默认只选中主文件名，扩展名不动，避免手滑改掉后缀
    const dot = entry.dir ? -1 : entry.name.lastIndexOf(".");
    input.setSelectionRange(0, dot > 0 ? dot : entry.name.length);
  }

  async function submitRename(event) {
    event.preventDefault();
    if (!renameEntry) return;
    const input = $("rename-input");
    const name = input.value.trim();
    const message = $("rename-msg");
    if (!name) {
      message.textContent = "名称不能为空";
      message.className = "dialog-msg bad";
      input.focus();
      return;
    }
    if (name === renameEntry.name) {
      $("rename-dialog").close();
      return;
    }
    const button = $("btn-rename-save");
    button.disabled = true;
    message.textContent = "保存中…";
    message.className = "dialog-msg";
    try {
      await api("/api/files/rename", {
        method: "POST",
        body: JSON.stringify({ fid: renameEntry.fid, name }),
      });
      renameEntry.name = name;
      $("rename-dialog").close();
      renderFiles();
      saveSession();
      showToast(`已重命名为「${name}」`, "ok");
    } catch (error) {
      message.textContent = error.message;
      message.className = "dialog-msg bad";
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------ 移动到

  let moveEntries = [];
  let moveStack = [];

  function openMoveDialog(entry) {
    openMoveDialogFor([entry]);
  }

  async function openMoveDialogFor(entries) {
    moveEntries = (entries || []).filter(Boolean);
    if (!moveEntries.length) return;
    moveStack = [{ fid: "0", name: "根目录" }];
    $("move-title").textContent =
      moveEntries.length === 1
        ? `移动「${moveEntries[0].name}」到`
        : `移动 ${moveEntries.length} 个项目到`;
    const message = $("move-msg");
    message.textContent = "";
    message.className = "dialog-msg";
    const dialog = $("move-dialog");
    if (!dialog.open) dialog.showModal();
    await loadMoveList();
  }

  async function loadMoveList() {
    const current = moveStack[moveStack.length - 1];
    $("move-path").textContent = "/" + moveStack.slice(1).map((node) => node.name).join("/");
    $("move-up").disabled = moveStack.length <= 1;
    const list = $("move-list");
    list.innerHTML = '<div class="move-empty">加载中…</div>';
    try {
      const data = await api(`/api/files?fid=${encodeURIComponent(current.fid)}&page=1&size=200`);
      // 不能把文件夹移进它自己，排除后也就进不了它的子树
      const excluded = new Set(moveEntries.filter((item) => item.dir).map((item) => item.fid));
      const dirs = (data.entries || []).filter((item) => item.dir && !excluded.has(item.fid));
      list.innerHTML = "";
      if (!dirs.length) {
        list.innerHTML = '<div class="move-empty">这里没有子文件夹</div>';
        return;
      }
      dirs.forEach((dir) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "move-item";
        button.appendChild(fileIcon(dir, "icon"));
        const text = document.createElement("span");
        text.textContent = dir.name;
        button.appendChild(text);
        button.addEventListener("click", () => {
          moveStack.push({ fid: dir.fid, name: dir.name });
          loadMoveList();
        });
        list.appendChild(button);
      });
    } catch (error) {
      list.innerHTML = `<div class="move-empty">${escapeHtml(error.message)}</div>`;
    }
  }

  async function submitMove() {
    if (!moveEntries.length) return;
    const target = moveStack[moveStack.length - 1];
    const button = $("btn-move-here");
    const message = $("move-msg");
    button.disabled = true;
    message.textContent = "移动中…";
    message.className = "dialog-msg";
    try {
      const fids = moveEntries.map((entry) => entry.fid);
      await api("/api/files/move", {
        method: "POST",
        body: JSON.stringify({ fids, to_fid: target.fid }),
      });
      fids.forEach((fid) => state.selected.delete(fid));
      const label =
        moveEntries.length === 1 ? `「${moveEntries[0].name}」` : `${moveEntries.length} 个项目`;
      $("move-dialog").close();
      moveEntries = [];
      showToast(`已移动${label}到 ${target.fid === "0" ? "根目录" : target.name}`, "ok");
      await loadFiles();
    } catch (error) {
      message.textContent = error.message;
      message.className = "dialog-msg bad";
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------ 删除

  let rmEntries = [];

  function openDeleteDialog(entry) {
    openDeleteDialogFor([entry]);
  }

  function openDeleteDialogFor(entries) {
    rmEntries = (entries || []).filter(Boolean);
    if (!rmEntries.length) return;
    $("rm-msg").textContent =
      rmEntries.length === 1
        ? `确定删除${rmEntries[0].dir ? "文件夹" : "文件"}「${rmEntries[0].name}」？删除后可在夸克网盘回收站找回。`
        : `确定删除选中的 ${rmEntries.length} 个项目？删除后可在夸克网盘回收站找回。`;
    const result = $("rm-result");
    result.textContent = "";
    result.className = "dialog-msg";
    const dialog = $("rm-dialog");
    if (!dialog.open) dialog.showModal();
  }

  async function submitDelete() {
    if (!rmEntries.length) return;
    const button = $("btn-rm-confirm");
    const result = $("rm-result");
    button.disabled = true;
    result.textContent = "删除中…";
    result.className = "dialog-msg";
    try {
      const fids = rmEntries.map((entry) => entry.fid);
      await api("/api/files/delete", {
        method: "POST",
        body: JSON.stringify({ fids }),
      });
      fids.forEach((fid) => state.selected.delete(fid));
      const label = rmEntries.length === 1 ? `「${rmEntries[0].name}」` : `${rmEntries.length} 个项目`;
      $("rm-dialog").close();
      rmEntries = [];
      showToast(`已删除${label}`, "ok");
      await loadFiles();
    } catch (error) {
      result.textContent = error.message;
      result.className = "dialog-msg bad";
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------ 属性

  function fileTypeLabel(name) {
    const dot = name.lastIndexOf(".");
    if (dot <= 0 || dot === name.length - 1) return "文件";
    return `${name.slice(dot + 1).toUpperCase()} 文件`;
  }

  function openPropsDialog(entry) {
    const rows = [
      ["名称", entry.name],
      ["类型", entry.dir ? "文件夹" : fileTypeLabel(entry.name)],
      ["位置", state.mode === "cloud" ? `搜索结果 · ${currentPath()}` : currentPath()],
      ["大小", entry.dir ? dirSizeLabel(entry.fid) : fmtSize(entry.size)],
      ["修改时间", fmtTime(entry.updated_at)],
      ["文件 ID", entry.fid],
    ];
    const body = $("props-body");
    body.innerHTML = "";
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value || "-";
      // 目录大小是异步统计的，挂上 data-dir-size 让结果回来后直接回填
      if (label === "大小" && entry.dir) {
        dd.dataset.dirSize = entry.fid;
        if (!dirSizes.has(entry.fid)) {
          dd.textContent = "…";
          dd.title = "正在统计目录大小…";
          requestDirSize(entry.fid);
        }
      }
      body.append(dt, dd);
    });
    const dialog = $("props-dialog");
    if (!dialog.open) dialog.showModal();
  }

  // ------------------------------------------------------------ 新建文件夹

  function openFolderDialog() {
    if (state.mode === "cloud") {
      showToast("搜索结果中不能新建文件夹，请先进入目录", "bad");
      return;
    }
    $("folder-input").value = "";
    const message = $("folder-msg");
    message.textContent = "";
    message.className = "dialog-msg";
    const dialog = $("folder-dialog");
    if (!dialog.open) dialog.showModal();
    $("folder-input").focus();
  }

  async function submitFolder(event) {
    event.preventDefault();
    const name = $("folder-input").value.trim();
    const message = $("folder-msg");
    if (!name) {
      message.textContent = "名称不能为空";
      message.className = "dialog-msg bad";
      $("folder-input").focus();
      return;
    }
    const button = $("btn-folder-save");
    button.disabled = true;
    message.textContent = "创建中…";
    message.className = "dialog-msg";
    try {
      await api("/api/files/folder", {
        method: "POST",
        body: JSON.stringify({ pdir_fid: state.stack[state.stack.length - 1].fid, name }),
      });
      $("folder-dialog").close();
      showToast(`已创建文件夹「${name}」`, "ok");
      await loadFiles();
    } catch (error) {
      message.textContent = error.message;
      message.className = "dialog-msg bad";
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------ 单个下载

  async function enqueueDownload(entry) {
    try {
      const path = state.mode === "cloud" ? "" : currentPath();
      await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          items: [
            { fid: entry.fid, name: entry.name, dir: entry.dir, size: entry.size, path },
          ],
          keep_structure: true,
        }),
      });
      await loadTasks();
      showToast(`已加入下载「${entry.name}」`, "ok");
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  function toggleSelection(entry, checked) {
    if (checked) {
      state.selected.set(entry.fid, entry);
    } else {
      state.selected.delete(entry.fid);
    }
    updateSelectionBadge();
    saveSession();
  }

  function updateSelectionBadge() {
    const count = state.selected.size;
    $("sel-count").textContent = String(count);
    $("btn-download").disabled = count === 0;
    // 选中项为空时整条操作栏收起，避免占着列表上方的空间
    const bar = $("sel-bar");
    if (bar) bar.hidden = count === 0;
  }

  function renderPager() {
    const pager = $("pager");
    const pages = Math.max(1, Math.ceil(state.total / state.size));
    if (pages <= 1) {
      pager.innerHTML = `<span>共 ${state.total} 项</span>`;
      return;
    }
    pager.innerHTML = `
      <button class="btn tiny" id="page-prev" ${state.page <= 1 ? "disabled" : ""}>上一页</button>
      <span>${state.page} / ${pages} · 共 ${state.total} 项</span>
      <button class="btn tiny" id="page-next" ${state.page >= pages ? "disabled" : ""}>下一页</button>
    `;
    const prev = $("page-prev");
    const next = $("page-next");
    if (prev) {
      prev.addEventListener("click", () => {
        state.page -= 1;
        reloadCurrent();
      });
    }
    if (next) {
      next.addEventListener("click", () => {
        state.page += 1;
        reloadCurrent();
      });
    }
  }

  function reloadCurrent() {
    if (state.mode === "cloud" && state.keyword) {
      loadSearch();
    } else {
      loadFiles();
    }
  }

  async function loadFiles(options = {}) {
    renderCrumbs();
    const tbody = $("file-rows");
    tbody.innerHTML = `<tr><td colspan="4" class="empty">加载中…</td></tr>`;
    try {
      const fid = state.stack[state.stack.length - 1].fid;
      const result = await api(`/api/files?fid=${encodeURIComponent(fid)}&page=${state.page}&size=${state.size}${sortQuery()}`);
      state.entries = result.entries || [];
      state.total = result.total || 0;
      if (!options.preserveSelection) state.selected.clear();
      pruneSelection();
      renderFiles();
      renderPager();
      saveSession();
    } catch (error) {
      state.entries = [];
      state.total = 0;
      tbody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(error.message)}</td></tr>`;
      renderPager();
    }
  }

  async function loadSearch() {
    const keyword = state.keyword.trim();
    if (!keyword) {
      showToast("请输入搜索关键词", "bad");
      return;
    }
    renderCrumbs();
    const tbody = $("file-rows");
    tbody.innerHTML = `<tr><td colspan="4" class="empty">搜索中…</td></tr>`;
    try {
      const result = await api(
        `/api/files/search?keyword=${encodeURIComponent(keyword)}&page=${state.page}&size=${state.size}${sortQuery()}`
      );
      state.entries = result.entries || [];
      state.total = result.total || 0;
      pruneSelection();
      renderFiles();
      renderPager();
      saveSession();
    } catch (error) {
      state.entries = [];
      state.total = 0;
      tbody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(error.message)}</td></tr>`;
      renderPager();
    }
  }

  function pruneSelection() {
    const fids = new Set(state.entries.map((entry) => entry.fid));
    for (const fid of Array.from(state.selected.keys())) {
      if (!fids.has(fid)) state.selected.delete(fid);
    }
  }

  function syncSearchUi() {
    const modeSelect = $("search-mode");
    const input = $("search");
    if (modeSelect) modeSelect.value = state.mode;
    if (state.mode === "cloud") {
      input.placeholder = "输入关键词后回车搜索";
      input.value = state.keyword;
    } else {
      input.placeholder = "筛选当前目录";
      input.value = state.filter;
    }
    // 搜索结果是跨目录的，新建文件夹没有确定的落点
    const folderBtn = $("btn-new-folder");
    if (folderBtn) folderBtn.disabled = state.mode === "cloud";
  }

  $("search").addEventListener("input", (event) => {
    if (state.mode === "cloud") {
      state.keyword = event.target.value;
      return;
    }
    state.filter = event.target.value.trim();
    renderFiles();
    saveSession();
  });

  $("search").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    if (state.mode === "cloud") {
      event.preventDefault();
      state.keyword = event.target.value.trim();
      state.page = 1;
      state.selected.clear();
      loadSearch();
    }
  });

  $("search-mode").addEventListener("change", (event) => {
    state.mode = event.target.value === "cloud" ? "cloud" : "local";
    state.page = 1;
    state.selected.clear();
    state.filter = "";
    if (state.mode === "cloud") {
      const keyword = $("search").value.trim();
      state.keyword = keyword;
      syncSearchUi();
      if (keyword) {
        loadSearch();
      } else {
        renderCrumbs();
        $("file-rows").innerHTML = "";
        $("files-empty").hidden = false;
        $("files-empty").textContent = "输入关键词后按回车搜索整盘";
        $("pager").innerHTML = "";
      }
    } else {
      state.keyword = "";
      syncSearchUi();
      loadFiles();
    }
    saveSession();
  });

  $("btn-refresh").addEventListener("click", () => reloadCurrent());

  $("check-all").addEventListener("change", (event) => {
    if (event.target.checked) {
      visibleEntries().forEach((entry) => state.selected.set(entry.fid, entry));
    } else {
      visibleEntries().forEach((entry) => state.selected.delete(entry.fid));
    }
    renderFiles();
    saveSession();
  });

  $("btn-download").addEventListener("click", async () => {
    if (state.selected.size === 0) return;
    const button = $("btn-download");
    button.disabled = true;
    try {
      const path = state.mode === "cloud" ? "" : currentPath();
      const items = Array.from(state.selected.values()).map((entry) => ({
        fid: entry.fid,
        name: entry.name,
        dir: entry.dir,
        size: entry.size,
        path,
      }));
      const result = await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({ items, keep_structure: true }),
      });
      state.selected.clear();
      renderFiles();
      await loadTasks();
      const extra = result.skipped ? `，跳过 ${result.skipped} 个重复任务` : "";
      showToast(`已加入 ${result.created} 个任务${extra}`, "ok");
      saveSession();
    } catch (error) {
      showToast(error.message, "bad");
      updateSelectionBadge();
    }
  });

  $("btn-batch-move").addEventListener("click", () => {
    openMoveDialogFor(Array.from(state.selected.values()));
  });

  $("btn-batch-delete").addEventListener("click", () => {
    openDeleteDialogFor(Array.from(state.selected.values()));
  });

  $("btn-sel-clear").addEventListener("click", () => {
    state.selected.clear();
    renderFiles();
    saveSession();
  });

  // ------------------------------------------------------------ 回收站

  const recycle = { page: 1, size: 50, total: 0, entries: [], selected: new Set() };

  function recyclePicked() {
    return recycle.entries.filter((entry) => recycle.selected.has(entry.record_id));
  }

  function updateRecycleFoot() {
    const count = recycle.selected.size;
    $("btn-recycle-restore").disabled = count === 0;
    $("btn-recycle-purge").disabled = count === 0;
    const message = $("recycle-msg");
    message.textContent = count ? `已选 ${count} 项` : "";
    message.className = "dialog-msg";
  }

  function renderRecycle() {
    const list = $("recycle-list");
    list.innerHTML = "";
    $("recycle-empty").hidden = recycle.entries.length > 0;

    const pages = Math.max(1, Math.ceil(recycle.total / recycle.size));
    $("recycle-pager").hidden = pages <= 1;
    $("recycle-page-info").textContent = `${recycle.page} / ${pages}`;
    $("recycle-prev").disabled = recycle.page <= 1;
    $("recycle-next").disabled = recycle.page >= pages;

    recycle.entries.forEach((entry) => {
      const row = document.createElement("label");
      row.className = "recycle-item";

      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = recycle.selected.has(entry.record_id);
      box.addEventListener("change", () => {
        if (box.checked) recycle.selected.add(entry.record_id);
        else recycle.selected.delete(entry.record_id);
        updateRecycleFoot();
      });
      row.appendChild(box);
      row.appendChild(fileIcon(entry, "icon"));

      const name = document.createElement("span");
      name.className = "recycle-name";
      name.textContent = entry.name;
      name.title = entry.name;
      row.appendChild(name);

      const meta = document.createElement("span");
      meta.className = "recycle-meta";
      meta.textContent = `${entry.dir ? "文件夹" : fmtSize(entry.size)} · ${fmtTime(entry.deleted_at)}`;
      if (entry.expire_at) meta.title = `将于 ${fmtTime(entry.expire_at)} 自动清理`;
      row.appendChild(meta);

      list.appendChild(row);
    });

    updateRecycleFoot();
  }

  async function loadRecycle() {
    const list = $("recycle-list");
    list.innerHTML = '<div class="recycle-empty muted">加载中…</div>';
    $("recycle-empty").hidden = true;
    try {
      const data = await api(`/api/files/recycle?page=${recycle.page}&size=${recycle.size}`);
      recycle.entries = data.entries || [];
      recycle.total = data.total || 0;
      // 翻页后丢掉不在当前页的选中项，避免还原到看不见的条目
      const alive = new Set(recycle.entries.map((entry) => entry.record_id));
      Array.from(recycle.selected).forEach((record) => {
        if (!alive.has(record)) recycle.selected.delete(record);
      });
      renderRecycle();
    } catch (err) {
      list.innerHTML = "";
      showToast(err.message, "bad");
    }
  }

  async function openRecycle() {
    recycle.page = 1;
    recycle.selected.clear();
    const dialog = $("recycle-dialog");
    if (!dialog.open) dialog.showModal();
    await loadRecycle();
  }

  async function recyclePost(path, records) {
    await api(path, {
      method: "POST",
      body: JSON.stringify(records ? { records } : {}),
    });
  }

  $("btn-recycle").addEventListener("click", openRecycle);

  $("recycle-prev").addEventListener("click", () => {
    if (recycle.page <= 1) return;
    recycle.page -= 1;
    loadRecycle();
  });

  $("recycle-next").addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil(recycle.total / recycle.size));
    if (recycle.page >= pages) return;
    recycle.page += 1;
    loadRecycle();
  });

  $("btn-recycle-restore").addEventListener("click", async () => {
    const items = recyclePicked();
    if (!items.length) return;
    const button = $("btn-recycle-restore");
    button.disabled = true;
    try {
      await recyclePost("/api/files/recycle/restore", items.map((item) => item.record_id));
      recycle.selected.clear();
      showToast(`已还原 ${items.length} 项`, "ok");
      await Promise.all([loadRecycle(), loadFiles()]);
    } catch (err) {
      showToast(err.message, "bad");
      button.disabled = false;
    }
  });

  $("btn-recycle-purge").addEventListener("click", async () => {
    const items = recyclePicked();
    if (!items.length) return;
    if (!window.confirm(`彻底删除选中的 ${items.length} 项？该操作无法撤销。`)) return;
    const button = $("btn-recycle-purge");
    button.disabled = true;
    try {
      await recyclePost("/api/files/recycle/purge", items.map((item) => item.record_id));
      recycle.selected.clear();
      showToast(`已彻底删除 ${items.length} 项`, "ok");
      await Promise.all([loadRecycle(), loadFiles()]);
    } catch (err) {
      showToast(err.message, "bad");
      button.disabled = false;
    }
  });

  $("btn-recycle-clear").addEventListener("click", async () => {
    if (!window.confirm("清空回收站？其中的内容将被永久删除，无法恢复。")) return;
    const button = $("btn-recycle-clear");
    button.disabled = true;
    try {
      await recyclePost("/api/files/recycle/clear", null);
      recycle.selected.clear();
      showToast("回收站已清空", "ok");
      await Promise.all([loadRecycle(), loadFiles()]);
    } catch (err) {
      showToast(err.message, "bad");
      button.disabled = false;
    } finally {
      button.disabled = false;
    }
  });

  // ------------------------------------------------------------ 任务

  const STATUS_TEXT = {
    pending: "排队中",
    resolving: "获取直链",
    downloading: "下载中",
    verifying: "校验中",
    paused: "已暂停",
    done: "已完成",
    failed: "失败",
    canceled: "已取消",
    skipped: "已跳过",
  };

  let renderTimer = null;
  function scheduleTasksRender() {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      renderTasks();
      if (detailTaskId !== null) renderTaskDetail();
    }, 200);
  }

  function upsertTask(task) {
    if (!task || !task.id) return;
    const previous = state.tasks.get(task.id);
    state.tasks.set(task.id, { ...previous, ...task });
    if (previous && previous.status !== task.status) {
      if (task.status === "done" || task.status === "failed") {
        state.speeds.delete(task.id);
      }
      if (ACTIVE_STATUSES.includes(task.status) === false) {
        state.speeds.delete(task.id);
      }
    }
    scheduleTasksRender();
  }

  const TASK_STATUS_GROUPS = {
    active: ["pending", "resolving", "downloading", "verifying"],
    done: ["done", "skipped"],
    failed: ["failed"],
    paused: ["paused"],
    canceled: ["canceled"],
  };

  function matchesTaskFilter(task) {
    const group = TASK_STATUS_GROUPS[state.taskStatus];
    if (group && !group.includes(task.status)) return false;
    const keyword = state.taskSearch.trim().toLowerCase();
    if (!keyword) return true;
    const haystack = `${task.name || ""} ${task.dst_path || ""} ${task.batch_name || ""}`.toLowerCase();
    return haystack.includes(keyword);
  }

  function taskRank(task) {
    return {
      pending: task.status === "pending" ? 1 : 0,
      priority: Number(task.priority) || 0,
      id: task.id,
    };
  }

  // 排队中的任务按 priority 排在前面（可拖拽调整），其余保持新任务在上。
  function compareRank(a, b) {
    if (a.pending !== b.pending) return b.pending - a.pending;
    if (a.pending) {
      const diff = b.priority - a.priority;
      if (diff) return diff;
      return a.id - b.id;
    }
    return b.id - a.id;
  }

  function pendingOrder() {
    return Array.from(state.tasks.values())
      .filter((task) => task.status === "pending")
      .sort((a, b) => compareRank(taskRank(a), taskRank(b)))
      .map((task) => task.id);
  }

  function taskTree() {
    const tasks = Array.from(state.tasks.values()).filter(matchesTaskFilter);
    const groups = new Map();
    const nodes = [];
    tasks.forEach((task) => {
      const rank = taskRank(task);
      if (task.batch_id && task.batch_name) {
        let group = groups.get(task.batch_id);
        if (!group) {
          group = { type: "group", id: task.id, batchId: task.batch_id, name: task.batch_name, tasks: [], rank };
          groups.set(task.batch_id, group);
          nodes.push(group);
        }
        group.tasks.push(task);
        group.id = Math.max(group.id, task.id);
        if (compareRank(rank, group.rank) < 0) group.rank = rank;
      } else {
        nodes.push({ type: "task", id: task.id, task, rank });
      }
    });
    nodes.sort((a, b) => compareRank(a.rank, b.rank));
    nodes.forEach((node) => {
      if (node.type === "group") node.tasks.sort((a, b) => compareRank(taskRank(a), taskRank(b)));
    });
    return nodes;
  }

  function groupSummary(tasks) {
    let size = 0;
    let downloaded = 0;
    let done = 0;
    let failed = 0;
    let active = 0;
    let paused = 0;
    let finished = 0;
    tasks.forEach((task) => {
      size += Number(task.size) || 0;
      const current = Number(task.downloaded) || 0;
      downloaded += task.status === "done" ? Number(task.size) || current : Math.min(current, Number(task.size) || current);
      if (task.status === "done" || task.status === "skipped") done += 1;
      if (task.status === "failed") failed += 1;
      if (ACTIVE_STATUSES.includes(task.status)) active += 1;
      if (task.status === "paused") paused += 1;
      if (!ACTIVE_STATUSES.includes(task.status)) finished += 1;
    });
    return { size, downloaded, done, failed, active, paused, finished, total: tasks.length };
  }

  function renderTasks() {
    const list = $("task-list");
    const nodes = taskTree();
    if (nodes.length === 0) {
      const filtered = state.taskSearch.trim() || state.taskStatus !== "all";
      list.innerHTML = `<div class="empty">${filtered ? "没有匹配的任务" : "暂无任务"}</div>`;
      return;
    }
    list.innerHTML = "";
    nodes.forEach((node) => {
      if (node.type === "task") {
        list.appendChild(renderTaskCard(node.task));
        return;
      }

      const summary = groupSummary(node.tasks);
      const collapsed = state.collapsed.has(node.batchId);
      const card = document.createElement("div");
      card.className = `task group${summary.active > 0 ? " is-live" : ""}`;

      const toggle = document.createElement("button");
      toggle.className = `group-toggle${collapsed ? "" : " open"}`;
      toggle.type = "button";
      toggle.innerHTML = `<svg viewBox="0 0 12 12" aria-hidden="true">
        <path d="M4.3 2.5 7.9 6l-3.6 3.5" fill="none" stroke="currentColor"
          stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
      toggle.setAttribute("aria-label", collapsed ? "展开" : "折叠");
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.addEventListener("click", () => {
        if (collapsed) state.collapsed.delete(node.batchId);
        else state.collapsed.add(node.batchId);
        renderTasks();
      });

      const icon = document.createElement("span");
      icon.className = "group-icon";
      icon.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M2.6 3.2h3l1.3 1.5h6.5c.6 0 1.1.5 1.1 1.1v6.1c0 .6-.5 1.1-1.1 1.1H2.6c-.6 0-1.1-.5-1.1-1.1V4.3c0-.6.5-1.1 1.1-1.1z"
          fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>
      </svg>`;

      const name = document.createElement("div");
      name.className = "task-name";
      name.textContent = node.name;
      name.title = node.name;

      const count = document.createElement("span");
      count.className = "group-count";
      count.textContent = `${summary.done}/${summary.total}`;
      count.title = `共 ${summary.total} 个文件，已完成 ${summary.done} 个`;

      const status = document.createElement("span");
      const tone = summary.failed ? "failed" : summary.active ? "downloading" : summary.paused ? "paused" : "done";
      if (tone === "done") card.classList.add("done");
      status.className = `task-status ${tone}`;
      status.textContent = summary.failed
        ? "有失败"
        : summary.active
          ? "下载中"
          : summary.paused
            ? "已暂停"
            : summary.done === summary.total
              ? "已完成"
              : "已结束";
      status.title = status.textContent;

      const groupIds = node.tasks.map((task) => task.id);
      const ops = document.createElement("div");
      ops.className = "group-actions";
      if (summary.active > 0) {
        ops.appendChild(makeButton("暂停", "ghost", () => batchAction("pause", groupIds)));
      }
      if (summary.paused > 0) {
        ops.appendChild(makeButton("恢复", "ghost", () => batchAction("resume", groupIds)));
      }
      if (summary.failed > 0) {
        ops.appendChild(makeButton("重试", "ghost", () => batchAction("retry", groupIds)));
      }
      if (summary.finished > 0) {
        ops.appendChild(makeButton("清理", "ghost danger", () => batchAction("delete", groupIds)));
      }

      const percent = summary.size > 0 ? Math.min(100, (summary.downloaded / summary.size) * 100) : 0;
      const complete = summary.total > 0 && summary.done === summary.total;
      const shown = complete ? 100 : percent;
      const speed = node.tasks.reduce(
        (sum, task) => sum + (ACTIVE_STATUSES.includes(task.status) ? liveSpeed(task.id) : 0),
        0
      );
      const remaining = Math.max(0, summary.size - summary.downloaded);
      const row = taskBody({
        lead: toggle,
        icon,
        name,
        count,
        status,
        actions: ops.childNodes.length ? ops : null,
        size: fmtSizePair(summary.downloaded, summary.size),
        speed: speed > 0 ? `${fmtSize(speed)}/s` : "",
        eta: speed > 0 && remaining > 0 ? fmtEta(remaining / speed) : "",
        pct: fmtPercent(shown),
      });
      card.appendChild(row);

      const bar = document.createElement("div");
      bar.className = `bar${summary.failed ? " failed" : ""}${summary.active > 0 ? " live" : ""}`;
      const fill = document.createElement("i");
      fill.style.width = `${shown}%`;
      bar.appendChild(fill);
      card.appendChild(bar);

      if (!collapsed) {
        const body = document.createElement("div");
        body.className = "group-body";
        node.tasks.forEach((task) => body.appendChild(renderTaskCard(task, true)));
        card.appendChild(body);
      }

      list.appendChild(card);
    });
  }

  function renderTaskCard(task, compact = false) {
    const card = document.createElement("div");
    card.className = `task${compact ? " compact" : ""}`;
    card.dataset.taskId = task.id;
    card.dataset.pending = task.status === "pending" ? "1" : "0";
    const live = task.status === "resolving" || task.status === "downloading" || task.status === "verifying";
    if (live) card.classList.add("is-live");
    if (task.status === "done") card.classList.add("done");

    let lead = null;
    if (!compact && task.status === "pending") {
      lead = document.createElement("span");
      lead.className = "drag-handle";
      lead.title = "拖动调整排队顺序";
      lead.setAttribute("aria-hidden", "true");
      lead.innerHTML = `<svg viewBox="0 0 10 14"><g fill="currentColor">
        <circle cx="3" cy="2.5" r="1.1"/><circle cx="7" cy="2.5" r="1.1"/>
        <circle cx="3" cy="7" r="1.1"/><circle cx="7" cy="7" r="1.1"/>
        <circle cx="3" cy="11.5" r="1.1"/><circle cx="7" cy="11.5" r="1.1"/>
      </g></svg>`;
      attachDrag(card, task);
    }
    const name = document.createElement("div");
    name.className = "task-name";
    name.textContent = task.name || "(未知文件)";
    name.title = task.name || "";
    const status = document.createElement("span");
    status.className = `task-status ${task.status}`;
    status.textContent = `${STATUS_TEXT[task.status] || task.status}${
      task.retry_count > 0 ? `（重试 ${task.retry_count}）` : ""
    }`;
    status.title = status.textContent;

    const actions = document.createElement("div");
    actions.className = "task-actions";
    const statusName = task.status;
    actions.appendChild(makeButton("详情", "ghost", () => openTaskDetail(task.id)));
    if (statusName === "paused") {
      actions.appendChild(makeButton("恢复", "primary", () => taskAction(task.id, "resume")));
      actions.appendChild(makeButton("取消", "ghost", () => taskAction(task.id, "cancel")));
      actions.appendChild(makeButton("删除", "ghost danger", () => removeTask(task.id)));
    } else if (ACTIVE_STATUSES.includes(statusName)) {
      if (statusName === "pending") {
        actions.appendChild(makeButton("置顶", "ghost", () => taskAction(task.id, "top")));
      }
      actions.appendChild(makeButton("暂停", "ghost", () => taskAction(task.id, "pause")));
      actions.appendChild(makeButton("取消", "ghost", () => taskAction(task.id, "cancel")));
    } else {
      if (statusName !== "done") {
        actions.appendChild(makeButton("重试", "ghost", () => taskAction(task.id, "retry")));
      }
      actions.appendChild(makeButton("删除", "ghost danger", () => removeTask(task.id)));
    }

    const percent = task.size > 0 ? Math.min(100, (task.downloaded / task.size) * 100) : 0;
    const shown = task.status === "done" ? 100 : percent;
    const speed = task.status === "downloading" ? liveSpeed(task.id) : 0;
    const remaining = Math.max(0, Number(task.size) - Number(task.downloaded));
    const row = taskBody({
      lead,
      name,
      status,
      actions,
      size: fmtSizePair(task.downloaded, task.size),
      speed: speed > 0 ? `${fmtSize(speed)}/s` : "",
      eta: speed > 0 && remaining > 0 ? fmtEta(remaining / speed) : "",
      pct: fmtPercent(shown),
    });
    card.appendChild(row);

    // 组内文件不再逐行画进度条：父行的一条总进度已经表达整体，
    // 子行靠百分比列区分，避免一层套一层的卡片感。
    if (!compact) {
      const bar = document.createElement("div");
      bar.className = `bar${task.status === "failed" ? " failed" : ""}${live ? " live" : ""}`;
      const fill = document.createElement("i");
      fill.style.width = `${shown}%`;
      bar.appendChild(fill);
      card.appendChild(bar);
    }

    // 保存路径只在顶层任务里显示：文件夹内每个文件的落点都一样，重复没有意义
    if (!compact && task.dst_path) {
      const path = document.createElement("div");
      path.className = "m-path";
      path.textContent = task.dst_path;
      path.title = task.dst_path;
      card.appendChild(path);
    }

    if (task.error) {
      const error = document.createElement("div");
      error.className = "task-error";
      error.textContent = task.error;
      card.appendChild(error);
    }

    return card;
  }

  function attachDrag(card, task) {
    card.draggable = true;
    card.addEventListener("dragstart", (event) => {
      state.dragId = task.id;
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      try {
        event.dataTransfer.setData("text/plain", String(task.id));
      } catch (_) {
        /* 部分浏览器限制 setData */
      }
    });
    card.addEventListener("dragend", () => {
      state.dragId = null;
      card.classList.remove("dragging");
      clearDropMarks();
    });
    card.addEventListener("dragover", (event) => {
      if (state.dragId === null || state.dragId === task.id) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      const rect = card.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      clearDropMarks();
      card.classList.add(after ? "drop-after" : "drop-before");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drop-before", "drop-after"));
    card.addEventListener("drop", (event) => {
      event.preventDefault();
      const after = card.classList.contains("drop-after");
      const dragId = state.dragId;
      clearDropMarks();
      moveTask(dragId, task.id, after);
    });
  }

  function clearDropMarks() {
    document.querySelectorAll(".task.drop-before, .task.drop-after").forEach((el) => {
      el.classList.remove("drop-before", "drop-after");
    });
  }

  async function moveTask(dragId, targetId, after) {
    if (dragId === null || dragId === targetId) return;
    const ids = pendingOrder();
    const from = ids.indexOf(dragId);
    if (from < 0) return;
    ids.splice(from, 1);
    let to = ids.indexOf(targetId);
    if (to < 0) return;
    if (after) to += 1;
    ids.splice(to, 0, dragId);
    state.dragId = null;
    const total = ids.length;
    ids.forEach((id, index) => {
      const task = state.tasks.get(id);
      if (task) task.priority = total - index;
    });
    renderTasks();
    try {
      await api("/api/tasks/reorder", { method: "POST", body: JSON.stringify({ ids }) });
    } catch (error) {
      showToast(error.message, "bad");
      loadTasks().catch(() => {});
    }
  }

  function makeButton(text, cls, handler) {
    const button = document.createElement("button");
    button.className = `btn tiny ${cls}`;
    button.textContent = text;
    button.addEventListener("click", handler);
    return button;
  }

  let detailTaskId = null;

  function openTaskDetail(id) {
    detailTaskId = id;
    renderTaskDetail();
    const dialog = $("task-detail-dialog");
    if (dialog && !dialog.open) dialog.showModal();
  }

  function renderTaskDetail() {
    const body = $("detail-body");
    if (!body) return;
    const task = detailTaskId === null ? null : state.tasks.get(detailTaskId);
    if (!task) {
      detailTaskId = null;
      const dialog = $("task-detail-dialog");
      if (dialog && dialog.open) dialog.close();
      return;
    }

    $("detail-title").textContent = task.name || "任务详情";
    body.innerHTML = "";

    const size = Number(task.size) || 0;
    const downloaded = Number(task.downloaded) || 0;
    const percent = size > 0 ? Math.min(100, (downloaded / size) * 100) : task.status === "done" ? 100 : 0;

    const chips = document.createElement("div");
    chips.className = "detail-chips";
    const status = document.createElement("span");
    status.className = `task-status ${task.status}`;
    status.textContent = STATUS_TEXT[task.status] || task.status;
    chips.appendChild(status);
    const percentText = document.createElement("span");
    percentText.className = "detail-percent";
    percentText.textContent = `${percent.toFixed(1)}%`;
    chips.appendChild(percentText);
    body.appendChild(chips);

    const bar = document.createElement("div");
    const detailLive = task.status === "resolving" || task.status === "downloading" || task.status === "verifying";
    bar.className = `bar${task.status === "failed" ? " failed" : ""}${detailLive ? " live" : ""}`;
    const fill = document.createElement("i");
    fill.style.width = `${task.status === "done" ? 100 : percent}%`;
    bar.appendChild(fill);
    body.appendChild(bar);

    const stats = document.createElement("div");
    stats.className = "detail-stats";
    const rows = [
      ["已下载", `${fmtSize(downloaded)} / ${fmtSize(size)}`],
      ["重试次数", `${Number(task.retry_count) || 0} 次`],
      ["创建时间", fmtTime(task.created_at)],
      ["更新时间", fmtTime(task.updated_at)],
    ];
    if (task.status === "downloading") {
      const speed = liveSpeed(task.id);
      if (speed > 0) rows.splice(1, 0, ["当前速度", `${fmtSize(speed)}/s`]);
    }
    rows.forEach(([label, value]) => {
      const row = document.createElement("div");
      row.className = "detail-row";
      const key = document.createElement("span");
      key.textContent = label;
      const val = document.createElement("b");
      val.textContent = value;
      row.appendChild(key);
      row.appendChild(val);
      stats.appendChild(row);
    });
    body.appendChild(stats);

    if (task.error) {
      const error = document.createElement("div");
      error.className = "task-error detail-error";
      error.textContent = task.error;
      body.appendChild(error);
    }

    const path = document.createElement("div");
    path.className = "detail-path";
    path.textContent = task.dst_path || "";
    path.title = task.dst_path || "";
    body.appendChild(path);

    const section = document.createElement("div");
    section.className = "detail-segments";
    const head = document.createElement("div");
    head.className = "detail-section-title";
    const segments = Array.isArray(task.segments) ? task.segments : [];
    head.textContent = `分段进度（${segments.length}）`;
    section.appendChild(head);

    if (segments.length) {
      segments.forEach((seg, index) => {
        const length = Math.max(1, (Number(seg.end) - Number(seg.start) + 1) || 1);
        const done = Math.min(length, Number(seg.done) || 0);
        const row = document.createElement("div");
        row.className = "segment-row";
        const label = document.createElement("span");
        label.className = "segment-label";
        label.textContent = `#${index + 1}`;
        const segBar = document.createElement("div");
        segBar.className = "segment-bar";
        const segFill = document.createElement("i");
        segFill.style.width = `${(done / length) * 100}%`;
        segBar.appendChild(segFill);
        const value = document.createElement("span");
        value.className = "segment-value";
        value.textContent = `${fmtSize(done)} / ${fmtSize(length)}`;
        row.appendChild(label);
        row.appendChild(segBar);
        row.appendChild(value);
        section.appendChild(row);
      });
    } else {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent =
        task.status === "done"
          ? "已完成，分段信息已清理。"
          : "暂无分段信息（尚未开始、文件小于 4 MB 或单段下载）。";
      section.appendChild(empty);
    }
    body.appendChild(section);
  }

  const detailDialog = $("task-detail-dialog");
  if (detailDialog) {
    detailDialog.addEventListener("close", () => {
      detailTaskId = null;
    });
  }

  async function taskAction(id, action) {
    try {
      await api(`/api/tasks/${id}/${action}`, { method: "POST" });
      if (action === "retry") {
        const task = state.tasks.get(id);
        if (task) {
          task.status = "pending";
          task.error = null;
          renderTasks();
        }
      }
      if (action === "top") {
        await loadTasks();
        showToast("已置顶，将在当前任务结束后优先下载");
      }
      saveSession();
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  // ------------------------------------------------------------ 删除任务

  const DELETABLE = ["done", "failed", "canceled", "skipped"];
  let deleteResolve = null;

  function confirmDelete(title, message) {
    $("delete-title").textContent = title;
    $("delete-msg").textContent = message;
    const dialog = $("delete-dialog");
    if (dialog && !dialog.open) dialog.showModal();
    return new Promise((resolve) => {
      deleteResolve = resolve;
    });
  }

  function settleDelete(choice) {
    const dialog = $("delete-dialog");
    if (dialog && dialog.open) dialog.close();
    const resolve = deleteResolve;
    deleteResolve = null;
    if (resolve) resolve(choice);
  }

  $("btn-delete-cancel").addEventListener("click", () => settleDelete(null));
  $("btn-delete-record").addEventListener("click", () => settleDelete("record"));
  $("btn-delete-files").addEventListener("click", () => settleDelete("files"));
  $("delete-dialog").addEventListener("close", () => settleDelete(null));

  // 统计待删除记录数，以及删除本地文件可以释放的空间
  function deletableStats(ids = null) {
    const list = (ids && ids.length ? ids.map((id) => state.tasks.get(id)) : Array.from(state.tasks.values()))
      .filter((task) => task && DELETABLE.includes(task.status));
    const size = list.reduce(
      (sum, task) => sum + (task.status === "done" ? Number(task.size) || 0 : Number(task.downloaded) || 0),
      0
    );
    return { count: list.length, size };
  }

  function deletePrompt(title, stats, name = "") {
    const head = name ? `「${name}」` : `${stats.count} 个任务`;
    const fileHint = stats.size > 0
      ? `若同时删除本地文件，可释放约 ${fmtSize(stats.size)} 磁盘空间，`
      : "本地文件也会一并删除，";
    return confirmDelete(title, `${head}的任务记录将被移除。${fileHint}删除后无法恢复。`);
  }

  // 删除后闪一下「+40.0 MB」：GB 取整的剩余值几乎不动，这枚增量才是即时反馈
  let diskDeltaTimer = null;
  function flashDiskDelta(bytes) {
    const el = $("disk-delta");
    if (!el || !(Number(bytes) > 0)) return;
    el.textContent = `+${fmtSize(bytes)}`;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add("show"));
    if (diskDeltaTimer) clearTimeout(diskDeltaTimer);
    diskDeltaTimer = setTimeout(() => {
      el.classList.remove("show");
      diskDeltaTimer = setTimeout(() => {
        el.hidden = true;
      }, 220);
    }, 4000);
  }

  // Windows 回收被删文件的簇有时会滞后几秒到几十秒，只读一次容易停在旧数字上。
  // 删除后先读一次，再在随后一分多钟里补读几次，剩余空间涨到位就提前收手。
  let diskSettleTimers = [];
  async function refreshDiskAfterDelete(freed) {
    diskSettleTimers.forEach((id) => clearTimeout(id));
    diskSettleTimers = [];
    const before = diskFree;
    const expect = before > 0 && Number(freed) > 0 ? before + Number(freed) * 0.8 : null;
    const info = await loadDisk();
    if (!expect || (info && Number(info.free) >= expect)) return;
    [3000, 8000, 20000, 45000, 90000].forEach((delay) => {
      diskSettleTimers.push(
        setTimeout(async () => {
          const next = await loadDisk();
          if (!next || Number(next.free) >= expect) {
            diskSettleTimers.forEach((id) => clearTimeout(id));
            diskSettleTimers = [];
          }
        }, delay)
      );
    });
  }

  function deleteToast(result, withFiles) {
    const count = typeof result.ok === "number" ? result.ok : 1;
    // 删除动作结束就刷新磁盘：无论文件是否真的被移除，底部用量都要和实际一致
    refreshDiskAfterDelete(result.freed);
    if (result.files_removed > 0) {
      flashDiskDelta(result.freed);
      showToast(`已删除 ${result.files_removed} 个文件，释放 ${fmtSize(result.freed)}`, "ok");
    } else if (withFiles) {
      showToast("任务已移除，本地没有找到对应文件", "ok");
    } else {
      showToast(`已移除 ${count} 个任务记录`, "ok");
    }
  }

  async function removeTask(id) {
    const task = state.tasks.get(id);
    let withFiles = false;
    if (task && task.dst_path && !ACTIVE_STATUSES.includes(task.status)) {
      const choice = await deletePrompt("删除任务", deletableStats([id]), task.name || "");
      if (!choice) return;
      withFiles = choice === "files";
    }
    try {
      const query = withFiles ? "?delete_files=true" : "";
      const result = await api(`/api/tasks/${id}${query}`, { method: "DELETE" });
      state.tasks.delete(id);
      state.speeds.delete(id);
      if (detailTaskId === id) {
        detailTaskId = null;
        const dialog = $("task-detail-dialog");
        if (dialog && dialog.open) dialog.close();
      }
      renderTasks();
      deleteToast(result, withFiles);
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  async function batchAction(action, ids = null) {
    const payload = { action };
    if (ids && ids.length) payload.ids = ids;
    if (action === "delete") {
      const stats = deletableStats(ids);
      if (stats.count === 0) {
        showToast("没有可清理的任务");
        return;
      }
      const choice = await deletePrompt("清理任务", stats);
      if (!choice) return;
      payload.delete_files = choice === "files";
    }
    try {
      const result = await api("/api/tasks/batch", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (result.ok > 0) {
        await loadTasks();
        if (action === "delete") {
          deleteToast(result, payload.delete_files);
        } else {
          showToast(`已处理 ${result.ok} 个任务${result.failed ? `，${result.failed} 个失败` : ""}`, "ok");
        }
      } else {
        showToast("没有可执行的任务");
      }
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  $("btn-pause-all").addEventListener("click", () => batchAction("pause"));
  $("btn-resume-all").addEventListener("click", () => batchAction("resume"));
  $("btn-retry-all").addEventListener("click", () => batchAction("retry"));
  $("btn-cancel-all").addEventListener("click", () => batchAction("cancel"));

  $("task-search").addEventListener("input", (event) => {
    state.taskSearch = event.target.value.trim();
    renderTasks();
    saveSession();
  });

  $("task-status-filter").addEventListener("change", (event) => {
    state.taskStatus = event.target.value;
    renderTasks();
    saveSession();
  });

  $("btn-clear").addEventListener("click", async () => {
    const stats = deletableStats();
    if (stats.count === 0) {
      showToast("没有可清除的任务");
      return;
    }
    const choice = await deletePrompt("清除已完成", stats);
    if (!choice) return;
    const withFiles = choice === "files";
    try {
      const query = withFiles ? "?delete_files=true" : "";
      const result = await api(`/api/tasks/clear-finished${query}`, { method: "POST" });
      if (result.removed > 0) {
        await loadTasks();
        // 清除完成同样要刷新底部磁盘用量，让释放的空间立刻可见
        refreshDiskAfterDelete(result.freed);
        if (result.files_removed > 0) {
          flashDiskDelta(result.freed);
          showToast(
            `已清除 ${result.removed} 个任务，删除 ${result.files_removed} 个文件，释放 ${fmtSize(result.freed)}`,
            "ok"
          );
        } else {
          showToast(`已清除 ${result.removed} 个任务`, "ok");
        }
      } else {
        showToast("没有可清除的任务");
      }
    } catch (error) {
      showToast(error.message, "bad");
    }
  });

  $("btn-stats").addEventListener("click", async () => {
    const dialog = $("stats-dialog");
    if (dialog && !dialog.open) dialog.showModal();
    try {
      renderStats(await api("/api/tasks/stats"));
    } catch (error) {
      showToast(error.message, "bad");
    }
  });

  $("btn-export-csv").addEventListener("click", () => {
    window.location.href = "/api/tasks/export.csv";
  });

  function renderStats(data) {
    const counts = data.status_counts || {};
    $("stat-today").textContent = fmtSize(data.today.bytes);
    $("stat-today-count").textContent = `${data.today.count} 个任务`;
    $("stat-total").textContent = fmtSize(data.total.bytes);
    $("stat-total-count").textContent = `${data.total.count} 个任务`;
    $("stat-rate").textContent = `${Math.round((data.success_rate || 0) * 100)}%`;
    $("stat-counts").textContent = `完成 ${counts.done || 0} / 失败 ${counts.failed || 0}`;
    $("stat-failed").textContent = `${counts.failed || 0}`;

    const bars = $("stats-bars");
    bars.innerHTML = "";
    const days = data.days || [];
    const max = Math.max(1, ...days.map((day) => day.bytes || 0));
    days.forEach((day) => {
      const column = document.createElement("div");
      column.className = "stats-bar";
      column.title = `${day.date}：${fmtSize(day.bytes)}（${day.count} 个）`;
      const track = document.createElement("div");
      track.className = "stats-bar-track";
      const fill = document.createElement("i");
      const height = day.bytes > 0 ? Math.max(6, (day.bytes / max) * 100) : 0;
      fill.style.height = `${height}%`;
      track.appendChild(fill);
      const label = document.createElement("span");
      label.textContent = day.date.slice(5);
      column.appendChild(track);
      column.appendChild(label);
      bars.appendChild(column);
    });

    const failures = $("stats-failures");
    failures.innerHTML = "";
    if (!(data.failures || []).length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "暂无失败任务。";
      failures.appendChild(empty);
      return;
    }
    data.failures.forEach((item) => {
      const row = document.createElement("div");
      row.className = "stats-failure";
      const text = document.createElement("span");
      text.textContent = item.error;
      text.title = item.error;
      const count = document.createElement("b");
      count.textContent = `${item.count} 次`;
      row.appendChild(text);
      row.appendChild(count);
      failures.appendChild(row);
    });
  }

  // 最近一次读到的剩余空间，删除后用它判断系统有没有把空间真的还回来
  let diskFree = 0;

  async function loadDisk() {
    const hint = $("disk-hint");
    if (!hint) return null;
    try {
      const info = await api("/api/settings/disk");
      const total = Number(info.total) || 0;
      const used = Math.max(0, Number(info.used) || 0);
      hint.textContent = `剩余 ${fmtSize(info.free)} · 共 ${fmtSize(total)}`;
      hint.title = info.below_watermark
        ? `已用 ${fmtSize(used)}，低于 ${info.low_watermark_gb} GB 保留水位，新任务会因空间不足失败`
        : `已用 ${fmtSize(used)}，剩余 ${fmtSize(info.free)}`;
      hint.className = `disk-hint${info.below_watermark ? " bad" : ""}`;
      const meter = $("disk-meter");
      if (meter) meter.className = `disk-meter${info.below_watermark ? " bad" : ""}`;
      const fill = $("disk-fill");
      if (fill) fill.style.width = `${total > 0 ? Math.min(100, (used / total) * 100) : 0}%`;
      diskFree = Number(info.free) || 0;
      return info;
    } catch (_) {
      hint.textContent = "";
      return null;
    }
  }

  let riskTimer = null;

  function showRisk(risk) {
    const banner = $("risk-banner");
    if (!banner) return;
    if (!risk || !risk.active) {
      banner.hidden = true;
      if (riskTimer) {
        clearInterval(riskTimer);
        riskTimer = null;
      }
      return;
    }
    let seconds = Math.max(0, Number(risk.seconds) || 0);
    const text = $("risk-text");
    const count = $("risk-count");
    text.textContent = `已降至 ${risk.concurrency ?? 1} 并发 / ${risk.segments ?? 1} 分段，冷却后自动重试`;
    const paint = () => {
      count.textContent = seconds > 0 ? `${seconds} 秒后恢复` : "正在恢复…";
    };
    paint();
    banner.hidden = false;
    if (riskTimer) clearInterval(riskTimer);
    riskTimer = setInterval(() => {
      seconds -= 1;
      if (seconds <= 0) {
        clearInterval(riskTimer);
        riskTimer = null;
        count.textContent = "正在恢复…";
        loadTasks().catch(() => {});
        return;
      }
      paint();
    }, 1000);
  }

  async function loadTasks() {
    const result = await api("/api/tasks");
    state.tasks.clear();
    (result.tasks || []).forEach((task) => state.tasks.set(task.id, task));
    showRisk(result.risk);
    renderTasks();
  }

  // ------------------------------------------------------------ 速度与曲线

  function noteProgress(data) {
    const now = performance.now();
    const previous = state.speeds.get(data.id);
    const downloaded = Number(data.downloaded) || 0;
    if (!previous) {
      state.speeds.set(data.id, { bytes: downloaded, t: now, speed: 0 });
      return;
    }
    const elapsed = (now - previous.t) / 1000;
    if (elapsed < 0.2) return;
    const delta = downloaded - previous.bytes;
    if (delta < 0) {
      state.speeds.set(data.id, { bytes: downloaded, t: now, speed: 0 });
      return;
    }
    const instant = delta / elapsed;
    const speed = previous.speed > 0 ? previous.speed * 0.65 + instant * 0.35 : instant;
    state.speeds.set(data.id, { bytes: downloaded, t: now, speed });
  }

  function liveSpeed(id) {
    const entry = state.speeds.get(id);
    if (!entry) return 0;
    if (performance.now() - entry.t > SPEED_STALE_MS) return 0;
    return entry.speed;
  }

  function totalSpeed() {
    let total = 0;
    state.tasks.forEach((task) => {
      if (ACTIVE_STATUSES.includes(task.status)) total += liveSpeed(task.id);
    });
    return total;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach((key) => el.setAttribute(key, attrs[key]));
    return el;
  }

  // 速度曲线节点只创建一次，之后仅改属性：避免每次采样重建 DOM 导致
  // 光点脉冲动画被重置、渐变与阴影反复重绘（表现为曲线「跳一下」）。
  const spark = {
    svg: null,
    area: null,
    line: null,
    dot: null,
    max: 64 * 1024,
  };

  function buildSpark(svg) {
    const defs = svgEl("defs", {});
    const strokeGrad = svgEl("linearGradient", { id: "sparkStroke", x1: "0%", y1: "0%", x2: "100%", y2: "0%" });
    strokeGrad.appendChild(svgEl("stop", { offset: "0%", "stop-color": "rgba(34,211,238,.15)" }));
    strokeGrad.appendChild(svgEl("stop", { offset: "55%", "stop-color": "#22d3ee" }));
    strokeGrad.appendChild(svgEl("stop", { offset: "100%", "stop-color": "#a78bfa" }));
    const fillGrad = svgEl("linearGradient", { id: "sparkFill", x1: "0%", y1: "0%", x2: "0%", y2: "100%" });
    fillGrad.appendChild(svgEl("stop", { offset: "0%", "stop-color": "rgba(34,211,238,.30)" }));
    fillGrad.appendChild(svgEl("stop", { offset: "100%", "stop-color": "rgba(34,211,238,0)" }));
    defs.appendChild(strokeGrad);
    defs.appendChild(fillGrad);

    const axis = svgEl("line", { x1: 0, y1: 48 - 0.5, x2: 300, y2: 48 - 0.5, class: "spark-axis" });
    spark.area = svgEl("path", { d: "", class: "spark-area" });
    spark.line = svgEl("path", { d: "", class: "spark-line" });
    spark.dot = svgEl("circle", { cx: 0, cy: 0, r: 2.2, class: "spark-dot", visibility: "hidden" });
    svg.textContent = "";
    [defs, axis, spark.area, spark.line, spark.dot].forEach((node) => svg.appendChild(node));
    spark.svg = svg;
    spark.max = 64 * 1024;
  }

  // 纵轴上限取 1/2/5×10^n 的档位，避免采样值小幅波动时整条曲线持续缩放
  function niceCeil(value) {
    const exp = Math.floor(Math.log10(Math.max(1, value)));
    const base = Math.pow(10, exp);
    const step = value / base;
    return (step <= 1 ? 1 : step <= 2 ? 2 : step <= 5 ? 5 : 10) * base;
  }

  function drawSpark() {
    const svg = $("speed-chart");
    if (!svg) return;
    if (spark.svg !== svg) buildSpark(svg);
    const W = 300;
    const H = 48;
    const PAD = 2;
    const now = Date.now();
    const samples = state.samples;
    while (samples.length && now - samples[0].t > HISTORY_MS) samples.shift();

    if (samples.length < 2) {
      spark.area.setAttribute("d", "");
      spark.line.setAttribute("d", "");
      spark.dot.setAttribute("visibility", "hidden");
      spark.max = 64 * 1024;
      svg.parentElement.classList.add("empty");
      return;
    }
    svg.parentElement.classList.remove("empty");
    spark.dot.setAttribute("visibility", "visible");

    let peak = 0;
    samples.forEach((sample) => {
      if (sample.v > peak) peak = sample.v;
    });
    // 上限按档位取整，且峰值跌破当前上限一半才降档：
    // 否则每 2 秒重算上限会让整条曲线跟着上下缩放，看起来像在抖。
    const target = Math.max(64 * 1024, niceCeil(peak * 1.1));
    if (target >= spark.max || peak * 2 < spark.max) spark.max = target;

    let path = "";
    samples.forEach((sample, index) => {
      const x = W - ((now - sample.t) / HISTORY_MS) * W;
      const y = H - PAD - (sample.v / spark.max) * (H - PAD * 2);
      path += `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)} `;
    });
    const firstX = Math.max(0, W - ((now - samples[0].t) / HISTORY_MS) * W);
    const area = `${path}L${W},${H} L${firstX.toFixed(1)},${H} Z`;
    spark.area.setAttribute("d", area);
    spark.line.setAttribute("d", path);

    const last = samples[samples.length - 1];
    const lastX = Math.min(W - 3, Math.max(0, W - ((now - last.t) / HISTORY_MS) * W));
    const lastY = H - PAD - (last.v / spark.max) * (H - PAD * 2);
    spark.dot.setAttribute("cx", lastX.toFixed(1));
    spark.dot.setAttribute("cy", lastY.toFixed(1));
  }

  // 瞬时速度抖动大，采样值做指数平滑后入曲线，避免折线呈锯齿
  let smoothSpeed = null;

  function sampleSpeed() {
    const value = totalSpeed();
    smoothSpeed = smoothSpeed === null ? value : smoothSpeed * 0.55 + value * 0.45;
    state.samples.push({ t: Date.now(), v: smoothSpeed });
    drawSpark();
    saveSpeedSamples();
    const label = $("speed-now");
    if (label) label.textContent = value > 0 ? `${fmtSize(value)}/s` : "0 B/s";
  }

  // 浏览器本地缓存最近 10 分钟采样：刷新后无需等待接口就能立即回显曲线。
  function restoreSpeedSamples() {
    try {
      const raw = localStorage.getItem(SPEED_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (!Array.isArray(saved)) return;
      const cutoff = Date.now() - HISTORY_MS;
      state.samples = saved
        .map((item) => ({ t: Number(item.t), v: Number(item.v) }))
        .filter((item) => Number.isFinite(item.t) && Number.isFinite(item.v) && item.t >= cutoff)
        .sort((a, b) => a.t - b.t);
      if (state.samples.length) smoothSpeed = state.samples[state.samples.length - 1].v;
    } catch (_) {
      /* 隐私模式下不可读，忽略 */
    }
  }

  function saveSpeedSamples() {
    try {
      localStorage.setItem(SPEED_KEY, JSON.stringify(state.samples));
    } catch (_) {
      /* 配额已满或不可写时忽略，仅影响刷新回放 */
    }
  }

  // 首次加载时从服务端补回历史速度：刷新/重启后曲线不再从空白开始。
  async function loadSpeedHistory() {
    try {
      const seconds = Math.ceil(HISTORY_MS / 1000);
      const result = await api(`/api/tasks/speed?seconds=${seconds}`);
      const cutoff = Date.now() - HISTORY_MS;
      const history = (result.samples || [])
        .map((item) => ({ t: Number(item.t), v: Number(item.v) }))
        .filter((item) => item.t >= cutoff);
      if (!history.length) return;
      const local = state.samples.filter((item) => item.t >= cutoff);
      // 服务端与本地缓存都覆盖最近十分钟，按秒去重，避免曲线点重复堆叠。
      const merged = new Map();
      history.forEach((item) => merged.set(Math.round(item.t / 1000), item));
      local.forEach((item) => merged.set(Math.round(item.t / 1000), item));
      state.samples = Array.from(merged.values()).sort((a, b) => a.t - b.t);
      smoothSpeed = state.samples[state.samples.length - 1].v;
      drawSpark();
      saveSpeedSamples();
    } catch (_) {
      /* 历史速度不可用时忽略，仅影响曲线回放 */
    }
  }

  // ------------------------------------------------------------ 设置

  function cfgValue(key, fallback) {
    if (state.cfg && state.cfg[key] !== undefined) return state.cfg[key];
    return fallback;
  }

  function applySettings(values) {
    state.cfg = values || {};
    $("set-segments").value = state.cfg.segments ?? 4;
    $("set-concurrency").value = state.cfg.concurrency ?? 2;
    $("set-conflict").value = state.cfg.on_conflict || "skip";
    $("set-queue-order").value = state.cfg.queue_order || "time";
    $("set-cleanup-days").value = state.cfg.cleanup_days ?? 0;
    $("set-watermark").value = state.cfg.low_watermark_gb ?? 0;
    $("set-speed").value = state.cfg.speed_limit_mbps ?? 0;
    $("set-schedule").checked = !!state.cfg.schedule_enabled;
    $("set-schedule-start").value = state.cfg.schedule_start || "23:00";
    $("set-schedule-end").value = state.cfg.schedule_end || "07:00";
    $("set-retry-count").value = state.cfg.auto_retry_count ?? 0;
    $("set-retry-backoff").value = state.cfg.auto_retry_backoff ?? 30;
    $("set-tmdb-key").value = state.cfg.tmdb_api_key || "";
    $("set-tmdb-hosts").value = state.cfg.tmdb_hosts_url || "";
    $("set-tmdb-lang").value = state.cfg.tmdb_language || "zh-CN";
    $("set-tmdb-aliases").value = state.cfg.tmdb_aliases || "";
  }

  async function loadSettings(show) {
    try {
      const info = await api("/api/settings");
      applySettings(info.values || {});
      $("set-root").textContent = info.download_root || "";
      const message = $("settings-msg");
      message.textContent = "";
      message.className = "dialog-msg";
      if (show && !$("settings-dialog").open) $("settings-dialog").showModal();
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  function exportSettings() {
    const payload = {
      app: "QuarkNas",
      version: 1,
      exported_at: new Date().toISOString(),
      values: state.cfg || {},
    };
    const stamp = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const name = `quarknas-settings-${stamp.getFullYear()}${pad(stamp.getMonth() + 1)}${pad(stamp.getDate())}.json`;
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast("设置文件已导出", "ok");
  }

  async function importSettings(file) {
    const message = $("settings-msg");
    let values = null;
    try {
      const data = JSON.parse(await file.text());
      if (data && typeof data === "object" && !Array.isArray(data)) {
        values = data.values && typeof data.values === "object" ? data.values : data;
      }
    } catch (_) {
      values = null;
    }
    if (!values || Array.isArray(values)) {
      message.textContent = "无法解析设置文件，请选择导出的 JSON";
      message.className = "dialog-msg bad";
      return;
    }
    message.textContent = "导入中…";
    message.className = "dialog-msg";
    try {
      const result = await api("/api/settings", { method: "PUT", body: JSON.stringify(values) });
      applySettings(result.values || values);
      message.textContent = "设置已导入并生效";
      message.className = "dialog-msg ok";
      showToast("设置已导入", "ok");
    } catch (error) {
      message.textContent = error.message;
      message.className = "dialog-msg bad";
    }
  }

  $("btn-settings-export").addEventListener("click", exportSettings);
  $("btn-settings-import").addEventListener("click", () => $("settings-file").click());
  $("settings-file").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (file) await importSettings(file);
  });

  $("btn-settings").addEventListener("click", () => loadSettings(true));

  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("settings-msg");
    const button = $("btn-settings-save");
    const payload = {
      segments: Number($("set-segments").value),
      concurrency: Number($("set-concurrency").value),
      on_conflict: $("set-conflict").value,
      queue_order: $("set-queue-order").value,
      cleanup_days: Number($("set-cleanup-days").value),
      low_watermark_gb: Number($("set-watermark").value),
      speed_limit_mbps: Number($("set-speed").value),
      schedule_enabled: $("set-schedule").checked,
      schedule_start: $("set-schedule-start").value,
      schedule_end: $("set-schedule-end").value,
      auto_retry_count: Number($("set-retry-count").value),
      auto_retry_backoff: Number($("set-retry-backoff").value),
      tmdb_api_key: $("set-tmdb-key").value.trim(),
      tmdb_hosts_url: $("set-tmdb-hosts").value.trim(),
      tmdb_language: $("set-tmdb-lang").value,
      tmdb_aliases: $("set-tmdb-aliases").value.trim(),
    };

    button.disabled = true;
    message.textContent = "保存中…";
    message.className = "dialog-msg";
    try {
      const result = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      state.cfg = result.values || state.cfg;
      message.textContent = "设置已保存";
      message.className = "dialog-msg ok";
      showToast("设置已保存", "ok");
      setTimeout(() => $("settings-dialog").close(), 500);
    } catch (error) {
      message.textContent = error.message;
      message.className = "dialog-msg bad";
    } finally {
      button.disabled = false;
    }
  });

  // ------------------------------------------------------------ 日志

  let logTimer = null;

  async function loadLogs() {
    const level = $("log-level").value;
    const keyword = $("log-filter").value.trim();
    const params = new URLSearchParams({ limit: "300" });
    if (level) params.set("level", level);
    if (keyword) params.set("q", keyword);
    const list = $("log-list");
    list.innerHTML = `<div class="empty">加载中…</div>`;
    try {
      const result = await api(`/api/logs?${params.toString()}`);
      const items = result.items || [];
      if (items.length === 0) {
        list.innerHTML = `<div class="empty">没有匹配的日志</div>`;
        return;
      }
      list.innerHTML = "";
      items.forEach((item) => {
        const row = document.createElement("div");
        row.className = `log-row ${String(item.level || "").toLowerCase()}`;
        const meta = document.createElement("span");
        meta.className = "log-meta";
        meta.textContent = `${item.time} ${item.level} ${item.logger}`;
        const text = document.createElement("span");
        text.className = "log-text";
        text.textContent = item.message;
        row.appendChild(meta);
        row.appendChild(text);
        list.appendChild(row);
      });
    } catch (error) {
      list.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
    }
  }

  $("btn-logs").addEventListener("click", () => {
    if (!$("logs-dialog").open) $("logs-dialog").showModal();
    loadLogs();
  });

  $("btn-log-refresh").addEventListener("click", loadLogs);
  $("log-level").addEventListener("change", loadLogs);
  $("log-filter").addEventListener("input", () => {
    if (logTimer) clearTimeout(logTimer);
    logTimer = setTimeout(loadLogs, 300);
  });

  $("btn-diagnostics").addEventListener("click", () => {
    window.location.href = "/api/diagnostics";
  });

  $("rename-form").addEventListener("submit", submitRename);
  $("folder-form").addEventListener("submit", submitFolder);
  $("btn-new-folder").addEventListener("click", openFolderDialog);
  $("btn-move-here").addEventListener("click", submitMove);
  $("move-up").addEventListener("click", () => {
    if (moveStack.length <= 1) return;
    moveStack.pop();
    loadMoveList();
  });
  $("btn-rm-confirm").addEventListener("click", submitDelete);

  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = $(button.dataset.close);
      if (dialog && dialog.open) dialog.close();
    });
  });

  // ------------------------------------------------------------ 影视整理

  // 预演结果放在模块内：扫描 → 可编辑 → 分块执行，不进入会话持久化。
  const org = { items: [], picked: new Set(), busy: false, page: 1, retry: [], applying: false };
  const ORG_CHUNK = 10;
  // 结果可能上千条，表格只渲染当前页，勾选状态按 fid 保留在整份结果里。
  const ORG_PAGE_SIZE = 50;
  // 片名校准分批进行：每批 8 条，服务端限制真正发请求的次数，避免请求过久。
  const ORG_ENRICH_BATCH = 8;
  // 没查到的条目排到队尾重试一次，重试还不行就用本地识别结果。
  const ORG_ENRICH_RETRY = 1;
  let orgEnrichToken = 0;
  // 每次关闭弹窗都作废上一轮扫描/校正的回包，避免关掉后又被旧结果填满。
  let orgGen = 0;

  function orgOpen() {
    if (!$("organize-dialog").open) $("organize-dialog").showModal();
    // 执行整理中不要重置，否则会把正在处理的结果列表清掉。
    if (org.applying) return;
    orgReset();
    $("org-path").textContent = currentPath();
    // 只有配了 TMDB Key 才能校正片名，没配就禁用勾选框并说明原因。
    const hasKey = !!(state.cfg && String(state.cfg.tmdb_api_key || "").trim());
    const meta = $("org-meta");
    meta.disabled = !hasKey;
    meta.checked = hasKey;
    meta.title = hasKey ? "" : "先在设置里填写 TMDB API Key";
    $("org-meta-hint").textContent = hasKey ? "" : "未配置 TMDB Key，无法校正片名";
    orgScan();
  }

  // 关窗即丢弃预演结果：目录可能已经变了，重新打开一律重新扫描。
  function orgReset() {
    orgGen += 1;
    orgEnrichToken += 1;
    org.items = [];
    org.picked = new Set();
    org.retry = [];
    org.page = 1;
    org.busy = false;
    $("org-msg").textContent = "";
    $("org-msg").className = "dialog-msg";
    $("org-progress").textContent = "";
    $("btn-org-scan").disabled = false;
    $("btn-org-apply").disabled = false;
    orgBarHide();
    renderOrg();
  }

  function orgSummary(result) {
    const bits = [`识别 ${result.videos ?? org.items.length} 个视频`];
    if (result.sidecars) bits.push(`字幕 ${result.sidecars} 个`);
    if (result.skipped) bits.push(`跳过 ${result.skipped} 个样片/预告`);
    if (result.truncated) bits.push("目录项超过 2 万，已停止遍历");
    return bits.join(" · ");
  }

  // 每条结果的校正状态：待校正 → 校正中 → 命中/未命中。
  const ORG_STATE_LABEL = {
    pending: "待校正",
    checking: "校正中",
    retry: "重试中",
    hit: "已命中",
    miss: "未命中",
    off: "未校正",
  };
  // 命中的具体判断路径：每次都实时查 TMDB。
  const ORG_MATCH_LABEL = {
    tmdb: "TMDB 命中",
  };

  // 进度面板顶部：阶段名 + 右侧统计，下面才是进度条。
  function orgStage(text, counts) {
    $("org-progress-panel").hidden = !text;
    $("org-stage").textContent = text || "";
    $("org-stage-counts").textContent = counts || "";
  }

  // mode: scan=扫描中，meta=片名校准，retry=校正重试，apply=执行整理。
  function orgBarRun(mode, ratio) {
    const bar = $("org-meta-bar");
    bar.classList.toggle("indeterminate", mode === "scan");
    bar.classList.toggle("retry", mode === "retry");
    bar.classList.toggle("apply", mode === "apply");
    const percent = Math.max(0, Math.min(1, ratio || 0)) * 100;
    bar.firstElementChild.style.width = mode === "scan" ? "" : `${Math.round(percent)}%`;
  }

  function orgBarHide() {
    orgStage("", "");
    const bar = $("org-meta-bar");
    bar.classList.remove("indeterminate", "retry", "apply");
    bar.firstElementChild.style.width = "0%";
  }

  function orgMetaCounts() {
    let hit = 0;
    let miss = 0;
    let pending = 0;
    org.items.forEach((item) => {
      if (item.source) hit += 1;
      else if (["pending", "checking", "retry"].includes(item.metaState)) pending += 1;
      else miss += 1;
    });
    return { total: org.items.length, hit, miss, pending };
  }

  // 校正进度：按已经出结论的条目数推进，同时把命中/未命中/待校正摊开显示。
  function orgMetaPaint(retrying) {
    const { total, hit, miss, pending } = orgMetaCounts();
    if (!total) {
      orgBarHide();
      return;
    }
    orgBarRun(retrying ? "retry" : "meta", (hit + miss) / total);
    orgStage(
      retrying ? "片名校准 · 重试队列" : "片名校准",
      `命中 ${hit} · 未命中 ${miss} · 待校正 ${pending}`
    );
  }

  // 校正接口按批处理，扫描先出结果，再在后台一批批校准片名。
  // 没查到的条目先挂起，等新条目都处理完，再排到队尾重试。
  async function orgEnrich(baseText) {
    const token = ++orgEnrichToken;
    const message = $("org-msg");
    let corrected = 0;
    orgMetaPaint(false);
    while (orgEnrichToken === token) {
      const fresh = org.items.filter((item) => !item.metaDone);
      let batch;
      if (fresh.length) {
        batch = fresh.slice(0, ORG_ENRICH_BATCH).map((item) => ({ item, retry: false }));
      } else if (org.retry.length) {
        // 重试的条目已经标记过 metaDone，不会再被当成新条目，只能从队尾取。
        batch = org.retry
          .splice(0, ORG_ENRICH_BATCH)
          .map((fid) => ({ item: org.items.find((one) => one.fid === fid), retry: true }))
          .filter((one) => one.item);
      } else {
        break;
      }
      if (!batch.length) break;
      // 先把本批标成「校正中」，让界面能看出正在查哪些条目。
      batch.forEach(({ item }) => {
        item.metaState = "checking";
      });
      renderOrg();
      orgMetaPaint(batch[0].retry);
      let result;
      try {
        result = await api("/api/organize/enrich", {
          method: "POST",
          body: JSON.stringify({
            items: batch.map(({ item }) => ({
              fid: item.fid,
              title: item.title,
              kind: item.kind,
              year: item.year,
              season: item.season,
              episode: item.episode,
              ext: item.ext,
            })),
          }),
        });
      } catch (_) {
        break;
      }
      if (orgEnrichToken !== token) break;

      if (result.error) {
        // Key 无效这类错误重试也没用，直接停下并说明原因。
        org.items.forEach((item) => {
          if (["pending", "checking", "retry"].includes(item.metaState)) item.metaState = "miss";
        });
        renderOrg();
        orgBarHide();
        message.textContent = `${baseText} · ${result.error}`;
        message.className = "dialog-msg bad";
        return;
      }

      const changed = (result.done || []).length + (result.updates || []).length;
      (result.done || []).forEach((fid) => {
        const item = org.items.find((one) => one.fid === fid);
        if (!item) return;
        item.metaDone = true;
        // 默认按「未命中」算，后面拿到 updates 的会改成命中。
        item.metaState = "miss";
      });
      // 没查到的放回队尾；重试次数用完就保留本地识别结果。
      (result.retry || []).forEach((fid) => {
        const item = org.items.find((one) => one.fid === fid);
        if (!item || org.retry.includes(fid)) return;
        item.metaTries = (item.metaTries || 0) + 1;
        if (item.metaTries <= ORG_ENRICH_RETRY) {
          item.metaState = "retry";
          org.retry.push(fid);
        }
      });
      (result.updates || []).forEach((update) => {
        const item = org.items.find((one) => one.fid === update.fid);
        if (!item) return;
        item.title = update.title;
        item.year = update.year;
        item.source = update.source;
        item.match = update.match || "";
        item.metaState = "hit";
        item.target_parts = update.target_parts || [];
        item.target_name = update.target_name;
        item.target_dir = item.target_parts.join("/");
        corrected += 1;
      });
      renderOrg();
      orgMetaPaint(batch[0].retry);
      message.textContent = batch[0].retry
        ? `${baseText} · 片名校准 ${corrected} 条（重试中）…`
        : `${baseText} · 片名校准 ${corrected} 条…`;
      if (!changed) break;
    }
    if (orgEnrichToken === token) {
      // 队列清空：还挂着「待校正/重试中」的就是最终没查到，落成未命中。
      org.items.forEach((item) => {
        if (["pending", "checking", "retry"].includes(item.metaState)) item.metaState = "miss";
      });
      renderOrg();
      orgBarHide();
      message.textContent = corrected ? `${baseText} · 片名校准 ${corrected} 条` : baseText;
    }
  }

  function syncOrgCount() {
    if (org.busy) return;
    $("org-progress").textContent = org.picked.size ? `已选 ${org.picked.size} 条` : "";
    const pickable = org.items.reduce((n, item) => n + (item.conflict ? 0 : 1), 0);
    const all = $("org-check-all");
    all.checked = pickable > 0 && org.picked.size >= pickable;
    all.indeterminate = org.picked.size > 0 && !all.checked;
  }

  function renderOrg() {
    const rows = $("org-rows");
    rows.innerHTML = "";
    $("org-empty").hidden = org.items.length > 0;

    const total = org.items.length;
    const pages = Math.max(1, Math.ceil(total / ORG_PAGE_SIZE));
    org.page = Math.min(Math.max(1, org.page), pages);
    const start = (org.page - 1) * ORG_PAGE_SIZE;
    const visible = org.items.slice(start, start + ORG_PAGE_SIZE);

    visible.forEach((item) => {
      const tr = document.createElement("tr");
      tr.dataset.fid = item.fid;
      // 命中元数据的行左侧加绿边，未命中的灰边，扫一眼就能分区。
      if (item.source) tr.classList.add("org-row-hit");
      else if (item.metaState === "miss") tr.classList.add("org-row-miss");

      const checkCell = document.createElement("td");
      checkCell.className = "col-check";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = org.picked.has(item.fid);
      box.disabled = !!item.conflict;
      box.addEventListener("change", () => {
        if (box.checked) org.picked.add(item.fid);
        else org.picked.delete(item.fid);
        syncOrgCount();
      });
      checkCell.appendChild(box);
      tr.appendChild(checkCell);

      const src = document.createElement("td");
      src.className = "org-src";
      const name = document.createElement("div");
      name.className = "org-name";
      name.textContent = item.name;
      name.title = item.name;
      const meta = document.createElement("small");
      meta.textContent = [fmtSize(item.size), item.sidecars.length ? `+${item.sidecars.length} 字幕` : ""]
        .filter(Boolean)
        .join(" · ");
      src.appendChild(name);
      src.appendChild(meta);
      // 准备改成的名字：和原名不同时单独标一行，方便扫一眼确认。
      if (item.target_name && item.target_name !== item.name) {
        const will = document.createElement("small");
        will.className = "org-willbe";
        will.textContent = `→ ${item.target_name}`;
        will.title = "整理后使用的文件名";
        src.appendChild(will);
      }
      tr.appendChild(src);

      const dst = document.createElement("td");
      dst.className = "org-dst";
      const dirInput = document.createElement("input");
      dirInput.className = "org-input";
      dirInput.value = item.target_dir;
      dirInput.title = "目标目录，用 / 分隔层级";
      const nameInput = document.createElement("input");
      nameInput.className = "org-input";
      nameInput.value = item.target_name;
      nameInput.title = "目标文件名";
      dirInput.addEventListener("input", () => {
        item.target_dir = dirInput.value.trim();
      });
      nameInput.addEventListener("input", () => {
        item.target_name = nameInput.value.trim();
      });
      dst.appendChild(dirInput);
      dst.appendChild(nameInput);
      if (item.conflict) {
        const warn = document.createElement("small");
        warn.className = "org-warn";
        warn.textContent = item.conflict_reason;
        dst.appendChild(warn);
      }
      tr.appendChild(dst);

      const info = document.createElement("td");
      info.className = "org-info";
      const tag = document.createElement("span");
      tag.className = `org-tag ${item.kind}${item.confidence === "low" ? " low" : ""}`;
      tag.textContent = item.kind === "episode" ? "剧集" : "电影";
      // 元数据状态单独一个浅色标签，和「电影/剧集」区分开。
      const state = item.source ? "hit" : item.metaState || "off";
      const stateChip = document.createElement("span");
      stateChip.className = `org-state ${state}`;
      stateChip.textContent = ORG_STATE_LABEL[state] || "";
      const reason = document.createElement("small");
      if (item.source) {
        const path = ORG_MATCH_LABEL[item.match] || "元数据";
        reason.textContent = `${item.title}${item.year ? ` (${item.year})` : ""} · ${path}`;
      } else {
        reason.textContent = `本地识别 · ${item.reason}`;
      }
      // 两个标签放同一行（可换行），说明文字另起一行，窄列下也不会叠字。
      const infoHead = document.createElement("div");
      infoHead.className = "org-info-head";
      infoHead.appendChild(tag);
      infoHead.appendChild(stateChip);
      info.appendChild(infoHead);
      info.appendChild(reason);
      tr.appendChild(info);

      rows.appendChild(tr);
    });

    $("org-pager").hidden = total <= ORG_PAGE_SIZE;
    $("org-page-info").textContent = `第 ${org.page} / ${pages} 页 · 共 ${total} 条`;
    $("org-prev").disabled = org.page <= 1;
    $("org-next").disabled = org.page >= pages;
    syncOrgCount();
  }

  async function orgScan() {
    if (org.busy) return;
    const gen = orgGen;
    const button = $("btn-org-scan");
    const message = $("org-msg");
    let baseText = "";
    // 重新扫描时先中止上一轮还没跑完的片名校准。
    orgEnrichToken += 1;
    org.busy = true;
    button.disabled = true;
    message.textContent = "扫描中…";
    message.className = "dialog-msg";
    orgBarRun("scan");
    orgStage("扫描目录", "正在遍历网盘目录，稍候…");
    try {
      const result = await api("/api/organize/scan", {
        method: "POST",
        body: JSON.stringify({
          fid: state.stack[state.stack.length - 1].fid,
          path: state.stack.slice(1).map((node) => node.name),
        }),
      });
      const useMeta = $("org-meta").checked;
      // 扫描期间弹窗被关掉，这份结果已经作废，不要再填回去。
      if (gen !== orgGen) return;
      org.items = result.items || [];
      // 扫描结果先标成「待校正」；不启用元数据校正就直接算「未校正」。
      org.items.forEach((item) => {
        item.metaState = useMeta ? "pending" : "off";
      });
      org.picked = new Set(org.items.filter((item) => !item.conflict).map((item) => item.fid));
      org.retry = [];
      org.page = 1;
      orgBarHide();
      renderOrg();
      baseText = org.items.length ? orgSummary(result) : "";
      message.textContent = baseText || "这个目录里没有可整理的视频";
      message.className = "dialog-msg";
    } catch (error) {
      orgBarHide();
      message.textContent = error.message;
      message.className = "dialog-msg bad";
    } finally {
      org.busy = false;
      button.disabled = false;
      syncOrgCount();
    }
    if (baseText && $("org-meta").checked) orgEnrich(baseText);
  }

  async function orgApply() {
    if (org.busy) return;
    const chosen = org.items.filter((item) => org.picked.has(item.fid));
    if (!chosen.length) {
      showToast("请先勾选要整理的文件", "bad");
      return;
    }
    const button = $("btn-org-apply");
    const message = $("org-msg");
    org.busy = true;
    org.applying = true;
    button.disabled = true;
    message.textContent = "";
    message.className = "dialog-msg";
    orgBarRun("apply", 0);
    orgStage("执行整理", `0 / ${chosen.length}`);

    const succeeded = new Set();
    const errors = new Map();
    for (let start = 0; start < chosen.length; start += ORG_CHUNK) {
      const slice = chosen.slice(start, start + ORG_CHUNK);
      const payload = slice.map((item) => ({
        fid: item.fid,
        name: item.name,
        parent_fid: item.parent_fid,
        target_dir: item.target_dir.split("/").map((part) => part.trim()).filter(Boolean),
        target_name: item.target_name,
        sidecars: item.sidecars.map((side) => ({
          fid: side.fid,
          name: side.name,
          parent_fid: side.parent_fid,
          target_name: side.target_name,
        })),
      }));
      try {
        const result = await api("/api/organize/apply", {
          method: "POST",
          body: JSON.stringify({ items: payload }),
        });
        (result.results || []).forEach((row) => {
          if (row.ok) succeeded.add(row.fid);
          else errors.set(row.fid, row.error || "整理失败");
        });
      } catch (error) {
        slice.forEach((item) => errors.set(item.fid, error.message));
      }
      const processed = Math.min(start + slice.length, chosen.length);
      $("org-progress").textContent = `已处理 ${processed} / ${chosen.length}`;
      orgBarRun("apply", processed / chosen.length);
      orgStage("执行整理", `${processed} / ${chosen.length}`);
    }

    // 成功的从预演列表移除，失败的留下并标出原因，方便修正后重试。
    const failedItems = [];
    org.items = org.items.filter((item) => {
      if (!succeeded.has(item.fid)) {
        if (errors.has(item.fid)) {
          item.conflict = true;
          item.conflict_reason = errors.get(item.fid);
          failedItems.push(item);
        }
        return true;
      }
      return false;
    });
    org.picked = new Set(Array.from(org.picked).filter((fid) => !succeeded.has(fid)));
    org.busy = false;
    org.applying = false;
    button.disabled = false;
    renderOrg();
    message.textContent = `已整理 ${succeeded.size} 条${failedItems.length ? `，失败 ${failedItems.length} 条` : ""}`;
    message.className = `dialog-msg ${failedItems.length ? "bad" : "ok"}`;
    $("org-progress").textContent = "";
    orgBarHide();
    showToast(`影视整理完成：成功 ${succeeded.size} 条`, failedItems.length ? "bad" : "ok");
    if (succeeded.size) await reloadCurrent();
  }

  $("btn-organize").addEventListener("click", orgOpen);
  // 关窗就丢掉预演结果，下次打开重新扫描。
  $("organize-dialog").addEventListener("close", () => {
    if (org.applying) return;
    orgReset();
  });
  $("btn-org-scan").addEventListener("click", orgScan);
  $("btn-org-apply").addEventListener("click", orgApply);
  $("org-check-all").addEventListener("change", (event) => {
    const on = event.target.checked;
    org.items.forEach((item) => {
      if (item.conflict) return;
      if (on) org.picked.add(item.fid);
      else org.picked.delete(item.fid);
    });
    $("org-rows")
      .querySelectorAll("tr")
      .forEach((tr) => {
        const box = tr.querySelector('input[type="checkbox"]');
        if (box && !box.disabled) box.checked = on;
      });
    syncOrgCount();
  });

  $("org-prev").addEventListener("click", () => {
    if (org.page > 1) {
      org.page -= 1;
      renderOrg();
    }
  });
  $("org-next").addEventListener("click", () => {
    org.page += 1;
    renderOrg();
  });

  // ------------------------------------------------------------ SSE

  function connectEvents() {
    const source = new EventSource("/api/events");
    source.addEventListener("task", (event) => {
      try {
        upsertTask(JSON.parse(event.data));
      } catch (_) {
        /* 忽略坏帧 */
      }
    });
    source.addEventListener("progress", (event) => {
      try {
        const data = JSON.parse(event.data);
        const task = state.tasks.get(data.id);
        if (!task) {
          state.tasks.set(data.id, {
            id: data.id,
            name: data.name,
            size: data.size,
            downloaded: data.downloaded,
            segments: data.segments || [],
            status: "downloading",
          });
        } else {
          task.downloaded = data.downloaded;
          task.size = data.size;
          task.segments = data.segments || [];
        }
        noteProgress(data);
        scheduleTasksRender();
      } catch (_) {
        /* 忽略坏帧 */
      }
    });
    source.addEventListener("removed", (event) => {
      try {
        const id = JSON.parse(event.data).id;
        state.tasks.delete(id);
        state.speeds.delete(id);
        renderTasks();
      } catch (_) {
        /* 忽略坏帧 */
      }
    });
    source.addEventListener("risk", (event) => {
      try {
        showRisk(JSON.parse(event.data));
      } catch (_) {
        /* 忽略坏帧 */
      }
    });
    source.addEventListener("account_invalid", () => {
      $("account-chip").textContent = "登录已失效";
      $("account-chip").className = "chip bad";
      hideCapacity();
      accountInfo = { ...accountInfo, valid: false };
      renderAccountCard();
      maybeAutoLogin();
    });
    source.onerror = () => {
      $("account-chip").title = "实时连接断开，正在重连";
    };
  }

  // ------------------------------------------------------------ 启动

  const restored = restoreSession();
  syncSearchUi();
  $("task-search").value = state.taskSearch;
  $("task-status-filter").value = state.taskStatus;
  renderCrumbs();
  updateSelectionBadge();
  loadAccount();
  loadSettings(false);
  loadTasks().catch(() => renderTasks());
  loadDisk();
  setInterval(loadDisk, 5000);
  if (state.mode === "cloud" && state.keyword) {
    loadSearch();
  } else {
    state.mode = "local";
    loadFiles({ preserveSelection: restored });
  }
  restoreSpeedSamples();
  drawSpark();
  loadSpeedHistory();
  setInterval(sampleSpeed, SAMPLE_MS);
  connectEvents();
})();
