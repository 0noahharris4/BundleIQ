(() => {
  const $ = (id) => document.getElementById(id);
  const fmtNum = (v) => (v === null || v === undefined ? "—" : v.toLocaleString("en-US"));
  const fmtMoney = (v) => (v === null || v === undefined ? "—" : "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 }));

  let currentData = null;
  let sortKey = "lift";
  let sortDir = "desc";
  let pendingFile = null;

  // The chat key itself lives server-side only (see /api/chat in app.py) --
  // this build never asks the visitor for one. chatEnabled just reflects
  // whether the server has one configured, from /api/meta.
  let chatEnabled = false;
  let chatHistory = [];
  let chatDigest = null;
  let chatBusy = false;

  const MAP_FIELDS = [
    { key: "order_id", label: "Order ID", required: true },
    { key: "product_name", label: "Product name", required: true },
    { key: "category", label: "Category", required: false },
    { key: "quantity", label: "Quantity", required: false },
    { key: "unit_price", label: "Unit price", required: false },
    { key: "order_date", label: "Order date", required: false },
  ];

  // -------------------------------------------------------------- upload --
  function showError(msg) {
    const el = $("error-banner");
    el.textContent = msg;
    el.hidden = false;
  }
  function clearError() { $("error-banner").hidden = true; }

  function setLoading(on, label) {
    $("upload-status").classList.toggle("active", on);
    if (label) $("upload-status-text").textContent = label;
  }

  async function uploadFile(file, mapping) {
    clearError();
    if (!file) return;
    pendingFile = file;
    setLoading(true, `Analyzing ${file.name}…`);
    try {
      const formData = new FormData();
      formData.append("file", file);
      if (mapping) {
        Object.entries(mapping).forEach(([field, col]) => formData.append(`map_${field}`, col));
      }
      const res = await fetch("/api/analyze", { method: "POST", body: formData });
      const data = await res.json();
      if (res.status === 422 && data.needs_mapping) {
        showMappingForm(data.columns, data.guess || {});
        return;
      }
      if (!res.ok) { showError(data.error || "Something went wrong analyzing that file."); return; }
      pendingFile = null;
      showResults(data);
    } catch (e) {
      showError("Couldn't reach the analysis service. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  // ----------------------------------------------------- column mapping --
  function showMappingForm(columns, guess) {
    $("upload-section").hidden = true;
    $("results-section").hidden = true;
    $("mapping-section").hidden = false;
    $("mapping-error").hidden = true;

    $("mapping-grid").innerHTML = MAP_FIELDS.map((f) => {
      const opts = [];
      if (!f.required) opts.push(`<option value="">-- none --</option>`);
      columns.forEach((c) => {
        const selected = guess[f.key] === c ? " selected" : "";
        opts.push(`<option value="${c}"${selected}>${c}</option>`);
      });
      return `
        <div class="mapping-row">
          <label for="map-${f.key}">${f.label}${f.required ? " *" : ""}</label>
          <select id="map-${f.key}">${opts.join("")}</select>
        </div>`;
    }).join("");
  }

  function submitMapping() {
    if (!pendingFile) return;
    const mapping = {};
    let missingRequired = false;
    MAP_FIELDS.forEach((f) => {
      const val = $(`map-${f.key}`).value;
      if (val) mapping[f.key] = val;
      else if (f.required) missingRequired = true;
    });
    if (missingRequired) {
      const el = $("mapping-error");
      el.textContent = "Please choose a column for both Order ID and Product name.";
      el.hidden = false;
      return;
    }
    $("mapping-section").hidden = true;
    $("upload-section").hidden = false;
    uploadFile(pendingFile, mapping);
  }

  function cancelMapping() {
    pendingFile = null;
    $("mapping-section").hidden = true;
    $("upload-section").hidden = false;
    $("file-input").value = "";
  }

  async function analyzeSample() {
    clearError();
    setLoading(true, "Analyzing sample data…");
    try {
      const res = await fetch("/api/analyze-sample");
      const data = await res.json();
      if (!res.ok) { showError(data.error || "Something went wrong."); return; }
      showResults(data);
    } catch (e) {
      showError("Couldn't reach the analysis service. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  function setupDropzone() {
    const zone = $("dropzone");
    const input = $("file-input");
    zone.addEventListener("click", () => input.click());
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => { if (input.files[0]) uploadFile(input.files[0]); });

    ["dragenter", "dragover"].forEach((evt) =>
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((evt) =>
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove("dragover"); }));
    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files[0];
      if (file) uploadFile(file);
    });
  }

  // -------------------------------------------------------------- render --
  function showResults(data) {
    currentData = data;
    sortKey = "lift"; sortDir = "desc";
    $("upload-section").hidden = true;
    $("results-section").hidden = false;
    $("results-filename").textContent = data.source_filename || "uploaded file";

    $("kpi-orders").textContent = fmtNum(data.n_orders);
    $("kpi-products").textContent = fmtNum(data.n_unique_products);
    $("kpi-basket-size").textContent = data.avg_basket_size;
    $("kpi-rules").textContent = fmtNum(data.n_rules);
    $("kpi-rules-sub").textContent = data.has_price && data.total_revenue ? `${fmtMoney(data.total_revenue)} in total revenue analyzed` : "";

    renderBundles(data.rules.slice(0, 3));
    renderHeatmap(data.heatmap);
    renderSeasonality(data.seasonality);
    renderRulesTable();
    renderProducts(data.products.slice(0, 12), data.has_price);

    chatDigest = buildDataDigest(data);
    chatHistory = [];
    setChatError(null);
    $("chat-widget").hidden = !chatEnabled;

    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderBundles(topRules) {
    $("bundle-grid").innerHTML = topRules.map((r) => `
      <div class="bundle-card">
        <div class="bundle-pair">${r.product_a} <span class="plus">+</span> ${r.product_b}</div>
        <div class="bundle-stat"><strong>${r.confidence_pct}%</strong> of orders with ${r.product_a} also include ${r.product_b}</div>
        <div class="bundle-lift-badge">${r.lift}&times; more likely than chance</div>
      </div>
    `).join("") || `<div class="card-sub">Not enough data to surface strong bundle opportunities yet.</div>`;
  }

  function liftColor(lift, maxLift) {
    if (!lift || lift <= 0) return "rgba(255,255,255,0.03)";
    const t = Math.max(0, Math.min(1, (lift - 1) / Math.max(0.5, maxLift - 1)));
    // Sequential single-hue (blue) ramp, anchor flipped for the dark surface:
    // low magnitude sits near-transparent/close to the surface, high magnitude
    // brightens toward a near-white blue so the strongest cells pop forward.
    const r = Math.round(57 + (214 - 57) * t);
    const g = Math.round(135 + (232 - 135) * t);
    const b = Math.round(229 + (255 - 229) * t);
    const alpha = 0.15 + t * 0.8;
    return `rgba(${r},${g},${b},${alpha.toFixed(2)})`;
  }

  function renderHeatmap(heatmap) {
    const products = heatmap.products;
    const matrix = heatmap.matrix;
    let maxLift = 1.5;
    matrix.forEach((row) => row.forEach((v) => { if (v && v > maxLift) maxLift = v; }));

    let thead = "<thead><tr><th></th>" + products.map((p) => `<th class="heatmap-col-label" title="${p}"><span class="heatmap-col-label-text">${p}</span></th>`).join("") + "</tr></thead>";
    let tbody = "<tbody>";
    products.forEach((rowProduct, i) => {
      tbody += `<tr><th class="heatmap-row-label" title="${rowProduct}">${rowProduct}</th>`;
      products.forEach((colProduct, j) => {
        const v = matrix[i][j];
        if (v === null) {
          tbody += `<td class="heatmap-cell diagonal"></td>`;
        } else {
          tbody += `<td class="heatmap-cell has-value" style="background:${liftColor(v, maxLift)}" data-row="${rowProduct}" data-col="${colProduct}" data-lift="${v}"></td>`;
        }
      });
      tbody += "</tr>";
    });
    tbody += "</tbody>";
    $("heatmap-table").innerHTML = thead + tbody;

    const tooltip = $("heatmap-tooltip");
    document.querySelectorAll("#heatmap-table .heatmap-cell.has-value").forEach((cell) => {
      cell.addEventListener("mouseenter", (e) => {
        const { row, col, lift } = cell.dataset;
        const liftNum = parseFloat(lift);
        tooltip.innerHTML = `<div class="tt-title">${row} &times; ${col}</div>
          <div class="tt-row"><span>Lift</span><strong>${liftNum > 0 ? liftNum + "x" : "n/a"}</strong></div>
          <div class="tt-row"><span>${liftNum > 1 ? "More likely together" : "Not enough data"}</span></div>`;
        tooltip.style.display = "block";
      });
      cell.addEventListener("mousemove", (e) => {
        const wrap = cell.closest(".heatmap-scroll");
        const rect = wrap.getBoundingClientRect();
        tooltip.style.left = (e.clientX - rect.left + wrap.scrollLeft + 14) + "px";
        tooltip.style.top = (e.clientY - rect.top + wrap.scrollTop - 10) + "px";
      });
      cell.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
    });
  }

  function seasonColor(index, maxIndex) {
    if (index === null || index === undefined) return "rgba(255,255,255,0.03)";
    // Anchored at index=1.0 ("typical" for this product), the same way the
    // association heatmap anchors at lift=1 -- at-or-below-average share
    // stays faint, only genuine over-indexing brightens.
    const t = Math.max(0, Math.min(1, (index - 1) / Math.max(0.5, maxIndex - 1)));
    // Sequential single-hue (violet) ramp, anchor flipped for the dark
    // surface, same principle as the blue association heatmap but a
    // distinct hue so the two charts read as different metrics at a glance.
    const r = Math.round(139 + (232 - 139) * t);
    const g = Math.round(92 + (220 - 92) * t);
    const b = Math.round(246 + (255 - 246) * t);
    const alpha = 0.15 + t * 0.8;
    return `rgba(${r},${g},${b},${alpha.toFixed(2)})`;
  }

  function renderSeasonality(seasonality) {
    const card = $("seasonality-card");
    if (!seasonality) { card.hidden = true; return; }
    card.hidden = false;
    const { seasons, products, matrix, pct_matrix, n_dated_orders, season_order_counts } = seasonality;

    let maxIndex = 1.5;
    matrix.forEach((row) => row.forEach((v) => { if (v !== null && v > maxIndex) maxIndex = v; }));

    let thead = "<thead><tr><th></th>" + seasons.map((s, i) =>
      `<th class="heatmap-col-label" title="${s} — ${fmtNum(season_order_counts[i])} orders"><span class="heatmap-col-label-text">${s}</span></th>`).join("") + "</tr></thead>";
    let tbody = "<tbody>";
    products.forEach((p, i) => {
      tbody += `<tr><th class="heatmap-row-label" title="${p}">${p}</th>`;
      seasons.forEach((s, j) => {
        const v = matrix[i][j];
        const pct = pct_matrix[i][j];
        if (v === null) {
          tbody += `<td class="heatmap-cell diagonal"></td>`;
        } else {
          tbody += `<td class="heatmap-cell has-value" style="background:${seasonColor(v, maxIndex)}" data-row="${p}" data-col="${s}" data-index="${v}" data-pct="${pct}"></td>`;
        }
      });
      tbody += "</tr>";
    });
    tbody += "</tbody>";
    $("seasonality-table").innerHTML = thead + tbody;

    const tooltip = $("seasonality-tooltip");
    document.querySelectorAll("#seasonality-table .heatmap-cell.has-value").forEach((cell) => {
      cell.addEventListener("mouseenter", () => {
        const { row, col, index, pct } = cell.dataset;
        const idxNum = parseFloat(index);
        tooltip.innerHTML = `<div class="tt-title">${row} &times; ${col}</div>
          <div class="tt-row"><span>Of ${col} orders</span><strong>${pct}%</strong></div>
          <div class="tt-row"><span>${idxNum > 1 ? "Over-indexes" : idxNum < 1 ? "Under-indexes" : "Typical"}</span><strong>${idxNum}&times;</strong></div>`;
        tooltip.style.display = "block";
      });
      cell.addEventListener("mousemove", (e) => {
        const wrap = cell.closest(".heatmap-scroll");
        const rect = wrap.getBoundingClientRect();
        tooltip.style.left = (e.clientX - rect.left + wrap.scrollLeft + 14) + "px";
        tooltip.style.top = (e.clientY - rect.top + wrap.scrollTop - 10) + "px";
      });
      cell.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
    });

    $("seasonality-sub").textContent = `Based on ${fmtNum(n_dated_orders)} orders with a usable order date.`;
  }

  function liftBadge(lift) {
    const cls = lift >= 3 ? "" : "moderate";
    return `<span class="lift-badge ${cls}">${lift}&times;</span>`;
  }

  function getFilteredSortedRules() {
    const q = $("rules-search").value.trim().toLowerCase();
    let rules = currentData.rules;
    if (q) rules = rules.filter((r) => r.product_a.toLowerCase().includes(q) || r.product_b.toLowerCase().includes(q));
    rules = [...rules].sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return rules;
  }

  function renderRulesTable() {
    const rules = getFilteredSortedRules();
    $("rules-count").textContent = `${rules.length} of ${currentData.rules.length} associations`;
    $("rules-table-body").innerHTML = rules.map((r) => `
      <tr>
        <td>${r.product_a}</td>
        <td>${r.product_b}</td>
        <td>${r.support_pct}%</td>
        <td>${r.confidence_pct}%</td>
        <td>${liftBadge(r.lift)}</td>
      </tr>
    `).join("") || `<tr><td colspan="5" style="color:var(--text-muted);">No associations match that search.</td></tr>`;

    document.querySelectorAll(".data-table th[data-sort]").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.sort === sortKey);
      th.classList.toggle("asc", th.dataset.sort === sortKey && sortDir === "asc");
    });
  }

  function renderProducts(products, hasPrice) {
    $("products-sub").textContent = hasPrice
      ? "Ranked by how many orders included them; revenue shown where prices were provided."
      : "Ranked by how many orders included them.";
    const max = Math.max(...products.map((p) => p.order_count), 1);
    $("product-list").innerHTML = products.map((p) => `
      <div class="product-row">
        <div>
          <div class="product-name">${p.product_name}</div>
          <div class="product-category">${p.category || ""}${hasPrice && p.revenue ? ` · ${fmtMoney(p.revenue)}` : ""}</div>
        </div>
        <div class="product-bar-track"><div class="product-bar-fill" style="width:${(p.order_count / max * 100).toFixed(0)}%"></div></div>
        <div class="product-count">${fmtNum(p.order_count)} orders</div>
      </div>
    `).join("");
  }

  function exportRulesCsv() {
    const rules = getFilteredSortedRules();
    const header = "product_a,product_b,support_pct,confidence_pct,confidence_reverse_pct,lift\n";
    const body = rules.map((r) =>
      [r.product_a, r.product_b, r.support_pct, r.confidence_pct, r.confidence_reverse_pct, r.lift]
        .map((v) => (typeof v === "string" && v.includes(",")) ? `"${v}"` : v).join(",")
    ).join("\n");
    const blob = new Blob([header + body], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "bundleiq_associations.csv";
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // ---------------------------------------------------------- chat widget --
  // Condensed, token-bounded digest of the current analysis -- this, not the
  // raw uploaded rows, is what gets sent to the LLM. Capped lists keep the
  // request small and cheap; the LLM only ever sees numbers already computed
  // by the same counting engine that renders the charts, so it can't answer
  // with anything the user couldn't already see on screen.
  function buildDataDigest(data) {
    if (!data) return null;
    const digest = {
      orders_analyzed: data.n_orders,
      unique_products: data.n_unique_products,
      avg_items_per_order: data.avg_basket_size,
      associations_found: data.n_rules,
      total_revenue: data.has_price ? data.total_revenue : null,
      top_associations: data.rules.slice(0, 25).map((r) => ({
        product_a: r.product_a, product_b: r.product_b, lift: r.lift,
        confidence_pct: r.confidence_pct, support_pct: r.support_pct,
      })),
      top_products: data.products.slice(0, 20).map((p) => ({
        product_name: p.product_name, category: p.category || null,
        order_count: p.order_count, support_pct: p.support_pct, revenue: p.revenue,
      })),
    };
    if (data.seasonality) {
      digest.seasonality = {
        seasons: data.seasonality.seasons,
        season_order_counts: data.seasonality.season_order_counts,
        products: data.seasonality.products,
        seasonal_index_1_0_is_typical: data.seasonality.matrix,
      };
    }
    return digest;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderChatMessages() {
    const el = $("chat-messages");
    if (chatHistory.length === 0) {
      el.innerHTML = `<div class="chat-empty">Try "what pairs best with [enter sales item]?" or "what should I stock more of heading into the summer?" &mdash; answers are grounded in this file's own analysis.</div>`;
    } else {
      el.innerHTML = chatHistory.map((m) =>
        `<div class="chat-msg ${m.role === "user" ? "user" : "assistant"}">${escapeHtml(m.content)}</div>`
      ).join("") + (chatBusy ? `<div class="chat-msg assistant chat-thinking"><span></span><span></span><span></span></div>` : "");
    }
    el.scrollTop = el.scrollHeight;
  }

  function setChatError(msg) {
    const el = $("chat-error");
    if (!msg) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.textContent = msg;
  }

  async function callChatBackend(question) {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        context: chatDigest,
        history: chatHistory.slice(0, -1).slice(-6),
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Something went wrong asking that.");
    return data.answer;
  }

  async function sendChatMessage(e) {
    e.preventDefault();
    if (chatBusy) return;
    const input = $("chat-input");
    const question = input.value.trim();
    if (!question) return;
    if (!chatDigest) {
      setChatError("Analyze a file first.");
      return;
    }
    setChatError(null);
    chatHistory.push({ role: "user", content: question });
    input.value = "";
    chatBusy = true;
    renderChatMessages();
    $("chat-send-btn").disabled = true;

    try {
      const answer = await callChatBackend(question);
      chatHistory.push({ role: "assistant", content: answer });
    } catch (err) {
      setChatError(err.message || "Something went wrong asking that.");
    } finally {
      chatBusy = false;
      $("chat-send-btn").disabled = false;
      renderChatMessages();
    }
  }

  function toggleChatPanel(force) {
    const panel = $("chat-panel");
    const open = force !== undefined ? force : panel.hidden;
    panel.hidden = !open;
    if (open) {
      renderChatMessages();
      setTimeout(() => $("chat-input").focus(), 50);
    }
  }

  // Asks the server whether a chat key is configured at all -- the widget
  // only ever shows up once a file's analyzed AND the server confirms it
  // has a key. There's no key-entry UI at all in this build; the key lives
  // only in the server's environment (see OPENAI_API_KEY in app.py).
  async function initChatAvailability() {
    try {
      const res = await fetch("/api/meta");
      const meta = await res.json();
      chatEnabled = !!meta.chat_enabled;
    } catch (e) {
      chatEnabled = false;
    }
  }

  function resetChat() {
    chatHistory = [];
    chatDigest = null;
    chatBusy = false;
    setChatError(null);
    $("chat-widget").hidden = true;
    $("chat-panel").hidden = true;
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupDropzone();
    initChatAvailability();
    $("sample-btn").addEventListener("click", analyzeSample);
    $("reset-btn").addEventListener("click", () => {
      $("results-section").hidden = true;
      $("mapping-section").hidden = true;
      $("upload-section").hidden = false;
      $("file-input").value = "";
      pendingFile = null;
      clearError();
      resetChat();
    });
    $("mapping-continue-btn").addEventListener("click", submitMapping);
    $("mapping-cancel-btn").addEventListener("click", cancelMapping);
    $("rules-search").addEventListener("input", renderRulesTable);
    $("export-rules-btn").addEventListener("click", exportRulesCsv);
    document.querySelectorAll(".data-table th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
        else { sortKey = key; sortDir = key === "lift" || key === "support_pct" || key === "confidence_pct" ? "desc" : "asc"; }
        renderRulesTable();
      });
    });
    $("chat-fab").addEventListener("click", () => toggleChatPanel());
    $("chat-close-btn").addEventListener("click", () => toggleChatPanel(false));
    $("chat-input-row").addEventListener("submit", sendChatMessage);
  });
})();
