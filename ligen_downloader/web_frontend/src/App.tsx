import { useEffect, useMemo, useRef, useState, useTransition } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import {
  ArrowClockwise,
  Brain,
  CheckCircle,
  DownloadSimple,
  FolderOpen,
  MagnifyingGlass,
  Play,
  Stop,
  Trash,
  WarningCircle
} from "@phosphor-icons/react";

import { Button } from "./components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "./components/ui/card";
import { DataTable } from "./components/ui/table";
import { asText, safeNumber } from "./lib/utils";
import type { AgentTurn, AppState, Dict, PreviewRow, ResearchRecord, ResultRow } from "./types";

const DEFAULT_STATE: AppState = {
  input_text: "",
  task_name: "",
  output_dir: "",
  output_dir_auto: true,
  page_settle_seconds: "6",
  sleep_seconds: "1.5",
  per_doi_timeout_seconds: "240",
  per_publisher_timeout_seconds: "3600",
  max_parallel_publishers: "3",
  max_warmup_per_publisher: "1",
  launch_chrome: true,
  keep_existing_tabs: true,
  resume_existing: true,
  research_query_text: "",
  research_search_strategy: "quality",
  research_limit_per_provider: "0",
  research_provider_crossref: true,
  research_provider_openalex: true,
  research_provider_local_manual: false,
  research_confirmed_terms_text: "",
  agent_api_key: "",
  agent_base_url: "",
  agent_model: ""
};

async function api<T>(path: string, method = "GET", payload?: Dict | null): Promise<T> {
  const options: RequestInit = { method, headers: {} };
  if (payload !== undefined && payload !== null) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data as T;
}

function formatPublisher(value: unknown) {
  const raw = asText(value).trim();
  if (!raw || raw === "UNKNOWN") return "待识别来源";
  if (raw === "local_manual") return "本地补充";
  return raw;
}

function formatRunStatus(value: unknown) {
  const raw = asText(value, "Idle");
  if (!raw || raw === "Idle") return "准备就绪";
  if (/^Running warmup/i.test(raw)) return "正在预热浏览器";
  if (/^Running download/i.test(raw)) return "正在下载 PDF";
  if (/^Finished \(0\)/i.test(raw)) return "任务已完成";
  return raw;
}

function formatResearchStatus(value: unknown) {
  const raw = asText(value);
  if (!raw || raw === "Draft a research query, then create terms or run search.") return "等待选题简报";
  return raw
    .replace(/Search run #(\d+) saved\./, "检索批次 #$1 已保存")
    .replace(/Draft #(\d+) created with (\d+) terms\./, "关键词方案 #$1 已生成，共 $2 个词")
    .replace(/Keywords confirmed in set #(\d+)\./, "关键词方案 #$1 已确认")
    .replaceAll("Raw", "原始记录")
    .replaceAll("Unique", "去重后")
    .replaceAll("Duplicates", "重复")
    .replaceAll("Overlap", "来源重叠");
}

function formatPath(value: unknown) {
  const raw = asText(value);
  if (!raw || raw === "No run folder yet") return "尚未建立归档";
  const parts = raw.split(/[\\/]+/);
  return parts.at(-1) || raw;
}

function getResearchStage(research: Dict) {
  const progress = research.progress || {};
  const doiFiles = research.doi_files || {};
  if (progress.running) return "正在检索 DOI";
  if (asText(doiFiles.all)) return "DOI 清单已生成";
  if (research.keywords_confirmed) return "关键词已确认，待生成 DOI";
  if (asText(research.confirmed_terms_text) || (research.include_terms || []).length) return "待确认关键词";
  if (asText(research.query_text)) return "待生成关键词";
  return "等待研究主题";
}

function formatLayer(value: unknown) {
  const raw = asText(value);
  if (raw === "oa") return "OA";
  if (raw === "non_oa") return "非 OA";
  return "未知";
}

function buildBrief(turns: AgentTurn[]) {
  return turns
    .filter((turn) => turn.role === "user" && turn.includeInBrief !== false)
    .map((turn, index) => `第 ${index + 1} 轮研究需求：${turn.normalizedRequirement || turn.text}`)
    .join("\n\n");
}

function normalizeAgentPayload(payload: Dict, originalText: string): { user: AgentTurn; agent: AgentTurn; error?: string } {
  const agent = payload.agent || payload || {};
  return {
    user: {
      role: "user",
      text: originalText,
      includeInBrief: Boolean(agent.include_in_brief ?? agent.includeInBrief),
      normalizedRequirement: asText(agent.normalized_requirement || agent.normalizedRequirement),
      taskNameHint: asText(agent.task_name_hint || agent.taskNameHint)
    },
    agent: {
      role: "agent",
      text: asText(agent.reply, "Agent 暂不可用：未返回有效回复。").replace(/\s+/g, " ").slice(0, 240)
    },
    error: asText(agent.model_error || agent.modelError)
  };
}

export default function App() {
  const [serverState, setServerState] = useState<AppState>(DEFAULT_STATE);
  const [form, setForm] = useState<AppState>(DEFAULT_STATE);
  const [agentTurns, setAgentTurns] = useState<AgentTurn[]>([
    { role: "agent", text: "告诉我主题。我会提炼关键词；闲聊不进简报。" }
  ]);
  const [agentText, setAgentText] = useState("");
  const [toast, setToast] = useState("");
  const [error, setError] = useState("");
  const [agentBusy, setAgentBusy] = useState(false);
  const [isPending, startTransition] = useTransition();
  const editingFields = useRef<Set<string>>(new Set());

  const research = serverState.research || {};
  const run = serverState.run || {};
  const progress = serverState.progress || {};
  const researchProgress = research.progress || {};
  const agentConfig = research.agent_config || {};
  const agentVerified = Boolean(agentConfig.verified);
  const agentConfigured = Boolean(agentConfig.available);
  const researchStage = getResearchStage(research);
  const doiFiles = research.doi_files || {};
  const oaSummary = research.oa_summary || {};
  const searchStrategy = asText(form.research_search_strategy || research.search_strategy, "quality") === "recall" ? "recall" : "quality";
  const effectivePercent = researchProgress.running
    ? Math.max(1, Math.min(99, safeNumber(researchProgress.percent, 1)))
    : safeNumber(progress.percent, 0);

  const showToast = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 2800);
  };

  const mergeState = (payload: AppState) => {
    setServerState(payload);
    startTransition(() => {
      setForm((current) => {
        const next: AppState = {
          ...current,
          ...payload,
          research_confirmed_terms_text:
            asText(payload.research?.confirmed_terms_text) || asText(payload.research_confirmed_terms_text),
          research_search_strategy:
            asText(payload.research?.search_strategy) || asText(payload.research_search_strategy) || "quality",
          agent_api_key: "",
          agent_base_url:
            asText(payload.research?.agent_config?.base_url) || asText(current.agent_base_url),
          agent_model:
            asText(payload.research?.agent_config?.model) || asText(current.agent_model)
        };
        editingFields.current.forEach((field) => {
          if (field === "agent_api_key") return;
          next[field] = current[field];
        });
        return next;
      });
    });
  };

  const refresh = async () => {
    try {
      setError("");
      mergeState(await api<AppState>("/api/state"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "刷新状态失败");
    }
  };

  useEffect(() => {
    refresh();
    const handle = window.setInterval(refresh, 2500);
    return () => window.clearInterval(handle);
  }, []);

  const payload = (): Dict => ({
    task_name: asText(form.task_name).trim(),
    input_text: asText(form.input_text),
    output_dir: asText(form.output_dir).trim(),
    output_dir_auto: Boolean(form.output_dir_auto),
    page_settle_seconds: asText(form.page_settle_seconds).trim(),
    sleep_seconds: asText(form.sleep_seconds).trim(),
    per_doi_timeout_seconds: asText(form.per_doi_timeout_seconds).trim(),
    per_publisher_timeout_seconds: asText(form.per_publisher_timeout_seconds).trim(),
    max_parallel_publishers: asText(form.max_parallel_publishers).trim(),
    max_warmup_per_publisher: asText(form.max_warmup_per_publisher).trim(),
    launch_chrome: Boolean(form.launch_chrome),
    keep_existing_tabs: Boolean(form.keep_existing_tabs),
    resume_existing: Boolean(form.resume_existing),
    research_query_text: buildBrief(agentTurns) || asText(form.research_query_text),
    research_search_strategy: asText(form.research_search_strategy, "quality"),
    research_limit_per_provider: asText(form.research_limit_per_provider).trim(),
    research_provider_crossref: Boolean(form.research_provider_crossref),
    research_provider_openalex: Boolean(form.research_provider_openalex),
    research_provider_local_manual: Boolean(form.research_provider_local_manual),
    research_confirmed_terms_text: asText(form.research_confirmed_terms_text),
    agent_api_key: asText(form.agent_api_key).trim(),
    agent_base_url: asText(form.agent_base_url).trim(),
    agent_model: asText(form.agent_model).trim(),
    port_map: serverState.port_map || {}
  });

  const runAction = async (path: string, success: string, extra?: Dict) => {
    try {
      setError("");
      mergeState(await api<AppState>(path, "POST", { ...payload(), ...extra }));
      if (success) showToast(success);
    } catch (err) {
      setError(err instanceof Error ? err.message : "请求失败");
    }
  };

  const addAgentTurn = async () => {
    const value = agentText.trim();
    if (!value || agentBusy) return;
    setAgentText("");
    const optimistic = [...agentTurns, { role: "user" as const, text: value }];
    setAgentTurns(optimistic);
    setAgentBusy(true);
    try {
      const data = await api<Dict>("/api/research/agent-turn", "POST", {
        ...payload(),
        message: value,
        user_message: value,
        history: optimistic,
        conversation: optimistic
      });
      const normalized = normalizeAgentPayload(data, value);
      const nextTurns = [...agentTurns, normalized.user, normalized.agent];
      setAgentTurns(nextTurns);
      if (data.state) mergeState(data.state as AppState);
      setForm((current) => ({
        ...current,
        research_query_text: buildBrief(nextTurns),
        task_name: asText(current.task_name) || asText(normalized.user.taskNameHint).slice(0, 42)
      }));
      if (normalized.error) setError(`Agent 暂不可用：${normalized.error}`);
    } catch (err) {
      setAgentTurns([...optimistic, { role: "agent", text: err instanceof Error ? err.message : "Agent 请求失败" }]);
    } finally {
      setAgentBusy(false);
    }
  };

  const clearAgent = async () => {
    setAgentTurns([{ role: "agent", text: "已清空。告诉我一个新主题，我重新提炼关键词。" }]);
    setAgentText("");
    setForm((current) => ({
      ...current,
      task_name: "",
      research_query_text: "",
      research_confirmed_terms_text: ""
    }));
    await runAction("/api/analyze", "已清空本轮研究简报", {
      task_name: "",
      research_query_text: "",
      research_confirmed_terms_text: "",
      research_keywords_confirmed: false
    });
  };

  const previewColumns = useMemo<ColumnDef<PreviewRow>[]>(
    () => [
      { header: "#", accessorKey: "idx", cell: ({ getValue }) => <span className="font-mono">{asText(getValue())}</span> },
      { header: "来源", accessorKey: "publisher", cell: ({ getValue }) => formatPublisher(getValue()) },
      { header: "DOI", accessorKey: "doi", cell: ({ getValue }) => <span className="font-mono">{asText(getValue())}</span> },
      { header: "URL", accessorKey: "url", cell: ({ getValue }) => <span className="font-mono">{asText(getValue())}</span> }
    ],
    []
  );

  const researchColumns = useMemo<ColumnDef<ResearchRecord>[]>(
    () => [
      { header: "来源", accessorKey: "provider_id", cell: ({ getValue }) => formatPublisher(getValue()) },
      { header: "获取层", accessorKey: "oa_layer", cell: ({ getValue }) => formatLayer(getValue()) },
      { header: "年份", accessorKey: "year" },
      { header: "DOI", accessorKey: "doi", cell: ({ getValue }) => <span className="font-mono">{asText(getValue())}</span> },
      { header: "题名", accessorKey: "title", cell: ({ getValue }) => <span className="leading-6">{asText(getValue())}</span> }
    ],
    []
  );

  const resultColumns = useMemo<ColumnDef<ResultRow>[]>(
    () => [
      { header: "来源", accessorKey: "publisher", cell: ({ getValue }) => formatPublisher(getValue()) },
      { header: "状态", accessorKey: "status" },
      { header: "DOI", accessorKey: "doi", cell: ({ getValue }) => <span className="font-mono">{asText(getValue())}</span> },
      { header: "PDF 路径", accessorKey: "pdf_path", cell: ({ getValue }) => <span className="font-mono">{asText(getValue())}</span> }
    ],
    []
  );

  const setField = (key: string, value: unknown) => {
    editingFields.current.add(key);
    setForm((current) => ({ ...current, [key]: value }));
  };

  const setManualOutputDir = (value: string) => {
    editingFields.current.add("output_dir");
    editingFields.current.add("output_dir_auto");
    setForm((current) => ({
      ...current,
      output_dir: value,
      output_dir_auto: false
    }));
  };

  const releaseField = (key: string) => {
    window.setTimeout(() => editingFields.current.delete(key), 150);
  };

  const commitField = (key: string) => {
    editingFields.current.delete(key);
    void runAction("/api/analyze", "");
  };

  const selectSearchStrategy = (strategy: "quality" | "recall") => {
    editingFields.current.delete("research_search_strategy");
    const limit = "0";
    setForm((current) => ({
      ...current,
      research_search_strategy: strategy,
      research_limit_per_provider: limit
    }));
    void runAction("/api/analyze", strategy === "quality" ? "已切换为高质量检索" : "已切换为高召回检索", {
      research_search_strategy: strategy,
      research_limit_per_provider: limit
    });
  };

  return (
    <main className="min-h-screen overflow-x-hidden bg-background text-foreground">
      <div className="fixed inset-0 -z-10 bg-[radial-gradient(circle_at_8%_0%,rgba(139,0,18,0.12),transparent_32%),radial-gradient(circle_at_88%_12%,rgba(27,98,82,0.12),transparent_30%),linear-gradient(115deg,#f8eee9,#fbf8f2_48%,#eef3ef)]" />
      <div className="mx-auto flex w-[min(1800px,calc(100vw-28px))] flex-col gap-5 py-4">
        <header className="rounded-3xl border border-white/70 bg-white/86 px-5 py-4 shadow-panel backdrop-blur-xl">
          <div className="flex flex-wrap items-center justify-between gap-5">
            <div className="flex min-w-0 items-center gap-4">
              <img className="h-12 w-auto shrink-0 sm:block" src="/assets/pku-wordmark.png" alt="北京大学" />
              <div className="min-w-0 border-l border-border pl-4">
                <p className="text-xs font-bold uppercase tracking-[0.18em] text-muted-foreground">PKU Literature Intelligence</p>
                <h1 className="text-2xl font-black tracking-[-0.05em] sm:text-3xl">北大文献智采工作台</h1>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <StatusPill tone={run.running ? "ok" : "idle"}>{formatRunStatus(run.status_text)}</StatusPill>
              <StatusPill>{asText(serverState.publisher_summary, "等待 DOI 队列")}</StatusPill>
              <StatusPill>归档 {formatPath(serverState.current_run_dir || serverState.output_dir)}</StatusPill>
            </div>
          </div>
        </header>

        {(error || toast) && (
          <div className={error ? "notice error" : "notice"}>{error || toast}</div>
        )}

        <section className="grid grid-cols-1 gap-5 2xl:grid-cols-[0.95fr_1.05fr]">
          <Card className="p-5">
            <CardHeader>
              <div>
                <p className="eyebrow">DOI Acquisition</p>
                <CardTitle>从 DOI 队列归档 PDF</CardTitle>
                <CardDescription>粘贴 DOI 或 DOI URL，系统自动处理下载通道，最终把 PDF 汇总到一个本地文件夹。</CardDescription>
              </div>
              <StatusPill>{(serverState.preview_rows || []).length} 条 DOI</StatusPill>
            </CardHeader>
            <div className="mt-5 grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
              <label className="field xl:row-span-2">
                <span>DOI 队列</span>
                <textarea
                  className="min-h-[280px] font-mono"
                  value={asText(form.input_text)}
                  onBlur={() => releaseField("input_text")}
                  onChange={(event) => setField("input_text", event.target.value)}
                  placeholder="10.1016/j.example.2025.001&#10;https://doi.org/10.1145/..."
                />
              </label>
              <label className="field">
                <span>任务名</span>
                <input
                  value={asText(form.task_name)}
                  onBlur={() => commitField("task_name")}
                  onChange={(event) => setField("task_name", event.target.value)}
                />
              </label>
              <label className="field">
                <span>任务目录</span>
                <input
                  value={asText(form.output_dir)}
                  onBlur={() => commitField("output_dir")}
                  onChange={(event) => setManualOutputDir(event.target.value)}
                />
                <label className="flex items-start gap-2 text-sm font-medium leading-6 text-muted-foreground">
                  <input
                    className="mt-1 h-4 w-4 accent-[#8b0012]"
                    checked={Boolean(form.output_dir_auto)}
                    type="checkbox"
                    onChange={(event) => {
                      const checked = event.target.checked;
                      editingFields.current.delete("output_dir");
                      editingFields.current.delete("output_dir_auto");
                      setForm((current) => ({ ...current, output_dir_auto: checked }));
                      window.setTimeout(() => void runAction("/api/analyze", "", { output_dir_auto: checked }), 0);
                    }}
                  />
                  <span>自动命名：任务名 + 关键词草案 + 时间。直接编辑上方目录会自动切换为手动目录。</span>
                </label>
              </label>
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              <Button onClick={() => runAction("/api/analyze", "DOI 队列已解析")}>解析 DOI</Button>
              <Button variant="warm" onClick={() => runAction("/api/start", "浏览器预热已启动", { mode: "warmup" })}>预热浏览器</Button>
              <Button variant="primary" onClick={() => runAction("/api/start", "PDF 下载已启动", { mode: "download" })}>
                <DownloadSimple size={18} weight="bold" />开始下载 PDF
              </Button>
              <Button variant="danger" onClick={() => runAction("/api/stop", "已请求停止任务")}>
                <Stop size={18} weight="bold" />停止
              </Button>
            </div>
          </Card>

          <Card className="p-5">
            <CardHeader>
              <div>
                <p className="eyebrow">Research Intelligence</p>
                <CardTitle>和 Agent 确认检索关键词</CardTitle>
                <CardDescription>先多轮表达研究意图，确认关键词后再检索 DOI。普通闲聊不会写入研究简报。</CardDescription>
              </div>
              <StatusPill tone={asText(doiFiles.all) ? "ok" : "idle"}>
                {researchStage}
              </StatusPill>
            </CardHeader>
            <div className="mt-2 rounded-2xl border border-border bg-white/80 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-bold">Agent 连接状态</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {agentVerified
                      ? `已验证：${asText(agentConfig.model, "默认模型")} · ${asText(agentConfig.base_url, "")}`
                      : agentConfigured
                        ? `已保存配置，尚未验证：${asText(agentConfig.model, "默认模型")} · ${asText(agentConfig.base_url, "")}`
                        : "未配置：在这里填入 API Key、Base URL 和模型名后保存。"}
                  </p>
                  {asText(agentConfig.last_test_message) ? (
                    <p className="mt-1 text-xs text-muted-foreground">
                      最近测试：{asText(agentConfig.last_test_message)}
                    </p>
                  ) : null}
                </div>
                <StatusPill tone={agentVerified ? "ok" : agentConfigured ? "warm" : "idle"}>
                  {agentVerified ? "已验证" : agentConfigured ? "待测试" : "未配置"}
                </StatusPill>
              </div>
              <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_1fr_0.8fr]">
                <label className="field">
                  <span>API Key</span>
                  <input
                    type="password"
                    value={asText(form.agent_api_key)}
                    onChange={(event) => setField("agent_api_key", event.target.value)}
                    placeholder={agentConfig.has_saved_key ? "已保存；留空不修改" : "粘贴你的 API Key"}
                  />
                </label>
                <label className="field">
                  <span>Base URL</span>
                  <input
                    value={asText(form.agent_base_url)}
                    onChange={(event) => setField("agent_base_url", event.target.value)}
                    placeholder="https://api.openai.com/v1"
                  />
                </label>
                <label className="field">
                  <span>模型</span>
                  <input
                    value={asText(form.agent_model)}
                    onChange={(event) => setField("agent_model", event.target.value)}
                    placeholder="gpt-4o-mini"
                  />
                </label>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <Button onClick={() => runAction("/api/analyze", "Agent 设置已保存")}>
                  保存 Agent 设置
                </Button>
                <Button variant="warm" onClick={() => runAction("/api/research/test-agent", "Agent 连接测试完成")}>
                  测试连接
                </Button>
                <Button variant="ghost" onClick={() => runAction("/api/analyze", "已清除本机保存的 API Key", { agent_clear_api_key: true, agent_api_key: "" })}>
                  清除已保存 Key
                </Button>
                <p className="text-xs text-muted-foreground">
                  Key 只保存在本机：{asText(agentConfig.settings_path, "用户配置目录")}
                </p>
              </div>
            </div>
            <div className="mt-5 grid gap-3 lg:grid-cols-2">
              <button
                className={`rounded-2xl border p-4 text-left transition ${
                  searchStrategy === "quality"
                    ? "border-primary bg-white shadow-soft"
                    : "border-border bg-white/60 hover:bg-white/85"
                }`}
                type="button"
                onClick={() => selectSearchStrategy("quality")}
              >
                <span className="text-sm font-black">高质量</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                  不限制采集数量；全量拉取后更严格去噪、去重和排序。
                </span>
              </button>
              <button
                className={`rounded-2xl border p-4 text-left transition ${
                  searchStrategy === "recall"
                    ? "border-primary bg-white shadow-soft"
                    : "border-border bg-white/60 hover:bg-white/85"
                }`}
                type="button"
                onClick={() => selectSearchStrategy("recall")}
              >
                <span className="text-sm font-black">高召回</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                  不限制采集数量；尽量保留边缘相关结果，后续再人工或 Agent 复核。
                </span>
              </button>
            </div>
            <div className="mt-5 grid gap-4 xl:grid-cols-[1fr_0.9fr]">
              <div className="agent-panel">
                {agentTurns.map((turn, index) => (
                  <div className={`agent-bubble ${turn.role}`} key={`${turn.role}-${index}`}>
                    <strong>{turn.role === "user" ? "你" : "选题研判助手"}</strong>
                    <span>{turn.text}</span>
                  </div>
                ))}
              </div>
              <div className="grid gap-4">
                <label className="field">
                  <span>发送补充</span>
                  <textarea
                    className="min-h-[132px]"
                    value={agentText}
                    onChange={(event) => setAgentText(event.target.value)}
                    placeholder="例如：我想找近五年 Transformer 在计算机视觉中的高影响力论文。"
                  />
                </label>
                <div className="flex flex-wrap gap-3">
                  <Button variant="primary" disabled={agentBusy} onClick={addAgentTurn}>
                    <Brain size={18} weight="bold" />{agentBusy ? "Agent 分析中..." : "发送给 Agent"}
                  </Button>
                  <Button onClick={() => runAction("/api/research/draft", "关键词草案已生成", { research_confirmed_terms_text: "" })}>生成草案</Button>
                  <Button variant="ghost" onClick={clearAgent}>
                    <Trash size={18} />清空
                  </Button>
                </div>
              </div>
            </div>
            <div className="mt-4 rounded-2xl border border-border bg-[#fffaf3]/78 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-bold">关键词草案</p>
                  <p className="mt-1 text-sm text-muted-foreground">{formatResearchStatus(research.summary_text)}</p>
                </div>
                <StatusPill>{formatResearchStatus(research.status_text)}</StatusPill>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {(research.include_terms || []).map((term: string) => <span className="term" key={term}>{term}</span>)}
              </div>
              <label className="field mt-4">
                <span>待确认关键词</span>
                <textarea
                  className="min-h-[110px] font-mono"
                  value={asText(form.research_confirmed_terms_text)}
                  onBlur={() => releaseField("research_confirmed_terms_text")}
                  onChange={(event) => setField("research_confirmed_terms_text", event.target.value)}
                />
              </label>
              <div className="mt-4 flex flex-wrap gap-3">
                <Button variant="primary" onClick={() => runAction("/api/research/confirm-and-search", "DOI 清单已生成")}>
                  <CheckCircle size={18} weight="bold" />确认并生成 DOI 清单
                </Button>
                <Button variant="warm" onClick={() => runAction("/api/research/confirm", "关键词已确认，尚未检索")}>
                  仅确认关键词
                </Button>
                <Button disabled={!research.keywords_confirmed} onClick={() => runAction("/api/research/search", "DOI 清单已重新生成")}>
                  <MagnifyingGlass size={18} weight="bold" />重新检索
                </Button>
                <Button disabled={!(research.records || []).length} onClick={() => runAction("/api/research/use-results", "候选 DOI 已导入下载队列")}>
                  导入下载队列
                </Button>
              </div>
              {asText(doiFiles.all) && (
                <div className="mt-4 grid gap-3 rounded-2xl border border-border bg-white/70 p-4 text-xs leading-6">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <strong className="text-sm">DOI 清单文件</strong>
                      <p className="text-muted-foreground">系统已自动生成两个主清单：OA 与非 OA。</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button disabled={!asText(doiFiles.oa)} onClick={() => runAction("/api/research/open-doi-file", "已打开 OA DOI list", { kind: "oa" })}>
                        打开 OA DOI list
                      </Button>
                      <Button disabled={!asText(doiFiles.non_oa)} onClick={() => runAction("/api/research/open-doi-file", "已打开非 OA DOI list", { kind: "non_oa" })}>
                        打开非 OA DOI list
                      </Button>
                    </div>
                  </div>
                  <span className="font-mono">OA txt：{asText(doiFiles.oa)}</span>
                  <span className="font-mono">非 OA txt：{asText(doiFiles.non_oa)}</span>
                  <span className="font-mono text-muted-foreground">全量备份：{asText(doiFiles.all)}</span>
                  <span className="font-mono text-muted-foreground">CSV 明细：{asText(doiFiles.csv)}</span>
                </div>
              )}
            </div>
          </Card>
        </section>

        <Card className="p-5">
          <div className="grid gap-4 lg:grid-cols-[1fr_280px]">
            <div>
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <p className="eyebrow">Live Progress</p>
                  <h2 className="text-xl font-black tracking-[-0.04em]">任务状态</h2>
                </div>
                <Button variant="ghost" onClick={refresh}>
                  <ArrowClockwise size={18} />刷新
                </Button>
              </div>
              <div className="h-3 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${effectivePercent}%` }} />
              </div>
              <p className="mt-3 text-sm text-muted-foreground">
                {researchProgress.running ? asText(researchProgress.label, "正在分页采集 DOI 候选") : asText(progress.label, "等待 DOI 队列")}
              </p>
            </div>
            <div className="grid grid-cols-3 gap-3 lg:grid-cols-1">
              <Metric label="进度" value={`${effectivePercent}%`} />
              <Metric label="已归档" value={serverState.results?.status_counts?.downloaded || 0} />
              <Metric label="剩余" value={progress.remaining ?? (serverState.preview_rows || []).length} />
            </div>
          </div>
        </Card>

        <section className="grid grid-cols-1 gap-5 xl:grid-cols-2">
          <Card className="p-5">
            <CardHeader>
              <div>
                <p className="eyebrow">Research Candidates</p>
                <CardTitle>DOI 候选分层</CardTitle>
              </div>
              <Button disabled={!(research.records || []).length} onClick={() => runAction("/api/research/review-titles", "Review Agent 已完成清洗")}>
                Review Agent 清洗
              </Button>
            </CardHeader>
            <div className="mt-4 grid gap-4">
              <div className="grid grid-cols-3 gap-3">
                <Metric label="OA" value={safeNumber(oaSummary.oa_count, 0)} />
                <Metric label="非 OA" value={safeNumber(oaSummary.non_oa_count, 0)} />
                <Metric label="未知" value={safeNumber(oaSummary.unknown_oa_count, 0)} />
              </div>
              <div>
                <p className="mb-2 text-sm font-bold">OA 可直接获取</p>
                <DataTable columns={researchColumns} data={(research.records_oa || []).slice(0, 120)} emptyText="确认并生成 DOI 清单后，这里显示 OA 候选。" />
              </div>
              <div>
                <p className="mb-2 text-sm font-bold">非 OA / 需机构访问</p>
                <DataTable columns={researchColumns} data={(research.records_non_oa || []).slice(0, 120)} emptyText="暂无明确非 OA 候选。" />
              </div>
              <div>
                <p className="mb-2 text-sm font-bold">OA 状态未知</p>
                <DataTable columns={researchColumns} data={(research.records_unknown_oa || []).slice(0, 120)} emptyText="暂无 OA 状态未知候选。" />
              </div>
            </div>
          </Card>

          <Card className="p-5">
            <CardHeader>
              <div>
                <p className="eyebrow">Download Queue</p>
                <CardTitle>队列与结果</CardTitle>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => runAction("/api/open-output", "已打开 PDF 文件夹")}>
                  <FolderOpen size={18} />PDF 文件夹
                </Button>
                <Button onClick={() => runAction("/api/open-results", "已打开结果表")}>结果表</Button>
              </div>
            </CardHeader>
            <div className="mt-4 grid gap-4">
              <DataTable columns={previewColumns} data={(serverState.preview_rows || []).slice(0, 300)} emptyText="还没有可解析的 DOI 条目。" />
              <DataTable columns={resultColumns} data={(serverState.results?.rows || []).slice(0, 300)} emptyText={asText(serverState.results?.message, "还没有下载结果。")} />
            </div>
          </Card>
        </section>

        <footer className="pb-6 text-center text-xs text-muted-foreground">
          React/Vite Console · Tailwind + Radix-style components · TanStack Table
        </footer>
      </div>
      {isPending && <div className="fixed bottom-4 left-4 rounded-full bg-foreground px-4 py-2 text-sm text-white shadow-panel">正在同步界面...</div>}
    </main>
  );
}

function StatusPill({ children, tone = "idle" }: { children: React.ReactNode; tone?: "idle" | "ok" | "warm" }) {
  const dotClass = tone === "ok" ? "bg-[#17745f]" : tone === "warm" ? "bg-[#c49a2c]" : "bg-[#a66b18]";
  return (
    <span className="inline-flex max-w-full items-center gap-2 rounded-xl border border-border bg-white/75 px-3 py-2 text-sm text-foreground">
      <span className={`h-2 w-2 shrink-0 rounded-full ${dotClass}`} />
      <span className="truncate">{children}</span>
    </span>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-border bg-white/72 p-4">
      <p className="text-xs font-bold uppercase tracking-[0.14em] text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-black tracking-[-0.04em]">{value}</p>
    </div>
  );
}
