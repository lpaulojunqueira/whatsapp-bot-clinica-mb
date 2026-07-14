# Converte.ai

SaaS multi-tenant de atendimento via WhatsApp por IA para clínicas e prestadores da área da saúde.

## Stack
- Flask 3.0 (Python)
- PostgreSQL via `psycopg[binary]==3.2.13` (v3, NUNCA psycopg2 — incompatível com Python 3.14 no Render)
- Claude API direto via `requests` (modelo: `claude-haiku-4-5-20251001`)
- WhatsApp Cloud API v21.0 (oficial da Meta)
- OpenAI Whisper (transcrição de áudio)
- Hospedagem: Render Starter ($7/mês) + PostgreSQL Basic-256mb

## Arquivos principais
- `app.py` — webhook + processamento background + integração WhatsApp/Claude/Whisper + tool use + modo lead/dono + notificações
- `db.py` — todas funções PostgreSQL (clinicas, conversas, mensagens, usuarios, config_horarios, agendamentos, bloqueios)
- `painel.py` — painel Flask com login + tela admin (4 abas: Clientes, Usuários, Prompts, Horários) + view Agenda semanal

## Cliente piloto em produção
MB Odontologia (clinica_id=1) — Mogi Guaçu/SP — Dr. Matheus + Dra. Maryah

## Regras absolutas
- Modelo Claude sempre `claude-haiku-4-5-20251001`
- Postgres sempre `psycopg[binary]==3.2.13`, jamais psycopg2
- Multi-tenant: dados sempre isolados por `clinica_id` no WHERE
- Token WhatsApp por cliente: coluna `whatsapp_token` no banco, com fallback pra `WHATSAPP_TOKEN` global do Render
- Modo dono: ativa quando mensagem vem do `telefone_humano` do cliente (compara últimos 10 dígitos, ignora DDI)
- Persona da Ana: sem emoji, tom caloroso mas sóbrio, nunca cita preços em contexto de clínica

## Fluxo de deploy
Push no GitHub → Render puxa automaticamente → deploy em ~2-3 min

## Antes de mexer em algo
1. Ler o arquivo afetado por inteiro antes de editar
2. Validar sintaxe Python após qualquer mudança
3. Em mudanças que afetam múltiplos arquivos, fazer um por vez
4. NUNCA remover função sem confirmar que ninguém mais usa (grep antes)
5. Preservar identidade visual do painel (verde #1FBE82, carvão #2D2E3C, Inter)

## Estado atual (julho 2026)
- Sistema em produção com 1 cliente pagante (MB)
- Foco comercial: vender pra 3 primeiros clientes com oferta R$ 800 setup + R$ 800 mês 1 + R$ 1600/mês recorrente
- Modo agência (WABAs ficam na BM Luizpaulo.js do Luiz), não SaaS puro
- Sprints pendentes prioritários:
  1. ~~Notificação WhatsApp pro dono precisa citar TRATAMENTO/motivo do agendamento~~ (feito 10/jul)
  2. Múltiplos profissionais por clínica (adiado até 2-3 clientes)
  3. Confirmação 24h anti no-show (precisa entender custo Meta primeiro)

## PENDÊNCIA CRÍTICA: template de notificação (13/jul/2026)
A notificação de agendamento pro dono (`notificar_agendamento_para_responsavel` em
`app.py`) mandava mensagem de texto livre (`type: text`). Isso só entrega se o
destinatário tiver mandado mensagem pro número da Ana nas últimas 24h — como o
dono normalmente NÃO conversa direto com a Ana, a notificação vinha falhando
silenciosamente (só log no Render, ninguém via). É por isso que a MB parou de
receber aviso de agendamento.

Correção: `enviar_template_whatsapp()` manda via template aprovado (ignora a
janela de 24h), com fallback pro texto livre se o template falhar.

**Isso só funciona depois de criar e ter aprovado o template na Meta:**
- WhatsApp Manager → Modelos de mensagem → Criar modelo
- Nome: `notificacao_agendamento_ana` (bate com `TEMPLATE_NOTIFICACAO_AGENDAMENTO`
  em `app.py`; dá pra sobrescrever via env var do mesmo nome)
- Categoria: Utilitário
- Idioma: Português (BR)
- Corpo (6 variáveis, NESSA ORDEM — tipo, paciente, telefone, data, horário, motivo):
  ```
  📋 Notificação de agendamento pela Ana

  *Tipo:* {{1}}
  *Paciente:* {{2}}
  *Telefone:* {{3}}
  *Data:* {{4}}
  *Horário:* {{5}}
  *Motivo:* {{6}}

  Ver detalhes e gerenciar no painel Converte.ai
  ```
- Preencher exemplos de cada variável na hora de submeter (exigido pela Meta),
  ex: Novo / Maria Silva / 5519999999999 / Quinta-feira, 18 de junho de 2026 / 14:30 / Avaliação
- Aprovação da Meta costuma sair em minutos pra template utilitário simples,
  mas pode levar até 24h.
- Enquanto não aprovado, o fallback de texto livre mantém o comportamento atual
  (só funciona se o dono tiver conversado recentemente com a Ana).

## Contexto do Luiz
Estilo direto, anti-hype. Prefere respostas objetivas mas com desenvolvimento. Não busca validação. Quer conselho estratégico honesto, não gerador de respostas agradáveis.
