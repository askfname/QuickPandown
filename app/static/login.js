/* 登录/注册页逻辑（原生 ES2020，无构建） */
"use strict";

const $ = (id) => document.getElementById(id);
const USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/;
const CAPTCHA_RE = /^[A-Za-z0-9]{4}$/;

// 注册策略默认值：接口失败时按原有邀请码流程展示
let inviteRequired = true;

// 联系方式预置图标（图标 ID 由服务端白名单约束，不接受任意 SVG）
const INVITE_ICONS = {
  telegram:
    '<path fill="currentColor" d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/>',
  qq: '<path fill="currentColor" d="M21.395 15.035a40 40 0 0 0-.803-2.264l-1.079-2.695c.001-.032.014-.562.014-.836C19.526 4.632 17.351 0 12 0S4.474 4.632 4.474 9.241c0 .274.013.804.014.836l-1.08 2.695a39 39 0 0 0-.802 2.264c-1.021 3.283-.69 4.643-.438 4.673.54.065 2.103-2.472 2.103-2.472 0 1.469.756 3.387 2.394 4.771-.612.188-1.363.479-1.845.835-.434.32-.379.646-.301.778.343.578 5.883.369 7.482.189 1.6.18 7.14.389 7.483-.189.078-.132.132-.458-.301-.778-.483-.356-1.233-.646-1.846-.836 1.637-1.384 2.393-3.302 2.393-4.771 0 0 1.563 2.537 2.103 2.472.251-.03.581-1.39-.438-4.673"/>',
  whatsapp:
    '<path fill="currentColor" d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.626.712.226 1.36.194 1.872.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413z"/>',
  discord:
    '<path fill="currentColor" d="M20.3 4.5A19.8 19.8 0 0 0 15.4 3l-.6 1.3c-1.8-.3-3.7-.3-5.6 0L8.6 3a19.8 19.8 0 0 0-4.9 1.5C.5 9.4-.3 14.2.1 18.9a19.9 19.9 0 0 0 6 3l1.3-2.1c-.7-.3-1.4-.6-2-1l.5-.4a14.2 14.2 0 0 0 12.2 0l.5.4c-.6.4-1.3.7-2 1l1.3 2.1a19.9 19.9 0 0 0 6-3c.5-5.4-.8-10.1-3.6-14.4zM8 15.3c-1.2 0-2.1-1.1-2.1-2.4S6.8 10.5 8 10.5s2.1 1.1 2.1 2.4-.9 2.4-2.1 2.4zm8 0c-1.2 0-2.1-1.1-2.1-2.4s.9-2.4 2.1-2.4 2.1 1.1 2.1 2.4-.9 2.4-2.1 2.4z"/>',
  email:
    '<path fill="currentColor" d="M3 5h18a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm0 2v.4l9 6.3 9-6.3V7H3zm18 2.8-8.4 5.9a1 1 0 0 1-1.2 0L3 9.8V17h18v-7.2z"/>',
  link:
    '<path fill="currentColor" d="M10.6 13.4a1 1 0 0 1 0-1.4l1.4-1.4a1 1 0 0 1 1.4 1.4l-1.4 1.4a1 1 0 0 1-1.4 0zm-3.8 5.2a4 4 0 0 1 0-5.7l2.8-2.8 1.4 1.4-2.8 2.8a2 2 0 1 0 2.8 2.8l2.8-2.8 1.4 1.4-2.8 2.8a4 4 0 0 1-5.6.1zm7-10.4 1.4-1.4a4 4 0 0 1 5.7 5.7l-2.8 2.8-1.4-1.4 2.8-2.8a2 2 0 1 0-2.8-2.8l-1.4 1.4-1.4-1.4zM8.6 18a1 1 0 0 1-1.4-1.4l5.6-5.6A1 1 0 0 1 14.2 12l-5.6 6z"/>',
};

function setInvalid(input, invalid) {
  if (!input) return;
  input.classList.toggle("invalid", invalid);
  if (invalid) input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
}

function clearInvalid(form) {
  if (!form) return;
  form.querySelectorAll("input").forEach((input) => setInvalid(input, false));
}

function showValidationErrors(bad) {
  bad.forEach(({ input }) => setInvalid(input, true));
  const first = bad[0];
  first.input.focus();
  return first.message;
}

function switchTab(which) {
  $("login-form").classList.toggle("hidden", which !== "login");
  $("register-form").classList.toggle("hidden", which !== "register");
  $("tab-login").classList.toggle("active", which === "login");
  $("tab-register").classList.toggle("active", which === "register");
  clearInvalid($("login-form"));
  clearInvalid($("register-form"));
  $("login-message").textContent = "";
  $("register-message").textContent = "";
  refreshCaptcha(); // 切换表单换一张，避免旧码被前一次核销
}

/* ---------- 验证码 ---------- */
// 签发被限速（429）或网络失败时展示的占位图，点击可重试
const CAPTCHA_FALLBACK =
  "data:image/svg+xml," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 84 38">' +
      '<rect width="84" height="38" fill="#eef3fb"/>' +
      '<text x="42" y="24" text-anchor="middle" font-family="sans-serif" ' +
      'font-size="13" fill="#64748b">刷新重试</text></svg>'
  );

function visibleFormName() {
  // 返回元素 id 前缀：login-* / reg-*（注册表单的 id 是 register-form）
  return $("register-form").classList.contains("hidden") ? "login" : "reg";
}

function refreshCaptcha() {
  // 只刷新当前可见表单：服务端只有一枚 qp_cap Cookie，同时请求两张图会互相覆盖
  // （先返回的图片对应旧 id，提交时就会误报验证码错误）
  const which = visibleFormName();
  const img = $(`${which}-captcha-img`);
  const inp = $(`${which}-captcha-input`);
  if (img) img.src = `/api/auth/captcha?t=${Date.now()}`;
  if (inp) {
    inp.value = "";
    setInvalid(inp, false);
  }
}

/* ---------- 表单校验 ---------- */
function loginErrors() {
  const username = $("login-username");
  const password = $("login-password");
  const captcha = $("login-captcha-input");
  const bad = [];
  if (!username.value.trim()) bad.push({ input: username, message: "请输入用户名" });
  if (!password.value) bad.push({ input: password, message: "请输入密码" });
  if (!CAPTCHA_RE.test(captcha.value.trim())) {
    bad.push({ input: captcha, message: "请输入 4 位验证码" });
  }
  return bad;
}

function registerErrors() {
  const username = $("reg-username");
  const email = $("reg-email");
  const password = $("reg-password");
  const invite = $("reg-invite");
  const captcha = $("reg-captcha-input");
  const bad = [];
  if (!USERNAME_RE.test(username.value.trim())) {
    bad.push({ input: username, message: "用户名需为 3-32 位字母、数字、下划线或中划线" });
  }
  if (!EMAIL_RE.test(email.value.trim())) {
    bad.push({ input: email, message: "请输入有效邮箱地址" });
  }
  if (password.value.length < 8) {
    bad.push({ input: password, message: "密码至少 8 位" });
  }
  if (inviteRequired && !invite.value.trim()) {
    bad.push({ input: invite, message: "请输入邀请码" });
  }
  if (!CAPTCHA_RE.test(captcha.value.trim())) {
    bad.push({ input: captcha, message: "请输入 4 位验证码" });
  }
  return bad;
}

function markLoginServerError(message) {
  const text = String(message || "");
  if (text.includes("验证码")) setInvalid($("login-captcha-input"), true);
  if (text.includes("用户名或密码")) {
    setInvalid($("login-username"), true);
    setInvalid($("login-password"), true);
  } else if (text.includes("用户名")) {
    setInvalid($("login-username"), true);
  } else if (text.includes("密码")) {
    setInvalid($("login-password"), true);
  }
}

function markRegisterServerError(message) {
  const text = String(message || "");
  if (text.includes("验证码")) setInvalid($("reg-captcha-input"), true);
  if (text.includes("用户名")) setInvalid($("reg-username"), true);
  if (text.includes("邮箱")) setInvalid($("reg-email"), true);
  if (text.includes("密码")) setInvalid($("reg-password"), true);
  if (text.includes("邀请码")) setInvalid($("reg-invite"), true);
}

function bindInvalidClear() {
  document.querySelectorAll(".login-form input").forEach((input) => {
    const clear = () => {
      setInvalid(input, false);
      const msg = input.closest(".login-form")?.querySelector(".hint");
      if (msg) msg.textContent = "";
    };
    input.addEventListener("input", clear);
    input.addEventListener("change", clear);
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

function createInviteAction(contact) {
  const iconKey = INVITE_ICONS[contact.icon] ? contact.icon : "link";
  const value = String(contact.url || "").trim();
  const isEmail = iconKey === "email";
  if (isEmail && !/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(value)) return null;
  if (!isEmail && !/^https?:\/\//i.test(value)) return null;

  const a = document.createElement("a");
  a.className = "invite-action";
  a.href = isEmail ? `mailto:${value}` : value;
  if (!isEmail) {
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  }
  const name = String(contact.name || "获取邀请码");
  a.title = isEmail ? `${name}（发送邮件）` : `${name}（新窗口打开）`;
  const icon = document.createElement("span");
  icon.className = "invite-action-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = `<svg viewBox="0 0 24 24" focusable="false">${
    INVITE_ICONS[iconKey]
  }</svg>`; // icon 已由服务端白名单校验
  const label = document.createElement("span");
  label.className = "invite-action-label";
  label.textContent = name;
  a.append(icon, label);
  return a;
}

function applyRegistrationPolicy(policy) {
  inviteRequired = policy.invite_required !== false;
  const field = $("reg-invite-field");
  if (field) field.classList.toggle("hidden", !inviteRequired);
  const invite = $("reg-invite");
  if (invite) invite.required = inviteRequired;

  const help = $("invite-help");
  if (!help) return;
  const text = String(policy.tip_text || "").trim();
  const contacts = Array.isArray(policy.contacts) ? policy.contacts : [];
  const show = inviteRequired && policy.show_tip !== false && (text || contacts.length);
  help.classList.toggle("hidden", !show);
  if (!show) return;

  const tip = help.querySelector(".invite-help-text");
  if (tip) {
    tip.textContent = text;
    tip.classList.toggle("hidden", !text);
  }
  const actions = help.querySelector(".invite-actions");
  if (!actions) return;
  actions.textContent = "";
  contacts.forEach((contact) => {
    const action = createInviteAction(contact || {});
    if (action) actions.appendChild(action);
  });
  actions.classList.toggle("hidden", !actions.childElementCount);
}

async function loadRegistrationPolicy() {
  try {
    const resp = await fetch("/api/auth/registration-policy", { cache: "no-store" });
    if (!resp.ok) return;
    applyRegistrationPolicy(await resp.json());
  } catch (_) {
    /* 接口失败保留默认邀请码流程 */
  }
}

async function init() {
  bindInvalidClear();
  document.querySelectorAll(".captcha-img").forEach((img) => {
    img.addEventListener("click", refreshCaptcha); // 点击刷新
    img.addEventListener("error", () => {
      // 签发接口按 IP 限速（429）或网络异常：给出可理解的提示
      const msg = img.closest(".login-form")?.querySelector(".hint");
      if (msg) msg.textContent = "验证码加载失败，请稍后点击图片重试";
      if (!img.src.startsWith("data:")) img.src = CAPTCHA_FALLBACK;
    });
    img.addEventListener("load", () => {
      if (img.src.startsWith("data:")) return; // 占位图不清除提示
      const msg = img.closest(".login-form")?.querySelector(".hint");
      if (msg && msg.textContent === "验证码加载失败，请稍后点击图片重试") {
        msg.textContent = "";
      }
    });
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

  await loadRegistrationPolicy();

  $("tab-login").addEventListener("click", () => switchTab("login"));
  $("tab-register").addEventListener("click", () => switchTab("register"));

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("login-message");
    clearInvalid($("login-form"));
    const bad = loginErrors();
    if (bad.length) {
      msg.textContent = showValidationErrors(bad);
      return;
    }
    msg.textContent = "登录中…";
    $("login-btn").disabled = true;
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
      markLoginServerError(err.message);
    } finally {
      $("login-btn").disabled = false;
    }
  });

  $("register-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("register-message");
    clearInvalid($("register-form"));
    const bad = registerErrors();
    if (bad.length) {
      msg.textContent = showValidationErrors(bad);
      return;
    }
    msg.textContent = "注册中…";
    $("register-btn").disabled = true;
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
      markRegisterServerError(err.message);
    } finally {
      $("register-btn").disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
