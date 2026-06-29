import type {
  AnalyticsResponse,
  ProfileInfo,
  SessionInfo,
  SessionStoreStats,
} from "@/lib/api";

export type AgentSuggestionSeverity = "info" | "warning" | "success";

export interface AgentSuggestion {
  title: string;
  detail: string;
  severity: AgentSuggestionSeverity;
}

export interface AgentDashboardSnapshot {
  profile: ProfileInfo | null;
  stats: SessionStoreStats | null;
  analytics: AnalyticsResponse | null;
  recentSessions: SessionInfo[];
}

export interface AgentSourceCount {
  source: string;
  count: number;
  share: number;
}

export function formatSourceLabel(source: string | null | undefined): string {
  if (!source) return "Web UI / local";
  const normalized = source.trim().toLowerCase();
  if (!normalized) return "Web UI / local";
  const labels: Record<string, string> = {
    cli: "Web UI / CLI",
    local: "Web UI / local",
    web: "Web UI",
    "web ui": "Web UI",
    telegram: "Telegram",
    whatsapp: "WhatsApp",
    slack: "Slack",
    discord: "Discord",
    cron: "Cron",
  };
  return labels[normalized] ?? source;
}

export function sourceTone(source: string | null | undefined):
  | "success"
  | "warning"
  | "destructive"
  | "secondary"
  | "outline" {
  const normalized = (source ?? "").toLowerCase();
  if (normalized.includes("whatsapp")) return "success";
  if (normalized.includes("telegram")) return "warning";
  if (normalized.includes("web")) return "outline";
  if (normalized.includes("cron")) return "secondary";
  return "outline";
}

export function formatCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export function formatPercent(n: number): string {
  if (!Number.isFinite(n)) return "0%";
  return `${Math.round(n * 100)}%`;
}

export function formatDateTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

export function getAgentSourceCounts(stats: SessionStoreStats | null): AgentSourceCount[] {
  if (!stats) return [];
  const entries = Object.entries(stats.by_source || {}).filter(([, count]) => count > 0);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  return entries
    .sort((a, b) => b[1] - a[1])
    .map(([source, count]) => ({
      source,
      count,
      share: total > 0 ? count / total : 0,
    }));
}

function average(total: number, count: number): number {
  if (!count) return 0;
  return total / count;
}

export function buildAgentSuggestions(
  snapshot: AgentDashboardSnapshot,
): AgentSuggestion[] {
  const suggestions: AgentSuggestion[] = [];
  const stats = snapshot.stats;
  const analytics = snapshot.analytics;
  const sessions = snapshot.recentSessions;
  const sourceCounts = getAgentSourceCounts(stats);
  const topSource = sourceCounts[0];

  const totalSessions = analytics?.totals.total_sessions ?? stats?.total ?? 0;
  const totalMessages = stats?.messages ?? 0;
  const avgMessagesPerSession = average(totalMessages, totalSessions);
  const avgApiCallsPerSession = average(
    analytics?.totals.total_api_calls ?? 0,
    totalSessions,
  );
  const avgOutputTokensPerSession = average(
    analytics?.totals.total_output ?? 0,
    totalSessions,
  );
  const activeShare = stats ? average(stats.active_store, stats.total) : 0;
  const untitledShare = sessions.length
    ? sessions.filter((session) => {
        const title = (session.title ?? "").trim().toLowerCase();
        return !title || title === "untitled";
      }).length / sessions.length
    : 0;

  if (topSource && topSource.share >= 0.45) {
    const channel = formatSourceLabel(topSource.source);
    suggestions.push({
      severity: topSource.source === "whatsapp" ? "success" : "info",
      title: `Padronizar o canal dominante (${channel})`,
      detail:
        topSource.source === "whatsapp"
          ? "Crie respostas rápidas, abreviações e playbooks de objeção para reduzir tempo de resposta no WhatsApp."
          : `Esse agente recebe boa parte dos atendimentos por ${channel}. Vale criar templates e fluxos curtos para esse canal.`,
    });
  }

  if (avgMessagesPerSession >= 10) {
    suggestions.push({
      severity: "warning",
      title: "Sessões longas pedem estrutura",
      detail:
        `A média atual está em ${avgMessagesPerSession.toFixed(1)} mensagens por atendimento. Considere roteiros, checkpoints e resumos intermediários.`,
    });
  }

  if (avgApiCallsPerSession >= 5) {
    suggestions.push({
      severity: "warning",
      title: "Muito tool-calling por atendimento",
      detail:
        `Há cerca de ${avgApiCallsPerSession.toFixed(1)} chamadas de API por sessão. Revise prompts, fallback e critérios de uso de ferramentas.`,
    });
  }

  if (avgOutputTokensPerSession >= 2_000) {
    suggestions.push({
      severity: "info",
      title: "Respostas podem ser mais objetivas",
      detail:
        `O agente está gerando em média ${formatCount(Math.round(avgOutputTokensPerSession))} tokens de saída por sessão. Vale padronizar respostas curtas para etapas iniciais.`,
    });
  }

  if (activeShare >= 0.25) {
    suggestions.push({
      severity: "warning",
      title: "Fila ativa alta",
      detail:
        `Quase ${formatPercent(activeShare)} do estoque está ativo. Pode ser hora de revisar follow-up, pendências e automações de fechamento.`,
    });
  }

  if (untitledShare >= 0.35) {
    suggestions.push({
      severity: "info",
      title: "Mais títulos automáticos",
      detail:
        "Há muitas sessões sem título útil. Automatizar títulos melhora triagem, busca e leitura gerencial.",
    });
  }

  if (suggestions.length === 0) {
    suggestions.push({
      severity: "success",
      title: "Fluxo estável",
      detail:
        "Não apareceu um gargalo forte nos dados atuais. O próximo ganho tende a vir de automações de canal e padronização de títulos.",
    });
  }

  return suggestions.slice(0, 4);
}

function escapeCsvCell(value: string): string {
  if (/[",\n;]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function buildAgentCsv(snapshot: AgentDashboardSnapshot): string {
  const profileName = snapshot.profile?.name ?? "default";
  const stats = snapshot.stats;
  const analytics = snapshot.analytics;
  const suggestions = buildAgentSuggestions(snapshot);
  const lines: string[] = [];

  lines.push("Section,Field,Value");
  lines.push(
    [
      "Resumo",
      "Agente",
      profileName,
    ].map(escapeCsvCell).join(","),
  );
  lines.push(
    [
      "Resumo",
      "Modelo",
      snapshot.profile?.model ?? "—",
    ].map(escapeCsvCell).join(","),
  );
  lines.push(
    [
      "Resumo",
      "Provider",
      snapshot.profile?.provider ?? "—",
    ].map(escapeCsvCell).join(","),
  );
  lines.push(
    [
      "Resumo",
      "Total de sessões",
      String(analytics?.totals.total_sessions ?? stats?.total ?? 0),
    ].map(escapeCsvCell).join(","),
  );
  lines.push(
    [
      "Resumo",
      "Mensagens",
      String(stats?.messages ?? 0),
    ].map(escapeCsvCell).join(","),
  );

  lines.push("");
  lines.push("Source,Count");
  for (const [source, count] of Object.entries(stats?.by_source ?? {})) {
    lines.push([escapeCsvCell(formatSourceLabel(source)), String(count)].join(","));
  }

  lines.push("");
  lines.push("Suggestion,Detail,Severity");
  for (const suggestion of suggestions) {
    lines.push(
      [
        escapeCsvCell(suggestion.title),
        escapeCsvCell(suggestion.detail),
        suggestion.severity,
      ].join(","),
    );
  }

  lines.push("");
  lines.push("Recent sessions");
  lines.push("ID,Title,Source,Messages,Input tokens,Output tokens,Last active");
  for (const session of snapshot.recentSessions) {
    lines.push(
      [
        escapeCsvCell(session.id),
        escapeCsvCell(session.title ?? "Untitled"),
        escapeCsvCell(formatSourceLabel(session.source)),
        String(session.message_count),
        String(session.input_tokens),
        String(session.output_tokens),
        formatDateTime(session.last_active),
      ].join(","),
    );
  }

  return lines.join("\n");
}

function xmlEscape(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function buildWorksheet(name: string, rows: string[][]): string {
  const rowXml = rows
    .map(
      (row) =>
        `<Row>${row
          .map((cell) => `<Cell><Data ss:Type="String">${xmlEscape(cell)}</Data></Cell>`)
          .join("")}</Row>`,
    )
    .join("");
  return `<Worksheet ss:Name="${xmlEscape(name)}"><Table>${rowXml}</Table></Worksheet>`;
}

export function buildAgentSpreadsheetXml(snapshot: AgentDashboardSnapshot): string {
  const profileName = snapshot.profile?.name ?? "default";
  const stats = snapshot.stats;
  const analytics = snapshot.analytics;
  const suggestions = buildAgentSuggestions(snapshot);

  const summaryRows: string[][] = [
    ["Campo", "Valor"],
    ["Agente", profileName],
    ["Modelo", snapshot.profile?.model ?? "—"],
    ["Provider", snapshot.profile?.provider ?? "—"],
    ["Total de sessões", String(analytics?.totals.total_sessions ?? stats?.total ?? 0)],
    ["Ativas no estoque", String(stats?.active_store ?? 0)],
    ["Arquivadas", String(stats?.archived ?? 0)],
    ["Mensagens", String(stats?.messages ?? 0)],
    ["Input tokens", String(analytics?.totals.total_input ?? 0)],
    ["Output tokens", String(analytics?.totals.total_output ?? 0)],
    ["Chamadas de API", String(analytics?.totals.total_api_calls ?? 0)],
  ];

  const sourceRows: string[][] = [["Fonte", "Quantidade", "Participação"]];
  const sourceCounts = getAgentSourceCounts(stats);
  for (const source of sourceCounts) {
    sourceRows.push([
      formatSourceLabel(source.source),
      String(source.count),
      formatPercent(source.share),
    ]);
  }

  const suggestionRows: string[][] = [["Sugestão", "Detalhe", "Nível"]];
  for (const suggestion of suggestions) {
    suggestionRows.push([
      suggestion.title,
      suggestion.detail,
      suggestion.severity,
    ]);
  }

  const sessionsRows: string[][] = [[
    "ID",
    "Título",
    "Fonte",
    "Mensagens",
    "Tool calls",
    "Input tokens",
    "Output tokens",
    "Última atividade",
  ]];
  for (const session of snapshot.recentSessions) {
    sessionsRows.push([
      session.id,
      session.title ?? "Untitled",
      formatSourceLabel(session.source),
      String(session.message_count),
      String(session.tool_call_count),
      String(session.input_tokens),
      String(session.output_tokens),
      formatDateTime(session.last_active),
    ]);
  }

  const workbook = [
    '<?xml version="1.0"?>',
    '<?mso-application progid="Excel.Sheet"?>',
    '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
    ' xmlns:o="urn:schemas-microsoft-com:office:office"',
    ' xmlns:x="urn:schemas-microsoft-com:office:excel"',
    ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"',
    ' xmlns:html="http://www.w3.org/TR/REC-html40">',
    "<Styles>",
    '<Style ss:ID="Header"><Font ss:Bold="1"/></Style>',
    "</Styles>",
    buildWorksheet("Resumo", summaryRows),
    buildWorksheet("Fontes", sourceRows),
    buildWorksheet("Sugestoes", suggestionRows),
    buildWorksheet("Atendimentos", sessionsRows),
    "</Workbook>",
  ].join("");

  return workbook;
}

export function downloadTextFile(name: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

