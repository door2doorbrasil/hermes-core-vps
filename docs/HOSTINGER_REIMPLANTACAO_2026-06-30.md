# Hermes Reimplantacao Hostinger

Data base: 2026-06-30

## Objetivo

Subir o Hermes customizado do repositório local para a VPS Hostinger de forma limpa, sem reaproveitar a stack padrao de Hermes Workspace e sem herdar artefatos antigos do volume.

## Estado validado localmente

- A imagem customizada do repo compila em `linux/amd64`.
- O dashboard do Hermes responde em `GET /login?next=%2F` no container de smoke.
- O gateway inicia sob `s6` e o WhatsApp sobe a partir do bridge embutido na imagem.
- O bridge do WhatsApp conecta e registra a sessao em `/opt/data/whatsapp/session`.

## Dependencias de runtime

- Base Linux `amd64`.
- Python 3.13 via `uv`.
- Node 22 LTS com `npm`, `npx` e `corepack`.
- Playwright Chromium instalado na imagem.
- `libolm-dev`, `ffmpeg`, `git`, `openssh-client`, `docker-cli`, toolchain C/C++ e utilitarios de sistema.
- Bridge do WhatsApp com `@whiskeysockets/baileys`, `express`, `pino`, `qrcode-terminal` e `link-preview-js`.

## Dados e volumes obrigatorios

- `/opt/data`
- `/opt/data/whatsapp/session/creds.json`
- `/opt/data/whatsapp/bridge.log`
- `/opt/data/hermes-mail`
- bases e caches persistidos de operacao do Hermes

## Variaveis criticas

- `OPENAI_API_KEY`
- `API_SERVER_KEY`
- `BUY_IMAP_HOST`
- `BUY_IMAP_PORT`
- `BUY_IMAP_SSL`
- `BUY_IMAP_USERNAME`
- `BUY_IMAP_PASSWORD`
- `BUY_SMTP_HOST`
- `BUY_SMTP_PORT`
- `BUY_SMTP_SSL`
- `BUY_SMTP_USERNAME`
- `BUY_SMTP_PASSWORD`
- `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`
- `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`
- `HERMES_DASHBOARD_BASIC_AUTH_SECRET`
- `WHATSAPP_ALLOWED_USERS`
- `LITELLM_MASTER_KEY`

## Pontos que quebravam o WhatsApp

1. O runtime reutilizava uma copia antiga do bridge em `/opt/data/scripts/whatsapp-bridge`.
2. Essa copia antiga nao tinha `node_modules` e forçava `npm install` em boot.
3. O bridge novo da imagem estava correto, mas o volume persistido tinha prioridade pratica no boot.
4. Havia tambem falta de `link-preview-js` como dependencia em runtime para alguns caminhos do Baileys.

## Correcao aplicada

- O resolver do bridge foi endurecido para preferir a copia da imagem quando a copia do volume esta stale ou incompleta.
- O bridge ganhou `link-preview-js` como dependencia direta.
- A imagem foi reconstruida em `linux/amd64` e validada novamente no smoke.

## Resultado do smoke local

- `hermes-agent` sobe com dashboard ativo.
- O WhatsApp conecta com dependencias instaladas corretamente.
- O bridge registra sessao em `/opt/data/whatsapp/session`.
- O `Open WebUI` e o `LiteLLM` continuam como servicos auxiliares, mas nao foram o foco da validacao do WhatsApp.

## Notas de implantacao na VPS

- A VPS deve receber a stack customizada do repositório `hermes-core-vps`, nao a stack padrao do Hostinger Hermes Workspace.
- O deploy deve partir de volume limpo para o projeto Hermes.
- Se existir copia antiga do bridge no volume da VPS, ela deve ser removida antes do primeiro boot.
- Depois do deploy, o smoke principal e verificar o dashboard do Hermes e os logs do WhatsApp.

## Pendencia residual

- O `Open WebUI` e o `LiteLLM` subiram no container, mas o endpoint HTTP nao estava respondendo de forma estavel durante este smoke.
- Isso nao bloqueia o corte do Hermes principal, mas merece uma validacao separada depois da virada da VPS.
