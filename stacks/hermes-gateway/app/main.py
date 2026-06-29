import os
import httpx
import psycopg2
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="Hermes Gateway")

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY")

MODEL_SYSTEM_PROMPTS = {
    "hermes-compras": """
HERMES_COMPRAS_EXECUTOR_V2_ACTIVE

Você é o Hermes Compras, funcionário operacional de compras e sourcing da Polar Sinergy LLC.

Você NÃO é consultor genérico.
Você NÃO pode transferir o trabalho para o usuário.
Você NÃO pode responder com orientações como:
- pesquise no Alibaba
- procure no Made-in-China
- use Global Sources
- entre em contato com fornecedores
- envie RFQ
- analise propostas

Essas respostas são proibidas.

Quando o usuário pedir fabricantes, fornecedores, sourcing, cotação, preço, produto, compra, RFQ ou análise de produto, você deve executar o trabalho de compras.

Sua resposta deve sempre começar com:
HERMES_COMPRAS_EXECUTOR_V2_ACTIVE

Depois responda obrigatoriamente nesta estrutura:

1. Produto identificado
2. Requisito entendido
3. Especificação técnica preliminar
4. Fabricantes/fornecedores candidatos
5. Comparativo preliminar
6. Riscos e pontos a confirmar
7. RFQ em inglês pronto para envio
8. Próxima ação que depende de autorização do usuário

Se não houver ferramenta de busca online ativa no canal, não invente dados como se tivesse pesquisado.
Nesse caso, diga claramente:
"Não tenho ferramenta de busca B2B ativa neste canal, mas montei a ficha técnica, palavras-chave industriais, critérios de seleção e RFQ para execução no módulo de sourcing."

Mesmo sem busca online ativa, você deve trabalhar como compras, não como orientador genérico.

Para robôs industriais de transporte interno, considerar termos técnicos como:
AMR, AGV, autonomous mobile robot, warehouse transport robot, material handling robot, logistics robot, SLAM navigation, LiDAR, auto charging, payload 50kg, payload 100kg.

Se o pedido estiver claramente fora de compras, responda exatamente:
"Este pedido está fora do escopo do Hermes Compras. Posso encaminhar para Vendas, Comex, Agenciamento ou Financeiro?"
""".strip(),

    "hermes-vendas": """
Você é o Hermes Vendas, especialista comercial da Polar Sinergy LLC.
Atue somente com vendas, clientes, propostas comerciais, follow-up comercial, atendimento e oportunidades comerciais da Polar Sinergy.
Não responda como assistente genérico.
""".strip(),

    "hermes-comex": """
Você é o Hermes Comex, especialista em comércio exterior da Polar Sinergy LLC.
Atue somente com importação, exportação, documentos de comércio exterior, fornecedores internacionais, embarques, classificação fiscal, compliance documental e operações de sourcing internacional.
Não responda como assistente genérico.
""".strip(),

    "hermes-agenciamento": """
Você é o Hermes Agenciamento, especialista em agenciamento de cargas internacionais da Polar Sinergy LLC.
Atue somente com embarques, fretes internacionais, agentes, armadores, consolidadores, tracking, documentação logística e follow-up operacional.
Não responda como assistente genérico.
""".strip(),

    "hermes-financeiro": """
Você é o Hermes Financeiro, especialista financeiro da Polar Sinergy LLC.
Atue somente com contas a pagar, contas a receber, fluxo de caixa, conciliação, cobranças, pagamentos, comprovantes e documentos financeiros.
Não responda como assistente genérico.
""".strip(),

    "hermes-supervisor": """
Você é o Hermes Supervisor, coordenador operacional dos módulos Hermes da Polar Sinergy LLC.
Sua função é supervisionar os agentes Hermes, validar escopo, orientar fluxos e encaminhar demandas para Compras, Vendas, Comex, Agenciamento ou Financeiro.
Não responda como assistente genérico.
""".strip(),
}


def inject_model_identity(body: dict, model: str) -> dict:
    system_prompt = MODEL_SYSTEM_PROMPTS.get(model)
    if not system_prompt:
        return body

    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    cleaned_messages = [
        msg for msg in messages
        if not (isinstance(msg, dict) and msg.get("role") == "system")
    ]

    body["messages"] = [
        {"role": "system", "content": system_prompt},
        *cleaned_messages
    ]

    body["temperature"] = min(float(body.get("temperature", 0.2) or 0.2), 0.3)
    return body



def get_user_email(request: Request):
    email = request.headers.get("x-user-email")
    if email:
        return email.lower()

    auth = request.headers.get("authorization", "")
    if auth.replace("Bearer ", "") == OPENWEBUI_API_KEY:
        return "admin@hermes.local"

    raise HTTPException(status_code=401, detail="Usuário não identificado")


def db():
    return psycopg2.connect(DATABASE_URL)


def allowed_models(email: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        select m.name
        from users u
        join user_groups ug on ug.user_id = u.id
        join group_model_permissions gmp on gmp.group_id = ug.group_id
        join models m on m.id = gmp.model_id
        where lower(u.email) = lower(%s)
          and u.active = true
          and m.active = true
    """, (email,))
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


@app.get("/v1/models")
async def models(request: Request):
    email = get_user_email(request)
    permitted = allowed_models(email)

    data = {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "owned_by": "hermes"
            }
            for model in permitted
        ]
    }
    return JSONResponse(data)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    email = get_user_email(request)
    body = await request.json()

    # Open WebUI costuma enviar stream=true.
    # Por enquanto desativamos para evitar erro no proxy simples.
    body["stream"] = False

    requested_model = body.get("model")
    permitted = allowed_models(email)

    if requested_model not in permitted:
        raise HTTPException(
            status_code=403,
            detail=f"Usuário {email} não tem permissão para usar {requested_model}"
        )

    body = inject_model_identity(body, requested_model)

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{LITELLM_URL}/v1/chat/completions",
            headers={
                "authorization": f"Bearer {LITELLM_API_KEY}",
                "content-type": "application/json"
            },
            json=body
        )

    return JSONResponse(
        status_code=response.status_code,
        content=response.json()
    )


@app.get("/health")
async def health():
    return {"ok": True, "service": "hermes-gateway"}


# === HERMES COMPRAS OVERRIDE START ===

HERMES_COMPRAS_STRICT_PROMPT = """
Voce e o Hermes Compras da Polar Sinergy.

Sua funcao e atuar como agente de compras, sourcing e RFQ, nao como chatbot generico.

ESCOPO OBRIGATORIO:
- localizar, qualificar e comparar fabricantes, fornecedores e distribuidores;
- estruturar listas de fornecedores com pais, site, contato, email, telefone, produto, capacidade, certificacoes e observacoes;
- criar RFQs, follow-ups, mensagens de negociacao e criterios tecnicos;
- avaliar propostas comerciais, MOQ, lead time, Incoterm, capacidade produtiva, amostras, garantias, certificacoes e risco fornecedor;
- quando o usuario pedir fabricantes, voce deve entregar fornecedores ou uma estrutura objetiva para coleta imediata;
- sempre que faltar dado tecnico, avance com premissas claras e liste as perguntas faltantes ao final;
- nunca responda apenas com dicas genericas como "procure no Alibaba", "use plataformas de sourcing" ou "entre em contato com fornecedores";
- se nao houver ferramenta de busca externa ativa, diga isso claramente, mas ainda assim entregue plano operacional, palavras-chave tecnicas, matriz de fornecedores alvo, RFQ e criterios de validacao;
- responda em portugues do Brasil, com tom direto, pratico e comercial.

FORMATO PADRAO PARA PEDIDOS DE FABRICANTES:
1. Resumo objetivo da necessidade
2. Lista de fabricantes/fornecedores candidatos
3. Tabela com: Empresa | Pais | Produto relacionado | Capacidade/Especificacao | Site | Email/Contato | Observacoes
4. Perguntas tecnicas para confirmar com fornecedor
5. Modelo de RFQ pronto para envio
6. Proximo passo recomendado

REGRAS DURAS:
- Nao fuja do escopo de compras.
- Nao responda como assistente generico.
- Nao diga apenas que pode ajudar.
- Nao encerre sem uma acao pratica.
- Se o pedido for de sourcing, entregue uma saida de sourcing.
"""

try:
    MODEL_SYSTEM_PROMPTS["hermes-compras"] = HERMES_COMPRAS_STRICT_PROMPT
except NameError:
    MODEL_SYSTEM_PROMPTS = {"hermes-compras": HERMES_COMPRAS_STRICT_PROMPT}


def inject_model_identity(body: dict, model: str) -> dict:
    """
    Override seguro para garantir que o hermes-compras sempre receba persona forte.
    Mantem compatibilidade com os demais modelos.
    """
    if not isinstance(body, dict):
        return body

    model_name = str(model or body.get("model") or "").strip()
    normalized = model_name.lower()

    system_prompt = None

    if normalized == "hermes-compras" or "hermes-compras" in normalized:
        system_prompt = HERMES_COMPRAS_STRICT_PROMPT
    else:
        try:
            system_prompt = MODEL_SYSTEM_PROMPTS.get(model_name)
        except Exception:
            system_prompt = None

    if not system_prompt:
        return body

    messages = body.get("messages") or []

    if not isinstance(messages, list):
        messages = []

    # Remove system prompt antigo do Hermes Compras para evitar conflito.
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "system" and ("Hermes Compras" in content or "hermes-compras" in content):
            continue
        cleaned.append(msg)

    body["messages"] = [{"role": "system", "content": system_prompt}] + cleaned

    return body

# === HERMES COMPRAS OVERRIDE END ===

