import React, { useState, useEffect } from 'react';
import confetti from 'canvas-confetti';
import Navbar from './components/Navbar.jsx';
import SetupView from './components/SetupView.jsx';
import ProgressView from './components/ProgressView.jsx';
import ReportView from './components/ReportView.jsx';
import AuthModal from './components/modals/AuthModal.jsx';
import SettingsModal from './components/modals/SettingsModal.jsx';
import HistoryModal from './components/modals/HistoryModal.jsx';
import AuditModal from './components/modals/AuditModal.jsx';
import CitationModal from './components/modals/CitationModal.jsx';
import { api, API_BASE_URL } from './services/api';

export default function App() {
  const [currentView, setCurrentView] = useState('setup'); // 'setup' | 'progress' | 'report'
  const [isDark, setIsDark] = useState(true);
  const [authUser, setAuthUser] = useState(null);
  const [llmMode, setLlmMode] = useState("Gemini 2.0 Flash");

  // Workflow state
  const [currentProjectId, setCurrentProjectId] = useState(null);
  const [currentTopic, setCurrentTopic] = useState("");
  const [currentQuestion, setCurrentQuestion] = useState("");
  const [progress, setProgress] = useState(5);
  const [activeAgent, setActiveAgent] = useState("Research Planning Agent");
  const [statusMessage, setStatusMessage] = useState("");
  const [logs, setLogs] = useState([]);
  const [report, setReport] = useState(null);

  // Modals
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [authModalMode, setAuthModalMode] = useState("login");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const [citationModalData, setCitationModalData] = useState(null);

  // Init Auth & Health
  useEffect(() => {
    try {
      const stored = localStorage.getItem("auth_user");
      if (stored) {
        setAuthUser(JSON.parse(stored));
      } else {
        setAuthModalMode("login");
        setAuthModalOpen(true);
      }
    } catch (e) {
      setAuthModalOpen(true);
    }

    api.checkHealth().then(data => {
      if (data) {
        setLlmMode(data.has_api_key ? data.live_llm_mode : "Offline Demo Mode");
      }
    });
  }, []);

  const handleAuthSuccess = (user) => {
    localStorage.setItem("auth_user", JSON.stringify(user));
    setAuthUser(user);
    setAuthModalOpen(false);
  };

  const handleLogout = () => {
    localStorage.removeItem("auth_user");
    setAuthUser(null);
    setAuthModalMode("login");
    setAuthModalOpen(true);
  };

  const handleCreateAccount = () => {
    localStorage.removeItem("auth_user");
    setAuthUser(null);
    setAuthModalMode("signup");
    setAuthModalOpen(true);
  };

  const startStream = (projectId) => {
    const sse = new EventSource(`${API_BASE_URL}/api/research/${projectId}/stream`);
    sse.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.stage_progress !== undefined) setProgress(data.stage_progress);
        if (data.current_agent) setActiveAgent(data.current_agent);
        if (data.status_message) setStatusMessage(data.status_message);
        if (data.logs) setLogs(data.logs);

        if (data.type === "complete" || data.stage_progress === 100) {
          sse.close();
          if (data.report) {
            setReport(data.report);
            setCurrentView('report');
            try { confetti({ particleCount: 100, spread: 70, origin: { y: 0.6 } }); } catch (e) {}
          }
        }
      } catch (err) {
        console.error("SSE parse error:", err);
      }
    };
    sse.onerror = () => {
      sse.close();
      // Poll fallback
      api.getReport(projectId).then(rep => {
        setReport(rep);
        setCurrentView('report');
      }).catch(console.warn);
    };
  };

  const handleStartResearch = async ({ topic, question, focus, files }) => {
    try {
      let fileIds = [];
      if (files && files.length > 0) {
        const docs = await api.uploadFiles(files);
        fileIds = docs.map(d => d.id);
      }
      const data = await api.startResearch({
        topic,
        research_question: question,
        focus,
        file_ids: fileIds,
        use_sample_data: fileIds.length === 0
      });

      setCurrentProjectId(data.project_id);
      setCurrentTopic(topic);
      setCurrentQuestion(question);
      setProgress(10);
      setCurrentView('progress');
      startStream(data.project_id);
    } catch (err) {
      alert(`Research launch failed: ${err.message}`);
    }
  };

  const handleRunDemo = async () => {
    try {
      const data = await api.startInstantDemo();
      setCurrentProjectId(data.project_id);
      setCurrentTopic("Retrieval-Augmented Generation for Clinical Decision Support");
      setCurrentQuestion("How does retrieval-augmented generation reduce hallucination rates in diagnostic reasoning compared to fine-tuning?");
      setProgress(10);
      setCurrentView('progress');
      startStream(data.project_id);
    } catch (err) {
      alert(`Demo failed: ${err.message}`);
    }
  };

  const handleDownloadPdf = () => {
    const id = report?.report_id || currentProjectId;
    if (!id) return;
    const url = api.getDownloadPdfUrl(id);
    const a = document.createElement("a");
    a.href = url;
    a.download = `Research_Report_${id}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  const handleCopyMarkdown = () => {
    if (!report) return;
    let md = `# ${report.topic}\n\n**Core Question:** ${report.research_question}\n\n---\n\n`;
    (report.sections || []).forEach(s => {
      md += `## ${s.number}. ${s.title}\n\n${s.content}\n\n`;
    });
    navigator.clipboard.writeText(md).then(() => alert("Report copied as Markdown!"));
  };

  const handleOpenCitation = (citId) => {
    const map = report?.citations_map || {};
    const data = map[citId] || {
      citation_id: citId,
      doc_title: "Analyzed Research Literature",
      filename: "source.pdf",
      page: 1,
      quote_snippet: "Direct evidence passage from source document."
    };
    setCitationModalData(data);
  };

  return (
    <div className="relative z-10 flex flex-col min-h-screen">
      <Navbar
        llmMode={llmMode}
        authUser={authUser}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenHistory={() => setHistoryOpen(true)}
        onOpenAuth={(mode) => { setAuthModalMode(mode); setAuthModalOpen(true); }}
        onLogout={handleLogout}
        onCreateAccount={handleCreateAccount}
        onRunDemo={handleRunDemo}
        onGoHome={() => setCurrentView('setup')}
        isDark={isDark}
        onToggleTheme={() => setIsDark(!isDark)}
      />

      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {currentView === 'setup' && (
          <SetupView onStartResearch={handleStartResearch} onRunDemo={handleRunDemo} />
        )}
        {currentView === 'progress' && (
          <ProgressView
            topic={currentTopic}
            question={currentQuestion}
            progress={progress}
            activeAgent={activeAgent}
            statusMessage={statusMessage}
            logs={logs}
          />
        )}
        {currentView === 'report' && (
          <ReportView
            report={report}
            projectId={currentProjectId}
            onNewResearch={() => setCurrentView('setup')}
            onOpenAudit={() => setAuditOpen(true)}
            onOpenCitation={handleOpenCitation}
            onDownloadPdf={handleDownloadPdf}
            onCopyMarkdown={handleCopyMarkdown}
          />
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 mt-auto py-6">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-slate-500">
          <div>
            <span>AI Research Assistant Agent</span> • Production-ready Agentic AI with LangGraph & Google GenAI
          </div>
          <div className="flex items-center space-x-4">
            <button onClick={() => setSettingsOpen(true)} className="hover:text-slate-400">Settings</button>
            <span>•</span>
            <button onClick={() => setHistoryOpen(true)} className="hover:text-slate-400">Project History</button>
            <span>•</span>
            <a href="http://127.0.0.1:8001/docs" target="_blank" rel="noreferrer" className="hover:text-slate-400">FastAPI Swagger</a>
          </div>
        </div>
      </footer>

      {/* Modals */}
      <AuthModal
        isOpen={authModalOpen}
        initialMode={authModalMode}
        onClose={authUser ? () => setAuthModalOpen(false) : null}
        onAuthSuccess={handleAuthSuccess}
      />
      <SettingsModal
        isOpen={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSave={({ model }) => setLlmMode(model)}
      />
      <HistoryModal
        isOpen={historyOpen}
        onClose={() => setHistoryOpen(false)}
        onSelectProject={(id) => {
          api.getReport(id).then(rep => {
            setReport(rep);
            setCurrentProjectId(id);
            setCurrentView('report');
          });
        }}
      />
      <AuditModal
        isOpen={auditOpen}
        onClose={() => setAuditOpen(false)}
        auditItems={report?.verification_audit || []}
      />
      <CitationModal
        isOpen={!!citationModalData}
        citationData={citationModalData}
        onClose={() => setCitationModalData(null)}
      />
    </div>
  );
}
