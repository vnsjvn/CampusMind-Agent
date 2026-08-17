const AUTH_KEY = "mindbridge.auth";

const state = {
  sessionId: null,
  sending: false,
  profile: null,
  modelName: "mock"
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  switchAccount: document.querySelector("#switchAccount"),
  messages: document.querySelector("#messages"),
  chatForm: document.querySelector("#chatForm"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  newSession: document.querySelector("#newSession"),
  sessionBadge: document.querySelector("#sessionBadge")
};

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function clearAuth() {
  sessionStorage.removeItem(AUTH_KEY);
}

function authHeader() {
  const auth = readAuth();
  if (!auth?.token) {
    window.location.replace("/");
    return "";
  }
  return `Basic ${auth.token}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function displayModel(model) {
  return (model || "").includes("mindbridge-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
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

async function loadProfile() {
  try {
    const response = await api("/api/profile");
    const profile = await response.json();
    if (isAdmin(profile)) {
      window.location.replace("/admin.html");
      return null;
    }
    state.profile = profile;
    els.activeAccount.textContent = profile.displayName || profile.username;
    return profile;
  } catch {
    clearAuth();
    window.location.replace("/");
    return null;
  }
}

async function loadAgentStatus() {
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  if (status.realModelEnabled) {
    setPill(els.modelState, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
  } else {
    setPill(els.modelState, "mock 演示", "warn");
  }
}

function clearWelcome() {
  const empty = els.messages.querySelector(".empty");
  if (empty) empty.remove();
}

function addMessage(role, content) {
  clearWelcome();
  const row = document.createElement("article");
  row.className = `message ${role}`;
  row.innerHTML = `
    <div class="message-role">${role === "user" ? "我" : "MindBridge"}</div>
    <div class="bubble"></div>
  `;
  row.querySelector(".bubble").textContent = content;
  els.messages.append(row);
  els.messages.scrollTop = els.messages.scrollHeight;
  return row.querySelector(".bubble");
}

function parseSse(buffer, onEvent) {
  const parts = buffer.split("\n\n");
  const rest = parts.pop();
  for (const part of parts) {
    const dataLine = part.split("\n").find((line) => line.startsWith("data: "));
    if (!dataLine) continue;
    onEvent(JSON.parse(dataLine.slice(6)));
  }
  return rest;
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending) return;
  const message = els.messageInput.value.trim();
  if (!message) return;
  state.sending = true;
  els.sendButton.disabled = true;
  setPill(els.sessionBadge, "THINKING", "warn");
  els.messageInput.value = "";
  addMessage("user", message);
  const assistant = addMessage("assistant", "");
  let raw = "";

  try {
    const response = await api("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: state.sessionId, message })
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamFailed = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, (eventData) => {
        if (eventData.type === "meta") state.sessionId = eventData.sessionId;
        if (eventData.type === "token") {
          raw += eventData.content || "";
          assistant.textContent = raw;
          els.messages.scrollTop = els.messages.scrollHeight;
        }
        if (eventData.type === "error") {
          streamFailed = true;
          if (!raw) assistant.textContent = eventData.message || "MCP 工具调用失败";
          setPill(els.sessionBadge, "ERROR", "danger");
        }
      });
    }
    if (!streamFailed) setPill(els.sessionBadge, "DONE", "ok");
  } catch (error) {
    assistant.textContent = `发送失败：${error.message}`;
    setPill(els.sessionBadge, "ERROR", "danger");
  } finally {
    state.sending = false;
    els.sendButton.disabled = false;
  }
}

function resetSession() {
  state.sessionId = null;
  els.messages.innerHTML = `<div class="empty"><strong>新会话已开始</strong><p>你可以继续输入新的问题。</p></div>`;
  setPill(els.sessionBadge, "READY");
}

function logout() {
  clearAuth();
  window.location.assign("/");
}

document.querySelectorAll("[data-quick]").forEach((button) => {
  button.addEventListener("click", () => {
    els.messageInput.value = button.dataset.quick;
    els.messageInput.focus();
  });
});
els.chatForm.addEventListener("submit", sendMessage);
els.newSession.addEventListener("click", resetSession);
els.switchAccount.addEventListener("click", logout);

checkHealth();
loadProfile().then((profile) => {
  if (profile) loadAgentStatus();
});
