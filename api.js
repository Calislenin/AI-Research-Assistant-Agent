export const API_BASE_URL = "http://127.0.0.1:8001";

export const getHeaders = () => {
  const customKey = localStorage.getItem("gemini_api_key");
  const headers = { "Content-Type": "application/json" };
  if (customKey) {
    headers["X-API-Key"] = customKey;
  }
  return headers;
};

export const api = {
  async checkHealth() {
    const res = await fetch(`${API_BASE_URL}/api/system/health`, { headers: getHeaders() });
    return res.ok ? await res.json() : null;
  },

  async login(email, password) {
    try {
      const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password })
      });
      if (res.ok) return await res.json();
    } catch (e) {
      console.warn("Backend auth unreachable, falling back to local session:", e);
    }
    return { user: { email, authenticatedAt: new Date().toISOString(), isLocal: true } };
  },

  async signup(email, password) {
    try {
      const res = await fetch(`${API_BASE_URL}/api/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password })
      });
      if (res.ok) return await res.json();
    } catch (e) {
      console.warn("Backend auth unreachable, falling back to local session:", e);
    }
    return { user: { email, authenticatedAt: new Date().toISOString(), isLocal: true } };
  },

  async uploadFiles(files) {
    const formData = new FormData();
    files.forEach(f => formData.append("files", f));
    const res = await fetch(`${API_BASE_URL}/api/research/upload`, {
      method: "POST",
      body: formData
    });
    if (!res.ok) throw new Error("File upload failed");
    return await res.json();
  },

  async startResearch(payload) {
    const res = await fetch(`${API_BASE_URL}/api/research/start`, {
      method: "POST",
      headers: getHeaders(),
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to start research");
    }
    return await res.json();
  },

  async startInstantDemo() {
    const res = await fetch(`${API_BASE_URL}/api/sample-demo`, {
      method: "POST",
      headers: getHeaders()
    });
    if (!res.ok) throw new Error("Could not start instant demo");
    return await res.json();
  },

  async getReport(projectId) {
    const res = await fetch(`${API_BASE_URL}/api/research/${projectId}/report`);
    if (!res.ok) throw new Error("Report not ready");
    return await res.json();
  },

  async sendChatMessage(projectId, message, history) {
    const res = await fetch(`${API_BASE_URL}/api/chat/message`, {
      method: "POST",
      headers: getHeaders(),
      body: JSON.stringify({ project_id: projectId, message, history })
    });
    if (!res.ok) throw new Error("Chat request failed");
    return await res.json();
  },

  async getProjects() {
    const res = await fetch(`${API_BASE_URL}/api/projects`);
    if (!res.ok) return [];
    return await res.json();
  },

  getDownloadPdfUrl(reportId) {
    return `${API_BASE_URL}/api/reports/${reportId}/download-pdf`;
  }
};
