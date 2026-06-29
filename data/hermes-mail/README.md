# Hermes Mail — implantação controlada de email em produção

Este diretório concentra o stack operacional de email do projeto Hermes Mail.
A abordagem é **dry-run-first**: primeiro validar leitura, parsing, auditoria,
identidade e drafts; **envio real só depois de aprovação manual explícita**.

## Objetivo

Preparar uma operação segura para:

- **Compras internacionais** com a identidade **Polar Sinergy LLC**
- **Vendas / representação comercial** com a identidade **D2D Representação Comercial Ltda**
- separação rígida entre perfis, assinaturas, histórico, CRM e credenciais
- rastreabilidade completa de RFQ, threads, drafts e aprovações
- integração com **Open WebUI + LiteLLM + Hermes Agent** sem expor modelos irrestritos
- notificações operacionais via **Telegram**

## O que já foi feito

### 1) Base de email controlado

Arquivos principais criados/ajustados:

- `scripts/email_real_common.py`
- `scripts/imap_ingestor.py`
- `scripts/email_real_parser.py`
- `scripts/smtp_sender.py`
- `scripts/approval_queue.py`
- `scripts/reply_processor.py`
- `scripts/hermes_mail_store.py`
- `scripts/data_audit.py`

Esses módulos cobrem:

- leitura IMAP controlada
- parsing MIME / materialização de dados úteis
- criação de drafts SMTP
- fila de aprovação manual
- revisão de replies
- backup e auditoria

### 2) IDs estáveis e rastreabilidade

O fluxo passou a usar identificadores estáveis baseados em **SHA-1** para evitar
comportamento não determinístico em IDs derivados de `Message-ID` e anexos.

### 3) Notificações e observabilidade

- integração com **Telegram** nos eventos principais
- estatísticas e validação via CLI dos módulos centrais
- auditoria contínua dos JSONL e diretórios operacionais
- backup dos dados relevantes, incluindo `config/`

### 4) Marca e templates RFQ

- `config/brand_profile.json`
- `scripts/rfq_template_engine.py`
- `docs/RFQ_TEMPLATE_ENGINE.md`

Isso padroniza assinatura, tom, estrutura de RFQ e materiais de resposta.

### 5) CRM / Vendas / exclusões

Foi criado o módulo:

- `scripts/hermes_sales.py`
- `docs/HERMES_SALES.md`

Ele cobre:

- cadastro de exclusões comerciais
- importação de relatórios
- follow-ups
- base para CRM internacional

### 6) Identidade, perfis e credenciais

Foi criada a camada de identidade para separar fluxos e permissões por perfil.
O fluxo de compras resolve a identidade correta e o fluxo de vendas retorna de
forma segura quando ainda não está configurado.

### 7) Stack de orquestração local

Também foram preparados overlays locais para a cadeia:

- **Open WebUI**
- **LiteLLM**
- **Hermes Agent**

Arquivos relacionados:

- `/opt/data/open-webui/docker-compose.open-webui.yml`
- `/opt/data/open-webui/README.md`
- `/opt/data/litellm/docker-compose.litellm.yml`
- `/opt/data/litellm/litellm.yaml`
- `/opt/data/litellm/README.md`

## Validações já executadas

Últimos sinais válidos observados:

- `imap_ingestor.py validate` → **OK**
- `smtp_sender.py stats` → **OK**, sem envio real
- `approval_queue.py list-pending` → **OK**, fila vazia
- `email_real_parser.py process-latest` → **OK**, sem pendências locais
- `rfq_template_engine.py validate` → **OK**
- `open_webui_api.py validate` → **OK**
- `telegram_notifier.py get-me` → **OK**
- `data_audit.py audit` → **OK**, sem issues conhecidos no último estado consolidado

### Ponto que ainda bloqueia o go-live

- `imap_ingestor.py connect-check` ainda falha por **autenticação/conexão IMAP**
- `imap_ingestor.py fetch-latest --limit 5` falha pela mesma causa
- portanto, **a leitura real de caixa ainda não está liberada**

## Estado atual

### Compras

- fluxo pronto para operação controlada
- foco em `BUY_*`
- leitura IMAP ainda precisa da credencial/host corretos
- SMTP real continua bloqueado por aprovação manual

### Vendas

- fluxo preparado com separação rígida
- usa `SALES_*` quando disponível
- se vendas ainda não estiver configurado, o sistema responde de forma segura
  com **`Vendas ainda não configurado`**

### Segurança operacional

- nenhuma política de envio em massa foi habilitada
- nenhum envio SMTP real foi feito
- o modo continua **controlado** e com rastreabilidade

## Próximos passos para produção

1. **Corrigir o IMAP de compras**
   - validar `BUY_IMAP_HOST`
   - validar `BUY_IMAP_PORT`
   - validar `BUY_IMAP_SSL`
   - validar `BUY_IMAP_USERNAME`
   - validar `BUY_IMAP_PASSWORD`

2. **Executar leitura real mínima**
   - ler os **5 primeiros emails** da caixa de compras
   - conferir thread, anexos, remetente e assunto

3. **Processar e-mails reais**
   - materializar produtos, fornecedores e cotações
   - revisar se o parser está registrando tudo corretamente

4. **Gerar drafts e aprovações**
   - criar draft de resposta
   - validar fila de aprovação
   - manter envio real bloqueado até aprovação explícita

5. **Fazer o primeiro SMTP real aprovado**
   - apenas depois de validação humana
   - sem envio em massa
   - com rastreio de `draft_id` e `rfq_id`

6. **Consolidar identidade/perfis**
   - ligar os headers/sessão do Open WebUI ao resolvedor de identidade
   - garantir assinatura, conta e permissões corretas por fluxo

7. **Estender para vendas quando liberado**
   - ativar `SALES_*`
   - importar lista comercial real
   - validar follow-up de 7 dias em um caso real, como embaixada/SECOM

## Regras de operação

- **Compras** usam `BUY_*` exclusivamente
- **Vendas** usam `SALES_*` somente quando configurado
- nunca misturar histórico, templates ou assinaturas entre os fluxos
- IMAP permanece em leitura controlada
- SMTP real somente com aprovação manual explícita
- manter backups e auditoria sempre ativos

## Observação

Este README é o ponto de entrada operacional para a implantação de email em
produção. Ele registra o que já foi preparado e o que ainda falta para liberar
o go-live com segurança.
