const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("登录已过期");
  }
  return response;
};

const productGrid = document.getElementById("product-grid");
const catalogSummary = document.getElementById("catalog-summary");
const pageSummary = document.getElementById("page-summary");
const pagination = document.getElementById("pagination");
const ordersList = document.getElementById("orders-list");
const ordersCount = document.getElementById("orders-count");
const ordersPageLabel = document.getElementById("orders-page");
const ordersPrev = document.getElementById("orders-prev");
const ordersNext = document.getElementById("orders-next");
const purchaseDialog = document.getElementById("purchase-dialog");
const purchaseForm = document.getElementById("purchase-form");
const quantityInput = document.getElementById("quantity");
const cancelDialog = document.getElementById("cancel-dialog");
const cancelForm = document.getElementById("cancel-form");
const toastElement = document.getElementById("toast");
let currentPage = 1;
let ordersPage = 1;
let filters = { search: "", category: "" };
let purchaseProduct = null;
let purchaseRequestId = null;
let cancelTarget = null;
let cancelRequestId = null;
let toastTimer = null;

function money(cents) {
  return `¥${(cents / 100).toFixed(2)}`;
}

function toast(message, error = false) {
  toastElement.textContent = message;
  toastElement.classList.toggle("error", error);
  toastElement.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastElement.classList.remove("visible"), 3200);
}

function productPicture(product, extraClass = "") {
  const image = document.createElement("img");
  image.className = `product-photo ${extraClass}`.trim();
  image.loading = "lazy";
  image.alt = product.name;
  if (product.image_url) {
    image.src = product.image_url;
    image.addEventListener("error", () => {
      image.src = "/product-placeholder.svg";
      image.classList.add("placeholder-photo");
    });
  } else {
    image.src = "/product-placeholder.svg";
    image.classList.add("placeholder-photo");
    image.alt = `${product.name}暂无商品图片`;
  }
  return image;
}

function createProductCard(product) {
  const card = document.createElement("article");
  card.className = "product-card";
  card.innerHTML = `
    <div class="product-visual"><span class="stock-pill"></span></div>
    <div class="product-body">
      <div class="product-title-row"><div><h3></h3><p class="product-category"></p></div><div class="price"></div></div>
      <p class="product-description"></p>
      <div class="stock-line"><span>实时库存</span><strong></strong></div>
      <div class="stock-track"><div class="stock-fill"></div></div>
      <div class="product-actions"><button class="primary-button" type="button">立即购买</button></div>
    </div>`;
  const visual = card.querySelector(".product-visual");
  visual.append(productPicture(product));
  const pill = card.querySelector(".stock-pill");
  pill.textContent = product.stock === 0 ? "已售罄" : product.stock <= 5 ? "库存紧张" : "现货供应";
  pill.classList.toggle("low", product.stock > 0 && product.stock <= 5);
  pill.classList.toggle("out", product.stock === 0);
  card.querySelector("h3").textContent = product.name;
  card.querySelector(".product-category").textContent = `${product.category} · ${product.zone}`;
  card.querySelector(".product-description").textContent = product.description || product.sku;
  const price = card.querySelector(".price");
  price.textContent = money(product.price_cents);
  const unit = document.createElement("small");
  unit.textContent = ` / ${product.unit}`;
  price.append(unit);
  card.querySelector(".stock-line strong").textContent = `${product.stock} ${product.unit}`;
  const fill = card.querySelector(".stock-fill");
  fill.style.width = `${Math.min(100, product.stock / 30 * 100)}%`;
  fill.classList.toggle("low", product.stock > 0 && product.stock <= 5);
  fill.classList.toggle("out", product.stock === 0);
  const button = card.querySelector(".primary-button");
  button.textContent = product.stock === 0 ? "暂时缺货" : "立即购买";
  button.disabled = product.stock === 0;
  button.addEventListener("click", () => openPurchase(product));
  return card;
}

async function loadCatalog() {
  try {
    const query = new URLSearchParams({ page: currentPage, status: "active", ...filters });
    const response = await fetch(`/api/state?${query}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "读取商品失败");
    currentPage = data.page;
    productGrid.replaceChildren(...data.products.map(createProductCard));
    if (!data.products.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = filters.search || filters.category ? "没有符合条件的商品，换个条件试试。" : "暂时没有在售商品。";
      productGrid.append(empty);
    }
    catalogSummary.textContent = `共 ${data.product_count} 个在售商品`;
    const first = data.product_count ? (data.page - 1) * 6 + 1 : 0;
    const last = Math.min(data.page * 6, data.product_count);
    pageSummary.textContent = `显示 ${first}–${last} 个 · 第 ${data.page} / ${data.pages} 页`;
    renderPagination(data);
  } catch (error) {
    productGrid.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "商品读取失败，请稍后刷新。";
    productGrid.append(empty);
    catalogSummary.textContent = "商品读取失败";
  }
}

function renderPagination(data) {
  pagination.replaceChildren();
  function button(label, page, disabled = false, selected = false) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.disabled = disabled;
    if (selected) element.setAttribute("aria-current", "page");
    element.addEventListener("click", () => {
      currentPage = page;
      loadCatalog();
    });
    pagination.append(element);
  }
  button("上一页", data.page - 1, data.page === 1);
  const pages = [...new Set([1, data.pages, data.page - 1, data.page, data.page + 1])]
    .filter(page => page >= 1 && page <= data.pages).sort((a, b) => a - b);
  let previous = 0;
  for (const page of pages) {
    if (previous && page - previous > 1) {
      const gap = document.createElement("span");
      gap.textContent = "…";
      pagination.append(gap);
    }
    button(String(page), page, false, page === data.page);
    previous = page;
  }
  button("下一页", data.page + 1, data.page === data.pages);
}

async function loadCategories() {
  try {
    const response = await fetch("/api/categories");
    const data = await response.json();
    if (!response.ok) return;
    const select = document.getElementById("category");
    for (const category of data.categories) {
      const option = document.createElement("option");
      option.value = category;
      option.textContent = category;
      select.append(option);
    }
  } catch (error) {
    // The filter still works with the default "all categories" option.
  }
}

async function loadOrders() {
  try {
    const response = await fetch(`/api/orders?page=${ordersPage}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "读取订单失败");
    ordersPage = data.page;
    ordersCount.textContent = `${data.count} 个订单`;
    ordersList.replaceChildren();
    if (!data.orders.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "当前会话还没有订单，先挑一件商品吧。";
      ordersList.append(empty);
    }
    for (const order of data.orders) {
      const row = document.createElement("article");
      row.className = "order-row";
      row.innerHTML = '<div class="order-info"><strong></strong><small></small><p class="order-items"></p><p class="order-reason"></p></div><div class="order-summary"><strong></strong><span></span><button class="cancel-order" type="button">取消订单</button></div>';
      row.querySelector(".order-info strong").textContent = `订单 ${order.id.slice(0, 12)}`;
      row.querySelector(".order-info small").textContent = new Date(order.created_at).toLocaleString("zh-CN");
      row.querySelector(".order-items").textContent = order.items.map(item => `${item.product_name} × ${item.quantity} ${item.unit}（${money(item.unit_price_cents)}/${item.unit}）`).join("；");
      row.querySelector(".order-reason").textContent = order.cancellation_reason ? `取消原因：${order.cancellation_reason}` : "";
      row.querySelector(".order-summary strong").textContent = money(order.total_cents);
      row.querySelector(".order-summary span").textContent = order.status === "completed" ? "已成交" : "已取消";
      const cancel = row.querySelector(".cancel-order");
      cancel.hidden = order.status !== "completed";
      cancel.addEventListener("click", () => openCancel(order));
      ordersList.append(row);
    }
    ordersPageLabel.textContent = `第 ${data.page} / ${data.pages} 页`;
    ordersPrev.disabled = data.page === 1;
    ordersNext.disabled = data.page === data.pages;
  } catch (error) {
    ordersCount.textContent = "订单读取失败";
  }
}

function openPurchase(product) {
  purchaseProduct = product;
  purchaseRequestId = null;
  document.getElementById("purchase-error").textContent = "";
  document.getElementById("purchase-title").textContent = product.name;
  document.getElementById("purchase-description").textContent = `${product.category} · ${product.zone} · 当前库存 ${product.stock} ${product.unit}`;
  document.getElementById("quantity-hint").textContent = `最多 ${Math.min(9999, product.stock)} ${product.unit}`;
  const visual = document.getElementById("purchase-visual");
  visual.replaceChildren(productPicture(product));
  quantityInput.value = "1";
  quantityInput.max = String(Math.min(9999, product.stock));
  updateTotal();
  purchaseDialog.showModal();
  quantityInput.focus();
}

function updateTotal() {
  if (!purchaseProduct) return;
  const quantity = Number(quantityInput.value) || 0;
  document.getElementById("purchase-total").textContent = money(quantity * purchaseProduct.price_cents);
}

document.getElementById("purchase-close").addEventListener("click", () => purchaseDialog.close());
document.getElementById("decrease").addEventListener("click", () => {
  quantityInput.value = Math.max(1, (Number(quantityInput.value) || 1) - 1);
  quantityInput.dispatchEvent(new Event("input"));
});
document.getElementById("increase").addEventListener("click", () => {
  quantityInput.value = Math.min(Number(quantityInput.max), (Number(quantityInput.value) || 0) + 1);
  quantityInput.dispatchEvent(new Event("input"));
});
quantityInput.addEventListener("input", () => {
  purchaseRequestId = null;
  document.getElementById("purchase-error").textContent = "";
  updateTotal();
});

purchaseForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!purchaseProduct) return;
  const quantity = Number(quantityInput.value);
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > Number(quantityInput.max)) {
    document.getElementById("purchase-error").textContent = "请输入有效数量。";
    return;
  }
  purchaseRequestId ||= crypto.randomUUID();
  const product = purchaseProduct;
  const button = document.getElementById("purchase-submit");
  [...purchaseForm.elements].forEach(element => { element.disabled = true; });
  try {
    const response = await fetch("/api/movements", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "purchase", product_id: product.id, quantity, request_id: purchaseRequestId }),
    });
    const data = await response.json();
    if (!response.ok) {
      purchaseRequestId = null;
      document.getElementById("purchase-error").textContent = data.error || "购买失败，请重试。";
      loadCatalog();
      return;
    }
    purchaseDialog.close();
    toast(`${product.name}购买成功 · ${quantity} ${product.unit}`);
    await Promise.all([loadCatalog(), loadOrders()]);
  } catch (error) {
    document.getElementById("purchase-error").textContent = "连接失败，可直接重试，系统会避免重复提交。";
  } finally {
    [...purchaseForm.elements].forEach(element => { element.disabled = false; });
  }
});

function openCancel(order) {
  cancelTarget = order;
  cancelRequestId = null;
  document.getElementById("cancel-reason").value = "";
  document.getElementById("cancel-error").textContent = "";
  document.getElementById("cancel-description").textContent = `订单金额 ${money(order.total_cents)}。取消后库存会恢复，操作不可撤销。`;
  cancelDialog.showModal();
  document.getElementById("cancel-reason").focus();
}

document.getElementById("cancel-close").addEventListener("click", () => cancelDialog.close());
document.getElementById("cancel-reason").addEventListener("input", () => {
  cancelRequestId = null;
  document.getElementById("cancel-error").textContent = "";
});

cancelForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!cancelTarget) return;
  const reason = document.getElementById("cancel-reason").value.trim();
  if (!reason) {
    document.getElementById("cancel-error").textContent = "请填写取消原因。";
    return;
  }
  cancelRequestId ||= crypto.randomUUID();
  const button = document.getElementById("cancel-submit");
  [...cancelForm.elements].forEach(element => { element.disabled = true; });
  try {
    const response = await fetch(`/api/orders/${cancelTarget.id}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, request_id: cancelRequestId }),
    });
    const data = await response.json();
    if (!response.ok) {
      document.getElementById("cancel-error").textContent = data.error || "取消失败，请重试。";
      return;
    }
    cancelDialog.close();
    toast("订单已取消，库存已恢复");
    await Promise.all([loadCatalog(), loadOrders()]);
  } catch (error) {
    document.getElementById("cancel-error").textContent = "连接失败，可直接重试，系统会避免重复执行。";
  } finally {
    [...cancelForm.elements].forEach(element => { element.disabled = false; });
  }
});

document.getElementById("search-form").addEventListener("submit", event => {
  event.preventDefault();
  filters = {
    search: document.getElementById("search").value.trim(),
    category: document.getElementById("category").value,
  };
  currentPage = 1;
  loadCatalog();
});
document.getElementById("category").addEventListener("change", () => {
  filters.category = document.getElementById("category").value;
  currentPage = 1;
  loadCatalog();
});
ordersPrev.addEventListener("click", () => { ordersPage--; loadOrders(); });
ordersNext.addEventListener("click", () => { ordersPage++; loadOrders(); });
document.getElementById("logout").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } finally { window.location.href = "/login"; }
});

loadCategories();
loadCatalog();
loadOrders();
