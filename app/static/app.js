const AUTH_KEY = "mindbridge.auth";

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  loginForm: document.querySelector("#loginForm"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  loginState: document.querySelector("#loginState")
};

function authHeader(token) {
  return `Basic ${token}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function setPill(el, text, tone = "ok") {
  if (!el) return;
  el.textContent = text;
  el.className = `pill ${tone}`;
}

async function api(path, token, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader(token) };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function saveAuth(token, profile) {
  sessionStorage.setItem(AUTH_KEY, JSON.stringify({
    token,
    username: profile.username,
    displayName: profile.displayName,
    roles: profile.roles || []
  }));
}

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function routeProfile(profile) {
  window.location.assign(isAdmin(profile) ? "/admin.html" : "/student.html");
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health");
    const body = await response.json();
    setPill(els.serviceState, body.status === "UP" ? "服务正常" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setPill(els.serviceState, "服务 DOWN", "danger");
  }
}

async function resumeExistingLogin() {
  const auth = readAuth();
  if (!auth?.token) {
    setPill(els.modelState, "登录后读取", "warn");
    return;
  }
  try {
    const response = await api("/api/profile", auth.token);
    const profile = await response.json();
    saveAuth(auth.token, profile);
    routeProfile(profile);
  } catch {
    sessionStorage.removeItem(AUTH_KEY);
    setPill(els.modelState, "登录后读取", "warn");
  }
}

async function login(event) {
  event.preventDefault();
  const username = els.username.value.trim();
  const password = els.password.value;
  const token = btoa(`${username}:${password}`);
  els.loginState.textContent = "正在登录...";
  try {
    const response = await api("/api/profile", token);
    const profile = await response.json();
    saveAuth(token, profile);
    els.loginState.textContent = "登录成功，正在进入工作台";
    routeProfile(profile);
  } catch (error) {
    sessionStorage.removeItem(AUTH_KEY);
    els.loginState.textContent = `登录失败：${error.message}`;
  }
}

els.loginForm.addEventListener("submit", login);
checkHealth();
resumeExistingLogin();
