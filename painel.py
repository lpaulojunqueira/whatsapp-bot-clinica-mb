"""
Painel web do produto Converte.ai.

- Tela de login estilizada com a marca.
- Inbox de conversas (atualiza sozinha a cada 5s).
- Visualização da conversa selecionada.
- Botão "Assumir" pausa a Ana e permite enviar mensagens manualmente.
- Botão "Devolver pra Ana" retoma o atendimento automático.

Multi-tenant: admin (sem clinica_id) vê tudo; usuário comum vê só a clínica dele.
"""

import os
import requests
from functools import wraps
from flask import (
    request, jsonify, session, redirect, url_for, render_template_string
)
from werkzeug.security import check_password_hash

from db import (
    buscar_usuario_por_email,
    listar_conversas,
    buscar_conversa_completa,
    marcar_pausa_conversa,
    salvar_mensagem,
    listar_clinicas,
    criar_usuario_clinica,
)


# ============================================================
# AUTENTICAÇÃO
# ============================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/painel/api"):
                return jsonify({"erro": "nao autenticado"}), 401
            return redirect(url_for("painel_login_page"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("clinica_id") is not None:
            return jsonify({"erro": "apenas admin"}), 403
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# TELA DE LOGIN
# ============================================================
LOGIN_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — Converte.ai</title>
<link rel="icon" type="image/png" sizes="64x64" href="/static/favicon-64.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --verde: #1FBE82;
    --verde-escuro: #16A370;
    --verde-claro: #E6F7EF;
    --carvao: #2D2E3C;
    --cinza-texto: #6B7280;
    --cinza-borda: #E5E7EB;
    --cinza-bg: #F9FAFB;
    --laranja: #F59E0B;
    --laranja-bg: #FEF3C7;
    --vermelho: #EF4444;
    --vermelho-bg: #FEE2E2;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, "Segoe UI", system-ui, sans-serif;
    background: var(--cinza-bg);
    color: var(--carvao);
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }
  .card {
    background: #fff;
    padding: 40px;
    border-radius: 16px;
    box-shadow: 0 8px 32px rgba(45, 46, 60, 0.08);
    width: 100%;
    max-width: 400px;
  }
  .logo-wrap {
    display: flex; justify-content: center; margin-bottom: 28px;
  }
  .logo-wrap img { height: 36px; }
  h1 {
    font-size: 20px; font-weight: 600; text-align: center;
    margin-bottom: 4px; color: var(--carvao);
  }
  .sub {
    color: var(--cinza-texto); font-size: 14px;
    text-align: center; margin-bottom: 28px;
  }
  label {
    display: block; font-size: 13px; font-weight: 500;
    color: var(--carvao); margin-bottom: 8px;
  }
  input {
    width: 100%;
    padding: 12px 14px;
    border: 1.5px solid var(--cinza-borda);
    border-radius: 10px;
    font-size: 15px;
    font-family: inherit;
    color: var(--carvao);
    margin-bottom: 18px;
    transition: border-color .15s, box-shadow .15s;
  }
  input:focus {
    outline: none;
    border-color: var(--verde);
    box-shadow: 0 0 0 4px var(--verde-claro);
  }
  button {
    width: 100%;
    padding: 13px;
    background: var(--carvao);
    color: #fff;
    border: none;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: background .15s;
    margin-top: 6px;
  }
  button:hover { background: #1f2030; }
  .erro {
    background: var(--vermelho-bg);
    color: #991B1B;
    padding: 12px 14px;
    border-radius: 10px;
    font-size: 13px;
    margin-bottom: 18px;
  }
  .footer {
    text-align: center;
    margin-top: 24px;
    font-size: 12px;
    color: var(--cinza-texto);
  }
</style>
</head>
<body>
  <form class="card" method="post" action="/painel/login">
    <div class="logo-wrap">
      <img src="/static/logo-converte.png" alt="Converte.ai">
    </div>
    <h1>Bem-vindo de volta</h1>
    <div class="sub">Entre pra acompanhar suas conversas</div>
    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
    <label>Email</label>
    <input type="email" name="email" required autofocus placeholder="seu@email.com">
    <label>Senha</label>
    <input type="password" name="senha" required placeholder="••••••••">
    <button type="submit">Entrar</button>
    <div class="footer">Plataforma de atendimento por IA</div>
  </form>
</body>
</html>
"""


# ============================================================
# PAINEL PRINCIPAL
# ============================================================
PAINEL_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Converte.ai — Atendimento</title>
<link rel="icon" type="image/png" sizes="64x64" href="/static/favicon-64.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --verde: #1FBE82;
    --verde-escuro: #16A370;
    --verde-claro: #E6F7EF;
    --verde-bolha: #DCFCE7;
    --carvao: #2D2E3C;
    --carvao-suave: #4B5563;
    --cinza-texto: #6B7280;
    --cinza-fraco: #9CA3AF;
    --cinza-borda: #E5E7EB;
    --cinza-divisor: #F3F4F6;
    --cinza-bg: #F9FAFB;
    --laranja: #D97706;
    --laranja-bg: #FEF3C7;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: 'Inter', -apple-system, "Segoe UI", system-ui, sans-serif;
    background: var(--cinza-bg);
    color: var(--carvao);
    overflow: hidden;
  }
  .app { display: flex; flex-direction: column; height: 100vh; }

  /* ========== HEADER (top da tela inteira) ========== */
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 24px;
    background: #fff;
    border-bottom: 1px solid var(--cinza-borda);
    height: 64px; flex-shrink: 0;
  }
  .header-logo img { height: 28px; display: block; }
  .header-user {
    display: flex; align-items: center; gap: 14px;
  }
  .user-info {
    text-align: right; line-height: 1.2;
  }
  .user-info .nome {
    font-size: 13px; font-weight: 600; color: var(--carvao);
  }
  .user-info .role {
    font-size: 11px; color: var(--cinza-texto); margin-top: 2px;
  }
  .user-avatar {
    width: 36px; height: 36px; border-radius: 50%;
    background: var(--carvao); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 14px;
  }
  .header-user .sair {
    color: var(--cinza-texto);
    text-decoration: none;
    font-size: 13px;
    padding: 8px 12px;
    border-radius: 8px;
    transition: background .15s, color .15s;
  }
  .header-user .sair:hover {
    background: var(--cinza-divisor);
    color: var(--carvao);
  }

  /* ========== LAYOUT PRINCIPAL ========== */
  .main { display: flex; flex: 1; overflow: hidden; }

  /* ========== SIDEBAR ========== */
  .sidebar {
    width: 380px; flex-shrink: 0;
    background: #fff;
    border-right: 1px solid var(--cinza-borda);
    display: flex; flex-direction: column;
  }
  .sidebar-top {
    padding: 18px 20px 14px;
    border-bottom: 1px solid var(--cinza-divisor);
  }
  .sidebar-top .label {
    font-size: 11px; font-weight: 600;
    color: var(--cinza-fraco);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 4px;
  }
  .sidebar-top .clinica-nome {
    font-size: 16px; font-weight: 600; color: var(--carvao);
  }
  .lista {
    flex: 1; overflow-y: auto;
  }
  .conversa-item {
    padding: 14px 20px;
    border-bottom: 1px solid var(--cinza-divisor);
    cursor: pointer;
    display: flex; gap: 12px;
    transition: background .12s;
    border-left: 3px solid transparent;
  }
  .conversa-item:hover { background: var(--cinza-bg); }
  .conversa-item.ativa {
    background: var(--verde-claro);
    border-left-color: var(--verde);
  }
  .avatar {
    width: 42px; height: 42px; border-radius: 50%;
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-weight: 600; font-size: 15px;
  }
  .ci-corpo { flex: 1; min-width: 0; }
  .ci-topo {
    display: flex; justify-content: space-between;
    align-items: baseline;
    margin-bottom: 3px;
    gap: 8px;
  }
  .ci-lead {
    font-size: 14px; font-weight: 600; color: var(--carvao);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ci-clinica {
    font-size: 10px; color: var(--verde-escuro);
    text-transform: uppercase; letter-spacing: 0.5px;
    font-weight: 600; flex-shrink: 0;
  }
  .ci-preview {
    font-size: 13px; color: var(--cinza-texto);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ci-badge {
    display: inline-block;
    background: var(--laranja-bg);
    color: var(--laranja);
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 6px;
    margin-left: 6px;
    font-weight: 600;
    vertical-align: middle;
  }
  .vazio {
    padding: 40px 20px; color: var(--cinza-fraco);
    text-align: center; font-size: 14px;
  }

  /* ========== DETALHE ========== */
  .detalhe {
    flex: 1;
    display: flex; flex-direction: column;
    background: var(--cinza-bg);
    min-width: 0;
  }
  .detalhe-topo {
    padding: 14px 24px;
    border-bottom: 1px solid var(--cinza-borda);
    background: #fff;
    display: flex; justify-content: space-between; align-items: center;
    flex-shrink: 0;
  }
  .det-info { display: flex; align-items: center; gap: 12px; }
  .det-info .titulo {
    font-weight: 600; font-size: 15px; color: var(--carvao);
  }
  .det-info .sub {
    font-size: 12px; color: var(--cinza-texto); margin-top: 2px;
  }
  .btn {
    padding: 9px 16px;
    border-radius: 8px;
    border: none;
    font-size: 13px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: background .15s, transform .05s;
  }
  .btn:active { transform: scale(0.98); }
  .btn-primario {
    background: var(--carvao); color: #fff;
  }
  .btn-primario:hover { background: #1f2030; }
  .btn-verde {
    background: var(--verde); color: #fff;
  }
  .btn-verde:hover { background: var(--verde-escuro); }

  .mensagens {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .msg {
    max-width: 65%;
    padding: 10px 14px;
    border-radius: 14px;
    font-size: 14px;
    line-height: 1.5;
    word-wrap: break-word;
    white-space: pre-wrap;
  }
  .msg-lead {
    background: #fff;
    border: 1px solid var(--cinza-borda);
    align-self: flex-start;
    border-bottom-left-radius: 4px;
    color: var(--carvao);
  }
  .msg-ana {
    background: var(--verde-bolha);
    align-self: flex-end;
    border-bottom-right-radius: 4px;
    color: var(--carvao);
  }

  .aviso {
    padding: 12px 24px;
    background: var(--laranja-bg);
    color: var(--laranja);
    font-size: 13px; font-weight: 500;
    text-align: center;
    border-top: 1px solid #FDE68A;
    flex-shrink: 0;
  }

  .composer {
    padding: 14px 20px;
    border-top: 1px solid var(--cinza-borda);
    background: #fff;
    display: flex; gap: 10px; align-items: flex-end;
    flex-shrink: 0;
  }
  .composer textarea {
    flex: 1; resize: none;
    padding: 11px 14px;
    border: 1.5px solid var(--cinza-borda);
    border-radius: 10px;
    font-family: inherit; font-size: 14px;
    height: 44px; max-height: 120px;
    color: var(--carvao);
    transition: border-color .15s, box-shadow .15s;
  }
  .composer textarea:focus {
    outline: none;
    border-color: var(--verde);
    box-shadow: 0 0 0 3px var(--verde-claro);
  }

  /* ========== ESTADO VAZIO ========== */
  .placeholder {
    flex: 1;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    color: var(--cinza-fraco);
    text-align: center;
    padding: 40px;
  }
  .placeholder-icone {
    width: 80px; height: 80px;
    background: #fff;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 20px;
    box-shadow: 0 2px 8px rgba(45, 46, 60, 0.05);
  }
  .placeholder-icone svg {
    width: 36px; height: 36px;
    stroke: var(--verde);
  }
  .placeholder-titulo {
    font-size: 16px; font-weight: 600;
    color: var(--carvao); margin-bottom: 6px;
  }
  .placeholder-sub {
    font-size: 14px; color: var(--cinza-texto);
  }

  /* ========== BOTÃO VOLTAR (só mobile) ========== */
  .btn-voltar {
    display: none;
    background: none; border: none;
    width: 36px; height: 36px;
    align-items: center; justify-content: center;
    cursor: pointer;
    margin-right: 4px;
    border-radius: 8px;
    transition: background .15s;
    flex-shrink: 0;
  }
  .btn-voltar:hover { background: var(--cinza-divisor); }
  .btn-voltar svg {
    width: 22px; height: 22px;
    stroke: var(--carvao);
  }

  /* ========== RESPONSIVO MOBILE ========== */
  @media (max-width: 768px) {
    .header { padding: 10px 16px; height: 56px; }
    .header-logo img { height: 24px; }
    .user-info { display: none; }
    .user-avatar { width: 32px; height: 32px; font-size: 13px; }
    .header-user { gap: 10px; }
    .header-user .sair { padding: 6px 10px; font-size: 12px; }

    /* No mobile: sidebar OU detalhe (não os dois ao mesmo tempo) */
    .sidebar {
      width: 100%;
      border-right: none;
    }
    .detalhe {
      display: none;
      position: fixed;
      top: 56px; left: 0; right: 0; bottom: 0;
      background: var(--cinza-bg);
      z-index: 10;
    }
    /* Quando estado é "conversa aberta": esconde sidebar, mostra detalhe */
    body.conversa-aberta .sidebar { display: none; }
    body.conversa-aberta .detalhe { display: flex; }

    .btn-voltar { display: flex; }

    .detalhe-topo { padding: 10px 14px; }
    .det-info .titulo { font-size: 14px; }
    .det-info .sub { font-size: 11px; }
    .det-info .avatar { width: 36px; height: 36px; font-size: 13px; }
    .btn { padding: 7px 12px; font-size: 12px; }

    .mensagens { padding: 16px; gap: 4px; }
    .msg { max-width: 80%; font-size: 14px; padding: 9px 12px; }

    .composer { padding: 10px 14px; }
    .composer textarea { font-size: 14px; padding: 10px 12px; }

    .sidebar-top { padding: 14px 16px 10px; }
    .conversa-item { padding: 12px 16px; }
    .ci-clinica { font-size: 9px; }

    .placeholder { padding: 30px 20px; }
    .placeholder-icone { width: 64px; height: 64px; margin-bottom: 16px; }
    .placeholder-icone svg { width: 28px; height: 28px; }
    .placeholder-titulo { font-size: 15px; }
    .placeholder-sub { font-size: 13px; }
  }
</style>
</head>
<body>
<div class="app">

  <header class="header">
    <div class="header-logo">
      <img src="/static/logo-converte.png" alt="Converte.ai">
    </div>
    <div class="header-user">
      <div class="user-info">
        <div class="nome">{{ nome }}</div>
        <div class="role">{{ "Admin" if eh_admin else "Cliente" }}</div>
      </div>
      <div class="user-avatar">{{ inicial }}</div>
      <a href="/painel/logout" class="sair">Sair</a>
    </div>
  </header>

  <div class="main">
    <aside class="sidebar">
      <div class="sidebar-top">
        <div class="label">Atendimento de</div>
        <div class="clinica-nome">{{ contexto }}</div>
      </div>
      <div class="lista" id="lista">
        <div class="vazio">Carregando...</div>
      </div>
    </aside>

    <main class="detalhe">
      <div class="placeholder" id="placeholder">
        <div class="placeholder-icone">
          <svg fill="none" stroke="currentColor" stroke-width="2"
               viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.86 9.86 0 0 1-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>
          </svg>
        </div>
        <div class="placeholder-titulo">Selecione uma conversa</div>
        <div class="placeholder-sub">As mensagens aparecem aqui em tempo real</div>
      </div>

      <div class="detalhe-topo" id="detalhe-topo" style="display:none">
        <div class="det-info">
          <button type="button" class="btn-voltar" onclick="voltarParaLista()" aria-label="Voltar">
            <svg fill="none" stroke="currentColor" stroke-width="2.5"
                 viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="15 18 9 12 15 6"></polyline>
            </svg>
          </button>
          <div class="avatar" id="det-avatar" style="background:#1FBE82">—</div>
          <div>
            <div class="titulo" id="det-titulo">—</div>
            <div class="sub" id="det-sub">—</div>
          </div>
        </div>
        <button class="btn" id="btn-acao" onclick="alternarPausa()">—</button>
      </div>
      <div class="mensagens" id="mensagens" style="display:none"></div>
      <div class="aviso" id="aviso-pausada" style="display:none">
        Você assumiu essa conversa. A Ana não vai responder até você devolver o controle.
      </div>
      <form class="composer" id="composer" style="display:none"
            onsubmit="enviarMsg(event)">
        <textarea id="msg-texto" placeholder="Digite sua mensagem..." required></textarea>
        <button class="btn btn-primario" type="submit">Enviar</button>
      </form>
    </main>
  </div>
</div>

<script>
let conversaSelecionada = null;
let conversas = [];

// Gera cor estável a partir do número (cada lead tem sua cor).
function corDoLead(numero) {
  const cores = [
    '#1FBE82', '#3B82F6', '#8B5CF6', '#EC4899',
    '#F59E0B', '#10B981', '#6366F1', '#14B8A6',
    '#F97316', '#06B6D4'
  ];
  let hash = 0;
  for (let i = 0; i < numero.length; i++) {
    hash = numero.charCodeAt(i) + ((hash << 5) - hash);
  }
  return cores[Math.abs(hash) % cores.length];
}

function inicialDe(numero) {
  // Pega os 2 últimos dígitos pra usar como "inicial visual"
  const s = (numero || '').replace(/\\D/g, '');
  return s.slice(-2) || '?';
}

function formatarNumero(n) {
  // Formata BR: 55 11 99999-9999 → (11) 99999-9999
  if (!n) return '—';
  const limpo = n.replace(/\\D/g, '');
  if (limpo.length >= 12 && limpo.startsWith('55')) {
    const ddd = limpo.slice(2, 4);
    const resto = limpo.slice(4);
    if (resto.length === 9) {
      return `(${ddd}) ${resto.slice(0,5)}-${resto.slice(5)}`;
    } else if (resto.length === 8) {
      return `(${ddd}) ${resto.slice(0,4)}-${resto.slice(4)}`;
    }
  }
  return n;
}

async function carregarConversas() {
  const r = await fetch('/painel/api/conversas');
  if (!r.ok) return;
  conversas = await r.json();

  const lista = document.getElementById('lista');
  if (conversas.length === 0) {
    lista.innerHTML = '<div class="vazio">Nenhuma conversa ainda.<br>Vai aparecer aqui quando algum lead mandar mensagem.</div>';
    return;
  }

  const eh_admin = {{ "true" if eh_admin else "false" }};

  lista.innerHTML = conversas.map(c => `
    <div class="conversa-item ${c.id === conversaSelecionada ? 'ativa' : ''}"
         onclick="abrirConversa(${c.id})">
      <div class="avatar" style="background:${corDoLead(c.numero_lead)}">
        ${inicialDe(c.numero_lead)}
      </div>
      <div class="ci-corpo">
        <div class="ci-topo">
          <span class="ci-lead">${formatarNumero(c.numero_lead)}</span>
          ${eh_admin ? `<span class="ci-clinica">${escapar(c.clinica_nome)}</span>` : ''}
        </div>
        <div class="ci-preview">
          ${c.ultima_role === 'user' ? '' : 'Ana: '}${escapar((c.ultima_mensagem || '—').substring(0, 70))}
          ${c.pausada ? '<span class="ci-badge">HUMANO</span>' : ''}
        </div>
      </div>
    </div>
  `).join('');
}

async function abrirConversa(id) {
  const ehMesmaConversa = (id === conversaSelecionada);
  conversaSelecionada = id;

  // No mobile, ativa o modo "conversa em tela cheia"
  document.body.classList.add('conversa-aberta');

  const r = await fetch('/painel/api/conversas/' + id);
  if (!r.ok) return;
  const data = await r.json();

  document.getElementById('placeholder').style.display = 'none';
  document.getElementById('detalhe-topo').style.display = 'flex';
  document.getElementById('mensagens').style.display = 'flex';

  document.getElementById('det-titulo').textContent = formatarNumero(data.info.numero_lead);
  document.getElementById('det-sub').textContent = data.info.clinica_nome;
  const av = document.getElementById('det-avatar');
  av.style.background = corDoLead(data.info.numero_lead);
  av.textContent = inicialDe(data.info.numero_lead);

  const btn = document.getElementById('btn-acao');
  if (data.info.pausada) {
    btn.textContent = 'Devolver pra Ana';
    btn.className = 'btn btn-verde';
    document.getElementById('aviso-pausada').style.display = 'block';
    document.getElementById('composer').style.display = 'flex';
  } else {
    btn.textContent = 'Assumir conversa';
    btn.className = 'btn btn-primario';
    document.getElementById('aviso-pausada').style.display = 'none';
    document.getElementById('composer').style.display = 'none';
  }

  const ms = document.getElementById('mensagens');

  // Decide se devemos forçar o scroll pro fim.
  // Regra: força se for conversa nova OU se o usuário já estava perto do fim.
  // Se ele tá lendo histórico (rolou pra cima), respeita a posição dele.
  const estavaNoFim = !ehMesmaConversa ||
    (ms.scrollHeight - ms.scrollTop - ms.clientHeight < 80);

  ms.innerHTML = data.mensagens.map(m => `
    <div class="msg ${m.role === 'user' ? 'msg-lead' : 'msg-ana'}">
      ${escapar(m.conteudo)}
    </div>
  `).join('');

  if (estavaNoFim) {
    ms.scrollTop = ms.scrollHeight;
  }

  carregarConversas();
}

function voltarParaLista() {
  // Sai do modo "conversa em tela cheia" (mobile)
  document.body.classList.remove('conversa-aberta');
  conversaSelecionada = null;
}

async function alternarPausa() {
  if (!conversaSelecionada) return;
  await fetch('/painel/api/conversas/' + conversaSelecionada + '/alternar-pausa',
              { method: 'POST' });
  abrirConversa(conversaSelecionada);
}

async function enviarMsg(e) {
  e.preventDefault();
  const ta = document.getElementById('msg-texto');
  const texto = ta.value.trim();
  if (!texto) return;
  ta.disabled = true;
  const r = await fetch('/painel/api/conversas/' + conversaSelecionada + '/enviar', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ texto })
  });
  ta.disabled = false;
  if (r.ok) {
    ta.value = '';
    abrirConversa(conversaSelecionada);
  } else {
    alert('Erro ao enviar mensagem.');
  }
}

function escapar(s) {
  return (s || '').toString()
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
}

carregarConversas();
setInterval(() => {
  carregarConversas();
  if (conversaSelecionada) abrirConversa(conversaSelecionada);
}, 5000);
</script>
</body>
</html>
"""


# ============================================================
# REGISTRO DAS ROTAS NO APP
# ============================================================
def registrar_rotas(app):
    """Anexa todas as rotas do painel ao app Flask principal."""

    @app.route("/painel", methods=["GET"])
    def painel_home():
        if "user_id" not in session:
            return redirect(url_for("painel_login_page"))
        nome = session.get("nome", "—")
        clinica_id = session.get("clinica_id")
        eh_admin = clinica_id is None
        # Define o contexto que aparece na sidebar (qual clínica essa pessoa atende)
        if eh_admin:
            contexto = "Todas as clínicas"
        else:
            # Busca o nome da clínica do usuário
            clinicas = listar_clinicas()
            clinica = next((c for c in clinicas if c["id"] == clinica_id), None)
            contexto = clinica["nome"] if clinica else "—"
        inicial = (nome[:1] or "?").upper()
        return render_template_string(
            PAINEL_HTML,
            nome=nome,
            inicial=inicial,
            eh_admin=eh_admin,
            contexto=contexto,
        )

    @app.route("/painel/login", methods=["GET"])
    def painel_login_page():
        return render_template_string(LOGIN_HTML, erro=None)

    @app.route("/painel/login", methods=["POST"])
    def painel_login_action():
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        user = buscar_usuario_por_email(email)
        if not user or not check_password_hash(user["senha_hash"], senha):
            return render_template_string(LOGIN_HTML, erro="Email ou senha inválidos.")
        session["user_id"] = user["id"]
        session["nome"] = user.get("nome") or email
        session["clinica_id"] = user.get("clinica_id")
        return redirect(url_for("painel_home"))

    @app.route("/painel/logout")
    def painel_logout():
        session.clear()
        return redirect(url_for("painel_login_page"))

    # ---------- API JSON ----------
    @app.route("/painel/api/conversas", methods=["GET"])
    @login_required
    def api_listar_conversas():
        clinica_id = session.get("clinica_id")
        rows = listar_conversas(clinica_id=clinica_id)
        for r in rows:
            if r.get("atualizada_em"):
                r["atualizada_em"] = r["atualizada_em"].isoformat()
        return jsonify(rows)

    @app.route("/painel/api/conversas/<int:conversa_id>", methods=["GET"])
    @login_required
    def api_conversa_detalhe(conversa_id):
        data = buscar_conversa_completa(conversa_id)
        if not data:
            return jsonify({"erro": "nao encontrada"}), 404
        if session.get("clinica_id") is not None:
            if data["info"]["clinica_id"] != session["clinica_id"]:
                return jsonify({"erro": "sem permissao"}), 403
        for m in data["mensagens"]:
            if m.get("criada_em"):
                m["criada_em"] = m["criada_em"].isoformat()
        return jsonify(data)

    @app.route("/painel/api/conversas/<int:conversa_id>/alternar-pausa", methods=["POST"])
    @login_required
    def api_alternar_pausa(conversa_id):
        data = buscar_conversa_completa(conversa_id)
        if not data:
            return jsonify({"erro": "nao encontrada"}), 404
        if session.get("clinica_id") is not None:
            if data["info"]["clinica_id"] != session["clinica_id"]:
                return jsonify({"erro": "sem permissao"}), 403
        nova = not data["info"]["pausada"]
        marcar_pausa_conversa(conversa_id, nova)
        return jsonify({"pausada": nova})

    @app.route("/painel/api/conversas/<int:conversa_id>/enviar", methods=["POST"])
    @login_required
    def api_enviar_msg(conversa_id):
        body = request.get_json() or {}
        texto = (body.get("texto") or "").strip()
        if not texto:
            return jsonify({"erro": "texto vazio"}), 400

        data = buscar_conversa_completa(conversa_id)
        if not data:
            return jsonify({"erro": "nao encontrada"}), 404
        if session.get("clinica_id") is not None:
            if data["info"]["clinica_id"] != session["clinica_id"]:
                return jsonify({"erro": "sem permissao"}), 403

        info = data["info"]
        try:
            url = f"https://graph.facebook.com/v21.0/{info['phone_number_id']}/messages"
            headers = {
                "Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}",
                "Content-Type": "application/json",
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": info["numero_lead"],
                "type": "text",
                "text": {"body": texto},
            }
            r = requests.post(url, headers=headers, json=payload, timeout=20)
            r.raise_for_status()
        except Exception as e:
            print(f"❌ Erro ao enviar mensagem manual: {e}")
            return jsonify({"erro": "falha no envio"}), 500

        salvar_mensagem(conversa_id, "assistant", texto)
        return jsonify({"ok": True})

    # ---------- Admin: criar usuário pra clínica ----------
    @app.route("/painel/admin/usuarios", methods=["GET"])
    @login_required
    @admin_required
    def admin_listar_clinicas():
        return jsonify(listar_clinicas())

    @app.route("/painel/admin/usuarios", methods=["POST"])
    @login_required
    @admin_required
    def admin_criar_usuario():
        body = request.get_json() or {}
        email = (body.get("email") or "").strip().lower()
        senha = body.get("senha") or ""
        nome = body.get("nome") or ""
        clinica_id = body.get("clinica_id")
        if not (email and senha and clinica_id):
            return jsonify({"erro": "campos obrigatorios: email, senha, clinica_id"}), 400
        try:
            uid = criar_usuario_clinica(email, senha, nome, int(clinica_id))
            return jsonify({"id": uid, "email": email})
        except Exception as e:
            return jsonify({"erro": str(e)}), 400
