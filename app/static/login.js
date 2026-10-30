/* 登录/注册页逻辑（原生 ES2020，无构建） */
"use strict";

const $ = (id) => document.getElementById(id);

function switchTab(which) {
  $("login-form").classList.toggle("hidden", which !== "login");
  $("register-form").classList.toggle("hidden", which !== "register");
  $("tab-login").classList.toggle("active", which === "login");
  $("tab-register").classList.toggle("active", which === "register");
  refreshCaptcha(); // 切换表单换一张，避免旧码被前一次核销
}

/* ---------- 验证码 ---------- */
function refreshCaptcha() {
  // cache-busting 防浏览器复用旧图；服务端每次签发新 id（Cookie 覆盖）
  const bust = Date.now();
  ["login-captcha-img", "reg-captcha-img"].forEach((id) => {
    const img = $(id);
    if (img) {
      img.src = "";
      img.src = `/api/auth/captcha?t=${bust}`;
    }
  });
  ["login-captcha-input", "reg-captcha-input"].forEach((id) => {
    const inp = $(id);
    if (inp) inp.value = "";
  });
}

async function postJSON(path, payload) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

async function init() {
  document.querySelectorAll(".captcha-img").forEach((img) => {
    img.addEventListener("click", refreshCaptcha); // 点击刷新
  });
  refreshCaptcha();

  // 已登录则直接进入主界面
  try {
    const me = await fetch("/api/auth/me").then((r) => (r.ok ? r.json() : null));
    if (me) {
      location.href = "/";
      return;
    }
  } catch (_) { /* 未登录 */ }

  $("tab-login").addEventListener("click", () => switchTab("login"));
  $("tab-register").addEventListener("click", () => switchTab("register"));

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("login-message");
    msg.textContent = "登录中…";
    try {
      await postJSON("/api/auth/login", {
        username: $("login-username").value.trim(),
        password: $("login-password").value,
        captcha: $("login-captcha-input").value.trim(),
      });
      location.href = "/";
    } catch (err) {
      msg.textContent = err.message;
      refreshCaptcha(); // 失败即换新码（旧码已被核销）
    }
  });

  $("register-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("register-message");
    msg.textContent = "注册中…";
    try {
      // 注册成功即登录（服务端直接签发会话 Cookie）
      await postJSON("/api/auth/register", {
        username: $("reg-username").value.trim(),
        email: $("reg-email").value.trim(),
        password: $("reg-password").value,
        inviteCode: $("reg-invite").value.trim(),
        captcha: $("reg-captcha-input").value.trim(),
      });
      location.href = "/";
    } catch (err) {
      msg.textContent = err.message;
      refreshCaptcha();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
