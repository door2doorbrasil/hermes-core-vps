# Manual Pratico dos Agentes Hermes

Este manual consolida, em um unico lugar, como operar os agentes atuais do ecossistema Hermes:

- Hermes Compras (`hermes-compras`)
- Hermes Vendas (`hermes-vendas`)
- Hermes Comex (`hermes-comex`)
- Hermes Agenciamento (`hermes-agenciamento`)
- Hermes Financeiro (`hermes-financeiro`)
- Hermes Supervisor (`hermes-supervisor`)
- Suporte Tecnico Hermes / Infraestrutura
- AIVO Note no WhatsApp

O gateway atual trabalha com perfis e fluxos operacionais isolados. Em vez de misturar tudo em um unico comportamento, cada perfil tem foco, canais, fontes, autorizacoes e regras de saida proprios. Este manual cobre os quatro fluxos mais usados na operação diária e referencia os demais perfis de runtime para contexto.

## Visao geral

### O que sempre vale para qualquer agente

- Trabalhe primeiro com fontes internas antes de improvisar.
- Nao invente acesso a dados, logs, servidores ou arquivos que nao foram fornecidos.
- Quando houver risco operacional, use aprovacao manual.
- Quando um canal for restrito, siga a politica do canal e nao responda fora do escopo.
- Quando houver perfil, use o perfil correto antes de abrir chat, dashboard ou automacao.

### Comandos base de perfis

Use estes comandos para administrar agentes/perfis:

- `hermes profile list`
- `hermes profile show <nome>`
- `hermes profile create <nome>`
- `hermes profile create <nome> --clone`
- `hermes profile create <nome> --clone-all`
- `hermes profile use <nome>`
- `hermes profile describe <nome> --auto`
- `hermes profile export <nome>`
- `hermes profile import <arquivo.tar.gz>`
- `hermes profile install <fonte>`
- `hermes profile update <nome>`
- `hermes profile rename <antigo> <novo>`
- `hermes profile delete <nome>`

Quando houver alias de shell criado para o perfil, tambem e possivel usar o atalho do proprio perfil, como `coder chat`, ou abrir um chat com `hermes -p coder chat`.

## Hermes Compras (`hermes-compras`)

### Objetivo

Fluxo comercial e operacional para fornecedores, cotacoes de compra, RFQs, pedidos de compra, importacao, procurement, produtos e historico operacional.

### Como executar chamadas

- Use o chat do perfil de compras para analisar RFQs, classificar fornecedores, preparar e-mails e organizar follow-up.
- Use o dashboard para revisar status, sessões, configuracao e eventos do agente.
- Quando o fluxo depender de dados externos, priorize fontes verificaveis e historico interno.

### Cadastros

- Fornecedor
- Produto
- Cotacao de compra
- RFQ
- Pedido de compra
- Historico operacional
- Aprovação de e-mail e draft

### Buscas

- ERP e CRM internos quando existirem
- Memoria Hermes
- E-mails e anexos
- Historico de compra
- Logs operacionais
- Buscas web para verificacao de fabricante, catalogo e sinais de exportacao

### Fontes recomendadas

- Site oficial do fabricante
- Catalogo do fabricante
- Feiras e expositores
- Registro de exportacao
- E-mails recebidos de fornecedores
- Anexos tecnicos

### Web UI e dashboards

- Dashboard local: `http://127.0.0.1:9119`
- Quando o dashboard estiver aberto, use-o para ver configuracao, sessoes, perfis e estado do ambiente.
- O painel de aprovacao do fluxo de compras pode aparecer em:
  - `/hermes/approval/panel`
  - `/hermes/approval/monitor.json`

### Telegram

- O Telegram e um canal interno autorizado para compras.
- Fora do escopo, o canal deve recusar usos genericos.
- Permissoes e allowlists do Telegram sao definidas por configuracao do bot.

### WhatsApp

- Use somente se o fluxo de compras estiver habilitado no ambiente.
- Em geral, WhatsApp e mais apropriado para notificacao, aprovacao e respostas operacionais curtas.
- Nao misture o fluxo de compras com mensagens comerciais de AIVO ou vendas.

### Dados

- Dados operacionais de email e aprovacao ficam em JSONL, drafts, arquivos brutos e metadados do stack Hermes Mail.
- O fluxo de compras no repositório segue a estrategia "dry-run-first".
- Envio real deve ocorrer apenas depois de aprovacao manual explicita.

### Autorizações

- Aprovação manual para envio real de e-mail.
- Allowlist de usuarios quando o canal exigir.
- Secretos devem ser redigidos e nunca expostos.

### URLs uteis

- BotFather do Telegram: `https://t.me/BotFather`
- Portal Hermes / dashboard self-hosted: `https://portal.nousresearch.com`
- Catalogo de modelos: `https://hermes-agent.nousresearch.com/docs/api/model-catalog.json`

### Modelo de e-mail de compras

- Assinatura e tom devem ser comerciais e objetivos.
- Estrutura tipica:
  - assunto com identificacao da RFQ
  - breve contexto do pedido
  - pergunta objetiva sobre preco, MOQ, lead time, incoterm, pagamento, garantia e especificacoes
  - fechamento padronizado da area de compras
- O stack usa engines e filas de aprovacao para manter rastreabilidade.

## Hermes Vendas (`hermes-vendas`)

### Objetivo

Fluxo comercial para clientes, CRM comercial, propostas, cotacoes de venda, pedidos, follow-ups, oportunidades e historico comercial.

### Como executar chamadas

- Use o chat do perfil de vendas para responder leads, estruturar propostas e seguir oportunidades.
- Use o dashboard para conferir o estado do perfil, as sessoes e a configuracao.
- Quando nao houver configuracao completa, o sistema deve falhar de forma segura em vez de improvisar.

### Cadastros

- Cliente
- Oportunidade
- Proposta
- Cotacao de venda
- Pedido
- Follow-up
- Historico comercial

### Buscas

- CRM comercial
- E-mails recebidos e enviados
- Historico de negociação
- Anexos e documentos de proposta
- Memoria Hermes

### Fontes recomendadas

- Base comercial interna
- Historico de e-mails
- Anexos comerciais
- Informacoes de contato do cliente
- Dados de produto e disponibilidade

### Web UI e dashboards

- Dashboard local: `http://127.0.0.1:9119`
- Se vendas estiver exposto em outro front, mantenha a mesma logica de perfil e separacao de contexto.

### Telegram

- O Telegram tambem pode transportar operacoes de vendas internas, desde que o canal esteja autorizado para isso.
- Nao use o Telegram como chat generico.

### WhatsApp

- Pode ser usado para relacao comercial, mas respeitando o fluxo e o tom da area de vendas.
- Nao reutilize o discurso do AIVO Note.

### Dados

- Dados de vendas devem ficar separados de compras.
- Historico, assinaturas e credenciais nao devem ser misturados com o fluxo de compras.

### Autorizações

- Separacao rigida entre credenciais de compras e vendas.
- Quando o fluxo comercial ainda nao estiver configurado, a resposta segura deve ser explicita.

### URLs uteis

- Portal Hermes / dashboard self-hosted: `https://portal.nousresearch.com`
- Dashboard local: `http://127.0.0.1:9119`

### Modelo de e-mail de vendas

- Assinatura comercial da area de vendas.
- Estrutura tipica:
  - saudacao curta
  - contexto da oportunidade
  - proposta ou resposta comercial
  - proximo passo claro
  - fechamento padronizado

## Suporte Tecnico Hermes / Infraestrutura

### Objetivo

Resolver problemas tecnicos de Hermes, Docker, containers, gateway, WhatsApp, Telegram, Open WebUI, LiteLLM, APIs, integracoes, logs, erros de Python, YAML, JavaScript, servidores, deploy, permissoes, variaveis de ambiente e banco de dados.

### Como executar chamadas

- Use o chat do perfil tecnico para diagnostico.
- Reuna evidencia local antes de propor mudancas.
- Se o caso envolver alteracao sensivel, proponha backup ou peça confirmacao.

### Cadastros

- Configuracoes do ambiente
- Variaveis de ambiente
- Credenciais de integracao
- Perfis e aliases
- Registros de servico e deploy

### Buscas

- Logs
- Arquivos de configuracao
- Dockerfiles e compose files
- YAML
- Scripts de inicializacao
- Mensagens de erro
- Estado do gateway e sessoes

### Fontes recomendadas

- Evidencia local primeiro
- Logs reais
- Arquivos do repositório
- Saida de comandos
- Stack trace

### Web UI e dashboards

- Dashboard local: `http://127.0.0.1:9119`
- Open WebUI local: `http://127.0.0.1:8080`
- Hermes API para Open WebUI: `http://hermes-api:8000/v1`

### Telegram

- O Telegram pode ser canal de alerta e operacao tecnica, mas deve respeitar o escopo interno.

### WhatsApp

- Use apenas se o problema for do proprio gateway ou da integracao.
- Evite misturar suporte tecnico com conversa comercial.

### Dados

- Use logs, JSONL, configs e arquivos de estado para diagnostico.
- Nunca exponha chaves, tokens ou senhas.

### Autorizações

- `redact_secrets` deve permanecer ligado.
- `allow_private_urls` esta desabilitado.
- `tirith` e validacao de seguranca devem ser respeitados.

### URLs uteis

- Dashboard local: `http://127.0.0.1:9119`
- Open WebUI: `http://127.0.0.1:8080`
- Hermes API: `http://hermes-api:8000/v1`

## AIVO Note no WhatsApp

### Objetivo

Atendimento comercial do AIVO Note no WhatsApp.

### Como executar chamadas

- Responda apenas a mensagem final para o cliente.
- Use frases curtas, em portugues do Brasil.
- Para remarketing, destaque o beneficio, o desconto e o link correto.

### Cadastros

- Conversas de WhatsApp
- Contexto de aprendizado
- Sessões de remarketing
- Allowlist de telefone

### Buscas

- Contexto de aprendizado do AIVO
- Historico da conversa
- Regras do produto
- Mensagens anteriores do cliente, quando permitidas

### Fontes recomendadas

- Materiais do produto AIVO Note
- Conversa atual
- Base de aprendizado do AIVO
- Regras comerciais do produto

### Web UI e dashboards

- O atendimento AIVO normalmente acontece no WhatsApp, nao no dashboard de uso geral.
- Se houver integrações de monitoramento, elas devem respeitar o mesmo contexto comercial.

### Telegram

- Nao usar o Telegram como canal principal do AIVO Note.

### WhatsApp

- Canal principal do AIVO Note.
- Nao falar em lead, CRM, automacao de WhatsApp ou chatbot.
- Nao comecar com "Claro".
- Nao usar lista no primeiro contato.

### Dados

- O fluxo usa contexto de aprendizado e estado de sessão.
- Quando houver remarketing, o link padrao do repositório e:
  - `https://www.aivonote.com.br/discount/AIVONOTE10?redirect=%2Fproducts%2Fgravador-digital-com-inteligencia-artificial-6996073195503`

### Autorizações

- O AIVO usa allowlist de telefone no modo canonico.
- No arquivo de configuracao atual, o numero permitido canonico e `554491569673`.

### URLs uteis

- Link de remarketing / desconto: `https://www.aivonote.com.br/discount/AIVONOTE10?redirect=%2Fproducts%2Fgravador-digital-com-inteligencia-artificial-6996073195503`

### Modelo de mensagem do AIVO

- Primeiro contato:
  - "Oi! Que bom que voce chamou."
  - "O AIVO Note e um gravador inteligente com IA para registrar reunioes, aulas, entrevistas ou atendimentos sem perder detalhes importantes."
  - "Voce pensa em usar mais em reunioes, aulas, entrevistas ou atendimentos?"

## Padrao de aprovacao

Use esta ordem de seguranca para qualquer agente:

1. Identificar o perfil correto.
2. Verificar o canal.
3. Buscar a fonte interna mais confiavel.
4. Validar se a resposta depende de aprovacao.
5. Evitar vazamento de segredo ou dados sensiveis.
6. Responder curto, objetivo e com proxima acao clara.

## URLs e portas importantes

- Dashboard Hermes: `http://127.0.0.1:9119`
- Open WebUI: `http://127.0.0.1:8080`
- Hermes API para Open WebUI: `http://hermes-api:8000/v1`
- Painel de aprovacao: `/hermes/approval/panel`
- Monitor de aprovacao: `/hermes/approval/monitor.json`
- Telegram BotFather: `https://t.me/BotFather`
- Portal Hermes / Nous: `https://portal.nousresearch.com`
- Catalogo de modelos: `https://hermes-agent.nousresearch.com/docs/api/model-catalog.json`

## Modelo padrao para os proximos manuais

Sempre que um novo agente for criado, gere o manual com esta estrutura:

- Nome do agente
- Objetivo
- O que pode fazer
- Como executar chamadas
- Cadastros principais
- Buscas e fontes
- Web UI e dashboards
- Telegram
- WhatsApp
- Dados
- Autorizações
- URLs uteis
- Modelo de e-mail ou mensagem
- Observacoes de seguranca

Para manter consistencia, cada novo manual deve repetir o mesmo formato e apenas trocar o conteudo especifico do agente.
