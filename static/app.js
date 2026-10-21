const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("登录已过期");
  }
  return response;
};

const categoryMeta = {
  水果: { english: "FRUIT", tone: "apple" },
  蔬菜: { english: "VEGETABLES", tone: "vegetable" },
  肉禽水产: { english: "MEAT & SEAFOOD", tone: "seafood" },
  乳品饮料: { english: "DAIRY & DRINKS", tone: "seafood" },
  粮油食品: { english: "FOOD & GRAINS", tone: "banana" },
  日用百货: { english: "DAILY ESSENTIALS", tone: "other" },
  其他: { english: "MARKET PICKS", tone: "other" },
};

const dialog = document.getElementById("action-dialog");
const form = document.getElementById("action-form");
const quantityInput = document.getElementById("quantity");
let currentPage = 1;
let catalogStatus = "active";
let catalogFilters = {search: "", category: ""};
let refreshSequence = 0;
let selection = null;
let pendingRequestId = null;
let toastTimer = null;
let lastStateSignature = null;
let databaseEngine = "数据库";

function money(cents) {
  return `¥${(cents / 100).toFixed(2)}`;
}

function toast(message, error = false) {
  const element = document.getElementById("toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 3200);
}

function setConnection(online) {
  const element = document.getElementById("connection");
  element.innerHTML = '<span class="live-dot"></span>';
  element.append(document.createTextNode(online ? ` 已连接 ${databaseEngine}` : " 连接已中断"));
  element.classList.toggle("offline", !online);
}

function createProductCard(product) {
  const category = categoryMeta[product.category] || categoryMeta.其他;
  const card = document.createElement("article");
  card.className = "product-card";
  card.innerHTML = `
    <div class="product-visual ${category.tone}"><span class="visual-label">${category.english}</span><span class="stock-pill"></span><span class="placeholder-caption" hidden>暂无商品图片</span></div>
    <div class="product-body"><div class="product-title-row"><div><h3></h3><p class="product-category"></p></div><div class="price"></div></div><p class="product-description"></p>
    <div class="stock-line"><span>实时库存</span><strong></strong></div><div class="stock-track"><div class="stock-fill"></div></div>
    <div class="card-actions"><button type="button" class="restock-button">补充库存</button><button type="button" class="relist-button" hidden>重新上架</button></div><div class="card-manage"><button type="button" class="edit-button">编辑商品</button><button type="button" class="adjust-button">调整库存</button><button type="button" class="ledger-button">库存台账</button></div><button type="button" class="delist-button">下架商品</button></div>`;
  card.querySelector("h3").textContent = product.name;
  card.querySelector(".product-category").textContent = `${product.category} · ${product.zone}`;
  card.querySelector(".product-description").textContent = product.description || product.sku;
  card.querySelector(".edit-button").addEventListener("click", () => openListing(product));
  card.querySelector(".adjust-button").addEventListener("click", () => openOperation("adjust", product));
  card.querySelector(".ledger-button").addEventListener("click", () => openLedger(product));
  const price = card.querySelector(".price");
  price.textContent = money(product.price_cents);
  const unit = document.createElement("small");
  unit.textContent = ` / ${product.unit}`;
  price.append(unit);
  card.querySelector(".stock-line strong").textContent = `${product.stock} ${product.unit}`;
  card.querySelector(".product-visual").append(productPicture(product, card.querySelector(".placeholder-caption")));
  const pill = card.querySelector(".stock-pill");
  pill.textContent = !product.active ? "已下架" : product.stock === 0 ? "已售罄" : product.stock <= 5 ? "库存紧张" : "现货供应";
  pill.classList.toggle("inactive", !product.active);
  pill.classList.toggle("low", product.stock > 0 && product.stock <= 5);
  pill.classList.toggle("out", product.stock === 0);
  const fill = card.querySelector(".stock-fill");
  fill.style.width = `${Math.min(100, product.stock / 30 * 100)}%`;
  fill.classList.toggle("low", product.stock > 0 && product.stock <= 5);
  fill.classList.toggle("out", product.stock === 0);
  const relist = card.querySelector(".relist-button");
  relist.hidden = !!product.active;
  relist.addEventListener("click", () => openStatus(product, true));
  const delist = card.querySelector(".delist-button");
  delist.hidden = !product.active;
  delist.addEventListener("click", () => openStatus(product, false));
  card.querySelector(".restock-button").addEventListener("click", () => openAction("restock", product));
  return card;
}

function productPicture(product, caption = null) {
  const image = document.createElement("img");
  image.className = "product-photo";
  image.loading = "lazy";
  function placeholder() {
    image.src = "/product-placeholder.svg";
    image.alt = `${product.name}暂无商品图片`;
    image.classList.add("placeholder-photo");
    if (caption) caption.hidden = false;
  }
  if (product.image_url) {
    image.src = product.image_url;
    image.alt = product.name;
  } else placeholder();
  image.addEventListener("error", () => {
    if (!image.classList.contains("placeholder-photo")) placeholder();
  });
  return image;
}

function renderMovements(movements) {
  const list = document.getElementById("activity-list");
  list.replaceChildren();
  if (movements.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "暂无记录，先试着补货或购买吧。";
    list.append(empty);
    return;
  }
  for (const movement of movements) {
    const purchase = movement.action === "purchase";
    const lifecycle = ["delist", "relist"].includes(movement.action);
    const label = {delist: "下架", relist: "重新上架"}[movement.action];
    const extraLabel = {cancel: "取消订单", adjust: "库存调整"}[movement.action];
    const row = document.createElement("div");
    row.className = "activity-row";
    row.innerHTML = `<div class="activity-title"><span class="activity-symbol ${purchase ? "purchase" : ""}">${purchase ? "↗" : "+"}</span><div><strong></strong><small></small></div></div><span class="activity-qty ${purchase ? "purchase" : ""}"></span><span class="activity-time"></span>`;
    row.querySelector("strong").textContent = `${lifecycle ? label : extraLabel || (purchase ? "购买" : movement.reason === "initial_stock" ? "上架" : "补货")} · ${movement.product_name}`;
    const reason = movement.reason === "initial_stock" ? "新品初始库存" : movement.reason;
    row.querySelector("small").textContent = lifecycle ? `保留库存 ${movement.stock_after} ${movement.unit}` : purchase ? `成交 ${money(movement.quantity * movement.unit_price_cents)}` : `${reason ? reason + " · " : ""}库存 ${movement.stock_after} ${movement.unit}`;
    const delta = movement.delta;
    row.querySelector(".activity-qty").textContent = lifecycle ? "未变动" : `${delta < 0 ? "−" : "+"}${Math.abs(delta)} ${movement.unit}`;
    if (lifecycle) row.querySelector(".activity-symbol").textContent = movement.action === "delist" ? "↓" : "↑";
    row.querySelector(".activity-time").textContent = new Date(movement.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    list.append(row);
  }
}

async function refresh(showError = false) {
  const sequence = ++refreshSequence;
  try {
    const query = new URLSearchParams({page: currentPage, status: catalogStatus, ...catalogFilters});
    const response = await fetch(`/api/state?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error("读取数据失败");
    const state = await response.json();
    if (sequence !== refreshSequence) return;
    databaseEngine = state.engine === "postgresql" ? "PostgreSQL" : "SQLite";
    const signature = JSON.stringify(state);
    if (signature === lastStateSignature) {
      setConnection(true);
      await refreshOrders();
      return;
    }
    lastStateSignature = signature;
    currentPage = state.page;
    const grid = document.getElementById("product-grid");
    grid.replaceChildren(...state.products.map(createProductCard).filter(Boolean));
    if (!state.products.length) {
      const empty = document.createElement("div");
      empty.className = "loading-card";
      empty.textContent = catalogFilters.search || catalogFilters.category ? "没有符合筛选条件的商品，请调整搜索条件。" : state.status === "active" ? "暂无在售商品，可以上架新品或在已下架列表中恢复商品。" : "暂无已下架商品。";
      grid.append(empty);
    }
    document.getElementById("stat-products").textContent = state.active_count;
    document.getElementById("active-count").textContent = state.active_count;
    document.getElementById("inactive-count").textContent = state.inactive_count;
    document.getElementById("stat-stock").textContent = state.stock_total;
    document.getElementById("stat-sales").textContent = money(state.sales_cents);
    document.getElementById("records-count").textContent = `${state.movement_count} 条记录`;
    renderMovements(state.movements);
    renderPagination(state);
    setConnection(true);
    await refreshOrders();
  } catch (error) {
    if (sequence !== refreshSequence) return;
    setConnection(false);
    if (showError) toast("无法读取数据，请检查后端与数据库服务", true);
  }
}

function renderPagination(state) {
  const first = state.product_count ? (state.page - 1) * 6 + 1 : 0;
  const last = Math.min(state.page * 6, state.product_count);
  document.getElementById("page-summary").textContent = `共 ${state.product_count} 个商品 · 显示 ${first}–${last} 个 · 第 ${state.page} / ${state.pages} 页`;
  const nav = document.getElementById("pagination");
  nav.replaceChildren();
  function button(label, page, disabled = false, selected = false) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.disabled = disabled;
    if (selected) element.setAttribute("aria-current", "page");
    element.addEventListener("click", () => {
      currentPage = page;
      refresh(true);
    });
    nav.append(element);
  }
  button("上一页", state.page - 1, state.page === 1);
  const visible = new Set([1, state.pages, state.page - 1, state.page, state.page + 1]);
  let previous = 0;
  for (const page of [...visible].filter(page => page >= 1 && page <= state.pages).sort((a, b) => a - b)) {
    if (previous && page - previous > 1) {
      const gap = document.createElement("span");
      gap.textContent = "…";
      nav.append(gap);
    }
    button(String(page), page, false, page === state.page);
    previous = page;
  }
  button("下一页", state.page + 1, state.page === state.pages);
}

function openAction(action, product) {
  selection = { action, product };
  pendingRequestId = null;
  quantityInput.value = "1";
  document.getElementById("form-error").textContent = "";
  document.getElementById("dialog-emoji").replaceChildren(productPicture(product));
  document.getElementById("dialog-kicker").textContent = action === "purchase" ? "PURCHASE · 购买商品" : "RESTOCK · 补充库存";
  document.getElementById("dialog-title").textContent = product.name;
  document.getElementById("dialog-description").textContent = action === "purchase" ? "选择购买数量，确认后将扣减库存。" : "选择补货数量，确认后将增加库存。";
  document.getElementById("quantity-hint").textContent = action === "purchase" ? `当前库存 ${product.stock} ${product.unit}` : `每次最多 9999 ${product.unit}`;
  quantityInput.max = action === "purchase" ? String(Math.min(9999, product.stock)) : "9999";
  document.getElementById("submit-action").textContent = action === "purchase" ? "确认购买" : "确认补货";
  updateTotal();
  dialog.showModal();
  quantityInput.focus();
}

function updateTotal() {
  if (!selection) return;
  const purchase = selection.action === "purchase";
  const total = document.getElementById("dialog-total");
  total.style.visibility = purchase ? "visible" : "hidden";
  total.querySelector("strong").textContent = money((Number(quantityInput.value) || 0) * selection.product.price_cents);
}

quantityInput.addEventListener("input", () => {
  pendingRequestId = null;
  document.getElementById("form-error").textContent = "";
  updateTotal();
});
document.getElementById("decrease").addEventListener("click", () => {
  quantityInput.value = Math.max(1, (Number(quantityInput.value) || 1) - 1);
  quantityInput.dispatchEvent(new Event("input"));
});
document.getElementById("increase").addEventListener("click", () => {
  quantityInput.value = Math.min(Number(quantityInput.max), (Number(quantityInput.value) || 0) + 1);
  quantityInput.dispatchEvent(new Event("input"));
});
document.getElementById("dialog-close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target === dialog && !document.getElementById("submit-action").disabled) dialog.close();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selection) return;
  const quantity = Number(quantityInput.value);
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > Number(quantityInput.max)) {
    document.getElementById("form-error").textContent = "请输入有效数量。";
    return;
  }
  pendingRequestId ||= crypto.randomUUID();
  const submit = document.getElementById("submit-action");
  const operation = selection;
  [...form.elements].forEach(element => element.disabled = true);
  submit.disabled = true;
  const payload = { action: operation.action, product_id: operation.product.id, quantity, request_id: pendingRequestId };
  try {
    const response = await fetch("/api/movements", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) {
      pendingRequestId = null;
      document.getElementById("form-error").textContent = data.error || "操作失败，请重试。";
      await refresh();
      return;
    }
    const productName = operation.product.name;
    const action = operation.action;
    pendingRequestId = null;
    dialog.close();
    await refresh();
    toast(`${productName}${action === "purchase" ? "购买" : "补货"}成功 · ${quantity} ${operation.product.unit}`);
  } catch (error) {
    document.getElementById("form-error").textContent = "连接失败。可直接重试，系统会避免重复提交。";
    setConnection(false);
  } finally {
    [...form.elements].forEach(element => element.disabled = false);
  }
});

dialog.addEventListener("cancel", event => { if (document.getElementById("submit-action").disabled) event.preventDefault(); });

const listingDialog = document.getElementById("listing-dialog");
const listingForm = document.getElementById("listing-form");
let listingRequestId = null;
let previewUrl = null;
let listingEditingProduct = null;
function openListing(product = null) {
  listingForm.reset();
  listingEditingProduct = product;
  listingRequestId = null;
  document.getElementById("listing-error").textContent = "";
  document.getElementById("listing-preview").hidden = true;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  document.getElementById("listing-title").textContent = product ? "编辑商品资料" : "上架你的商品";
  document.getElementById("listing-submit").textContent = product ? "保存修改" : "确认上架";
  document.getElementById("remove-image-label").hidden = !product;
  for (const field of ["sku", "unit", "stock"]) document.getElementById(`listing-${field}`).disabled = !!product;
  if (product) {
    for (const field of ["name", "sku", "category", "zone", "unit", "stock", "description"]) document.getElementById(`listing-${field}`).value = product[field];
    document.getElementById("listing-price").value = (product.price_cents / 100).toFixed(2);
    if (product.image_url) {
      document.getElementById("listing-preview").src = product.image_url;
      document.getElementById("listing-preview").hidden = false;
    }
  }
  listingDialog.showModal();
  document.getElementById("listing-name").focus();
}
document.getElementById("open-listing").addEventListener("click", () => openListing());
document.getElementById("listing-close").addEventListener("click", () => listingDialog.close());
listingForm.addEventListener("input", () => { listingRequestId = null; document.getElementById("listing-error").textContent = ""; });
document.getElementById("listing-image").addEventListener("change", event => {
  listingRequestId = null;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  const file = event.target.files[0];
  const preview = document.getElementById("listing-preview");
  preview.hidden = true;
  if (file && file.size <= 1024 * 1024 && ["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    preview.hidden = false;
  } else if (file) {
    document.getElementById("listing-error").textContent = "请选择 1 MB 以内的 JPG、PNG 或 WebP 图片。";
  }
});
listingDialog.addEventListener("cancel", event => { if (document.getElementById("listing-fields").disabled) event.preventDefault(); });
listingForm.addEventListener("submit", async event => {
  event.preventDefault();
  const error = document.getElementById("listing-error");
  const priceText = document.getElementById("listing-price").value.trim();
  const [whole, fraction = ""] = priceText.split(".");
  const price = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  const stock = Number(document.getElementById("listing-stock").value);
  const name = document.getElementById("listing-name").value.trim();
  const unit = document.getElementById("listing-unit").value.trim();
  if (!name || !unit || !/^\d+(\.\d{1,2})?$/.test(priceText) || price < 1 || price > 100000000 || !Number.isInteger(stock) || stock < 0 || stock > 9999) {
    error.textContent = "请检查名称、单位、单价及库存。";
    return;
  }
  const file = document.getElementById("listing-image").files[0];
  if (file && (file.size > 1024 * 1024 || !["image/jpeg", "image/png", "image/webp"].includes(file.type))) {
    error.textContent = "请选择 1 MB 以内的 JPG、PNG 或 WebP 图片。";
    return;
  }
  listingRequestId ||= crypto.randomUUID();
  const editing = listingEditingProduct;
  const payload = { name, price_cents: price, category: document.getElementById("listing-category").value,
    zone: document.getElementById("listing-zone").value.trim(), description: document.getElementById("listing-description").value.trim(), request_id: listingRequestId };
  if (editing) payload.version = editing.version;
  else Object.assign(payload, {unit, stock, sku: document.getElementById("listing-sku").value.trim()});
  const fields = document.getElementById("listing-fields");
  fields.disabled = true;
  error.textContent = "";
  try {
    if (file) payload.image_data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("图片读取失败"));
      reader.readAsDataURL(file);
    });
    else if (editing && document.getElementById("remove-image").checked) payload.image_data = null;
    const response = await fetch(editing ? `/api/products/${editing.id}` : "/api/products", { method: editing ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) {
      listingRequestId = null;
      error.textContent = data.error || "保存失败，请重试。";
      if (response.status === 409) await refresh();
      return;
    }
    listingRequestId = null;
    if (!editing) {
      clearSearch();
      currentPage = data.page;
      switchCatalog(data.product.active ? "active" : "inactive", false);
    }
    listingDialog.close();
    await refresh(true);
    toast(editing ? `${name}资料已更新` : `${name}上架成功，已跳转到商品所在页`);
  } catch (exception) {
    error.textContent = "提交失败。可直接重试，系统会避免重复操作。";
  } finally {
    fields.disabled = false;
  }
});

function clearSearch() {
  catalogFilters = {search: "", category: ""};
  document.getElementById("catalog-search").reset();
}
document.getElementById("catalog-search").addEventListener("submit", event => {
  event.preventDefault();
  catalogFilters = {search: document.getElementById("search-products").value.trim(), category: document.getElementById("search-category").value};
  currentPage = 1;
  refresh(true);
});

function switchCatalog(status, reset = true) {
  catalogStatus = status;
  if (reset) currentPage = 1;
  document.getElementById("show-active").setAttribute("aria-pressed", String(status === "active"));
  document.getElementById("show-inactive").setAttribute("aria-pressed", String(status === "inactive"));
}
document.getElementById("show-active").addEventListener("click", () => { switchCatalog("active"); refresh(true); });
document.getElementById("show-inactive").addEventListener("click", () => { switchCatalog("inactive"); refresh(true); });

const statusDialog = document.getElementById("status-dialog");
const statusForm = document.getElementById("status-form");
let statusOperation = null;
function openStatus(product, active) {
  statusOperation = {product, active, request_id: crypto.randomUUID()};
  document.getElementById("status-title").textContent = active ? "重新上架商品" : "下架商品";
  document.getElementById("status-product").textContent = product.name;
  document.getElementById("status-description").textContent = active ? "恢复后商品会重新出现在在售列表，继续使用原有库存。" : "下架后停止购买，库存和交易记录会保留，可随时重新上架。";
  document.getElementById("status-submit").textContent = active ? "确认重新上架" : "确认下架";
  document.getElementById("status-error").textContent = "";
  statusDialog.showModal();
}
document.getElementById("status-close").addEventListener("click", () => statusDialog.close());
statusDialog.addEventListener("cancel", event => { if (document.getElementById("status-submit").disabled) event.preventDefault(); });
statusForm.addEventListener("submit", async event => {
  event.preventDefault();
  const operation = statusOperation;
  if (!operation) return;
  [...statusForm.elements].forEach(element => element.disabled = true);
  try {
    const response = await fetch("/api/product-status", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({product_id: operation.product.id, active: operation.active, request_id: operation.request_id})});
    const result = await response.json();
    if (!response.ok) {
      document.getElementById("status-error").textContent = result.error || "操作失败，请重试";
      return;
    }
    statusDialog.close();
    await refresh(true);
    toast(`${operation.product.name}${operation.active ? "已重新上架" : "已下架"}`);
  } catch (exception) {
    document.getElementById("status-error").textContent = "连接失败，可重试，系统不会重复记录。";
  } finally {
    [...statusForm.elements].forEach(element => element.disabled = false);
  }
});

let ordersPage = 1;
let ordersSequence = 0;
let ordersSignature = null;
async function refreshOrders() {
  const sequence = ++ordersSequence;
  try {
    const response = await fetch(`/api/orders?page=${ordersPage}`, {cache: "no-store"});
    if (!response.ok) throw new Error("订单读取失败");
    const data = await response.json();
    if (sequence !== ordersSequence) return;
    const signature = JSON.stringify(data);
    if (signature === ordersSignature) return;
    ordersSignature = signature;
    ordersPage = data.page;
    document.getElementById("orders-count").textContent = `${data.count} 个订单`;
    const list = document.getElementById("orders-list");
    list.replaceChildren();
    if (!data.orders.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "暂无订单，购买商品后会自动记录。";
      list.append(empty);
    }
    for (const order of data.orders) {
      const row = document.createElement("article");
      row.className = "order-row";
      row.dataset.orderId = order.id;
      row.innerHTML = '<div class="order-info"><strong></strong><small></small><p class="order-items"></p><p class="order-reason"></p></div><div class="order-summary"><strong></strong><span></span><button type="button" class="cancel-order">取消订单</button></div>';
      row.querySelector(".order-info strong").textContent = `订单 ${order.id.slice(0, 12)}`;
      row.querySelector("small").textContent = new Date(order.created_at).toLocaleString("zh-CN");
      row.querySelector(".order-items").textContent = order.items.map(item => `${item.product_name} × ${item.quantity} ${item.unit}（${money(item.unit_price_cents)}/${item.unit}）`).join("；");
      row.querySelector(".order-reason").textContent = order.cancellation_reason ? `取消原因：${order.cancellation_reason}` : "";
      row.querySelector(".order-summary strong").textContent = money(order.total_cents);
      row.querySelector(".order-summary span").textContent = order.status === "completed" ? "已成交" : "已取消";
      const button = row.querySelector("button");
      button.hidden = order.status !== "completed";
      button.addEventListener("click", () => openOperation("cancel", order));
      list.append(row);
    }
    document.getElementById("orders-page").textContent = `第 ${data.page} / ${data.pages} 页`;
    document.getElementById("orders-prev").disabled = data.page === 1;
    document.getElementById("orders-next").disabled = data.page === data.pages;
  } catch (error) {
    if (sequence === ordersSequence) document.getElementById("orders-count").textContent = "订单连接失败";
  }
}
document.getElementById("orders-prev").addEventListener("click", () => { ordersPage--; refreshOrders(); });
document.getElementById("orders-next").addEventListener("click", () => { ordersPage++; refreshOrders(); });

const operationDialog = document.getElementById("operation-dialog");
const operationForm = document.getElementById("operation-form");
let managementOperation = null;
function openOperation(kind, entity) {
  managementOperation = {kind, entity, request_id: crypto.randomUUID()};
  operationForm.reset();
  document.getElementById("operation-title").textContent = kind === "adjust" ? "调整商品库存" : "取消订单";
  document.getElementById("operation-description").textContent = kind === "adjust" ? `${entity.name}当前库存 ${entity.stock} ${entity.unit}。请输入差异数量并记录原因。` : `订单金额 ${money(entity.total_cents)}。取消后商品数量会返回库存，销售统计同步更新。`;
  document.getElementById("delta-label").hidden = kind !== "adjust";
  document.getElementById("operation-delta").required = kind === "adjust";
  document.getElementById("operation-delta").disabled = kind !== "adjust";
  document.getElementById("operation-error").textContent = "";
  document.getElementById("operation-submit").textContent = kind === "adjust" ? "确认调整" : "确认取消订单";
  operationDialog.showModal();
  document.getElementById("operation-reason").focus();
}
document.getElementById("operation-close").addEventListener("click", () => operationDialog.close());
operationDialog.addEventListener("cancel", event => { if (document.getElementById("operation-submit").disabled) event.preventDefault(); });
operationForm.addEventListener("input", () => { if (managementOperation) managementOperation.request_id = crypto.randomUUID(); });
operationForm.addEventListener("submit", async event => {
  event.preventDefault();
  const operation = managementOperation;
  if (!operation) return;
  const payload = {reason: document.getElementById("operation-reason").value.trim(), request_id: operation.request_id};
  if (operation.kind === "adjust") Object.assign(payload, {product_id: operation.entity.id, delta: Number(document.getElementById("operation-delta").value)});
  [...operationForm.elements].forEach(element => element.disabled = true);
  try {
    const url = operation.kind === "adjust" ? "/api/inventory/adjust" : `/api/orders/${operation.entity.id}/cancel`;
    const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) { document.getElementById("operation-error").textContent = result.error; return; }
    operationDialog.close();
    await refresh(true);
    toast(operation.kind === "adjust" ? "库存调整已记录" : "订单已取消，库存已恢复");
  } catch (exception) {
    document.getElementById("operation-error").textContent = "连接失败，可直接重试，系统会避免重复执行。";
  } finally {
    [...operationForm.elements].forEach(element => element.disabled = false);
    document.getElementById("operation-delta").disabled = operation.kind !== "adjust";
  }
});

const ledgerDialog = document.getElementById("ledger-dialog");
let ledgerProduct = null;
let ledgerPage = 1;
let ledgerSequence = 0;
function openLedger(product) {
  ledgerProduct = product;
  ledgerPage = 1;
  document.getElementById("ledger-product").textContent = `${product.name} · ${product.sku}`;
  ledgerDialog.showModal();
  refreshLedger();
}
async function refreshLedger() {
  const sequence = ++ledgerSequence;
  const list = document.getElementById("ledger-list");
  list.textContent = "正在读取台账…";
  try {
    const response = await fetch(`/api/inventory?product_id=${ledgerProduct.id}&page=${ledgerPage}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    if (sequence !== ledgerSequence) return;
    ledgerPage = data.page;
    list.replaceChildren();
    const labels = {opening:"期初库存",listing:"上架入库",purchase:"订单购买",restock:"补货入库",cancel:"订单取消",adjust:"库存调整"};
    for (const entry of data.entries) {
      const row = document.createElement("div");
      row.className = "ledger-entry";
      row.innerHTML = '<div><strong></strong><p></p><small></small></div><div class="ledger-amount"><strong></strong><small></small></div>';
      row.querySelector("strong").textContent = labels[entry.kind] || entry.kind;
      row.querySelector("p").textContent = entry.reason === "initial_stock" ? "新品初始库存" : entry.reason;
      row.querySelector("small").textContent = new Date(entry.created_at).toLocaleString("zh-CN");
      row.querySelector(".ledger-amount strong").textContent = `${entry.delta >= 0 ? "+" : ""}${entry.delta} ${entry.unit}`;
      row.querySelector(".ledger-amount small").textContent = `结存 ${entry.stock_after} ${entry.unit}`;
      list.append(row);
    }
    document.getElementById("ledger-page").textContent = `第 ${data.page} / ${data.pages} 页 · ${data.count} 条`;
    document.getElementById("ledger-prev").disabled = data.page === 1;
    document.getElementById("ledger-next").disabled = data.page === data.pages;
  } catch (error) { if (sequence === ledgerSequence) list.textContent = "台账读取失败，请稍后重新打开。"; }
}
document.getElementById("ledger-close").addEventListener("click", () => ledgerDialog.close());
document.getElementById("ledger-prev").addEventListener("click", () => { ledgerPage--; refreshLedger(); });
document.getElementById("ledger-next").addEventListener("click", () => { ledgerPage++; refreshLedger(); });

const importDialog = document.getElementById("import-dialog");
const importForm = document.getElementById("import-form");
let importRequest = null;
document.getElementById("open-import").addEventListener("click", () => {
  importForm.reset(); importRequest = null;
  document.getElementById("import-error").textContent = "";
  importDialog.showModal();
});
document.getElementById("import-close").addEventListener("click", () => importDialog.close());
document.getElementById("import-file").addEventListener("change", () => { importRequest = null; });
importDialog.addEventListener("cancel", event => { if (document.getElementById("import-fields").disabled) event.preventDefault(); });
importForm.addEventListener("submit", async event => {
  event.preventDefault();
  const file = document.getElementById("import-file").files[0];
  const error = document.getElementById("import-error");
  if (!file || file.size > 1024 * 1024) { error.textContent = "请选择 1 MB 以内的 JSON 文件。"; return; }
  importRequest ||= crypto.randomUUID();
  const fields = document.getElementById("import-fields");
  fields.disabled = true;
  try {
    const parsed = JSON.parse(await file.text());
    const products = Array.isArray(parsed) ? parsed : parsed.products;
    const response = await fetch("/api/products/import", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({products,request_id:importRequest})});
    const result = await response.json();
    if (!response.ok) { error.textContent = result.error; return; }
    importDialog.close();
    clearSearch(); switchCatalog("active");
    await refresh(true);
    toast(`已导入 ${result.count} 个商品`);
  } catch (exception) { error.textContent = exception instanceof SyntaxError ? "JSON 文件格式无效。" : "读取或提交失败，可直接重试。"; }
  finally { fields.disabled = false; }
});

document.getElementById("refresh").addEventListener("click", () => refresh(true));
document.getElementById("logout").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } finally { window.location.href = "/login"; }
});
refresh(true);
setInterval(() => refresh(), 5000);
