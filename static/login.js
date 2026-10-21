const loginError = document.getElementById("login-error");
const roleButtons = [...document.querySelectorAll("[data-role]")];

for (const button of roleButtons) {
  button.addEventListener("click", async () => {
    loginError.textContent = "";
    roleButtons.forEach(item => { item.disabled = true; });
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: button.dataset.role }),
      });
      const data = await response.json();
      if (!response.ok) {
        loginError.textContent = data.error || "选择身份失败，请重试。";
        return;
      }
      window.location.href = data.redirect;
    } catch (error) {
      loginError.textContent = "连接失败，请确认服务已经启动。";
    } finally {
      roleButtons.forEach(item => { item.disabled = false; });
    }
  });
}
