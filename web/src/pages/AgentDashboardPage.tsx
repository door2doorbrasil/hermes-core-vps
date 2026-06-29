import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocation } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  Bot,
  Download,
  FileSpreadsheet,
  FileText,
  Filter,
  Globe,
  MessageSquare,
  RefreshCw,
  Search,
  Sparkles,
  Users,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  AnalyticsResponse,
  AuthMeResponse,
  ProfileInfo,
  SessionInfo,
  SessionStoreStats,
} from "@/lib/api";
import { useProfileScope } from "@/contexts/useProfileScope";
import { usePageHeader } from "@/contexts/usePageHeader";
import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  buildAgentCsv,
  buildAgentSpreadsheetXml,
  buildAgentSuggestions,
  downloadTextFile,
  formatDateTime,
  formatPercent,
  formatSourceLabel,
  getAgentSourceCounts,
  sourceTone,
} from "@/lib/agent-dashboard";
import { cn } from "@/lib/utils";

const PERIODS = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

type ProfileStatsMap = Record<string, SessionStoreStats>;

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function safeModelName(model: string | null | undefined): string {
  if (!model) return "—";
  return model.split("/").pop() ?? model;
}

function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card className="min-w-0">
      <CardContent className="flex flex-col gap-1.5 py-4">
        <span className="text-xs font-mondwest text-display tracking-[0.12em] text-text-tertiary">
          {label}
        </span>
        <span className="text-2xl font-semibold tabular-nums leading-none text-foreground">
          {value}
        </span>
        {hint ? (
          <span className="text-xs text-text-secondary">{hint}</span>
        ) : null}
      </CardContent>
    </Card>
  );
}

function DailyChart({
  daily,
}: {
  daily: AnalyticsResponse["daily"];
}) {
  if (daily.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-sm text-text-secondary">
          Sem dados suficientes para montar o gráfico no período selecionado.
        </CardContent>
      </Card>
    );
  }

  const maxSessions = Math.max(...daily.map((d) => d.sessions), 1);
  const maxTokens = Math.max(
    ...daily.map((d) => d.input_tokens + d.output_tokens),
    1,
  );

  return (
    <Card className="min-w-0">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Activity className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">Atendimentos por dia</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {daily.map((day) => {
            const sessionHeight = Math.max(
              10,
              Math.round((day.sessions / maxSessions) * 140),
            );
            const tokenHeight = Math.max(
              6,
              Math.round(
                ((day.input_tokens + day.output_tokens) / maxTokens) * 140,
              ),
            );
            return (
              <div
                key={day.day}
                className="rounded border border-border bg-card/60 p-3"
              >
                <div className="mb-3 flex items-center justify-between gap-2 text-xs text-text-secondary">
                  <span>{new Date(`${day.day}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</span>
                  <span>{day.sessions} atend.</span>
                </div>
                <div className="flex items-end gap-2">
                  <div className="flex-1">
                    <div className="mb-1 text-[11px] text-text-tertiary">
                      sessões
                    </div>
                    <div
                      className="w-full rounded-t bg-primary/70"
                      style={{ height: sessionHeight }}
                    />
                  </div>
                  <div className="flex-1">
                    <div className="mb-1 text-[11px] text-text-tertiary">
                      tokens
                    </div>
                    <div
                      className="w-full rounded-t bg-warning/70"
                      style={{ height: tokenHeight }}
                    />
                  </div>
                </div>
                <div className="mt-2 flex justify-between text-[11px] text-text-tertiary">
                  <span>{compactNumber(day.input_tokens)} in</span>
                  <span>{compactNumber(day.output_tokens)} out</span>
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

function SourceBreakdown({
  stats,
}: {
  stats: SessionStoreStats | null;
}) {
  const sources = getAgentSourceCounts(stats);
  if (sources.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-sm text-text-secondary">
          Nenhuma fonte registrada para este agente.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="min-w-0">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Globe className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">Fonte dos atendimentos</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {sources.map((item) => (
          <div key={item.source} className="space-y-1">
            <div className="flex items-center justify-between gap-2 text-sm">
              <span className="font-medium text-text-primary">
                {formatSourceLabel(item.source)}
              </span>
              <span className="text-text-secondary">
                {item.count} · {formatPercent(item.share)}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-muted">
              <div
                className="h-full rounded bg-primary"
                style={{ width: `${Math.max(item.share * 100, 4)}%` }}
              />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function SessionRow({ session }: { session: SessionInfo }) {
  return (
    <tr className="border-b border-border/60 last:border-0">
      <td className="py-3 pr-3">
        <div className="min-w-0">
          <div className="truncate font-medium text-text-primary">
            {session.title?.trim() || "Untitled"}
          </div>
          <div className="truncate text-xs text-text-secondary">
            {session.id}
          </div>
        </div>
      </td>
      <td className="py-3 px-3 text-sm text-text-secondary">
        <Badge tone={sourceTone(session.source)} className="text-xs">
          {formatSourceLabel(session.source)}
        </Badge>
      </td>
      <td className="py-3 px-3 text-sm text-text-secondary">
        <span className="font-mono-ui text-xs">
          {session.user_id?.trim() || "—"}
        </span>
      </td>
      <td className="py-3 px-3 text-right text-sm tabular-nums">
        {session.message_count}
      </td>
      <td className="py-3 px-3 text-right text-sm tabular-nums text-text-secondary">
        {compactNumber(session.input_tokens)}
      </td>
      <td className="py-3 px-3 text-right text-sm tabular-nums text-text-secondary">
        {compactNumber(session.output_tokens)}
      </td>
      <td className="py-3 pl-3 text-right text-sm text-text-secondary">
        {formatDateTime(session.last_active)}
      </td>
    </tr>
  );
}

function AgentSuggestions({
  suggestions,
}: {
  suggestions: ReturnType<typeof buildAgentSuggestions>;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Sparkles className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            Sugestões automáticas no estilo ChatGPT
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {suggestions.map((suggestion) => (
          <div
            key={suggestion.title}
            className={cn(
              "rounded border p-3",
              suggestion.severity === "warning" &&
                "border-warning/30 bg-warning/5",
              suggestion.severity === "success" &&
                "border-success/30 bg-success/5",
              suggestion.severity === "info" &&
                "border-border bg-muted/20",
            )}
          >
            <div className="mb-1 flex items-center gap-2">
              <Badge
                tone={
                  suggestion.severity === "warning"
                    ? "warning"
                    : suggestion.severity === "success"
                      ? "success"
                      : "outline"
                }
                className="text-xs"
              >
                {suggestion.severity}
              </Badge>
              <span className="font-medium text-text-primary">
                {suggestion.title}
              </span>
            </div>
            <p className="text-sm text-text-secondary">{suggestion.detail}</p>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function RecentSessionsTable({
  sessions,
}: {
  sessions: SessionInfo[];
}) {
  if (sessions.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-sm text-text-secondary">
          Nenhuma sessão recente após os filtros aplicados.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="min-w-0 overflow-hidden">
      <CardHeader>
        <div className="flex items-center gap-2">
          <MessageSquare className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">Atendimentos recentes</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full min-w-[820px] text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-text-secondary">
              <th className="pb-2 pr-3 text-left">Título</th>
              <th className="pb-2 px-3 text-left">Fonte</th>
              <th className="pb-2 px-3 text-left">Usuário</th>
              <th className="pb-2 px-3 text-right">Msgs</th>
              <th className="pb-2 px-3 text-right">Input</th>
              <th className="pb-2 px-3 text-right">Output</th>
              <th className="pb-2 pl-3 text-right">Última atividade</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => (
              <SessionRow key={session.id} session={session} />
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function PrintableAgentReport({
  profile,
  stats,
  analytics,
  sessions,
}: {
  profile: ProfileInfo | null;
  stats: SessionStoreStats | null;
  analytics: AnalyticsResponse | null;
  sessions: SessionInfo[];
}) {
  const suggestions = buildAgentSuggestions({
    profile,
    stats,
    analytics,
    recentSessions: sessions,
  });

  return (
    <div className="mx-auto flex min-h-screen max-w-5xl flex-col gap-6 bg-white p-8 text-black">
      <header className="border-b border-black/20 pb-4">
        <p className="text-xs uppercase tracking-[0.2em] text-black/60">
          Hermes Agent
        </p>
        <h1 className="mt-2 text-3xl font-semibold">
          Relatório do agente {profile?.name ?? "default"}
        </h1>
        <p className="mt-2 text-sm text-black/70">
          {profile?.description || "Resumo operacional com fontes, volume e sugestões de melhoria."}
        </p>
      </header>

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded border border-black/15 p-4">
          <div className="text-xs uppercase tracking-[0.16em] text-black/50">
            Sessões
          </div>
          <div className="mt-2 text-2xl font-semibold">
            {analytics?.totals.total_sessions ?? stats?.total ?? 0}
          </div>
        </div>
        <div className="rounded border border-black/15 p-4">
          <div className="text-xs uppercase tracking-[0.16em] text-black/50">
            Mensagens
          </div>
          <div className="mt-2 text-2xl font-semibold">{stats?.messages ?? 0}</div>
        </div>
        <div className="rounded border border-black/15 p-4">
          <div className="text-xs uppercase tracking-[0.16em] text-black/50">
            Canais
          </div>
          <div className="mt-2 text-2xl font-semibold">
            {getAgentSourceCounts(stats).length}
          </div>
        </div>
        <div className="rounded border border-black/15 p-4">
          <div className="text-xs uppercase tracking-[0.16em] text-black/50">
            APIs
          </div>
          <div className="mt-2 text-2xl font-semibold">
            {analytics?.totals.total_api_calls ?? 0}
          </div>
        </div>
      </section>

      <section className="rounded border border-black/15 p-4">
        <h2 className="text-lg font-semibold">Fontes</h2>
        <div className="mt-4 space-y-3">
          {getAgentSourceCounts(stats).map((item) => (
            <div key={item.source}>
              <div className="flex items-center justify-between text-sm">
                <span>{formatSourceLabel(item.source)}</span>
                <span>
                  {item.count} · {formatPercent(item.share)}
                </span>
              </div>
              <div className="mt-1 h-2 rounded bg-black/10">
                <div
                  className="h-2 rounded bg-black"
                  style={{ width: `${Math.max(item.share * 100, 4)}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded border border-black/15 p-4">
        <h2 className="text-lg font-semibold">Sugestões automáticas</h2>
        <div className="mt-4 space-y-3">
          {suggestions.map((suggestion) => (
            <div key={suggestion.title} className="rounded border border-black/10 p-3">
              <div className="text-sm font-semibold">{suggestion.title}</div>
              <div className="mt-1 text-sm text-black/70">{suggestion.detail}</div>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded border border-black/15 p-4">
        <h2 className="text-lg font-semibold">Atendimentos recentes</h2>
        <table className="mt-4 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-black/20 text-black/60">
              <th className="pb-2 pr-3">Título</th>
              <th className="pb-2 px-3">Fonte</th>
              <th className="pb-2 px-3">Usuário</th>
              <th className="pb-2 px-3">Msgs</th>
              <th className="pb-2 px-3">Input</th>
              <th className="pb-2 px-3">Output</th>
              <th className="pb-2 pl-3">Última atividade</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => (
              <tr key={session.id} className="border-b border-black/10">
                <td className="py-2 pr-3">
                  <div className="font-medium">{session.title ?? "Untitled"}</div>
                  <div className="text-xs text-black/50">{session.id}</div>
                </td>
                <td className="py-2 px-3">{formatSourceLabel(session.source)}</td>
                <td className="py-2 px-3">{session.user_id?.trim() || "—"}</td>
                <td className="py-2 px-3 text-right">{session.message_count}</td>
                <td className="py-2 px-3 text-right">{compactNumber(session.input_tokens)}</td>
                <td className="py-2 px-3 text-right">{compactNumber(session.output_tokens)}</td>
                <td className="py-2 pl-3 text-right">{formatDateTime(session.last_active)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

export default function AgentDashboardPage() {
  const { search } = useLocation();
  const printMode = new URLSearchParams(search).get("print") === "1";
  const { setTitle } = usePageHeader();
  const { profile, profiles, setProfile } = useProfileScope();
  const [profileDetails, setProfileDetails] = useState<ProfileInfo[]>([]);
  const [profileStats, setProfileStats] = useState<ProfileStatsMap>({});
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [selectedLoading, setSelectedLoading] = useState(true);
  const [selectedAnalytics, setSelectedAnalytics] = useState<AnalyticsResponse | null>(null);
  const [recentSessions, setRecentSessions] = useState<SessionInfo[]>([]);
  const [periodDays, setPeriodDays] = useState<(typeof PERIODS)[number]["days"]>(30);
  const [sessionSearch, setSessionSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [userFilter, setUserFilter] = useState("all");
  const [agentSearch, setAgentSearch] = useState("");
  const [authMe, setAuthMe] = useState<AuthMeResponse | null>(null);
  const printedRef = useRef(false);

  const selectedProfile = profile || "default";

  useLayoutEffect(() => {
    setTitle("Agentes");
    return () => setTitle(null);
  }, [setTitle]);

  useEffect(() => {
    let cancelled = false;
    setOverviewLoading(true);
    Promise.all([
      api.getSelectableProfiles(),
      api.getActiveProfile(),
      api.getAuthMe().catch(() => null),
    ])
      .then(([profilesRes, activeInfo, me]) => {
        if (cancelled) return;
        setProfileDetails(profilesRes.profiles);
        setAuthMe(me);
        if (!profile && activeInfo.current) {
          setProfile(activeInfo.current || activeInfo.active || "default");
        }
        const activeProfiles = profilesRes.profiles.map((item) => item.name);
        return Promise.all(
          activeProfiles.map(async (name) => {
            try {
              const stats = await api.getSessionStats(name);
              return [name, stats] as const;
            } catch {
              return [name, null] as const;
            }
          }),
        );
      })
      .then((statsPairs) => {
        if (cancelled || !statsPairs) return;
        const next: ProfileStatsMap = {};
        for (const [name, stats] of statsPairs) {
          if (stats) next[name] = stats;
        }
        setProfileStats(next);
      })
      .catch(() => {
        if (!cancelled) {
          setProfileDetails([]);
          setProfileStats({});
        }
      })
      .finally(() => {
        if (!cancelled) setOverviewLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadSelected = useCallback(() => {
    setSelectedLoading(true);
    Promise.all([
      api.getAnalytics(periodDays, selectedProfile),
      api.getSessions(
        50,
        0,
        selectedProfile,
        "recent",
        userFilter === "all" ? undefined : userFilter,
      ),
    ])
      .then(([analytics, sessions]) => {
        setSelectedAnalytics(analytics);
        setRecentSessions(sessions.sessions);
      })
      .catch(() => {
        setSelectedAnalytics(null);
        setRecentSessions([]);
      })
      .finally(() => setSelectedLoading(false));
  }, [periodDays, selectedProfile, userFilter]);

  useEffect(() => {
    loadSelected();
  }, [loadSelected]);

  useEffect(() => {
    if (authMe?.user_id && userFilter === "all") {
      setUserFilter(authMe.user_id);
    }
  }, [authMe?.user_id, userFilter]);

  useEffect(() => {
    if (!printMode || printedRef.current || selectedLoading || !selectedAnalytics) return;
    printedRef.current = true;
    const timer = window.setTimeout(() => window.print(), 350);
    return () => window.clearTimeout(timer);
  }, [printMode, selectedAnalytics, selectedLoading]);

  useEffect(() => {
    const onFocus = () => {
      if (document.visibilityState !== "visible") return;
      setOverviewLoading(true);
      void Promise.all([
        api.getSelectableProfiles(),
        api.getActiveProfile(),
        api.getAuthMe().catch(() => null),
      ])
        .then(([profilesRes, _activeInfo, me]) => {
          setProfileDetails(profilesRes.profiles);
          setAuthMe(me);
          return Promise.all(
            profilesRes.profiles.map(async (item) => {
              try {
                const stats = await api.getSessionStats(item.name);
                return [item.name, stats] as const;
              } catch {
                return [item.name, null] as const;
              }
            }),
          );
        })
        .then((pairs) => {
          const next: ProfileStatsMap = {};
          for (const [name, stats] of pairs ?? []) {
            if (stats) next[name] = stats;
          }
          setProfileStats(next);
        })
        .finally(() => setOverviewLoading(false));
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, []);

  const selectedProfileInfo = useMemo(
    () => profileDetails.find((item) => item.name === selectedProfile) ?? null,
    [profileDetails, selectedProfile],
  );

  const selectedStats = profileStats[selectedProfile] ?? null;
  const sourceCounts = useMemo(
    () => getAgentSourceCounts(selectedStats),
    [selectedStats],
  );
  const userOptions = useMemo(() => {
    const ids = new Set<string>();
    for (const session of recentSessions) {
      const id = (session.user_id ?? "").trim();
      if (id) ids.add(id);
    }
    const values = [...ids].sort();
    if (authMe?.user_id) {
      values.unshift(authMe.user_id);
    }
    return ["all", ...Array.from(new Set(values))];
  }, [authMe?.user_id, recentSessions]);
  const selectedSessions = useMemo(() => {
    const filtered = recentSessions.filter((session) => {
      const source = (session.source ?? "local").toLowerCase();
      const userId = (session.user_id ?? "").trim();
      const searchMatch =
        !sessionSearch ||
        `${session.id} ${session.title ?? ""} ${session.preview ?? ""}`.toLowerCase().includes(
          sessionSearch.toLowerCase(),
        );
      const sourceMatch = sourceFilter === "all" || source === sourceFilter;
      const userMatch =
        userFilter === "all" || userId === userFilter;
      return searchMatch && sourceMatch && userMatch;
    });
    return filtered;
  }, [recentSessions, sessionSearch, sourceFilter, userFilter]);

  const selectedSuggestions = useMemo(
    () =>
      buildAgentSuggestions({
        profile: selectedProfileInfo,
        stats: selectedStats,
        analytics: selectedAnalytics,
        recentSessions,
      }),
    [recentSessions, selectedAnalytics, selectedProfileInfo, selectedStats],
  );

  const overviewProfiles = useMemo(() => {
    const search = agentSearch.trim().toLowerCase();
    return profileDetails
      .filter((item) => {
        if (!search) return true;
        return (
          item.name.toLowerCase().includes(search) ||
          (item.description ?? "").toLowerCase().includes(search) ||
          (item.model ?? "").toLowerCase().includes(search) ||
          (item.provider ?? "").toLowerCase().includes(search)
        );
      })
      .map((item) => ({
        profile: item,
        stats: profileStats[item.name] ?? null,
      }));
  }, [agentSearch, profileDetails, profileStats]);

  const overviewTotals = useMemo(() => {
    const totals = Object.values(profileStats).reduce(
      (acc, stats) => {
        acc.total += stats.total;
        acc.active += stats.active_store;
        acc.archived += stats.archived;
        acc.messages += stats.messages;
        for (const [source, count] of Object.entries(stats.by_source)) {
          acc.sources[source] = (acc.sources[source] ?? 0) + count;
        }
        return acc;
      },
      {
        total: 0,
        active: 0,
        archived: 0,
        messages: 0,
        sources: {} as Record<string, number>,
      },
    );
    const topSource = Object.entries(totals.sources).sort((a, b) => b[1] - a[1])[0];
    return { ...totals, topSource };
  }, [profileStats]);

  const exportSnapshot = useMemo(
    () => ({
      profile: selectedProfileInfo,
      stats: selectedStats,
      analytics: selectedAnalytics,
      recentSessions,
    }),
    [recentSessions, selectedAnalytics, selectedProfileInfo, selectedStats],
  );

  const handleCsvExport = useCallback(() => {
    downloadTextFile(
      `agent-${selectedProfile}-report.csv`,
      buildAgentCsv(exportSnapshot),
      "text/csv;charset=utf-8",
    );
  }, [exportSnapshot, selectedProfile]);

  const handleXlsExport = useCallback(() => {
    downloadTextFile(
      `agent-${selectedProfile}-report.xls`,
      buildAgentSpreadsheetXml(exportSnapshot),
      "application/vnd.ms-excel;charset=utf-8",
    );
  }, [exportSnapshot, selectedProfile]);

  const handlePdfExport = useCallback(() => {
    const url = `${window.location.pathname}?profile=${encodeURIComponent(selectedProfile)}&print=1`;
    window.open(url, "_blank", "noopener,noreferrer");
  }, [selectedProfile]);

  const selectedUserLabel =
    userFilter === "all"
      ? "Todos os usuários"
      : userFilter === authMe?.user_id
        ? "Meu usuário"
        : userFilter;

  if (printMode) {
    return (
      <PrintableAgentReport
        profile={selectedProfileInfo}
        stats={selectedStats}
        analytics={selectedAnalytics}
        sessions={selectedSessions}
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Card className="overflow-hidden border-border/70">
        <CardHeader className="border-b border-border/60">
          <div className="flex flex-wrap items-center gap-2">
            <Users className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Visão gerencial dos agentes</CardTitle>
            <Badge tone="outline" className="text-xs">
              {profileDetails.length || profiles.length} agentes
            </Badge>
          </div>
          <p className="text-sm text-text-secondary">
            Monitora atendimentos, demandas, fontes de entrada e sugere melhorias
            por agente. O perfil selecionado abaixo define o dashboard detalhado.
          </p>
        </CardHeader>
        <CardContent className="grid gap-3 py-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label="Atendimentos"
            value={compactNumber(overviewTotals.total)}
            hint="soma de sessões em todos os agentes"
          />
          <StatCard
            label="Mensagens"
            value={compactNumber(overviewTotals.messages)}
            hint="volume total de mensagens registradas"
          />
          <StatCard
            label="Ativos"
            value={compactNumber(overviewTotals.active)}
            hint="sessões ativas no estoque"
          />
          <StatCard
            label="Fonte líder"
            value={
              overviewTotals.topSource
                ? formatSourceLabel(overviewTotals.topSource[0])
                : "—"
            }
            hint={
              overviewTotals.topSource
                ? `${overviewTotals.topSource[1]} atendimentos`
                : "sem dados suficientes"
            }
          />
          <StatCard
            label="ACL / login"
            value={
              authMe ? authMe.display_name || authMe.user_id : "sem sessão"
            }
            hint={
              authMe
                ? `perfil ${authMe.provider} · user ${authMe.user_id}${(authMe.groups ?? []).length ? ` · grupos ${(authMe.groups ?? []).join(", ")}` : ""}`
                : "dashboard aberto sem sessão autenticada"
            }
          />
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader className="border-b border-border/60">
          <div className="flex flex-wrap items-center gap-2">
            <Bot className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Agentes disponíveis</CardTitle>
            <div className="ml-auto flex w-full max-w-md items-center gap-2 sm:w-auto">
              <Search className="h-4 w-4 text-text-tertiary" />
              <Input
                value={agentSearch}
                onChange={(e) => setAgentSearch(e.target.value)}
                placeholder="Buscar agente, modelo ou descrição"
                className="h-9"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent className="overflow-x-auto py-4">
          {overviewLoading ? (
            <div className="flex items-center gap-2 py-8 text-sm text-text-secondary">
              <Spinner />
              Carregando agentes...
            </div>
          ) : (
            <div className="flex min-w-max gap-3">
              {overviewProfiles.map(({ profile: item, stats }) => {
                const selected = item.name === selectedProfile;
                const topSource = getAgentSourceCounts(stats)[0];
                return (
                  <button
                    key={item.name}
                    type="button"
                    onClick={() => setProfile(item.name)}
                    className={cn(
                      "min-w-[240px] max-w-[280px] rounded border p-4 text-left transition-all",
                      selected
                        ? "border-primary bg-primary/5 shadow-sm"
                        : "border-border bg-card hover:border-primary/40 hover:bg-muted/20",
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-text-primary">
                          {item.name}
                        </div>
                        <div className="mt-1 truncate text-xs text-text-secondary">
                          {safeModelName(item.model)} · {item.provider ?? "provider não definido"}
                        </div>
                      </div>
                      {selected ? <ArrowRight className="h-4 w-4 text-primary" /> : null}
                    </div>
                    <p className="mt-3 line-clamp-3 text-xs text-text-secondary">
                      {item.description || "Sem descrição automática."}
                    </p>
                    <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
                      <div className="rounded bg-muted/40 p-2">
                        <div className="text-text-tertiary">sessões</div>
                        <div className="font-semibold text-text-primary">
                          {compactNumber(stats?.total ?? 0)}
                        </div>
                      </div>
                      <div className="rounded bg-muted/40 p-2">
                        <div className="text-text-tertiary">mensagens</div>
                        <div className="font-semibold text-text-primary">
                          {compactNumber(stats?.messages ?? 0)}
                        </div>
                      </div>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {topSource ? (
                        <Badge tone={sourceTone(topSource.source)} className="text-xs">
                          {formatSourceLabel(topSource.source)}
                        </Badge>
                      ) : (
                        <Badge tone="outline" className="text-xs">
                          sem fonte
                        </Badge>
                      )}
                      {selected ? (
                        <Badge tone="success" className="text-xs">
                          selecionado
                        </Badge>
                      ) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader className="border-b border-border/60">
          <div className="flex flex-wrap items-center gap-2">
            <Filter className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              Dashboard do agente {selectedProfileInfo?.name ?? selectedProfile}
            </CardTitle>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {PERIODS.map((period) => (
                <Button
                  key={period.days}
                  type="button"
                  size="sm"
                  outlined={periodDays !== period.days}
                  onClick={() => setPeriodDays(period.days)}
                >
                  {period.label}
                </Button>
              ))}
              <Button
                type="button"
                ghost
                size="icon"
                onClick={loadSelected}
                aria-label="Recarregar dashboard"
                className="text-text-secondary hover:text-foreground"
              >
                {selectedLoading ? <Spinner /> : <RefreshCw />}
              </Button>
              <Button type="button" outlined size="sm" onClick={handlePdfExport}>
                <FileText className="mr-2 h-4 w-4" />
                PDF
              </Button>
              <Button type="button" outlined size="sm" onClick={handleXlsExport}>
                <FileSpreadsheet className="mr-2 h-4 w-4" />
                XLS
              </Button>
              <Button type="button" size="sm" onClick={handleCsvExport}>
                <Download className="mr-2 h-4 w-4" />
                CSV
              </Button>
            </div>
          </div>
          <p className="text-sm text-text-secondary">
            {selectedProfileInfo?.description ||
              "Use esta área para acompanhar volume, canais, produtividade e melhorias sugeridas."}
          </p>
        </CardHeader>
        <CardContent className="space-y-6 py-6">
          {selectedLoading ? (
            <div className="flex items-center gap-2 py-8 text-sm text-text-secondary">
              <Spinner />
              Carregando detalhes do agente...
            </div>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <StatCard
                  label="Sessões"
                  value={compactNumber(selectedAnalytics?.totals.total_sessions ?? selectedStats?.total ?? 0)}
                  hint="volume no período selecionado"
                />
                <StatCard
                  label="Mensagens"
                  value={compactNumber(selectedStats?.messages ?? 0)}
                  hint="mensagens totalizadas"
                />
                <StatCard
                  label="Chamadas"
                  value={compactNumber(selectedAnalytics?.totals.total_api_calls ?? 0)}
                  hint="tool calls / requests internos"
                />
                <StatCard
                  label="Ativas"
                  value={compactNumber(selectedStats?.active_store ?? 0)}
                  hint="sessões ainda abertas no estoque"
                />
              </div>

              <div className="grid gap-6 xl:grid-cols-[1.6fr_1fr]">
                <DailyChart daily={selectedAnalytics?.daily ?? []} />
                <SourceBreakdown stats={selectedStats} />
              </div>

              <div className="grid gap-6 xl:grid-cols-[1fr_1.2fr]">
                <AgentSuggestions suggestions={selectedSuggestions} />

                <Card className="min-w-0">
                  <CardHeader>
                    <div className="flex flex-wrap items-center gap-2">
                      <MessageSquare className="h-5 w-5 text-muted-foreground" />
                      <CardTitle className="text-base">
                        Filtros de atendimentos
                      </CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div className="flex flex-col gap-3">
                      <div className="flex items-center gap-2">
                        <Search className="h-4 w-4 text-text-tertiary" />
                        <Input
                          value={sessionSearch}
                          onChange={(e) => setSessionSearch(e.target.value)}
                          placeholder="Buscar por título, id ou trecho"
                          className="h-9"
                        />
                      </div>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="flex flex-col gap-1.5 text-xs text-text-secondary">
                          <span className="font-mondwest text-display tracking-[0.12em] text-text-tertiary">
                            Usuário
                          </span>
                          <select
                            value={userFilter}
                            onChange={(e) => setUserFilter(e.target.value)}
                            className="h-9 rounded border border-border bg-background px-2 text-sm text-foreground outline-none"
                          >
                            {userOptions.map((value) => (
                              <option key={value} value={value}>
                                {value === "all"
                                  ? "Todos os usuários"
                                  : value === authMe?.user_id
                                    ? "Meu usuário"
                                    : value}
                              </option>
                            ))}
                          </select>
                        </label>
                        <div className="rounded border border-border bg-muted/20 p-3">
                          <div className="text-xs text-text-tertiary">Filtro ativo</div>
                          <div className="mt-1 text-sm font-medium text-text-primary">
                            {selectedUserLabel}
                          </div>
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          size="sm"
                          outlined={sourceFilter !== "all"}
                          onClick={() => setSourceFilter("all")}
                        >
                          Todas
                        </Button>
                        {sourceCounts.map((item) => (
                          <Button
                            key={item.source}
                            type="button"
                            size="sm"
                            outlined={sourceFilter !== item.source}
                            onClick={() => setSourceFilter(item.source)}
                          >
                            {formatSourceLabel(item.source)} ({item.count})
                          </Button>
                        ))}
                      </div>
                    </div>

                    <div className="grid gap-2 sm:grid-cols-3">
                      <div className="rounded border border-border bg-muted/20 p-3">
                        <div className="text-xs text-text-tertiary">Fonte líder</div>
                        <div className="mt-1 text-sm font-medium text-text-primary">
                          {sourceCounts[0]
                            ? formatSourceLabel(sourceCounts[0].source)
                            : "—"}
                        </div>
                      </div>
                      <div className="rounded border border-border bg-muted/20 p-3">
                        <div className="text-xs text-text-tertiary">Mensagens / sessão</div>
                        <div className="mt-1 text-sm font-medium text-text-primary">
                          {selectedAnalytics?.totals.total_sessions
                            ? (
                                (selectedStats?.messages ?? 0) /
                                selectedAnalytics.totals.total_sessions
                              ).toFixed(1)
                            : "0.0"}
                        </div>
                      </div>
                      <div className="rounded border border-border bg-muted/20 p-3">
                        <div className="text-xs text-text-tertiary">Canal filtrado</div>
                        <div className="mt-1 text-sm font-medium text-text-primary">
                          {sourceFilter === "all"
                            ? "Todos"
                            : formatSourceLabel(sourceFilter)}
                        </div>
                      </div>
                      <div className="rounded border border-border bg-muted/20 p-3">
                        <div className="text-xs text-text-tertiary">Usuário filtrado</div>
                        <div className="mt-1 text-sm font-medium text-text-primary">
                          {selectedUserLabel}
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </div>

              <RecentSessionsTable sessions={selectedSessions} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
