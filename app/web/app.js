const state = { status: null, sources: [], selectedPool: null };
const $ = (id) => document.getElementById(id);
const formatBytes = (bytes) => {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[ch]);
const api = async (path, options) => {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
};
function renderPools(sources, stats) {
  const statsByPool = Object.fromEntries((stats.pools || []).map((pool) => [pool.pool, pool]));
  const poolDefinitions = [
    { name: "conda", title: "Conda" },
    { name: "pip", title: "pip" },
  ];
  $("sources").innerHTML = poolDefinitions.map((pool) => {
    const usage = statsByPool[pool.name] || { artifact_count: 0, blob_count: 0, total_bytes: 0, total_hits: 0 };
    return `<article class="source clickable" data-pool="${pool.name}" role="button" tabindex="0"><span class="tag">${pool.name}</span><h3>${pool.title} 缓存池</h3><p>${usage.artifact_count} 个对象 · ${usage.blob_count} 个 Blob · ${formatBytes(usage.total_bytes)} · ${usage.total_hits} 次命中</p><p class="source-action">查看内容 →</p></article>`;
  }).join("");
  document.querySelectorAll(".source.clickable").forEach((element) => {
    const open = () => openPool(element.dataset.pool);
    element.addEventListener("click", open);
    element.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });
  const condaSources = sources.filter((source) => source.kind === "conda");
  const pypiSources = sources.filter((source) => source.kind === "pypi" && !source.dynamic);
  const defaultPypi = pypiSources.find((source) => source.name === "pypi") || pypiSources[0];
  const specialisedPypi = pypiSources.filter((source) => source !== defaultPypi);
  const condaConfig = ["# ~/.condarc", "channels:", ...condaSources.map((source) => `  - ${location.origin}/get/${source.name}`), "channel_priority: strict"];
  const pipConfig = defaultPypi ? ["# ~/.config/pip/pip.conf", "[global]", `index-url = ${location.origin}/get/pypi/${defaultPypi.name}/simple`] : [];
  const specialisedConfig = specialisedPypi.flatMap((source) => ["", `# ${source.name} 专用源（按需替换包名）`, `# pip3 install <package> --index-url ${location.origin}/get/pypi/${source.name}/simple`]);
  $("client-config").textContent = [...condaConfig, "", ...pipConfig, ...specialisedConfig].join("\n");
}
function renderArtifacts(artifacts) {
  $("artifacts").innerHTML = artifacts.length ? artifacts.map((item) => `<tr><td>${escapeHtml(item.source_id)}</td><td class="path">${escapeHtml(item.path)}</td><td>${formatBytes(item.size)}</td><td>${item.hit_count}</td><td class="upstream">${escapeHtml(item.upstream_url)}</td><td>${new Date(item.last_accessed_at * 1000).toLocaleString()}</td></tr>`).join("") : '<tr><td colspan="6" class="muted">尚无缓存对象。</td></tr>';
}
async function openPool(pool) {
  state.selectedPool = pool;
  $("detail-title").textContent = `${pool === "conda" ? "Conda" : "pip"} 缓存池内容`;
  $("detail-summary").textContent = "";
  $("detail-search").value = "";
  $("source-dialog").showModal();
  await loadSourceArtifacts();
}
async function loadSourceArtifacts() {
  const pool = state.selectedPool;
  if (!pool) return;
  const query = $("detail-search").value.trim();
  $("detail-artifacts").innerHTML = '<tr><td colspan="5" class="muted">加载中…</td></tr>';
  try {
    const params = new URLSearchParams({ limit: "500" });
    if (query) params.set("query", query);
    const result = await api(`/api/v1/pools/${encodeURIComponent(pool)}/artifacts?${params}`);
    const shown = Math.min(result.total, 500);
    $("detail-count").textContent = query ? `匹配 ${result.total} 个对象，显示 ${shown} 个` : `共 ${result.total} 个缓存对象，显示 ${shown} 个`;
    $("detail-artifacts").innerHTML = result.artifacts.length ? result.artifacts.map((item) => `<tr><td class="path">${escapeHtml(item.path)}</td><td>${formatBytes(item.size)}</td><td>${item.hit_count}</td><td>${new Date(item.fetched_at * 1000).toLocaleString()}</td><td class="upstream">${escapeHtml(item.upstream_url)}</td></tr>`).join("") : '<tr><td colspan="5" class="muted">没有匹配的缓存对象。</td></tr>';
  } catch (error) { $("detail-artifacts").innerHTML = `<tr><td colspan="5" class="muted">加载失败：${escapeHtml(error.message)}</td></tr>`; }
}
async function refresh() {
  $("refresh").disabled = true;
  try {
    const [status, sourceData, artifactData] = await Promise.all([api("/api/v1/status"), api("/api/v1/sources"), api("/api/v1/artifacts?limit=20")]);
    state.status = status; state.sources = sourceData.sources;
    $("artifact-count").textContent = status.cache.artifact_count.toLocaleString();
    $("cache-bytes").textContent = formatBytes(status.cache.total_bytes);
    $("cache-hits").textContent = status.cache.total_hits.toLocaleString();
    $("source-count").textContent = "2";
    $("health-text").textContent = "服务正常";
    $("health-dot").classList.remove("offline");
    renderPools(sourceData.sources, status.cache); renderArtifacts(artifactData.artifacts);
  } catch (error) { $("health-text").textContent = `连接失败：${error.message}`; $("health-dot").classList.add("offline"); }
  finally { $("refresh").disabled = false; }
}
$("refresh").addEventListener("click", refresh);
$("detail-close").addEventListener("click", () => $("source-dialog").close());
$("detail-search-button").addEventListener("click", loadSourceArtifacts);
$("detail-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadSourceArtifacts(); } });
refresh();
