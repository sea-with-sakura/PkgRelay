const state = { status: null, sources: [], selectedPool: null, selectedSource: null };
const $ = (id) => document.getElementById(id);
const formatBytes = (bytes) => {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[ch]);
const api = async (path) => {
  const response = await fetch(path);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
};
const sourceUsage = (sourceId) => (state.status?.cache?.sources || []).find((item) => item.source_id === sourceId) || {
  artifact_count: 0, package_count: 0, metadata_count: 0, package_bytes: 0, total_hits: 0,
};
const isCondaSource = (source) => source.kind === "conda";
const displayName = (sourceId) => sourceId === "defaults-main" ? "defaults · main" : sourceId === "defaults-r" ? "defaults · r" : sourceId;
const shortUpstream = (upstreams = []) => upstreams.map((url) => {
  try { return new URL(url).hostname.replace("mirrors.tuna.tsinghua.edu.cn", "TUNA"); } catch { return url; }
}).join("  →  ");
const packagePath = (path) => {
  const parts = String(path || "").split("/");
  return { platform: parts.length > 1 ? parts[0] : "—", name: parts.at(-1) || "—" };
};

function renderPools(stats) {
  const pools = Object.fromEntries((stats.pools || []).map((pool) => [pool.pool, pool]));
  const cards = [
    { id: "conda", name: "Conda 缓存池", text: "按 Channel 浏览包文件与索引", action: "浏览 Channel" },
    { id: "pip", name: "pip 缓存池", text: "统一缓存 Python 分发包", action: "浏览文件" },
  ];
  $("pools").innerHTML = cards.map((card) => {
    const usage = pools[card.id] || { blob_count: 0, total_bytes: 0, total_hits: 0 };
    return `<article class="pool-card ${card.id}" data-pool="${card.id}" role="button" tabindex="0">
      <div class="pool-card-top"><span class="tag">${card.id}</span><span class="pool-arrow">↗</span></div>
      <h3>${card.name}</h3><p>${card.text}</p>
      <div class="pool-stats"><span>${usage.blob_count} 个对象</span><span>${formatBytes(usage.total_bytes)}</span><span>${usage.total_hits} 命中</span></div>
      <span class="source-action">${card.action} →</span>
    </article>`;
  }).join("");
  document.querySelectorAll(".pool-card").forEach((element) => {
    const open = () => openPool(element.dataset.pool);
    element.addEventListener("click", open);
    element.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); open(); } });
  });
}

function renderArtifacts(artifacts) {
  $("artifacts").innerHTML = artifacts.length ? artifacts.map((item) => {
    const file = packagePath(item.path);
    return `<tr><td><span class="table-channel">${escapeHtml(displayName(item.source_id))}</span></td><td class="path">${escapeHtml(file.name)}</td><td>${formatBytes(item.size)}</td><td>${item.hit_count}</td><td>${new Date(item.last_accessed_at * 1000).toLocaleString()}</td></tr>`;
  }).join("") : '<tr><td colspan="5" class="muted">尚无缓存对象。</td></tr>';
}

function renderUsage(usage) {
  const totals = usage.totals || {};
  $("usage-users").textContent = (totals.client_count || 0).toLocaleString();
  $("usage-machines").textContent = (totals.machine_count || 0).toLocaleString();
  $("usage-distributed").textContent = formatBytes(totals.bytes_served || 0);
  $("usage-saved").textContent = formatBytes(totals.bytes_saved || 0);
  const clients = usage.clients || [];
  $("usage-clients").innerHTML = clients.length ? clients.map((client) => {
    const downloads = Number(client.downloads || 0);
    const hits = Number(client.cache_hits || 0);
    const hitRate = downloads ? `${(hits * 100 / downloads).toFixed(0)}%` : "—";
    const lastSeen = client.last_seen_at ? new Date(client.last_seen_at * 1000).toLocaleString() : "—";
    return `<tr><td><span class="machine-name">${escapeHtml(client.machine)}</span></td><td>${escapeHtml(client.username)}</td><td>${downloads.toLocaleString()}</td><td>${hitRate}</td><td>${formatBytes(client.bytes_served || 0)}</td><td class="saved">${formatBytes(client.bytes_saved || 0)}</td><td>${lastSeen}</td></tr>`;
  }).join("") : '<tr><td colspan="7" class="muted">暂无已识别的客户端包下载记录。</td></tr>';
}

function renderChannels() {
  const channels = state.sources.filter(isCondaSource);
  $("detail-eyebrow").textContent = "CONDA CACHE";
  $("detail-title").textContent = "按 Channel 浏览";
  $("detail-summary").textContent = "包文件与索引按来源分组；实际磁盘占用已在缓存池概览中按内容去重。";
  $("channel-view").hidden = false;
  $("artifact-view").hidden = true;
  $("detail-back").hidden = true;
  $("channel-cards").innerHTML = channels.map((source) => {
    const usage = sourceUsage(source.name);
    return `<article class="channel-card" data-source="${escapeHtml(source.name)}" role="button" tabindex="0">
      <div class="channel-card-top"><span class="tag">CONDA CHANNEL</span><span class="channel-arrow">→</span></div>
      <h3>${escapeHtml(displayName(source.name))}</h3>
      <p class="upstream-line">${escapeHtml(shortUpstream(source.upstreams))}</p>
      <div class="channel-stats"><span><b>${usage.package_count}</b> 包文件</span><span><b>${usage.metadata_count}</b> 索引</span><span><b>${usage.total_hits}</b> 命中</span></div>
      <p class="channel-size">${formatBytes(usage.package_bytes)} 包文件缓存</p>
    </article>`;
  }).join("") || '<p class="muted">尚未配置 Conda Channel。</p>';
  document.querySelectorAll(".channel-card").forEach((card) => {
    const open = () => openSource(card.dataset.source);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); open(); } });
  });
}

async function openPool(pool) {
  state.selectedPool = pool;
  state.selectedSource = null;
  $("detail-search").value = "";
  $("detail-metadata").checked = false;
  $("browser-dialog").showModal();
  if (pool === "conda") renderChannels(); else await openArtifacts();
}

async function openSource(sourceId) {
  state.selectedSource = sourceId;
  $("detail-search").value = "";
  $("detail-metadata").checked = false;
  await openArtifacts();
}

async function openArtifacts() {
  const source = state.selectedSource && state.sources.find((item) => item.name === state.selectedSource);
  const pool = state.selectedPool;
  $("channel-view").hidden = true;
  $("artifact-view").hidden = false;
  $("detail-back").hidden = pool !== "conda";
  $("detail-eyebrow").textContent = source ? "CONDA CHANNEL" : "PIP CACHE";
  $("detail-title").textContent = source ? displayName(source.name) : "pip 缓存文件";
  $("detail-summary").textContent = source ? shortUpstream(source.upstreams) : "按内容去重后的 pip 分发包。";
  document.querySelector(".metadata-toggle").hidden = !source;
  await loadArtifacts();
}

async function loadArtifacts() {
  const query = $("detail-search").value.trim();
  const includeMetadata = $("detail-metadata").checked;
  $("detail-artifacts").innerHTML = '<tr><td colspan="5" class="muted">加载中…</td></tr>';
  try {
    const params = new URLSearchParams({ limit: "500" });
    if (query) params.set("query", query);
    if (includeMetadata) params.set("include_metadata", "true");
    const endpoint = state.selectedSource
      ? `/api/v1/sources/${encodeURIComponent(state.selectedSource)}/artifacts?${params}`
      : `/api/v1/pools/${encodeURIComponent(state.selectedPool)}/artifacts?${params}`;
    const result = await api(endpoint);
    $("detail-count").textContent = `显示 ${Math.min(result.total, 500)} / ${result.total} 个${includeMetadata ? "文件" : "包文件"}`;
    $("detail-artifacts").innerHTML = result.artifacts.length ? result.artifacts.map((item) => {
      const file = packagePath(item.path);
      return `<tr><td><span class="platform">${escapeHtml(file.platform)}</span></td><td class="path">${escapeHtml(file.name)}</td><td>${formatBytes(item.size)}</td><td>${item.hit_count}</td><td>${new Date(item.fetched_at * 1000).toLocaleString()}</td></tr>`;
    }).join("") : '<tr><td colspan="5" class="muted">没有匹配的缓存包。</td></tr>';
  } catch (error) {
    $("detail-artifacts").innerHTML = `<tr><td colspan="5" class="muted">加载失败：${escapeHtml(error.message)}</td></tr>`;
  }
}

async function refresh() {
  $("refresh").disabled = true;
  try {
    const [status, sourceData, artifactData, usage] = await Promise.all([api("/api/v1/status"), api("/api/v1/sources"), api("/api/v1/artifacts?limit=20"), api("/api/v1/usage")]);
    state.status = status; state.sources = sourceData.sources;
    $("artifact-count").textContent = status.cache.blob_count.toLocaleString();
    $("cache-bytes").textContent = formatBytes(status.cache.total_bytes);
    $("cache-hits").textContent = status.cache.total_hits.toLocaleString();
    $("channel-count").textContent = state.sources.filter(isCondaSource).length.toLocaleString();
    $("health-text").textContent = "服务正常";
    $("health-dot").classList.remove("offline");
    $("client-config").textContent = `wget -qO /tmp/setenv.sh ${location.origin}/bootstrap/setenv.sh && bash /tmp/setenv.sh && exec bash -l`;
    renderPools(status.cache); renderArtifacts(artifactData.artifacts); renderUsage(usage);
  } catch (error) {
    $("health-text").textContent = `连接失败：${error.message}`;
    $("health-dot").classList.add("offline");
  } finally { $("refresh").disabled = false; }
}

$("refresh").addEventListener("click", refresh);
$("detail-close").addEventListener("click", () => $("browser-dialog").close());
$("detail-back").addEventListener("click", renderChannels);
$("detail-search-button").addEventListener("click", loadArtifacts);
$("detail-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadArtifacts(); } });
$("detail-metadata").addEventListener("change", loadArtifacts);
refresh();
