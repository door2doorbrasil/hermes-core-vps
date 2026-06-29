import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  Bot,
  CheckCircle2,
  CreditCard,
  FileText,
  RefreshCw,
  Send,
  ServerCog,
  Sparkles,
  TriangleAlert,
  Warehouse,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type {
  ComprasProductRecord,
  ComprasRfqDashboardBatch,
  ComprasRfqDashboardResponse,
  ComprasSecomOffice,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const DEFAULT_CANDIDATES = [
  {
    legal_name: "Alpha Robotics",
    country: "CN",
    city: "Shenzhen",
    website: "https://alpha.example",
    source_url: "https://alpha.example",
    manufacturer_flag: true,
    verified_status: "verified",
    data_quality_status: "complete",
  },
  {
    legal_name: "Beta Trading",
    country: "US",
    city: "Miami",
    website: "https://beta.example",
    source_url: "https://beta.example",
    trading_company_flag: true,
    verified_status: "unverified",
    data_quality_status: "pending_validation",
  },
];

function parseJson<T>(value: string, fallback: T): T {
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

function fmt(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "sim" : "não";
  if (typeof value === "number") return new Intl.NumberFormat("pt-BR").format(value);
  return String(value);
}

function KpiCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <Card className="border-white/10 bg-slate-950/70 shadow-[0_20px_70px_-40px_rgba(56,189,248,0.5)]">
      <CardContent className="flex flex-col gap-1.5 py-4">
        <span className="text-[11px] uppercase tracking-[0.18em] text-slate-400">{label}</span>
        <span className="text-2xl font-semibold text-white">{value}</span>
        <span className="text-xs text-slate-400">{hint}</span>
      </CardContent>
    </Card>
  );
}

function BatchPill({ batch }: { batch: ComprasRfqDashboardBatch }) {
  const status = String(batch.batch.status || "draft");
  const tone =
    status === "approved" || status === "authorized" || status === "approved_without_email"
      ? "success"
      : status === "rejected"
        ? "destructive"
        : "secondary";
  return <Badge tone={tone}>{status.replace(/_/g, " ")}</Badge>;
}

export default function RfqPage() {
  const { setEnd } = usePageHeader();
  const [dashboard, setDashboard] = useState<ComprasRfqDashboardResponse | null>(null);
  const [products, setProducts] = useState<ComprasProductRecord[]>([]);
  const [secoms, setSecoms] = useState<ComprasSecomOffice[]>([]);
  const [loading, setLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [selectedBatchId, setSelectedBatchId] = useState<number | null>(null);
  const [productName, setProductName] = useState("AMR Robot");
  const [selectedProductId, setSelectedProductId] = useState<number | null>(null);
  const [productSearch, setProductSearch] = useState("");
  const [productFormName, setProductFormName] = useState("Bicarbonato de Sódio");
  const [productFormDescription, setProductFormDescription] = useState("");
  const [productFormApplication, setProductFormApplication] = useState("");
  const [productFormUnit, setProductFormUnit] = useState("kg");
  const [productFormPackaging, setProductFormPackaging] = useState("Sacos de 25 kg");
  const [productFormSpecs, setProductFormSpecs] = useState(
    JSON.stringify(
      {
        purity: ">= 99%",
        moisture: "<= 0.25%",
        food_grade: true,
      },
      null,
      2,
    ),
  );
  const [specs, setSpecs] = useState(
    JSON.stringify(
      {
        application: "warehouse transport",
        unit: "unit",
        ncm: "",
        packaging: "carton",
        technical_specs: {
          payload_kg: 100,
          navigation: "SLAM + LiDAR",
          charging: "auto charging",
        },
      },
      null,
      2,
    ),
  );
  const [candidateJson, setCandidateJson] = useState(JSON.stringify(DEFAULT_CANDIDATES, null, 2));
  const [notes, setNotes] = useState("Prepare the RFQ and keep approval controls on.");
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [quoteJson] = useState(
    JSON.stringify(
      {
        currency: "USD",
        unit_price: 1345,
        quantity: 25,
        unit: "MT",
        incoterm: "FOB",
        payment_terms: "30% advance / 70% after shipment",
        status: "parsed",
      },
      null,
      2,
    ),
  );
  const [inboundJson, setInboundJson] = useState(
    JSON.stringify(
      {
        from_email: "sales@example.com",
        from_name: "Sales Team",
        subject: "Re: RFQ",
        body_text: "USD 1345/MT FOB China Main Port",
        detected_language: "en",
        has_attachments: true,
        attachment_count: 2,
      },
      null,
      2,
    ),
  );
  const [pricingJson, setPricingJson] = useState(
    JSON.stringify(
      {
        margin_type: "percentage",
        margin_value: 0.2,
        international_freight: 100,
        insurance: 10,
        origin_charges: 5,
        destination_charges: 15,
        customs_clearance_cost: 20,
        import_duties_estimated: 30,
        taxes_estimated: 40,
        inland_freight: 50,
        warehouse_cost: 60,
        financial_cost: 25,
        other_costs: 5,
        requires_user_approval: false,
      },
      null,
      2,
    ),
  );
  const [lastAction, setLastAction] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadDashboard = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getComprasRfqDashboard(12, 0);
      setDashboard(result);
      if (selectedBatchId === null && result.recent_batches.length > 0) {
        setSelectedBatchId(Number(result.recent_batches[0].batch.id));
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  const loadCatalog = async () => {
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const [productResult, secomResult] = await Promise.all([
        api.getComprasProducts({ limit: 100 }),
        api.getComprasSecoms(true),
      ]);
      setProducts(productResult.products);
      setSecoms(secomResult.offices);
      const matching = productResult.products.find(
        (item) => item.name.toLowerCase() === productName.toLowerCase(),
      );
      if (matching) {
        setSelectedProductId(matching.id);
      } else if (productResult.products.length > 0 && selectedProductId === null) {
        setSelectedProductId(productResult.products[0].id);
      }
    } catch (err) {
      setCatalogError(String(err));
    } finally {
      setCatalogLoading(false);
    }
  };

  useEffect(() => {
    void loadDashboard();
    void loadCatalog();
  }, []);

  useEffect(() => {
    const match = products.find((item) => item.name.toLowerCase() === productName.toLowerCase());
    if (match) {
      setSelectedProductId(match.id);
    }
  }, [productName, products]);

  useEffect(() => {
    setEnd(
      <Button ghost size="sm" onClick={() => void loadDashboard()}>
        <RefreshCw className="mr-2 h-4 w-4" />
        Atualizar RFQ
      </Button>,
    );
    return () => setEnd(null);
  }, [setEnd]);

  const selectedBatch = useMemo(
    () => dashboard?.recent_batches.find((item) => Number(item.batch.id) === selectedBatchId) ?? null,
    [dashboard, selectedBatchId],
  );
  const selectedProduct = useMemo(
    () => products.find((item) => item.id === selectedProductId) ?? null,
    [products, selectedProductId],
  );

  const candidatePayload = useMemo(
    () => parseJson<Record<string, unknown>[]>(candidateJson, DEFAULT_CANDIDATES),
    [candidateJson],
  );

  const selectedProductBrief = selectedProduct?.sales_brief ?? null;
  const recommendedSecoms = useMemo(() => {
    if (!selectedProductBrief) return secoms;
    const family = selectedProductBrief.product_family;
    const hints: string[] = {
      alcalinos: ["Chile", "Argentina", "Colombia", "Peru", "Mexico", "United States", "Spain", "Portugal"],
      levedantes: ["Argentina", "Chile", "Mexico", "Peru", "Colombia", "United States", "Spain"],
      amidos: ["Chile", "Argentina", "Peru", "Colombia", "Mexico", "United States", "Spain", "Portugal", "United Arab Emirates"],
      geral: [],
    }[family as "alcalinos" | "levedantes" | "amidos" | "geral"] ?? [];
    if (hints.length === 0) return secoms;
    const scored = [...secoms].sort((a, b) => {
      const aScore = hints.includes(a.country) ? 1 : 0;
      const bScore = hints.includes(b.country) ? 1 : 0;
      return bScore - aScore || a.country.localeCompare(b.country);
    });
    return scored;
  }, [selectedProductBrief, secoms]);

  const handleAction = async <T extends object>(label: string, action: () => Promise<T>) => {
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      setLastAction({ label, result });
      await loadDashboard();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleSaveProduct = async () => {
    setBusy(true);
    setCatalogError(null);
    try {
      const technicalSpecs = parseJson<Record<string, unknown>>(productFormSpecs, {});
      const result = await api.upsertComprasProduct({
        name: productFormName.trim(),
        description: productFormDescription.trim() || undefined,
        technical_specs: technicalSpecs,
        application: productFormApplication.trim() || undefined,
        unit: productFormUnit.trim() || undefined,
        packaging: productFormPackaging.trim() || undefined,
      });
      await loadCatalog();
      setProductName(result.product.name);
      setSpecs(JSON.stringify(technicalSpecs, null, 2));
      setSelectedProductId(result.product.id);
      setLastAction({ label: "upsert_product", result });
    } catch (err) {
      setCatalogError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const applyProductToRfq = (product: ComprasProductRecord) => {
    setProductName(product.name);
    setSelectedProductId(product.id);
    if (product.technical_specs) {
      setSpecs(product.technical_specs);
      setProductFormSpecs(product.technical_specs);
    }
    setProductFormName(product.name);
    setProductFormDescription(product.description ?? "");
    setProductFormApplication(product.application ?? "");
    setProductFormUnit(product.unit ?? "kg");
    setProductFormPackaging(product.packaging ?? "Sacos de 25 kg");
  };

  const selectedBatchIdNumber = selectedBatch ? Number(selectedBatch.batch.id) : null;
  const selectedCandidates = selectedBatch?.candidates ?? [];
  const latestQuote = selectedBatch?.latest_quote ?? null;
  const selectedSupplierId = Number(selectedCandidates[0]?.supplier_id ?? 0) || null;
  const latestQuoteId = latestQuote?.id !== undefined && latestQuote?.id !== null ? Number(latestQuote.id) : null;

  return (
    <div className="relative min-h-0 overflow-hidden rounded-3xl border border-white/10 bg-slate-950 text-white shadow-[0_30px_120px_-60px_rgba(14,165,233,0.55)]">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(45,212,191,0.18),transparent_32%),radial-gradient(circle_at_top_right,rgba(251,191,36,0.16),transparent_28%),linear-gradient(180deg,rgba(15,23,42,0.98),rgba(2,6,23,0.98))]" />
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-cyan-300/60 to-transparent" />

      <div className="relative space-y-6 p-4 sm:p-6 lg:p-8">
        <section className="grid gap-4 lg:grid-cols-[1.4fr_0.9fr]">
          <Card className="border-white/10 bg-slate-950/80">
            <CardContent className="space-y-4 p-6">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="success" className="gap-1">
                  <Sparkles className="h-3.5 w-3.5" />
                  RFQ dedicado
                </Badge>
                <Badge tone="secondary" className="gap-1">
                  <Bot className="h-3.5 w-3.5" />
                  Compras
                </Badge>
              </div>
              <div className="space-y-2">
                <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
                  Fluxo RFQ de compras com painel, aprovação e monitoramento
                </h1>
                <p className="max-w-3xl text-sm leading-6 text-slate-300 sm:text-base">
                  Esta tela organiza o ciclo completo de compras internacionais:
                  preparar RFQ, aprovar fornecedores, registrar respostas,
                  aplicar margem, gerar proposta e acompanhar o worker de e-mail.
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => void loadDashboard()} disabled={loading || busy}>
                  {loading ? <Spinner className="mr-2" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                  Recarregar painel
                </Button>
                <Button outlined onClick={() => void handleAction("worker_run", () => api.runComprasRfqWorkerOnce())} disabled={busy}>
                  <ServerCog className="mr-2 h-4 w-4" />
                  Rodar worker agora
                </Button>
              </div>
            </CardContent>
          </Card>

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1">
            <KpiCard
              label="Lotes ativos"
              value={fmt(dashboard?.summary.batches ?? 0)}
              hint="RFQs carregados no painel"
            />
            <KpiCard
              label="Candidatos"
              value={fmt(dashboard?.summary.candidates ?? 0)}
              hint="Fornecedores já qualificados"
            />
            <KpiCard
              label="Respostas inbound"
              value={fmt(dashboard?.summary.inbound_emails ?? 0)}
              hint="E-mails vinculados ao RFQ"
            />
          </div>
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
          <Card className="border-cyan-500/20 bg-slate-950/85">
            <CardHeader>
              <div className="flex items-center gap-2">
                <FileText className="h-5 w-5 text-cyan-300" />
                <CardTitle className="text-lg">Criar RFQ</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="rfq-product">Produto</Label>
                  <Input id="rfq-product" value={productName} onChange={(e) => setProductName(e.target.value)} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="rfq-notes">Observações</Label>
                  <Input id="rfq-notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="rfq-specs">Especificações / contexto JSON</Label>
                  <textarea
                    id="rfq-specs"
                    className="min-h-52 w-full rounded-lg border border-white/10 bg-slate-900/80 p-3 font-mono text-xs text-slate-100 outline-none ring-0 placeholder:text-slate-500 focus:border-cyan-400"
                    value={specs}
                    onChange={(e) => setSpecs(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="rfq-candidates">Candidatos JSON de apoio</Label>
                  <textarea
                    id="rfq-candidates"
                    className="min-h-52 w-full rounded-lg border border-white/10 bg-slate-900/80 p-3 font-mono text-xs text-slate-100 outline-none ring-0 placeholder:text-slate-500 focus:border-cyan-400"
                    value={candidateJson}
                    onChange={(e) => setCandidateJson(e.target.value)}
                  />
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <Button
                  onClick={() =>
                    void handleAction("real_sourcing", async () => {
                      const result = await api.runComprasRfqRealSourcing({
                        product: productName,
                        description: notes,
                        ...parseJson<Record<string, unknown>>(specs, {}),
                        candidate_suppliers: candidatePayload,
                      });
                      setCandidateJson(JSON.stringify(result.candidates ?? [], null, 2));
                      return result;
                    })
                  }
                  disabled={busy}
                >
                  <CheckCircle2 className="mr-2 h-4 w-4" />
                  Rodar sourcing real
                </Button>
                <Button
                  outlined
                  onClick={() =>
                    void handleAction("prepare_rfq_manual", () =>
                      api.prepareComprasRfq({
                        product: productName,
                        description: notes,
                        ...parseJson<Record<string, unknown>>(specs, {}),
                        candidate_suppliers: candidatePayload,
                      }),
                    )
                  }
                  disabled={busy}
                >
                  <FileText className="mr-2 h-4 w-4" />
                  Preparar manual
                </Button>
                <Button
                  outlined
                  onClick={() =>
                    void handleAction("approve_all", async () => {
                      if (!selectedBatch) throw new Error("Selecione um lote primeiro.");
                      return api.approveComprasRfq({
                        rfq_batch_id: Number(selectedBatch.batch.id),
                        approved_supplier_candidates: selectedCandidates,
                        rejected_supplier_candidates: [],
                        approved_by: "web-ui",
                        approval_notes: notes,
                        authorize_email_send: true,
                      });
                    })
                  }
                  disabled={busy || !selectedBatch}
                >
                  <Send className="mr-2 h-4 w-4" />
                  Aprovar candidatos
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card className="border-white/10 bg-slate-950/85">
            <CardHeader>
              <div className="flex items-center gap-2">
                <Warehouse className="h-5 w-5 text-amber-300" />
                <CardTitle className="text-lg">Worker e proposta</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="rounded-xl border border-amber-400/20 bg-amber-500/5 p-4">
                <div className="text-sm font-medium text-amber-200">Monitoramento de e-mail</div>
                <div className="mt-1 text-sm text-slate-300">
                  O worker lê o IMAP, registra inbound, processa respostas e deixa
                  o heartbeat em disco para o painel e o backend consultarem.
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button outlined onClick={() => void handleAction("worker_status", () => api.getComprasRfqWorkerStatus())} disabled={busy}>
                    Estado do worker
                  </Button>
                  <Button outlined onClick={() => void handleAction("worker_run_once", () => api.runComprasRfqWorkerOnce())} disabled={busy}>
                    Rodar um ciclo
                  </Button>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="rfq-inbound">Inbound JSON</Label>
                <textarea
                  id="rfq-inbound"
                  className="min-h-40 w-full rounded-lg border border-white/10 bg-slate-900/80 p-3 font-mono text-xs text-slate-100 outline-none ring-0 focus:border-cyan-400"
                  value={inboundJson}
                  onChange={(e) => setInboundJson(e.target.value)}
                />
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  outlined
                  onClick={() =>
                    void handleAction("record_inbound", async () => {
                      if (!selectedBatchIdNumber) throw new Error("Selecione um lote primeiro.");
                      return api.recordComprasInboundEmail({
                        rfq_batch_id: selectedBatchIdNumber,
                        ...parseJson<Record<string, unknown>>(inboundJson, {}),
                      });
                    })
                  }
                  disabled={busy || !selectedBatchIdNumber}
                >
                  Registrar inbound
                </Button>
                <Button
                  outlined
                  onClick={() =>
                    void handleAction("record_quote", async () => {
                      if (!selectedBatchIdNumber) throw new Error("Selecione um lote primeiro.");
                      if (!selectedSupplierId) throw new Error("O lote selecionado ainda não tem supplier_id para cotação.");
                      return api.recordComprasQuote({
                        rfq_batch_id: selectedBatchIdNumber,
                        supplier_id: selectedSupplierId,
                        ...parseJson<Record<string, unknown>>(quoteJson, {}),
                      });
                    })
                  }
                  disabled={busy || !selectedBatchIdNumber}
                >
                  Registrar cotação
                </Button>
              </div>

              <div className="space-y-2">
                <Label htmlFor="rfq-pricing">Margem e custos JSON</Label>
                <textarea
                  id="rfq-pricing"
                  className="min-h-40 w-full rounded-lg border border-white/10 bg-slate-900/80 p-3 font-mono text-xs text-slate-100 outline-none ring-0 focus:border-cyan-400"
                  value={pricingJson}
                  onChange={(e) => setPricingJson(e.target.value)}
                />
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  outlined
                  onClick={() =>
                    void handleAction("calculate_pricing", async () => {
                      if (!selectedBatchIdNumber) throw new Error("Selecione um lote primeiro.");
                      if (!selectedSupplierId) throw new Error("O lote selecionado ainda não tem supplier_id para cálculo.");
                      if (!latestQuoteId) throw new Error("Registre uma cotação antes de calcular a margem.");
                      const quote = parseJson<Record<string, unknown>>(quoteJson, {});
                      return api.calculateComprasSalePrice({
                        rfq_batch_id: selectedBatchIdNumber,
                        supplier_id: selectedSupplierId,
                        product_id: Number(selectedBatch?.batch.product_id ?? 1),
                        quote_id: latestQuoteId,
                        purchase_unit_price: quote.unit_price ?? 0,
                        quantity: quote.quantity ?? 1,
                        purchase_currency: quote.currency ?? "USD",
                        ...parseJson<Record<string, unknown>>(pricingJson, {}),
                      });
                    })
                  }
                  disabled={busy || !selectedBatchIdNumber}
                >
                  <CreditCard className="mr-2 h-4 w-4" />
                  Calcular margem
                </Button>
                <Button
                  outlined
                  onClick={() =>
                    void handleAction("build_proposal", async () => {
                      if (!selectedBatchIdNumber) throw new Error("Selecione um lote primeiro.");
                      return api.buildComprasProposal({
                        rfq_batch_id: selectedBatchIdNumber,
                        quote_id: latestQuoteId ?? undefined,
                        sale_price_calculation_id: undefined,
                      });
                    })
                  }
                  disabled={busy || !selectedBatchIdNumber}
                >
                  <ArrowRight className="mr-2 h-4 w-4" />
                  Montar proposta
                </Button>
              </div>
            </CardContent>
          </Card>
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
          <Card className="border-emerald-400/15 bg-slate-950/85">
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="text-lg">Cadastro de produtos</CardTitle>
                <Badge tone="secondary">{products.length} produtos</Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {catalogError && (
                <div className="rounded-xl border border-red-400/20 bg-red-500/10 p-3 text-sm text-red-200">
                  {catalogError}
                </div>
              )}
              {catalogLoading ? (
                <div className="flex items-center gap-2 text-sm text-slate-400">
                  <Spinner />
                  Carregando catálogo...
                </div>
              ) : (
                <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
                  <div className="space-y-3">
                    <div className="space-y-2">
                      <Label htmlFor="product-search">Buscar produto</Label>
                      <Input
                        id="product-search"
                        placeholder="Ex.: polvilho, fécula, bicarbonato"
                        value={productSearch}
                        onChange={(e) => setProductSearch(e.target.value)}
                      />
                    </div>
                    <div className="max-h-72 space-y-2 overflow-auto pr-1">
                      {products
                        .filter((item) =>
                          !productSearch.trim() ||
                          item.name.toLowerCase().includes(productSearch.toLowerCase()) ||
                          (item.description ?? "").toLowerCase().includes(productSearch.toLowerCase()),
                        )
                        .map((product) => {
                          const selected = product.id === selectedProductId;
                          return (
                            <button
                              key={product.id}
                              onClick={() => applyProductToRfq(product)}
                              className={cn(
                                "w-full rounded-2xl border p-3 text-left transition",
                                selected
                                  ? "border-emerald-400/50 bg-emerald-500/10"
                                  : "border-white/10 bg-slate-900/60 hover:border-emerald-400/30",
                              )}
                            >
                              <div className="flex items-center justify-between gap-2">
                                <span className="font-medium text-sm">{product.name}</span>
                                <Badge tone="outline">{product.unit ?? "kg"}</Badge>
                              </div>
                              <p className="mt-1 text-xs text-slate-300">
                                {product.description || product.application || "Sem descrição cadastrada"}
                              </p>
                            </button>
                          );
                        })}
                    </div>
                  </div>

                  <div className="space-y-4">
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-2">
                        <Label htmlFor="product-name">Nome do produto</Label>
                        <Input id="product-name" value={productFormName} onChange={(e) => setProductFormName(e.target.value)} />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="product-unit">Unidade</Label>
                        <Input id="product-unit" value={productFormUnit} onChange={(e) => setProductFormUnit(e.target.value)} />
                      </div>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="product-description">Descrição</Label>
                      <Input id="product-description" value={productFormDescription} onChange={(e) => setProductFormDescription(e.target.value)} />
                    </div>
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-2">
                        <Label htmlFor="product-application">Aplicação</Label>
                        <Input id="product-application" value={productFormApplication} onChange={(e) => setProductFormApplication(e.target.value)} />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="product-packaging">Embalagem</Label>
                        <Input id="product-packaging" value={productFormPackaging} onChange={(e) => setProductFormPackaging(e.target.value)} />
                      </div>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="product-specs">Especificações técnicas JSON</Label>
                      <textarea
                        id="product-specs"
                        className="min-h-40 w-full rounded-lg border border-white/10 bg-slate-900/80 p-3 font-mono text-xs text-slate-100 outline-none ring-0 focus:border-emerald-400"
                        value={productFormSpecs}
                        onChange={(e) => setProductFormSpecs(e.target.value)}
                      />
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button onClick={() => void handleSaveProduct()} disabled={busy}>
                        Salvar produto
                      </Button>
                      <Button
                        outlined
                        onClick={() => {
                          if (selectedProduct) applyProductToRfq(selectedProduct);
                        }}
                        disabled={!selectedProduct}
                      >
                        Usar no RFQ
                      </Button>
                    </div>

                    {selectedProductBrief && (
                      <div className="rounded-2xl border border-emerald-400/20 bg-emerald-500/5 p-4 space-y-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone="success">Análise comercial</Badge>
                          <span className="text-sm text-emerald-100">{selectedProduct?.name}</span>
                        </div>
                        <div className="grid gap-3 sm:grid-cols-2">
                          <div>
                            <div className="text-[11px] uppercase tracking-[0.18em] text-emerald-200/80">Especificações obrigatórias</div>
                            <ul className="mt-2 space-y-1 text-sm text-slate-100">
                              {selectedProductBrief.mandatory_specs.map((item) => (
                                <li key={item}>• {item}</li>
                              ))}
                            </ul>
                          </div>
                          <div>
                            <div className="text-[11px] uppercase tracking-[0.18em] text-emerald-200/80">Diferenciais transacionais</div>
                            <ul className="mt-2 space-y-1 text-sm text-slate-100">
                              {selectedProductBrief.market_differentiators.map((item) => (
                                <li key={item}>• {item}</li>
                              ))}
                            </ul>
                          </div>
                        </div>
                        <div>
                          <div className="text-[11px] uppercase tracking-[0.18em] text-emerald-200/80">Perguntas-chave do comprador</div>
                          <p className="mt-1 text-sm text-slate-200">
                            {selectedProductBrief.buyer_questions.join(" | ")}
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="border-white/10 bg-slate-950/85">
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="text-lg">SECOMs cadastradas</CardTitle>
                <Badge tone="secondary">{secoms.length} ativas</Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="rounded-2xl border border-cyan-400/20 bg-cyan-500/5 p-4 text-sm text-slate-300">
                O cadastro vem do banco local e é usado para disparos de 30 dias para compradores potenciais e 45 dias para pedidos de relatório às SECOMs.
              </div>
              <div className="space-y-2">
                <div className="text-xs uppercase tracking-[0.18em] text-slate-400">
                  Países cadastrados
                </div>
                <div className="flex flex-wrap gap-2">
                  {Array.from(new Set(secoms.map((item) => item.country))).map((country) => (
                    <Badge key={country} tone="outline">
                      {country}
                    </Badge>
                  ))}
                </div>
              </div>
              <div className="space-y-2">
                <div className="text-xs uppercase tracking-[0.18em] text-slate-400">
                  Recomendadas para {selectedProduct?.name || "o produto selecionado"}
                </div>
                <div className="max-h-80 space-y-2 overflow-auto pr-1">
                  {recommendedSecoms.length === 0 ? (
                    <div className="rounded-2xl border border-white/10 bg-slate-900/60 p-3 text-sm text-slate-400">
                      Nenhuma SECOM encontrada no cadastro.
                    </div>
                  ) : (
                    recommendedSecoms.slice(0, 8).map((office) => (
                      <div
                        key={office.id}
                        className="rounded-2xl border border-white/10 bg-slate-900/60 p-3"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className="font-medium text-sm text-white">{office.office_name}</div>
                            <div className="text-xs text-slate-400">
                              {office.country}
                              {office.city ? ` • ${office.city}` : ""}
                            </div>
                          </div>
                          <Badge tone="outline">
                            {typeof office.recommendation_score === "number"
                              ? office.recommendation_score.toFixed(1)
                              : "—"}
                          </Badge>
                        </div>
                        <div className="mt-2 text-xs text-slate-300">
                          {office.email_primary || "sem e-mail principal cadastrado"}
                        </div>
                        <div className="mt-1 text-xs text-slate-400">
                          Ciclo: {office.followup_interval_days}d | Relatório: {office.report_interval_days}d
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
          <Card className="border-white/10 bg-slate-950/85">
            <CardHeader>
              <CardTitle className="text-lg">Lotes recentes</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {loading ? (
                <div className="flex items-center gap-2 text-sm text-slate-400">
                  <Spinner />
                  Carregando lotes...
                </div>
              ) : dashboard?.recent_batches.length ? (
                <div className="space-y-3">
                  {dashboard.recent_batches.map((item) => {
                    const batchId = Number(item.batch.id);
                    const selected = batchId === selectedBatchId;
                    return (
                      <button
                        key={batchId}
                        onClick={() => setSelectedBatchId(batchId)}
                        className={cn(
                          "w-full rounded-2xl border p-4 text-left transition",
                          selected
                            ? "border-cyan-300/40 bg-cyan-400/10"
                            : "border-white/10 bg-white/[0.03] hover:bg-white/[0.06]",
                        )}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="font-medium text-white">
                              {fmt(item.batch.batch_code)} · {fmt(item.batch.product_name ?? item.batch.product_id)}
                            </div>
                            <div className="text-xs text-slate-400">
                              {fmt(item.candidate_count)} candidatos · {fmt(item.quote_count)} cotações · {fmt(item.inbound_count)} inbound
                            </div>
                          </div>
                          <BatchPill batch={item} />
                        </div>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div className="rounded-2xl border border-dashed border-white/10 p-8 text-sm text-slate-400">
                  Nenhum lote RFQ encontrado ainda. Use o bloco de criação acima
                  para gerar o primeiro fluxo.
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="border-white/10 bg-slate-950/85">
            <CardHeader>
              <CardTitle className="text-lg">Detalhe do lote selecionado</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {selectedBatch ? (
                <>
                  <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                    <div className="flex items-center justify-between gap-2">
                      <div className="space-y-1">
                        <div className="text-sm text-slate-400">Batch</div>
                        <div className="text-lg font-medium text-white">{fmt(selectedBatch.batch.batch_code)}</div>
                      </div>
                      <BatchPill batch={selectedBatch} />
                    </div>
                    <div className="mt-3 grid gap-2 text-sm text-slate-300 sm:grid-cols-2">
                      <div>Produto: {fmt(selectedBatch.batch.product_name ?? selectedBatch.batch.product_id)}</div>
                      <div>Solicitado por: {fmt(selectedBatch.batch.requested_by)}</div>
                      <div>Candidatos: {fmt(selectedBatch.candidate_count)}</div>
                      <div>Fornecedor aprovados: {fmt(selectedBatch.recipient_count)}</div>
                      <div>Quotes: {fmt(selectedBatch.quote_count)}</div>
                      <div>Inbound: {fmt(selectedBatch.inbound_count)}</div>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="text-sm font-medium text-slate-200">Candidatos</div>
                    <div className="space-y-2">
                      {selectedCandidates.length ? (
                        selectedCandidates.map((candidate, index) => (
                          <div key={String(candidate.id ?? index)} className="rounded-xl border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-300">
                            <div className="font-medium text-white">
                              {fmt(candidate.legal_name ?? candidate.trade_name ?? candidate.name)}
                            </div>
                            <div className="mt-1 text-xs text-slate-400">
                              {fmt(candidate.city)} / {fmt(candidate.country)} · {fmt(candidate.website)}
                            </div>
                          </div>
                        ))
                      ) : (
                        <div className="rounded-xl border border-dashed border-white/10 p-3 text-sm text-slate-400">
                          Sem candidatos carregados para este lote.
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="text-sm font-medium text-slate-200">Última cotação</div>
                    <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-300">
                      {latestQuote ? (
                        <>
                          <div>Fornecedor: {fmt(latestQuote.supplier_id)}</div>
                          <div>Preço: {fmt(latestQuote.unit_price)} {fmt(latestQuote.currency)}</div>
                          <div>Incoterm: {fmt(latestQuote.incoterm)}</div>
                          <div>Status: {fmt(latestQuote.status)}</div>
                        </>
                      ) : (
                        <div className="text-slate-400">Nenhuma cotação registrada ainda.</div>
                      )}
                    </div>
                  </div>
                </>
              ) : (
                <div className="rounded-2xl border border-dashed border-white/10 p-8 text-sm text-slate-400">
                  Selecione um lote na lista ao lado para abrir o detalhamento.
                </div>
              )}
            </CardContent>
          </Card>
        </section>

        <section className="grid gap-4 lg:grid-cols-2">
          <Card className="border-white/10 bg-slate-950/85">
            <CardHeader>
              <div className="flex items-center gap-2">
                <TriangleAlert className="h-5 w-5 text-amber-300" />
                <CardTitle className="text-lg">Worker status</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="text-sm text-slate-300">
              <pre className="overflow-auto rounded-2xl border border-white/10 bg-black/30 p-4 text-xs text-slate-200">
                {JSON.stringify(dashboard?.worker_status ?? {}, null, 2)}
              </pre>
            </CardContent>
          </Card>

          <Card className="border-white/10 bg-slate-950/85">
            <CardHeader>
              <CardTitle className="text-lg">Última ação</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-slate-300">
              {error ? (
                <div className="rounded-2xl border border-red-400/20 bg-red-500/10 p-4 text-red-100">
                  {error}
                </div>
              ) : null}
              {lastAction ? (
                <pre className="overflow-auto rounded-2xl border border-white/10 bg-black/30 p-4 text-xs text-slate-200">
                  {JSON.stringify(lastAction, null, 2)}
                </pre>
              ) : (
                <div className="rounded-2xl border border-dashed border-white/10 p-8 text-slate-400">
                  As respostas das ações vão aparecer aqui depois do primeiro clique.
                </div>
              )}
            </CardContent>
          </Card>
        </section>
      </div>
    </div>
  );
}
