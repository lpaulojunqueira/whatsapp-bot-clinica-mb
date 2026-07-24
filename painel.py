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
import threading
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
    listar_clinicas_com_stats,
    obter_clinica,
    criar_clinica,
    atualizar_clinica,
    atualizar_prompt_clinica,
    criar_usuario_clinica,
    obter_config_horarios,
    atualizar_config_horarios,
    listar_agendamentos,
    listar_bloqueios,
    cancelar_agendamento,
    remover_bloqueio,
    obter_agendamento,
    criar_agendamento,
    remarcar_agendamento,
    atualizar_agendamento,
    existe_conflito,
    criar_bloqueio,
    listar_profissionais,
    contar_profissionais_ativos,
    obter_profissional,
    criar_profissional,
    atualizar_profissional,
    obter_conversa,
    registrar_venda,
    buscar_conversa_por_numero,
    listar_agendamentos_para_reenvio_capi,
    capi_evento_ja_enviado,
    diagnostico_capi,
)
import capi


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

  /* ============================================================ */
  /* ABAS PRINCIPAIS (Conversas / Agenda)                          */
  /* ============================================================ */
  .header-abas {
    display: flex;
    gap: 4px;
    margin: 0 24px;
    flex: 1;
    max-width: 400px;
  }
  .aba-principal {
    background: none;
    border: none;
    padding: 8px 16px;
    font-family: inherit;
    font-size: 14px;
    font-weight: 600;
    color: var(--cinza-texto);
    cursor: pointer;
    border-radius: 8px;
    transition: all 0.15s;
    position: relative;
  }
  .aba-principal:hover {
    background: rgba(31, 190, 130, 0.06);
    color: var(--carvao);
  }
  .aba-principal.aba-ativa {
    color: var(--verde-escuro);
    background: rgba(31, 190, 130, 0.10);
  }

  @media (max-width: 768px) {
    .header-abas { margin: 0 12px; max-width: none; gap: 2px; }
    .aba-principal { padding: 6px 10px; font-size: 12px; }
  }

  /* ============================================================ */
  /* AGENDA (view semanal tipo Google Calendar)                    */
  /* ============================================================ */
  .sidebar-agenda { padding-bottom: 20px; }

  .agenda-nav {
    display: flex;
    gap: 6px;
    padding: 12px 20px;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--cinza-borda);
  }
  .agenda-nav-btn {
    background: #fff;
    border: 1.5px solid var(--cinza-borda);
    color: var(--carvao);
    width: 34px; height: 34px;
    border-radius: 8px;
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer;
    transition: all 0.15s;
  }
  .agenda-nav-btn:hover {
    background: var(--cinza-bg);
    border-color: var(--verde);
  }
  .agenda-hoje-btn {
    background: var(--verde);
    color: #fff;
    border: none;
    padding: 8px 16px;
    border-radius: 8px;
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
    flex: 1;
    max-width: 100px;
    transition: background 0.15s;
  }
  .agenda-hoje-btn:hover { background: var(--verde-escuro); }

  .agenda-legenda {
    padding: 16px 20px;
    border-bottom: 1px solid var(--cinza-borda);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .legenda-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--cinza-texto);
  }
  .legenda-cor {
    width: 14px; height: 14px;
    border-radius: 3px;
    display: inline-block;
    border: 1px solid rgba(0,0,0,0.08);
  }
  .cor-confirmado { background: var(--verde); }
  .cor-bloqueio { background: #FCA5A5; }
  .cor-livre { background: #F3F4F6; }
  .cor-fora { background: #E5E7EB; opacity: 0.5; }

  .agenda-resumo {
    padding: 16px 20px;
    font-size: 12px;
    color: var(--cinza-texto);
    line-height: 1.6;
  }
  .agenda-resumo strong {
    color: var(--carvao);
    font-size: 20px;
    display: block;
    line-height: 1.2;
    margin-bottom: 2px;
  }

  .detalhe-agenda {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .agenda-topo {
    padding: 16px 24px;
    border-bottom: 1px solid var(--cinza-borda);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .agenda-titulo {
    font-size: 16px;
    font-weight: 600;
    color: var(--carvao);
  }

  .agenda-container {
    flex: 1;
    overflow: auto;
    background: #FAFBFC;
    position: relative;
  }
  .agenda-vazio {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--cinza-texto);
    padding: 40px;
  }
  .agenda-vazio-icone {
    width: 80px; height: 80px;
    background: var(--cinza-bg);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 16px;
  }
  .agenda-vazio-titulo { font-size: 16px; font-weight: 600; margin-bottom: 6px; }
  .agenda-vazio-sub { font-size: 13px; }

  .agenda-grid {
    display: grid;
    grid-template-columns: 60px repeat(7, 1fr);
    min-width: 700px;
  }
  .agenda-cabecalho {
    display: contents;
  }
  .agenda-cab-canto {
    background: #fff;
    border-bottom: 1px solid var(--cinza-borda);
    border-right: 1px solid var(--cinza-borda);
    position: sticky; top: 0; left: 0; z-index: 3;
  }
  .agenda-cab-dia {
    background: #fff;
    padding: 12px 8px;
    text-align: center;
    border-bottom: 1px solid var(--cinza-borda);
    border-right: 1px solid var(--cinza-borda);
    font-size: 12px;
    font-weight: 600;
    color: var(--cinza-texto);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky; top: 0; z-index: 2;
  }
  .agenda-cab-dia .numero {
    font-size: 20px;
    font-weight: 700;
    color: var(--carvao);
    text-transform: none;
    letter-spacing: 0;
    margin-top: 2px;
  }
  .agenda-cab-dia.hoje .numero {
    background: var(--verde);
    color: #fff;
    width: 30px; height: 30px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .agenda-hora-label {
    background: #fff;
    padding: 4px 8px;
    text-align: right;
    font-size: 11px;
    color: var(--cinza-texto);
    border-right: 1px solid var(--cinza-borda);
    border-top: 1px solid #F0F0F0;
    position: sticky; left: 0; z-index: 1;
    line-height: 1;
    padding-top: 6px;
  }
  .agenda-celula {
    background: #fff;
    border-top: 1px solid #F0F0F0;
    border-right: 1px solid var(--cinza-borda);
    min-height: 40px;
    position: relative;
    padding: 2px;
  }
  .agenda-celula.fora-expediente {
    background: #F9FAFB;
    background-image: repeating-linear-gradient(
      45deg,
      transparent, transparent 6px,
      rgba(0,0,0,0.02) 6px, rgba(0,0,0,0.02) 12px
    );
  }

  .agenda-bloco {
    background: var(--verde);
    color: #fff;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 11px;
    line-height: 1.3;
    cursor: pointer;
    overflow: hidden;
    position: absolute;
    left: 2px; right: 2px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.1);
    transition: transform 0.1s, box-shadow 0.1s;
    z-index: 2;
  }
  .agenda-bloco:hover {
    transform: scale(1.02);
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    z-index: 5;
  }
  .agenda-bloco .bloco-hora {
    font-size: 10px;
    opacity: 0.9;
    font-weight: 600;
  }
  .agenda-bloco .bloco-nome {
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .agenda-bloco .bloco-prof {
    font-size: 9px;
    opacity: 0.9;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 1px;
  }
  .agenda-bloco.bloco-bloqueio {
    background: #FCA5A5;
    color: #7F1D1D;
  }
  .agenda-bloco.bloco-manual {
    background: #3B82F6;
  }

  /* Modal */
  .agenda-modal {
    position: fixed; top:0; left:0; right:0; bottom:0;
    background: rgba(0,0,0,0.5);
    z-index: 100;
    display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }
  .agenda-modal-conteudo {
    background: #fff;
    border-radius: 12px;
    width: 100%; max-width: 420px;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  }
  .agenda-modal-topo {
    padding: 18px 20px;
    border-bottom: 1px solid var(--cinza-borda);
    display: flex; justify-content: space-between; align-items: center;
  }
  .agenda-modal-titulo { font-size: 16px; font-weight: 600; }
  .agenda-modal-fechar {
    background: none; border: none; font-size: 26px; color: var(--cinza-texto);
    cursor: pointer; padding: 0; line-height: 1; width: 30px; height: 30px;
  }
  .agenda-modal-body { padding: 20px; font-size: 14px; line-height: 1.6; }
  .agenda-modal-body .linha { display: flex; margin-bottom: 10px; }
  .agenda-modal-body .rotulo {
    color: var(--cinza-texto); width: 90px; font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .agenda-modal-body .valor { color: var(--carvao); flex: 1; }
  .agenda-modal-rodape {
    padding: 14px 20px; border-top: 1px solid var(--cinza-borda);
    display: flex; gap: 10px; justify-content: flex-end;
  }
  .btn-perigo {
    background: #DC2626; color: #fff; border: none;
    padding: 8px 16px; border-radius: 8px; font-weight: 600; font-size: 13px;
    cursor: pointer; font-family: inherit;
  }
  .btn-perigo:hover { background: #B91C1C; }

  /* Formulários dentro do modal da agenda (criar/editar) */
  .ag-form label {
    display: block; font-size: 12px; font-weight: 600;
    color: var(--cinza-texto); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 6px;
  }
  .ag-form input, .ag-form select {
    width: 100%; padding: 10px 12px;
    border: 1.5px solid var(--cinza-borda); border-radius: 8px;
    font-size: 14px; font-family: inherit; color: var(--carvao);
    margin-bottom: 14px; background: #fff;
    transition: border-color .15s, box-shadow .15s;
  }
  .ag-form input:focus, .ag-form select:focus {
    outline: none; border-color: var(--verde);
    box-shadow: 0 0 0 3px var(--verde-claro);
  }

  /* Célula livre é clicável (cria agendamento/bloqueio) */
  .agenda-celula { cursor: pointer; }
  .agenda-celula:not(.fora-expediente):hover { background: var(--verde-claro); }

  /* ============================================================ */
  /* DATA/HORA nas mensagens do painel de conversas                */
  /* ============================================================ */
  .msg-wrapper {
    display: flex;
    flex-direction: column;
  }
  .msg-wrapper.wrap-lead { align-items: flex-start; }
  .msg-wrapper.wrap-ana { align-items: flex-end; }
  .msg-hora {
    font-size: 10px;
    color: var(--cinza-texto);
    margin: 2px 8px 4px;
    opacity: 0.75;
  }
  .separador-dia {
    align-self: center;
    background: rgba(0,0,0,0.05);
    color: var(--cinza-texto);
    font-size: 11px;
    font-weight: 600;
    padding: 4px 12px;
    border-radius: 12px;
    margin: 16px 0 8px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  @media (max-width: 768px) {
    .agenda-grid { min-width: 640px; grid-template-columns: 48px repeat(7, 1fr); }
    .agenda-cab-dia { padding: 8px 4px; font-size: 10px; }
    .agenda-cab-dia .numero { font-size: 16px; }
    .agenda-cab-dia.hoje .numero { width: 24px; height: 24px; font-size: 12px; }
    .agenda-hora-label { font-size: 9px; padding: 4px 4px 0; }
    .agenda-celula { min-height: 32px; }
    .agenda-bloco { font-size: 10px; padding: 2px 4px; }
    .agenda-bloco .bloco-hora { font-size: 9px; }

    /* MOBILE — agenda usa layout EMPILHADO (não sidebar-OU-detalhe como conversas) */
    .view-agenda {
      flex-direction: column;
    }
    .view-agenda .sidebar-agenda {
      width: 100% !important;
      border-right: none;
      border-bottom: 1px solid var(--cinza-borda);
      display: block !important;    /* sobrescreve display:none do estado conversa-aberta */
      position: static !important;
      height: auto;
    }
    .view-agenda .detalhe-agenda {
      display: flex !important;      /* sobrescreve display:none padrão de .detalhe mobile */
      position: static !important;
      top: auto; left: auto; right: auto; bottom: auto;
      min-height: 60vh;
      z-index: auto;
    }

    /* Sidebar da agenda no mobile: layout mais compacto */
    .sidebar-agenda .sidebar-top { padding: 12px 14px 8px; }
    .agenda-nav { padding: 8px 14px; }
    .agenda-legenda {
      padding: 10px 14px;
      flex-direction: row;
      flex-wrap: wrap;
      gap: 12px;
    }
    .agenda-resumo { padding: 10px 14px; display: flex; gap: 20px; }
    .agenda-resumo > div { margin-bottom: 0 !important; }
    .agenda-resumo strong { font-size: 16px; display: inline; margin-right: 4px; }

    .agenda-topo { padding: 12px 14px; }
    .agenda-titulo { font-size: 14px; }
  }
</style>
</head>
<body>
<div class="app">

  <header class="header">
    <div class="header-logo">
      <img src="/static/logo-converte.png" alt="Converte.ai">
    </div>
    <nav class="header-abas">
      <button type="button" class="aba-principal aba-ativa" data-view="conversas"
              onclick="trocarView('conversas')">Conversas</button>
      <button type="button" class="aba-principal" data-view="agenda"
              onclick="trocarView('agenda')">Agenda</button>
    </nav>
    <div class="header-user">
      <div class="user-info">
        <div class="nome">{{ nome }}</div>
        <div class="role">{{ "Admin" if eh_admin else "Cliente" }}</div>
      </div>
      <div class="user-avatar">{{ inicial }}</div>
      {% if eh_admin %}<a href="/painel/admin" class="sair" style="color: var(--verde-escuro)">Admin</a>{% endif %}
      <a href="/painel/logout" class="sair">Sair</a>
    </div>
  </header>

  <div class="main view-conversas" id="view-conversas">
    <aside class="sidebar">
      <div class="sidebar-top">
        <div class="label">{{ "Filtrar por" if eh_admin else "Atendimento de" }}</div>
        {% if eh_admin %}
          <select id="filtro-cliente" onchange="trocarFiltro()"
                  style="width:100%; padding:8px 10px; margin-top:6px;
                         border:1.5px solid var(--cinza-borda); border-radius:8px;
                         font-family:inherit; font-size:14px; font-weight:600;
                         color:var(--carvao); background:#fff; cursor:pointer;">
            <option value="">Todos os clientes</option>
          </select>
        {% else %}
          <div class="clinica-nome">{{ contexto }}</div>
        {% endif %}
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
        <div style="display:flex; gap:8px; align-items:center;">
          <button class="btn btn-pequeno" id="btn-venda" onclick="abrirModalVenda()">Registrar venda</button>
          <button class="btn" id="btn-acao" onclick="alternarPausa()">—</button>
        </div>
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

  <!-- =========================== VIEW: AGENDA =========================== -->
  <div class="main view-agenda" id="view-agenda" style="display:none">
    <aside class="sidebar sidebar-agenda">
      <div class="sidebar-top">
        <div class="label">{{ "Filtrar por" if eh_admin else "Agenda de" }}</div>
        {% if eh_admin %}
          <select id="agenda-cliente" onchange="agendaTrocarCliente()"
                  style="width:100%; padding:8px 10px; margin-top:6px;
                         border:1.5px solid var(--cinza-borda); border-radius:8px;
                         font-family:inherit; font-size:14px; font-weight:600;
                         color:var(--carvao); background:#fff; cursor:pointer;">
            <option value="">Selecione um cliente</option>
          </select>
        {% else %}
          <div class="clinica-nome">{{ contexto }}</div>
        {% endif %}
      </div>

      <div class="agenda-nav">
        <button type="button" class="agenda-nav-btn" onclick="agendaSemanaAnt()" aria-label="Semana anterior">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
               stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>
        </button>
        <button type="button" class="agenda-hoje-btn" onclick="agendaIrHoje()">Hoje</button>
        <button type="button" class="agenda-nav-btn" onclick="agendaSemanaProx()" aria-label="Próxima semana">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
               stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
        </button>
      </div>

      <div id="agenda-filtro-prof-wrap" style="display:none; padding:12px 20px; border-bottom:1px solid var(--cinza-borda);">
        <div class="label" style="font-size:11px; font-weight:600; color:var(--cinza-fraco); text-transform:uppercase; letter-spacing:0.6px; margin-bottom:6px;">Profissional</div>
        <select id="agenda-filtro-prof" onchange="renderizarAgenda()"
                style="width:100%; padding:8px 10px; border:1.5px solid var(--cinza-borda);
                       border-radius:8px; font-family:inherit; font-size:14px; font-weight:600;
                       color:var(--carvao); background:#fff; cursor:pointer;">
          <option value="">Todos os profissionais</option>
        </select>
      </div>

      <div class="agenda-legenda">
        <div class="legenda-item"><span class="legenda-cor cor-confirmado"></span> Agendamento</div>
        <div class="legenda-item"><span class="legenda-cor cor-bloqueio"></span> Bloqueio</div>
        <div class="legenda-item"><span class="legenda-cor cor-livre"></span> Livre</div>
        <div class="legenda-item"><span class="legenda-cor cor-fora"></span> Fora do expediente</div>
      </div>

      <div id="agenda-legenda-prof" class="agenda-legenda" style="display:none; border-top:1px dashed var(--cinza-borda);"></div>

      <div class="agenda-resumo" id="agenda-resumo"></div>
    </aside>

    <main class="detalhe detalhe-agenda">
      <div class="agenda-topo">
        <div class="agenda-titulo" id="agenda-titulo">Selecione um cliente pra ver a agenda</div>
      </div>
      <div class="agenda-container" id="agenda-container">
        <div class="agenda-vazio" id="agenda-vazio">
          <div class="agenda-vazio-icone">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                 stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
              <line x1="16" y1="2" x2="16" y2="6"></line>
              <line x1="8" y1="2" x2="8" y2="6"></line>
              <line x1="3" y1="10" x2="21" y2="10"></line>
            </svg>
          </div>
          <div class="agenda-vazio-titulo">Nenhuma agenda carregada</div>
          <div class="agenda-vazio-sub">Escolha um cliente pra visualizar a semana</div>
        </div>
      </div>
    </main>
  </div>

  <!-- Modal de detalhe do agendamento/bloqueio -->
  <div id="agenda-modal" class="agenda-modal" style="display:none">
    <div class="agenda-modal-conteudo">
      <div class="agenda-modal-topo">
        <div class="agenda-modal-titulo" id="ag-modal-titulo">Detalhes</div>
        <button type="button" class="agenda-modal-fechar" onclick="fecharModalAgenda()"
                aria-label="Fechar">×</button>
      </div>
      <div class="agenda-modal-body" id="ag-modal-body"></div>
      <div class="agenda-modal-rodape" id="ag-modal-rodape"></div>
    </div>
  </div>

  <!-- Modal de registrar venda (dispara Purchase) -->
  <div id="venda-modal" class="agenda-modal" style="display:none">
    <div class="agenda-modal-conteudo">
      <div class="agenda-modal-topo">
        <div class="agenda-modal-titulo">Registrar venda</div>
        <button type="button" class="agenda-modal-fechar" onclick="fecharModalVenda()"
                aria-label="Fechar">×</button>
      </div>
      <div class="agenda-modal-body">
        <div class="ag-form">
          <label>Valor (R$) <span style="text-transform:none; font-weight:400">— recomendado pra otimizar por ROAS</span></label>
          <input type="number" id="venda-valor" step="0.01" min="0" placeholder="Ex: 3000">
          <label>Descrição <span style="text-transform:none; font-weight:400">— opcional</span></label>
          <input type="text" id="venda-descricao" placeholder="Ex: contrato fechado, plano X">
        </div>
        <div style="font-size:12px; color:var(--cinza-texto); margin-top:2px;">
          Registra o fecho e, se o rastreamento Meta estiver ativo e o lead veio de anúncio,
          dispara o evento de conversão (Purchase) pra Meta.
        </div>
      </div>
      <div class="agenda-modal-rodape">
        <button type="button" class="btn btn-pequeno" onclick="fecharModalVenda()">Cancelar</button>
        <button type="button" class="btn btn-verde" onclick="salvarVenda()">Registrar</button>
      </div>
    </div>
  </div>
</div>

<script>
let conversaSelecionada = null;
let conversas = [];
let filtroClienteId = '';  // vazio = todos (só admin usa)

// Carrega lista de clientes no dropdown (só admin enxerga)
async function carregarClientesNoFiltro() {
  const sel = document.getElementById('filtro-cliente');
  if (!sel) return;  // não é admin
  try {
    const r = await fetch('/painel/admin/usuarios');
    if (!r.ok) return;
    const clientes = await r.json();
    const atual = sel.value;
    sel.innerHTML = '<option value="">Todos os clientes</option>' +
      clientes.map(c => `<option value="${c.id}">${escapar(c.nome)}</option>`).join('');
    sel.value = atual;
  } catch (e) { /* silencioso */ }
}

function trocarFiltro() {
  const sel = document.getElementById('filtro-cliente');
  filtroClienteId = sel ? sel.value : '';
  conversaSelecionada = null;
  document.body.classList.remove('conversa-aberta');
  // limpa a tela de detalhe
  document.getElementById('placeholder').style.display = 'flex';
  document.getElementById('detalhe-topo').style.display = 'none';
  document.getElementById('mensagens').style.display = 'none';
  document.getElementById('composer').style.display = 'none';
  document.getElementById('aviso-pausada').style.display = 'none';
  carregarConversas();
}

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
  const url = filtroClienteId
    ? '/painel/api/conversas?clinica_id=' + encodeURIComponent(filtroClienteId)
    : '/painel/api/conversas';
  const r = await fetch(url);
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

  ms.innerHTML = renderizarMensagensComData(data.mensagens);

  if (estavaNoFim) {
    ms.scrollTop = ms.scrollHeight;
  }

  carregarConversas();
}

function renderizarMensagensComData(mensagens) {
  const hoje = new Date();
  const ontem = new Date(hoje); ontem.setDate(ontem.getDate() - 1);
  const chaveDia = d => d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
  const chaveHoje = chaveDia(hoje);
  const chaveOntem = chaveDia(ontem);
  const nomesDias = ['domingo','segunda-feira','terça-feira','quarta-feira',
                     'quinta-feira','sexta-feira','sábado'];
  const nomesMeses = ['janeiro','fevereiro','março','abril','maio','junho',
                      'julho','agosto','setembro','outubro','novembro','dezembro'];

  let ultimaChave = null;
  const partes = [];

  for (const m of mensagens) {
    if (!m.criada_em) continue;
    const dt = new Date(m.criada_em);
    const chave = chaveDia(dt);

    if (chave !== ultimaChave) {
      let rotulo;
      if (chave === chaveHoje) rotulo = 'hoje';
      else if (chave === chaveOntem) rotulo = 'ontem';
      else {
        const semanaAtras = new Date(hoje); semanaAtras.setDate(semanaAtras.getDate() - 7);
        if (dt >= semanaAtras) {
          rotulo = nomesDias[dt.getDay()];
        } else {
          rotulo = dt.getDate() + ' de ' + nomesMeses[dt.getMonth()];
          if (dt.getFullYear() !== hoje.getFullYear()) {
            rotulo += ' de ' + dt.getFullYear();
          }
        }
      }
      partes.push(`<div class="separador-dia">${rotulo}</div>`);
      ultimaChave = chave;
    }

    const hh = String(dt.getHours()).padStart(2,'0');
    const mm = String(dt.getMinutes()).padStart(2,'0');
    const eLead = m.role === 'user';
    partes.push(`
      <div class="msg-wrapper wrap-${eLead ? 'lead' : 'ana'}">
        <div class="msg ${eLead ? 'msg-lead' : 'msg-ana'}">${escapar(m.conteudo)}</div>
        <div class="msg-hora">${hh}:${mm}</div>
      </div>
    `);
  }
  return partes.join('');
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

// ============ REGISTRAR VENDA (evento Purchase) ============
function abrirModalVenda() {
  if (!conversaSelecionada) return;
  document.getElementById('venda-valor').value = '';
  document.getElementById('venda-descricao').value = '';
  document.getElementById('venda-modal').style.display = 'flex';
}

function fecharModalVenda() {
  document.getElementById('venda-modal').style.display = 'none';
}

async function salvarVenda() {
  if (!conversaSelecionada) return;
  const valorStr = document.getElementById('venda-valor').value.trim();
  const descricao = document.getElementById('venda-descricao').value.trim();
  const r = await fetch('/painel/api/conversas/' + conversaSelecionada + '/venda', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ valor: valorStr || null, descricao })
  });
  if (r.ok) {
    fecharModalVenda();
    alert('Venda registrada.');
  } else {
    const d = await r.json().catch(() => ({}));
    alert('Erro: ' + (d.erro || 'falha ao registrar'));
  }
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

// ============================================================
// NAVEGAÇÃO DE VIEWS (Conversas / Agenda)
// ============================================================
let viewAtual = 'conversas';

function trocarView(nome) {
  viewAtual = nome;
  document.querySelectorAll('.aba-principal').forEach(b => {
    b.classList.toggle('aba-ativa', b.dataset.view === nome);
  });
  document.getElementById('view-conversas').style.display =
    (nome === 'conversas') ? '' : 'none';
  document.getElementById('view-agenda').style.display =
    (nome === 'agenda') ? '' : 'none';

  // Limpa classes de estado da outra view pra não contaminar o layout mobile
  if (nome === 'agenda') {
    // Trocando pra agenda: se estava com conversa aberta, sai desse estado
    document.body.classList.remove('conversa-aberta');
    inicializarAgendaSePreciso();
  }
}

// ============================================================
// AGENDA (view semanal)
// ============================================================
let agendaClinicaId = null;   // clínica selecionada (null = nenhuma)
let agendaSemanaInicio = null; // Date do domingo da semana atual
let agendaConfig = null;
let agendaDados = null;
let agendaProfissionais = [];  // profissionais ativos da clínica atual
let agendaInicializada = false;

// Paleta estável de cores por profissional (id -> cor)
const CORES_PROFISSIONAL = [
  '#2563EB', '#7C3AED', '#DB2777', '#059669',
  '#D97706', '#0891B2', '#4F46E5', '#BE123C'
];
function corDoProfissional(profId) {
  if (!profId) return '#1FBE82';  // sem profissional = verde padrão
  const idx = agendaProfissionais.findIndex(p => p.id === profId);
  return CORES_PROFISSIONAL[(idx >= 0 ? idx : profId) % CORES_PROFISSIONAL.length];
}

function inicializarAgendaSePreciso() {
  if (agendaInicializada) return;
  agendaInicializada = true;

  // Define semana atual (começa no domingo)
  agendaSemanaInicio = comecoDaSemana(new Date());

  // Popula dropdown de cliente pro admin
  const selAg = document.getElementById('agenda-cliente');
  if (selAg) {
    // Reutiliza a mesma lista de clientes do filtro de conversas
    fetch('/painel/admin/usuarios').then(r => r.ok ? r.json() : []).then(cs => {
      selAg.innerHTML = '<option value="">Selecione um cliente</option>' +
        cs.map(c => `<option value="${c.id}">${escapar(c.nome)}</option>`).join('');
    }).catch(() => {});
  } else {
    // Cliente logado (não-admin): já sabe qual clínica é
    agendaClinicaId = 'meu';  // marca especial — backend usa a clínica da sessão
    carregarAgenda();
  }
}

function agendaTrocarCliente() {
  const sel = document.getElementById('agenda-cliente');
  const v = sel.value;
  agendaClinicaId = v ? parseInt(v, 10) : null;
  if (agendaClinicaId) carregarAgenda();
  else limparAgenda();
}

function comecoDaSemana(d) {
  // Semana começa no domingo (0). Retorna Date às 00:00 do domingo.
  const dt = new Date(d);
  const diaSemana = dt.getDay();
  dt.setDate(dt.getDate() - diaSemana);
  dt.setHours(0, 0, 0, 0);
  return dt;
}

function agendaSemanaAnt() {
  if (!agendaSemanaInicio) return;
  agendaSemanaInicio.setDate(agendaSemanaInicio.getDate() - 7);
  if (agendaClinicaId) carregarAgenda();
}
function agendaSemanaProx() {
  if (!agendaSemanaInicio) return;
  agendaSemanaInicio.setDate(agendaSemanaInicio.getDate() + 7);
  if (agendaClinicaId) carregarAgenda();
}
function agendaIrHoje() {
  agendaSemanaInicio = comecoDaSemana(new Date());
  if (agendaClinicaId) carregarAgenda();
}

function limparAgenda() {
  document.getElementById('agenda-container').innerHTML = `
    <div class="agenda-vazio">
      <div class="agenda-vazio-icone">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
          <line x1="16" y1="2" x2="16" y2="6"></line>
          <line x1="8" y1="2" x2="8" y2="6"></line>
          <line x1="3" y1="10" x2="21" y2="10"></line>
        </svg>
      </div>
      <div class="agenda-vazio-titulo">Nenhuma agenda carregada</div>
      <div class="agenda-vazio-sub">Escolha um cliente pra visualizar a semana</div>
    </div>`;
  document.getElementById('agenda-titulo').textContent = 'Selecione um cliente pra ver a agenda';
  document.getElementById('agenda-resumo').innerHTML = '';
}

function iso(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
}

async function carregarAgenda() {
  if (!agendaClinicaId || !agendaSemanaInicio) return;
  const semanaFim = new Date(agendaSemanaInicio);
  semanaFim.setDate(semanaFim.getDate() + 7);

  const params = new URLSearchParams({
    inicio: iso(agendaSemanaInicio),
    fim: iso(semanaFim)
  });
  if (agendaClinicaId !== 'meu') params.set('clinica_id', agendaClinicaId);

  document.getElementById('agenda-container').innerHTML =
    '<div class="agenda-vazio"><div class="agenda-vazio-sub">Carregando agenda...</div></div>';

  try {
    const r = await fetch('/painel/api/agenda?' + params);
    if (!r.ok) throw new Error('Falha ao carregar');
    agendaDados = await r.json();
    agendaConfig = agendaDados.config;
    agendaProfissionais = agendaDados.profissionais || [];
    montarFiltroProfissionais();
    renderizarAgenda();
  } catch (e) {
    document.getElementById('agenda-container').innerHTML =
      '<div class="agenda-vazio"><div class="agenda-vazio-titulo">Erro ao carregar agenda</div><div class="agenda-vazio-sub">Tenta atualizar a página.</div></div>';
  }
}

// Popula o filtro de profissional + legenda de cores (só se a clínica tiver).
// Chamado a cada carga de dados; preserva a seleção atual do filtro.
function montarFiltroProfissionais() {
  const wrap = document.getElementById('agenda-filtro-prof-wrap');
  const sel = document.getElementById('agenda-filtro-prof');
  const legenda = document.getElementById('agenda-legenda-prof');
  if (agendaProfissionais.length === 0) {
    wrap.style.display = 'none';
    legenda.style.display = 'none';
    sel.value = '';
    return;
  }
  const anterior = sel.value;
  sel.innerHTML = '<option value="">Todos os profissionais</option>' +
    agendaProfissionais.map(p => `<option value="${p.id}">${escapar(p.nome)}</option>`).join('');
  // Só mantém a seleção se o profissional ainda existir
  sel.value = agendaProfissionais.some(p => String(p.id) === anterior) ? anterior : '';
  wrap.style.display = 'block';

  legenda.innerHTML = agendaProfissionais.map(p => `
    <div class="legenda-item">
      <span class="legenda-cor" style="background:${corDoProfissional(p.id)}"></span>
      ${escapar(p.nome)}
    </div>`).join('');
  legenda.style.display = 'flex';
}

function agendaFiltroProfId() {
  const sel = document.getElementById('agenda-filtro-prof');
  const v = sel ? sel.value : '';
  return v ? parseInt(v, 10) : null;
}

function renderizarAgenda() {
  if (!agendaDados) return;

  const cfg = agendaConfig;
  const horaAbre = parseInt(cfg.hora_inicio.split(':')[0], 10);
  const horaFecha = parseInt(cfg.hora_fim.split(':')[0], 10);
  // Mostra 1h antes/depois pra dar contexto visual
  const horaGridInicio = Math.max(0, horaAbre - 1);
  const horaGridFim = Math.min(24, horaFecha + 1);

  const diasSemanaCfg = new Set(cfg.dias_semana.split(',').map(s => parseInt(s.trim(), 10)));
  // db usa 1=segunda ... 7=domingo. JS usa 0=domingo ... 6=sábado.
  const jsParaDb = jsDia => jsDia === 0 ? 7 : jsDia;

  const hoje = new Date();
  hoje.setHours(0, 0, 0, 0);
  const nomesDiasCurtos = ['DOM','SEG','TER','QUA','QUI','SEX','SÁB'];

  // Título da semana
  const fimSemana = new Date(agendaSemanaInicio);
  fimSemana.setDate(fimSemana.getDate() + 6);
  const nomesMeses = ['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'];
  let titulo;
  if (agendaSemanaInicio.getMonth() === fimSemana.getMonth()) {
    titulo = `${agendaSemanaInicio.getDate()} – ${fimSemana.getDate()} de ${nomesMeses[agendaSemanaInicio.getMonth()]} ${fimSemana.getFullYear()}`;
  } else {
    titulo = `${agendaSemanaInicio.getDate()} ${nomesMeses[agendaSemanaInicio.getMonth()]} – ${fimSemana.getDate()} ${nomesMeses[fimSemana.getMonth()]} ${fimSemana.getFullYear()}`;
  }
  document.getElementById('agenda-titulo').textContent = titulo;

  // Constrói HTML do grid
  let html = '<div class="agenda-grid">';
  // Cabeçalho: canto + 7 dias
  html += '<div class="agenda-cab-canto"></div>';
  for (let i = 0; i < 7; i++) {
    const d = new Date(agendaSemanaInicio);
    d.setDate(d.getDate() + i);
    const ehHoje = d.getTime() === hoje.getTime();
    html += `<div class="agenda-cab-dia ${ehHoje ? 'hoje' : ''}">
      ${nomesDiasCurtos[d.getDay()]}
      <div class="numero">${d.getDate()}</div>
    </div>`;
  }

  // Linhas: hora + 7 células
  for (let h = horaGridInicio; h < horaGridFim; h++) {
    html += `<div class="agenda-hora-label">${String(h).padStart(2,'0')}:00</div>`;
    for (let i = 0; i < 7; i++) {
      const d = new Date(agendaSemanaInicio);
      d.setDate(d.getDate() + i);
      const diaDb = jsParaDb(d.getDay());
      const eForaDia = !diasSemanaCfg.has(diaDb);
      const eForaHora = (h < horaAbre) || (h >= horaFecha);
      const foraExpediente = eForaDia || eForaHora;
      html += `<div class="agenda-celula ${foraExpediente ? 'fora-expediente' : ''}"
                    data-dia="${iso(d)}" data-hora="${h}"
                    onclick="clicarCelulaAgenda(event, '${iso(d)}', ${h})"></div>`;
    }
  }
  html += '</div>';

  document.getElementById('agenda-container').innerHTML = html;

  // Coloca os blocos de agendamento
  colocarBlocosAgenda(horaGridInicio);

  // Atualiza resumo
  atualizarResumoAgenda();

  // Scroll pro horário de abertura
  const container = document.getElementById('agenda-container');
  const alturaLinha = 40;
  container.scrollTop = (horaAbre - horaGridInicio) * alturaLinha - 20;
}

function colocarBlocosAgenda(horaGridInicio) {
  const ags = agendaDados.agendamentos || [];
  const blqs = agendaDados.bloqueios || [];

  const alturaLinha = 40; // altura de 1h em px

  const posicionar = (dt_inicio, duracao_min, colunaDia) => {
    const inicio = new Date(dt_inicio);
    const horaFrac = inicio.getHours() + inicio.getMinutes() / 60;
    const top = (horaFrac - horaGridInicio) * alturaLinha;
    const altura = Math.max(20, (duracao_min / 60) * alturaLinha - 2);
    return { top, altura, colunaDia };
  };

  const colDoDia = (dtStr) => {
    // dtStr é ISO com offset. Extraímos ano/mes/dia usando construção de Date
    const d = new Date(dtStr);
    const semanaInicioLocal = new Date(agendaSemanaInicio);
    const dif = Math.floor((d - semanaInicioLocal) / 86400000);
    return dif;
  };

  const filtroProf = agendaFiltroProfId();  // null = todos
  const temProfs = agendaProfissionais.length > 0;

  ags.forEach(a => {
    // Filtro por profissional (agendamento sem profissional aparece em qualquer filtro).
    if (filtroProf && a.profissional_id && a.profissional_id !== filtroProf) return;
    const col = colDoDia(a.data_hora);
    if (col < 0 || col > 6) return;
    const pos = posicionar(a.data_hora, a.duracao_minutos || 60, col);
    const dt = new Date(a.data_hora);
    const hh = String(dt.getHours()).padStart(2,'0');
    const mm = String(dt.getMinutes()).padStart(2,'0');
    const celula = document.querySelector(
      `.agenda-celula[data-dia="${iso(agendaVirarDia(col))}"][data-hora="${dt.getHours()}"]`
    );
    if (!celula) return;
    const bloco = document.createElement('div');
    bloco.className = 'agenda-bloco';
    // Cor por profissional (multi); fallback pro esquema antigo (verde/azul manual).
    if (temProfs) {
      bloco.style.background = corDoProfissional(a.profissional_id);
    } else if (a.origem === 'manual') {
      bloco.classList.add('bloco-manual');
    }
    const offsetMin = dt.getMinutes();
    const offsetTop = (offsetMin / 60) * alturaLinha;
    bloco.style.top = offsetTop + 'px';
    bloco.style.height = pos.altura + 'px';
    // Rótulo do profissional (curto) quando a clínica é multi.
    const rotuloProf = (temProfs && a.profissional_nome)
      ? `<div class="bloco-prof">${escapar(a.profissional_nome)}</div>` : '';
    bloco.innerHTML = `
      <div class="bloco-hora">${hh}:${mm}</div>
      <div class="bloco-nome">${escapar(a.nome_lead || 'Sem nome')}</div>
      ${rotuloProf}
    `;
    bloco.onclick = () => abrirDetalheAgendamento(a);
    celula.appendChild(bloco);
  });

  blqs.forEach(b => {
    // Bloqueio de um profissional específico some quando o filtro é outro.
    // Bloqueio geral (profissional_id null) aparece sempre.
    if (filtroProf && b.profissional_id && b.profissional_id !== filtroProf) return;
    const dtIni = new Date(b.inicio);
    const dtFim = new Date(b.fim);
    const col = colDoDia(b.inicio);
    if (col < 0 || col > 6) return;
    const duracaoMin = (dtFim - dtIni) / 60000;
    const pos = posicionar(b.inicio, duracaoMin, col);
    const hh = String(dtIni.getHours()).padStart(2,'0');
    const mm = String(dtIni.getMinutes()).padStart(2,'0');
    const celula = document.querySelector(
      `.agenda-celula[data-dia="${iso(agendaVirarDia(col))}"][data-hora="${dtIni.getHours()}"]`
    );
    if (!celula) return;
    const bloco = document.createElement('div');
    bloco.className = 'agenda-bloco bloco-bloqueio';
    const offsetMin = dtIni.getMinutes();
    const offsetTop = (offsetMin / 60) * alturaLinha;
    bloco.style.top = offsetTop + 'px';
    bloco.style.height = pos.altura + 'px';
    const rotuloProf = (temProfs && b.profissional_nome)
      ? `<div class="bloco-prof">${escapar(b.profissional_nome)}</div>` : '';
    bloco.innerHTML = `
      <div class="bloco-hora">${hh}:${mm}</div>
      <div class="bloco-nome">${escapar(b.motivo || 'Bloqueado')}</div>
      ${rotuloProf}
    `;
    bloco.onclick = () => abrirDetalheBloqueio(b);
    celula.appendChild(bloco);
  });
}

function agendaVirarDia(coluna) {
  const d = new Date(agendaSemanaInicio);
  d.setDate(d.getDate() + coluna);
  return d;
}

function atualizarResumoAgenda() {
  const ags = agendaDados.agendamentos || [];
  const blqs = agendaDados.bloqueios || [];
  const html = `
    <div style="margin-bottom:14px">
      <strong>${ags.length}</strong>
      ${ags.length === 1 ? 'agendamento' : 'agendamentos'} na semana
    </div>
    <div>
      <strong>${blqs.length}</strong>
      ${blqs.length === 1 ? 'bloqueio' : 'bloqueios'} na semana
    </div>
    <div style="margin-top:14px; padding-top:12px; border-top:1px solid var(--cinza-divisor); font-size:11px;">
      Clique num horário livre pra criar um agendamento ou bloqueio.
      Clique num bloco pra ver, editar ou cancelar.
    </div>
  `;
  document.getElementById('agenda-resumo').innerHTML = html;
}

function fmtDataHora(dtStr) {
  const d = new Date(dtStr);
  const dd = String(d.getDate()).padStart(2,'0');
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const yy = d.getFullYear();
  const hh = String(d.getHours()).padStart(2,'0');
  const min = String(d.getMinutes()).padStart(2,'0');
  return `${dd}/${mm}/${yy} às ${hh}:${min}`;
}

let agAtual = null;  // agendamento aberto no modal (pra edição)

function abrirDetalheAgendamento(ag) {
  agAtual = ag;
  document.getElementById('ag-modal-titulo').textContent = 'Agendamento';
  const origemLabel = ag.origem === 'manual' ? 'Manual' : 'Ana';
  const linhaProf = (agendaProfissionais.length > 0)
    ? `<div class="linha"><div class="rotulo">Profissional</div><div class="valor">${escapar(ag.profissional_nome || 'Não atribuído')}</div></div>`
    : '';
  document.getElementById('ag-modal-body').innerHTML = `
    <div class="linha"><div class="rotulo">Nome</div><div class="valor">${escapar(ag.nome_lead || '—')}</div></div>
    <div class="linha"><div class="rotulo">Telefone</div><div class="valor">${escapar(ag.numero_lead || '—')}</div></div>
    ${linhaProf}
    <div class="linha"><div class="rotulo">Data</div><div class="valor">${fmtDataHora(ag.data_hora)}</div></div>
    <div class="linha"><div class="rotulo">Duração</div><div class="valor">${ag.duracao_minutos || 60} min</div></div>
    <div class="linha"><div class="rotulo">Origem</div><div class="valor">${origemLabel}</div></div>
    ${ag.observacao ? `<div class="linha"><div class="rotulo">Obs.</div><div class="valor">${escapar(ag.observacao)}</div></div>` : ''}
  `;
  document.getElementById('ag-modal-rodape').innerHTML = `
    <button type="button" class="btn btn-pequeno" onclick="fecharModalAgenda()">Fechar</button>
    <button type="button" class="btn btn-primario" onclick="editarAgendamentoModal()">Editar</button>
    <button type="button" class="btn-perigo" onclick="cancelarAgendamentoAgenda(${ag.id})">Cancelar agendamento</button>
  `;
  document.getElementById('agenda-modal').style.display = 'flex';
}

function escaparAttr(s) {
  return (s || '').toString()
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function dtLocalValor(dtStr) {
  // Converte ISO em valor aceito pelo input datetime-local (AAAA-MM-DDTHH:MM)
  const d = new Date(dtStr);
  const pad = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function editarAgendamentoModal() {
  const ag = agAtual;
  if (!ag) return;
  const temProfs = agendaProfissionais.length > 0;
  // Seletor de profissional (só multi). Pré-seleciona o profissional atual.
  const selProf = temProfs ? `
      <label>Profissional</label>
      <select id="ed-ag-prof">
        <option value="">— Selecione —</option>
        ${agendaProfissionais.map(p =>
          `<option value="${p.id}" ${p.id === ag.profissional_id ? 'selected' : ''}>${escapar(p.nome)}</option>`
        ).join('')}
      </select>` : '';

  document.getElementById('ag-modal-titulo').textContent = 'Editar agendamento';
  document.getElementById('ag-modal-body').innerHTML = `
    <div class="ag-form">
      <label>Nome do paciente</label>
      <input type="text" id="ed-ag-nome" value="${escaparAttr(ag.nome_lead || '')}">
      ${selProf}
      <label>Data e horário</label>
      <input type="datetime-local" id="ed-ag-datahora" value="${dtLocalValor(ag.data_hora)}">
      <label>Motivo</label>
      <input type="text" id="ed-ag-motivo" value="${escaparAttr(ag.observacao || '')}"
             placeholder="Ex: avaliação, clareamento...">
    </div>
  `;
  document.getElementById('ag-modal-rodape').innerHTML = `
    <button type="button" class="btn btn-pequeno" onclick="abrirDetalheAgendamento(agAtual)">Voltar</button>
    <button type="button" class="btn btn-verde" onclick="salvarEdicaoAgendamento()">Salvar</button>
  `;
}

async function salvarEdicaoAgendamento() {
  if (!agAtual) return;
  const nome = document.getElementById('ed-ag-nome').value.trim();
  const dataHora = document.getElementById('ed-ag-datahora').value;
  if (nome.length < 3) { alert('Informe o nome do paciente.'); return; }
  if (!dataHora) { alert('Informe a data e o horário.'); return; }

  const body = {
    nome_lead: nome,
    observacao: document.getElementById('ed-ag-motivo').value.trim(),
    data_hora: dataHora.substring(0, 16),
  };
  const profEl = document.getElementById('ed-ag-prof');
  if (profEl) {
    if (!profEl.value) { alert('Selecione o profissional.'); return; }
    body.profissional_id = parseInt(profEl.value, 10);
  }

  const r = await fetch('/painel/api/agendamentos/' + agAtual.id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (r.ok) {
    fecharModalAgenda();
    carregarAgenda();
  } else {
    const data = await r.json().catch(() => ({}));
    alert('Erro: ' + (data.erro || 'falha ao salvar'));
  }
}

// ============================================================
// CRIAR AGENDAMENTO / BLOQUEIO (clique em célula vazia)
// ============================================================
function clicarCelulaAgenda(event, dia, hora) {
  // Clique num bloco (agendamento/bloqueio) abre o detalhe dele, não o criar.
  if (event.target.closest('.agenda-bloco')) return;
  if (!agendaClinicaId) return;
  abrirModalCriar(dia, hora);
}

function abrirModalCriar(dia, hora) {
  const hh = String(hora).padStart(2,'0');
  const hhFim = String(Math.min(hora + 1, 23)).padStart(2,'0');
  const temProfs = agendaProfissionais.length > 0;
  const filtroAtual = agendaFiltroProfId();

  // Selects de profissional (só quando a clínica é multi).
  const opcoesProf = agendaProfissionais.map(p =>
    `<option value="${p.id}" ${p.id === filtroAtual ? 'selected' : ''}>${escapar(p.nome)}</option>`
  ).join('');
  const selAgProf = temProfs ? `
        <label>Profissional</label>
        <select id="novo-prof">
          <option value="">— Selecione —</option>
          ${opcoesProf}
        </select>` : '';
  const selBloqProf = temProfs ? `
        <label>Profissional <span style="text-transform:none; font-weight:400">— opcional</span></label>
        <select id="novo-bloq-prof">
          <option value="">Toda a clínica</option>
          ${opcoesProf}
        </select>` : '';

  document.getElementById('ag-modal-titulo').textContent = 'Novo';
  document.getElementById('ag-modal-body').innerHTML = `
    <div class="ag-form">
      <label>Tipo</label>
      <select id="novo-tipo" onchange="alternarTipoNovo()">
        <option value="agendamento">Agendamento</option>
        <option value="bloqueio">Bloqueio</option>
      </select>
      <div id="novo-campos-agendamento">
        <label>Nome do paciente</label>
        <input type="text" id="novo-nome" placeholder="Nome completo">
        <label>Telefone <span style="text-transform:none; font-weight:400">— opcional</span></label>
        <input type="text" id="novo-telefone" placeholder="(19) 99999-9999">
        ${selAgProf}
        <label>Data e horário</label>
        <input type="datetime-local" id="novo-datahora" value="${dia}T${hh}:00">
        <label>Motivo <span style="text-transform:none; font-weight:400">— opcional</span></label>
        <input type="text" id="novo-motivo" placeholder="Ex: avaliação, clareamento...">
      </div>
      <div id="novo-campos-bloqueio" style="display:none">
        ${selBloqProf}
        <label>Início</label>
        <input type="datetime-local" id="novo-bloq-inicio" value="${dia}T${hh}:00">
        <label>Fim</label>
        <input type="datetime-local" id="novo-bloq-fim" value="${dia}T${hhFim}:00">
        <label>Motivo <span style="text-transform:none; font-weight:400">— opcional</span></label>
        <input type="text" id="novo-bloq-motivo" placeholder="Ex: almoço, compromisso, feriado...">
      </div>
    </div>
  `;
  document.getElementById('ag-modal-rodape').innerHTML = `
    <button type="button" class="btn btn-pequeno" onclick="fecharModalAgenda()">Cancelar</button>
    <button type="button" class="btn btn-verde" onclick="salvarNovoAgenda()">Salvar</button>
  `;
  document.getElementById('agenda-modal').style.display = 'flex';
}

function alternarTipoNovo() {
  const tipo = document.getElementById('novo-tipo').value;
  document.getElementById('novo-campos-agendamento').style.display =
    (tipo === 'agendamento') ? '' : 'none';
  document.getElementById('novo-campos-bloqueio').style.display =
    (tipo === 'bloqueio') ? '' : 'none';
}

async function salvarNovoAgenda() {
  const tipo = document.getElementById('novo-tipo').value;
  // Admin manda a clínica selecionada; cliente comum usa a da sessão no backend.
  const clinicaExtra = (agendaClinicaId && agendaClinicaId !== 'meu')
    ? { clinica_id: agendaClinicaId } : {};

  const temProfs = agendaProfissionais.length > 0;

  let url, body;
  if (tipo === 'agendamento') {
    const nome = document.getElementById('novo-nome').value.trim();
    const dataHora = document.getElementById('novo-datahora').value;
    if (nome.length < 3) { alert('Informe o nome do paciente.'); return; }
    if (!dataHora) { alert('Informe a data e o horário.'); return; }
    const profEl = document.getElementById('novo-prof');
    if (temProfs && (!profEl || !profEl.value)) {
      alert('Selecione o profissional.'); return;
    }
    url = '/painel/api/agendamentos';
    body = {
      ...clinicaExtra,
      nome,
      telefone: document.getElementById('novo-telefone').value.trim(),
      data_hora: dataHora.substring(0, 16),
      observacao: document.getElementById('novo-motivo').value.trim(),
    };
    if (profEl && profEl.value) body.profissional_id = parseInt(profEl.value, 10);
  } else {
    const inicio = document.getElementById('novo-bloq-inicio').value;
    const fim = document.getElementById('novo-bloq-fim').value;
    if (!inicio || !fim) { alert('Informe início e fim do bloqueio.'); return; }
    url = '/painel/api/bloqueios';
    body = {
      ...clinicaExtra,
      inicio: inicio.substring(0, 16),
      fim: fim.substring(0, 16),
      motivo: document.getElementById('novo-bloq-motivo').value.trim(),
    };
    const bprofEl = document.getElementById('novo-bloq-prof');
    if (bprofEl && bprofEl.value) body.profissional_id = parseInt(bprofEl.value, 10);
  }

  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (r.ok) {
    fecharModalAgenda();
    carregarAgenda();
  } else {
    const data = await r.json().catch(() => ({}));
    alert('Erro: ' + (data.erro || 'falha ao salvar'));
  }
}

function abrirDetalheBloqueio(b) {
  document.getElementById('ag-modal-titulo').textContent = 'Bloqueio';
  const dtIni = new Date(b.inicio);
  const dtFim = new Date(b.fim);
  const linhaProfB = (agendaProfissionais.length > 0)
    ? `<div class="linha"><div class="rotulo">Profissional</div><div class="valor">${escapar(b.profissional_nome || 'Toda a clínica')}</div></div>`
    : '';
  document.getElementById('ag-modal-body').innerHTML = `
    <div class="linha"><div class="rotulo">Motivo</div><div class="valor">${escapar(b.motivo || '—')}</div></div>
    ${linhaProfB}
    <div class="linha"><div class="rotulo">Início</div><div class="valor">${fmtDataHora(b.inicio)}</div></div>
    <div class="linha"><div class="rotulo">Fim</div><div class="valor">${fmtDataHora(b.fim)}</div></div>
    <div class="linha"><div class="rotulo">Duração</div><div class="valor">${Math.round((dtFim-dtIni)/60000)} min</div></div>
  `;
  document.getElementById('ag-modal-rodape').innerHTML = `
    <button type="button" class="btn btn-pequeno" onclick="fecharModalAgenda()">Fechar</button>
    <button type="button" class="btn-perigo" onclick="removerBloqueioAgenda(${b.id})">Remover bloqueio</button>
  `;
  document.getElementById('agenda-modal').style.display = 'flex';
}

function fecharModalAgenda() {
  document.getElementById('agenda-modal').style.display = 'none';
}

async function cancelarAgendamentoAgenda(id) {
  if (!confirm('Confirma o cancelamento desse agendamento?')) return;
  const r = await fetch('/painel/api/agendamentos/' + id + '/cancelar', { method: 'POST' });
  if (r.ok) {
    fecharModalAgenda();
    carregarAgenda();
  } else {
    alert('Erro ao cancelar');
  }
}

async function removerBloqueioAgenda(id) {
  if (!confirm('Confirma a remoção desse bloqueio?')) return;
  const r = await fetch('/painel/api/bloqueios/' + id, { method: 'DELETE' });
  if (r.ok) {
    fecharModalAgenda();
    carregarAgenda();
  } else {
    alert('Erro ao remover bloqueio');
  }
}

carregarClientesNoFiltro();
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
# TELA DE ADMINISTRAÇÃO (só pro admin)
# ============================================================
ADMIN_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — Converte.ai</title>
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
    --cinza-fraco: #9CA3AF;
    --cinza-borda: #E5E7EB;
    --cinza-divisor: #F3F4F6;
    --cinza-bg: #F9FAFB;
    --laranja: #D97706;
    --laranja-bg: #FEF3C7;
    --vermelho: #DC2626;
    --vermelho-bg: #FEE2E2;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: 'Inter', -apple-system, "Segoe UI", system-ui, sans-serif;
    background: var(--cinza-bg);
    color: var(--carvao);
  }

  /* Header igual ao painel */
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 24px;
    background: #fff;
    border-bottom: 1px solid var(--cinza-borda);
    height: 64px;
  }
  .header-logo img { height: 28px; display: block; }
  .header-user {
    display: flex; align-items: center; gap: 14px;
  }
  .user-info { text-align: right; line-height: 1.2; }
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
  .link-volta {
    color: var(--cinza-texto);
    text-decoration: none;
    font-size: 13px;
    padding: 8px 12px;
    border-radius: 8px;
    transition: background .15s, color .15s;
  }
  .link-volta:hover { background: var(--cinza-divisor); color: var(--carvao); }

  /* Conteúdo */
  .container {
    max-width: 960px;
    margin: 32px auto;
    padding: 0 24px;
  }
  .titulo-pagina {
    font-size: 22px; font-weight: 700; margin-bottom: 4px;
  }
  .sub-pagina {
    font-size: 14px; color: var(--cinza-texto); margin-bottom: 24px;
  }

  /* Abas */
  .abas {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--cinza-borda);
    margin-bottom: 24px;
  }
  .aba {
    padding: 10px 16px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    font-family: inherit;
    font-size: 14px;
    font-weight: 500;
    color: var(--cinza-texto);
    cursor: pointer;
    transition: color .15s, border-color .15s;
    margin-bottom: -1px;
  }
  .aba:hover { color: var(--carvao); }
  .aba.ativa {
    color: var(--verde-escuro);
    border-bottom-color: var(--verde);
    font-weight: 600;
  }

  /* Card */
  .card {
    background: #fff;
    border: 1px solid var(--cinza-borda);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 20px;
  }
  .card-titulo {
    font-size: 15px; font-weight: 600; margin-bottom: 16px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .card-sub {
    font-size: 13px; color: var(--cinza-texto); margin-bottom: 16px;
  }

  /* Tabela */
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th, td {
    text-align: left;
    padding: 10px 8px;
    font-size: 13px;
    border-bottom: 1px solid var(--cinza-divisor);
  }
  th {
    font-weight: 600;
    color: var(--cinza-texto);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  td.numero {
    color: var(--cinza-texto); font-variant-numeric: tabular-nums;
  }

  /* Form */
  label {
    display: block; font-size: 13px; font-weight: 500;
    color: var(--carvao); margin-bottom: 6px;
  }
  .label-dica {
    color: var(--cinza-texto); font-weight: 400; font-size: 12px;
  }
  input, textarea, select {
    width: 100%;
    padding: 10px 12px;
    border: 1.5px solid var(--cinza-borda);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    color: var(--carvao);
    margin-bottom: 16px;
    transition: border-color .15s, box-shadow .15s;
    background: #fff;
  }
  textarea {
    resize: vertical;
    min-height: 120px;
    font-family: 'SF Mono', Monaco, Consolas, monospace;
    font-size: 13px;
    line-height: 1.55;
  }
  input:focus, textarea:focus, select:focus {
    outline: none;
    border-color: var(--verde);
    box-shadow: 0 0 0 3px var(--verde-claro);
  }
  .row { display: flex; gap: 14px; }
  .row > div { flex: 1; }

  .btn {
    padding: 10px 18px;
    border-radius: 8px;
    border: none;
    font-size: 13px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: background .15s;
  }
  .btn-primario { background: var(--carvao); color: #fff; }
  .btn-primario:hover { background: #1f2030; }
  .btn-verde { background: var(--verde); color: #fff; }
  .btn-verde:hover { background: var(--verde-escuro); }
  .btn-pequeno {
    padding: 6px 12px; font-size: 12px;
    background: var(--cinza-divisor); color: var(--carvao);
  }
  .btn-pequeno:hover { background: var(--cinza-borda); }

  /* Mensagens */
  .alerta {
    padding: 12px 14px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 16px;
  }
  .alerta-sucesso {
    background: var(--verde-claro); color: var(--verde-escuro);
    border: 1px solid #A7F3D0;
  }
  .alerta-erro {
    background: var(--vermelho-bg); color: var(--vermelho);
    border: 1px solid #FCA5A5;
  }
  .alerta-info {
    background: var(--laranja-bg); color: var(--laranja);
    border: 1px solid #FDE68A;
  }
  .alerta strong { font-weight: 600; }
  .alerta code {
    background: rgba(0,0,0,0.06);
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'SF Mono', Monaco, monospace;
    font-size: 12px;
  }

  /* Seções */
  .secao { display: none; }
  .secao.ativa { display: block; }

  @media (max-width: 768px) {
    .container { padding: 0 16px; margin: 20px auto; }
    .abas { overflow-x: auto; }
    .aba { white-space: nowrap; }
    .row { flex-direction: column; gap: 0; }
    .user-info { display: none; }
    .header { padding: 10px 16px; height: 56px; }
    .header-logo img { height: 24px; }
  }
</style>
</head>
<body>

<header class="header">
  <div class="header-logo">
    <img src="/static/logo-converte.png" alt="Converte.ai">
  </div>
  <div class="header-user">
    <div class="user-info">
      <div class="nome">{{ nome }}</div>
      <div class="role">Admin</div>
    </div>
    <div class="user-avatar">{{ inicial }}</div>
    <a href="/painel" class="link-volta">← Painel</a>
  </div>
</header>

<div class="container">
  <div class="titulo-pagina">Administração</div>
  <div class="sub-pagina">Gerencie clientes, usuários e prompts da Ana.</div>

  <div class="abas">
    <button class="aba ativa" data-aba="clinicas" onclick="trocarAba('clinicas')">Clientes</button>
    <button class="aba" data-aba="usuarios" onclick="trocarAba('usuarios')">Usuários</button>
    <button class="aba" data-aba="prompts" onclick="trocarAba('prompts')">Prompts da Ana</button>
    <button class="aba" data-aba="profissionais" onclick="trocarAba('profissionais')">Profissionais</button>
    <button class="aba" data-aba="horarios" onclick="trocarAba('horarios')">Horários</button>
  </div>

  <div id="mensagem"></div>

  <!-- ============ ABA CLÍNICAS ============ -->
  <div class="secao ativa" id="sec-clinicas">

    <div class="card">
      <div class="card-titulo">Clientes cadastrados</div>
      <div id="lista-clinicas">Carregando...</div>
    </div>

    <div class="card">
      <div class="card-titulo">Rastreamento Meta — reenviar eventos</div>
      <div class="card-sub">
        Reenvia o evento de conversão (LeadSubmitted) dos agendamentos dos últimos
        7 dias que ainda não foram contabilizados na Meta. Usa a data real do
        agendamento, e pula os que já foram enviados com sucesso — pode clicar sem medo
        de duplicar. Só funciona pra leads que vieram de anúncio (com atribuição).
      </div>
      <button class="btn btn-primario" type="button" onclick="reenviarEventosCapi()">
        Reenviar eventos dos últimos 7 dias
      </button>
      <button class="btn btn-pequeno" type="button" onclick="diagnosticoCapi()" style="margin-left:8px;">
        Diagnóstico
      </button>
      <button class="btn btn-pequeno" type="button" onclick="reenviarEventosCapi(true)" style="margin-left:8px;">
        Forçar reenvio (teste)
      </button>
      <div id="capi-resultado" style="margin-top:14px;"></div>
      <div id="capi-diag" style="margin-top:14px;"></div>
    </div>

    <div class="card">
      <div class="card-titulo">Novo cliente</div>
      <div class="card-sub">
        Antes de cadastrar, certifique-se que o número do cliente já está ativo no
        WhatsApp Business API e que o webhook aponta pro nosso servidor.
      </div>
      <form onsubmit="criarClinicaSubmit(event)">
        <label>Nome do cliente</label>
        <input type="text" id="cl-nome" required placeholder="Ex: Estética Helena, Escritório Silva, Pet Shop XYZ...">

        <label>Phone Number ID <span class="label-dica">— do WhatsApp Business API</span></label>
        <input type="text" id="cl-phone-id" required placeholder="Ex: 654321987654321">

        <label>Token do WhatsApp <span class="label-dica">— Access Token do Business Manager do cliente</span></label>
        <input type="text" id="cl-token" required placeholder="EAAxxx..." autocomplete="off">

        <label>Telefone humano <span class="label-dica">— pra fallback quando a Ana redirecionar</span></label>
        <input type="text" id="cl-fone-humano" placeholder="Ex: 19 99999-9999">

        <label>Prompt da Ana <span class="label-dica">— pode colar o prompt-base e editar</span></label>
        <textarea id="cl-prompt" required placeholder="Você é Ana, secretária da..."></textarea>

        <button class="btn btn-verde" type="submit">Cadastrar cliente</button>
      </form>
    </div>
  </div>

  <!-- ============ ABA USUÁRIOS ============ -->
  <div class="secao" id="sec-usuarios">
    <div class="card">
      <div class="card-titulo">Criar usuário para um cliente</div>
      <div class="card-sub">
        O usuário criado pode logar no /painel e vai ver apenas as conversas do cliente vinculado.
      </div>
      <form onsubmit="criarUsuarioSubmit(event)">
        <label>Cliente</label>
        <select id="us-clinica" required>
          <option value="">— Selecione —</option>
        </select>

        <div class="row">
          <div>
            <label>Email</label>
            <input type="email" id="us-email" required placeholder="cliente@exemplo.com">
          </div>
          <div>
            <label>Nome <span class="label-dica">— opcional</span></label>
            <input type="text" id="us-nome" placeholder="Nome do responsável">
          </div>
        </div>

        <label>Senha <span class="label-dica">— anote, depois não dá pra ver</span></label>
        <input type="text" id="us-senha" required placeholder="Senha forte">

        <button class="btn btn-verde" type="submit">Criar usuário</button>
      </form>
    </div>
  </div>

  <!-- ============ ABA PROMPTS ============ -->
  <div class="secao" id="sec-prompts">
    <div class="card">
      <div class="card-titulo">Editar prompt da Ana</div>
      <div class="card-sub">
        Mudanças no prompt são aplicadas imediatamente nas próximas mensagens.
        Tome cuidado para não quebrar o tom já calibrado.
      </div>
      <label>Selecione o cliente</label>
      <select id="pr-clinica" onchange="carregarPrompt()">
        <option value="">— Selecione —</option>
      </select>

      <div id="pr-editor" style="display:none">
        <label>Prompt atual da Ana</label>
        <textarea id="pr-texto" style="min-height: 320px"></textarea>
        <button class="btn btn-verde" type="button" onclick="salvarPrompt()">Salvar mudanças</button>
      </div>
    </div>
  </div>

  <!-- ============ ABA PROFISSIONAIS ============ -->
  <div class="secao" id="sec-profissionais">
    <div class="card">
      <div class="card-titulo">Profissionais da clínica</div>
      <div class="card-sub">
        Cadastre os profissionais quando a clínica tiver mais de um (ex: dois
        dentistas com especialidades diferentes). Cada profissional ganha agenda
        e horários próprios, e a Ana marca no profissional certo conforme o prompt.
        <strong>Clínica sem profissionais cadastrados funciona no modo agenda única
        (como antes)</strong> — só cadastre se realmente houver mais de um.
      </div>

      <label>Selecione o cliente</label>
      <select id="pf-clinica" onchange="carregarProfissionais()">
        <option value="">— Selecione —</option>
      </select>

      <div id="pf-editor" style="display:none; margin-top:16px;">
        <div id="pf-lista"></div>

        <div style="margin-top:20px; padding-top:16px; border-top:1px solid var(--cinza-divisor);">
          <label>Adicionar profissional</label>
          <div class="row" style="align-items:flex-end;">
            <div>
              <input type="text" id="pf-novo-nome" placeholder="Ex: Dr. Matheus, Dra. Maryah..."
                     style="margin-bottom:0;">
            </div>
            <div style="flex:0 0 auto;">
              <button class="btn btn-verde" type="button" onclick="adicionarProfissional()">Adicionar</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ ABA HORÁRIOS ============ -->
  <div class="secao" id="sec-horarios">
    <div class="card">
      <div class="card-titulo">Configurar horários de atendimento</div>
      <div class="card-sub">
        Defina os dias, horários e duração da consulta. A Ana usa isso pra agendar
        sem ultrapassar os limites da clínica. Mudanças entram em vigor imediatamente.
      </div>

      <label>Selecione o cliente</label>
      <select id="hr-clinica" onchange="aoTrocarClinicaHorarios()">
        <option value="">— Selecione —</option>
      </select>

      <div id="hr-prof-wrap" style="display:none;">
        <label>Profissional
          <span class="label-dica">— cada um pode ter horário próprio</span>
        </label>
        <select id="hr-profissional" onchange="carregarHorarios()">
          <option value="">Padrão da clínica</option>
        </select>
      </div>

      <div id="hr-editor" style="display:none; margin-top: 16px;">
        <label>Dias da semana atendidos</label>
        <div style="display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px;">
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="1" style="width:auto; margin:0;"> Segunda
          </label>
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="2" style="width:auto; margin:0;"> Terça
          </label>
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="3" style="width:auto; margin:0;"> Quarta
          </label>
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="4" style="width:auto; margin:0;"> Quinta
          </label>
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="5" style="width:auto; margin:0;"> Sexta
          </label>
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="6" style="width:auto; margin:0;"> Sábado
          </label>
          <label style="display:inline-flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
            <input type="checkbox" class="hr-dia" value="7" style="width:auto; margin:0;"> Domingo
          </label>
        </div>

        <div class="row">
          <div>
            <label>Horário de abertura</label>
            <input type="time" id="hr-inicio" required>
          </div>
          <div>
            <label>Horário de fechamento</label>
            <input type="time" id="hr-fim" required>
          </div>
        </div>

        <div class="row">
          <div>
            <label>Início do almoço <span class="label-dica">— opcional</span></label>
            <input type="time" id="hr-almoco-inicio">
          </div>
          <div>
            <label>Fim do almoço <span class="label-dica">— opcional</span></label>
            <input type="time" id="hr-almoco-fim">
          </div>
        </div>

        <div class="row">
          <div>
            <label>Duração de cada consulta (minutos)</label>
            <input type="number" id="hr-duracao" min="15" max="480" step="15" required>
          </div>
          <div>
            <label>Antecedência mínima (minutos) <span class="label-dica">— ex: 180 = 3h</span></label>
            <input type="number" id="hr-antecedencia" min="0" max="10080" required>
          </div>
        </div>

        <button class="btn btn-verde" type="button" onclick="salvarHorarios()">Salvar configurações</button>
      </div>
    </div>
  </div>
</div>

<!-- ============ MODAL DE EDIÇÃO DE CLIENTE ============ -->
<div id="modal-edicao" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0;
     background:rgba(45,46,60,0.5); z-index:100; align-items:flex-start; justify-content:center;
     overflow-y:auto; padding:30px 16px;">
  <div style="background:#fff; border-radius:12px; max-width:640px; width:100%;
              padding:28px; box-shadow:0 8px 32px rgba(0,0,0,0.15);">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <div style="font-size:18px; font-weight:600;">Editar cliente</div>
      <button type="button" onclick="fecharEdicaoCliente()"
              style="background:none; border:none; font-size:24px; cursor:pointer; color:var(--cinza-texto);">×</button>
    </div>
    <div class="card-sub">Atualize qualquer campo. Mudanças entram em vigor imediatamente.</div>
    <form onsubmit="salvarEdicaoCliente(event)">
      <input type="hidden" id="ed-id">
      <label>Nome do cliente</label>
      <input type="text" id="ed-nome" required>

      <label>Phone Number ID</label>
      <input type="text" id="ed-phone-id" required>

      <label>Token do WhatsApp <span class="label-dica">— deixe em branco pra usar o global do Render</span></label>
      <input type="text" id="ed-token" autocomplete="off">

      <label>Telefone humano <span class="label-dica">— número do dono, recebe notificações e ativa "modo dono"</span></label>
      <input type="text" id="ed-fone-humano">

      <label>Prompt da Ana</label>
      <textarea id="ed-prompt" style="min-height:200px"></textarea>

      <div style="margin-top:8px; padding-top:16px; border-top:1px solid var(--cinza-borda);">
        <div style="font-size:14px; font-weight:600; margin-bottom:4px;">Rastreamento Meta (Conversions API)</div>
        <div class="card-sub" style="margin-bottom:14px;">
          Envia conversões (lead, agendamento) pra Meta atribuir aos anúncios click-to-WhatsApp.
          Deixe desativado e/ou em branco pra não rastrear este cliente.
        </div>

        <label>Dataset ID <span class="label-dica">— do Gerenciador de Eventos da conta que roda os anúncios</span></label>
        <input type="text" id="ed-meta-dataset" placeholder="Ex: 1234567890123456" autocomplete="off">

        <label>Page ID <span class="label-dica">— ID da Página do Facebook que veicula os anúncios (obrigatório: a Meta recusa o evento sem ele)</span></label>
        <input type="text" id="ed-meta-pageid" placeholder="Ex: 284718968060124" autocomplete="off">

        <label>Token da Conversions API <span class="label-dica">— token de acesso do dataset (secreto)</span></label>
        <input type="password" id="ed-meta-token" placeholder="Deixe em branco pra manter o atual" autocomplete="off">

        <label>Test Event Code <span class="label-dica">— opcional, só pra validar no Gerenciador de Eventos antes de produção</span></label>
        <input type="text" id="ed-meta-testcode" placeholder="Ex: TEST12345" autocomplete="off">

        <label style="display:inline-flex; align-items:center; gap:8px; font-weight:400; cursor:pointer; margin-top:4px;">
          <input type="checkbox" id="ed-capi-ativo" style="width:auto; margin:0;">
          Rastreamento ativo para este cliente
        </label>
      </div>

      <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:16px;">
        <button type="button" class="btn btn-pequeno" onclick="fecharEdicaoCliente()">Cancelar</button>
        <button type="submit" class="btn btn-verde">Salvar mudanças</button>
      </div>
    </form>
  </div>
</div>

<script>
// ============ ABAS ============
function trocarAba(nome) {
  document.querySelectorAll('.aba').forEach(a => {
    a.classList.toggle('ativa', a.dataset.aba === nome);
  });
  document.querySelectorAll('.secao').forEach(s => {
    s.classList.toggle('ativa', s.id === 'sec-' + nome);
  });
  limparMsg();
  if (nome === 'usuarios') carregarClinicasSelect('us-clinica');
  if (nome === 'prompts') carregarClinicasSelect('pr-clinica');
  if (nome === 'profissionais') carregarClinicasSelect('pf-clinica');
  if (nome === 'horarios') carregarClinicasSelect('hr-clinica');
}

// ============ PROFISSIONAIS ============
async function carregarProfissionais() {
  const clinicaId = document.getElementById('pf-clinica').value;
  const editor = document.getElementById('pf-editor');
  if (!clinicaId) { editor.style.display = 'none'; return; }

  const r = await fetch('/painel/admin/clinicas/' + clinicaId + '/profissionais');
  if (!r.ok) { mostrarMsg('Erro ao carregar profissionais.', 'erro'); return; }
  const profs = await r.json();

  const lista = document.getElementById('pf-lista');
  if (profs.length === 0) {
    lista.innerHTML = '<p style="color:#9CA3AF; font-size:13px;">' +
      'Nenhum profissional cadastrado. A clínica opera no modo agenda única.</p>';
  } else {
    lista.innerHTML = `
      <table>
        <thead><tr><th>Profissional</th><th>Status</th><th></th></tr></thead>
        <tbody>
          ${profs.map(p => `
            <tr>
              <td><strong>${escapar(p.nome)}</strong></td>
              <td>${p.ativo
                ? '<span style="color:var(--verde-escuro); font-weight:600;">Ativo</span>'
                : '<span style="color:var(--cinza-fraco);">Inativo</span>'}</td>
              <td style="text-align:right; white-space:nowrap;">
                <button class="btn btn-pequeno" onclick="renomearProfissional(${p.id}, '${escaparAttr(p.nome)}')">Renomear</button>
                <button class="btn btn-pequeno" onclick="alternarAtivoProfissional(${p.id}, ${p.ativo ? 'false' : 'true'})">
                  ${p.ativo ? 'Desativar' : 'Reativar'}</button>
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  }
  editor.style.display = 'block';
}

async function adicionarProfissional() {
  const clinicaId = document.getElementById('pf-clinica').value;
  const nome = document.getElementById('pf-novo-nome').value.trim();
  if (!clinicaId) return;
  if (nome.length < 2) { mostrarMsg('Informe o nome do profissional.', 'erro'); return; }
  const r = await fetch('/painel/admin/clinicas/' + clinicaId + '/profissionais', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ nome })
  });
  if (r.ok) {
    document.getElementById('pf-novo-nome').value = '';
    mostrarMsg('Profissional <strong>' + escapar(nome) + '</strong> cadastrado.', 'sucesso');
    carregarProfissionais();
  } else {
    const data = await r.json().catch(() => ({}));
    mostrarMsg('Erro: ' + (data.erro || 'falha'), 'erro');
  }
}

async function renomearProfissional(id, nomeAtual) {
  const novo = prompt('Novo nome do profissional:', nomeAtual);
  if (novo === null) return;
  if (novo.trim().length < 2) { mostrarMsg('Nome inválido.', 'erro'); return; }
  const r = await fetch('/painel/admin/profissionais/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ nome: novo.trim() })
  });
  if (r.ok) { carregarProfissionais(); }
  else {
    const data = await r.json().catch(() => ({}));
    mostrarMsg('Erro: ' + (data.erro || 'falha'), 'erro');
  }
}

async function alternarAtivoProfissional(id, novoAtivo) {
  const acao = novoAtivo ? 'reativar' : 'desativar';
  if (!confirm('Confirma ' + acao + ' esse profissional?')) return;
  const r = await fetch('/painel/admin/profissionais/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ativo: novoAtivo })
  });
  if (r.ok) { carregarProfissionais(); }
  else {
    const data = await r.json().catch(() => ({}));
    mostrarMsg('Erro: ' + (data.erro || 'falha'), 'erro');
  }
}

function escaparAttr(s) {
  return (s || '').toString()
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ============ MENSAGENS ============
function mostrarMsg(html, tipo) {
  const el = document.getElementById('mensagem');
  el.innerHTML = `<div class="alerta alerta-${tipo}">${html}</div>`;
  if (tipo === 'sucesso') setTimeout(limparMsg, 5000);
}
function limparMsg() { document.getElementById('mensagem').innerHTML = ''; }

// ============ CLÍNICAS — LISTA ============
async function carregarClinicas() {
  const r = await fetch('/painel/admin/clinicas');
  if (!r.ok) {
    document.getElementById('lista-clinicas').innerHTML = '<p style="color:#9CA3AF">Erro ao carregar.</p>';
    return;
  }
  const clinicas = await r.json();
  const div = document.getElementById('lista-clinicas');
  if (clinicas.length === 0) {
    div.innerHTML = '<p style="color:#9CA3AF">Nenhum cliente cadastrado ainda.</p>';
    return;
  }
  div.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Cliente</th>
          <th>Phone Number ID</th>
          <th class="numero">Conversas</th>
          <th class="numero">Usuários</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${clinicas.map(c => `
          <tr>
            <td><strong>${escapar(c.nome)}</strong></td>
            <td><code style="font-size:11px">${escapar(c.phone_number_id)}</code></td>
            <td class="numero">${c.total_conversas}</td>
            <td class="numero">${c.total_usuarios}</td>
            <td><button class="btn btn-pequeno" onclick="abrirEdicaoCliente(${c.id})">Editar</button></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

// ============ CAPI — REENVIO DE EVENTOS ============
async function reenviarEventosCapi(forcar) {
  const alvo = document.getElementById('capi-resultado');
  alvo.innerHTML = '<p style="color:#6B7280; font-size:13px;">Reenviando...</p>';
  const r = await fetch('/painel/admin/capi/reenviar', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ dias: 7, forcar: !!forcar })
  });
  if (!r.ok) {
    alvo.innerHTML = '<div class="alerta alerta-erro">Erro ao reenviar.</div>';
    return;
  }
  const d = await r.json();
  const linhas = (d.detalhes || []).map(x => '<li>' + escapar(x) + '</li>').join('');
  alvo.innerHTML = `
    <div class="alerta ${d.enviados > 0 ? 'alerta-sucesso' : 'alerta-info'}">
      <strong>${d.enviados}</strong> evento(s) enviado(s) agora ·
      ${d.ja_enviados} já contabilizado(s) antes ·
      ${d.sem_atribuicao} sem atribuição de anúncio ·
      ${d.falhas} falha(s)
      <div style="font-size:12px; margin-top:6px;">
        ${d.total_agendamentos} agendamento(s) analisado(s) nos últimos ${d.dias} dias.
      </div>
    </div>
    ${linhas ? '<ul style="font-size:12px; color:#6B7280; margin:8px 0 0 18px;">' + linhas + '</ul>' : ''}
  `;
}

async function diagnosticoCapi() {
  const alvo = document.getElementById('capi-diag');
  alvo.innerHTML = '<p style="color:#6B7280; font-size:13px;">Carregando diagnóstico...</p>';
  const r = await fetch('/painel/admin/capi/diagnostico');
  if (!r.ok) { alvo.innerHTML = '<div class="alerta alerta-erro">Erro.</div>'; return; }
  const d = await r.json();

  const sim = v => v ? '✅' : '—';
  const cfg = (d.clinicas || []).map(c => `
    <tr>
      <td><strong>${escapar(c.nome)}</strong></td>
      <td>${sim(c.capi_ativo)}</td>
      <td><code style="font-size:11px">${escapar(c.meta_dataset_id || '—')}</code></td>
      <td><code style="font-size:11px">${escapar(c.meta_page_id || '—')}</code></td>
      <td>${sim(c.tem_token)}</td>
      <td>${c.tem_test_code ? '⚠️ SIM' : '—'}</td>
    </tr>`).join('');

  const ev = (d.eventos || []).map(e => `
    <tr>
      <td style="white-space:nowrap">${escapar((e.criado_em || '').replace('T',' ').slice(0,16))}</td>
      <td>${escapar(e.clinica_nome || '')}</td>
      <td>${escapar(e.event_name || '')}</td>
      <td>${e.status === 'enviado' ? '✅ enviado' : '❌ ' + escapar(e.status || '')}</td>
      <td style="font-size:11px; color:#6B7280">${escapar(e.resposta || '')}</td>
    </tr>`).join('');

  const rf = (d.referrals || []).map(x => `
    <tr>
      <td style="white-space:nowrap">${escapar((x.referral_captado_em || '').replace('T',' ').slice(0,16))}</td>
      <td>${escapar(x.clinica_nome || '')}</td>
      <td>${escapar(x.numero_lead || '')}</td>
      <td>${x.ctwa_clid ? '✅ tem' : '❌ VAZIO'}</td>
      <td style="font-size:10px; color:#6B7280; max-width:420px; overflow:auto;">
        <code>${escapar(JSON.stringify(x.referral_json || {}))}</code>
      </td>
    </tr>`).join('');

  alvo.innerHTML = `
    <div style="font-weight:600; margin:14px 0 6px;">Configuração por cliente</div>
    <table><thead><tr><th>Cliente</th><th>Ativo</th><th>Dataset ID</th><th>Page ID</th><th>Token</th><th>Test code</th></tr></thead><tbody>${cfg}</tbody></table>
    <div style="font-weight:600; margin:18px 0 6px;">Últimos eventos enviados (resposta da Meta)</div>
    <table><thead><tr><th>Quando</th><th>Cliente</th><th>Evento</th><th>Status</th><th>Resposta</th></tr></thead><tbody>${ev || '<tr><td colspan=5>nenhum</td></tr>'}</tbody></table>
    <div style="font-weight:600; margin:18px 0 6px;">Últimos referrals capturados (JSON cru)</div>
    <table><thead><tr><th>Quando</th><th>Cliente</th><th>Número</th><th>ctwa_clid</th><th>referral_json</th></tr></thead><tbody>${rf || '<tr><td colspan=5>nenhum</td></tr>'}</tbody></table>
  `;
}

// ============ CLÍNICAS — EDIÇÃO ============
async function abrirEdicaoCliente(clinicaId) {
  const r = await fetch('/painel/admin/clinicas/' + clinicaId);
  if (!r.ok) {
    mostrarMsg('Erro ao carregar dados do cliente.', 'erro');
    return;
  }
  const c = await r.json();
  document.getElementById('ed-id').value = c.id;
  document.getElementById('ed-nome').value = c.nome || '';
  document.getElementById('ed-phone-id').value = c.phone_number_id || '';
  document.getElementById('ed-token').value = c.whatsapp_token || '';
  document.getElementById('ed-fone-humano').value = c.telefone_humano || '';
  document.getElementById('ed-prompt').value = c.system_prompt || '';
  // Rastreamento Meta. O token NÃO é pré-preenchido (fica em branco = mantém o atual).
  document.getElementById('ed-meta-dataset').value = c.meta_dataset_id || '';
  document.getElementById('ed-meta-pageid').value = c.meta_page_id || '';
  document.getElementById('ed-meta-token').value = '';
  document.getElementById('ed-meta-testcode').value = c.meta_test_event_code || '';
  document.getElementById('ed-capi-ativo').checked = !!c.capi_ativo;
  document.getElementById('modal-edicao').style.display = 'flex';
}

function fecharEdicaoCliente() {
  document.getElementById('modal-edicao').style.display = 'none';
}

async function salvarEdicaoCliente(e) {
  e.preventDefault();
  const id = document.getElementById('ed-id').value;
  const body = {
    nome: document.getElementById('ed-nome').value.trim(),
    phone_number_id: document.getElementById('ed-phone-id').value.trim(),
    whatsapp_token: document.getElementById('ed-token').value.trim(),
    telefone_humano: document.getElementById('ed-fone-humano').value.trim(),
    system_prompt: document.getElementById('ed-prompt').value.trim(),
    meta_dataset_id: document.getElementById('ed-meta-dataset').value.trim(),
    meta_page_id: document.getElementById('ed-meta-pageid').value.trim(),
    meta_test_event_code: document.getElementById('ed-meta-testcode').value.trim(),
    capi_ativo: document.getElementById('ed-capi-ativo').checked,
  };
  // Token só vai se foi digitado (em branco = mantém o atual, não apaga).
  const metaToken = document.getElementById('ed-meta-token').value.trim();
  if (metaToken) body.meta_capi_token = metaToken;

  const r = await fetch('/painel/admin/clinicas/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (r.ok) {
    mostrarMsg(`Cliente <strong>${escapar(body.nome)}</strong> atualizado.`, 'sucesso');
    fecharEdicaoCliente();
    carregarClinicas();
  } else {
    const data = await r.json().catch(() => ({}));
    mostrarMsg('Erro: ' + (data.erro || 'falha desconhecida'), 'erro');
  }
}

// ============ CLÍNICAS — FORMULÁRIO ============
async function criarClinicaSubmit(e) {
  e.preventDefault();
  const body = {
    nome: document.getElementById('cl-nome').value.trim(),
    phone_number_id: document.getElementById('cl-phone-id').value.trim(),
    whatsapp_token: document.getElementById('cl-token').value.trim(),
    telefone_humano: document.getElementById('cl-fone-humano').value.trim(),
    system_prompt: document.getElementById('cl-prompt').value.trim(),
  };
  const r = await fetch('/painel/admin/clinicas', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await r.json();
  if (r.ok) {
    mostrarMsg(`Cliente <strong>${escapar(body.nome)}</strong> criado com sucesso (id ${data.id}).`, 'sucesso');
    document.getElementById('cl-nome').value = '';
    document.getElementById('cl-phone-id').value = '';
    document.getElementById('cl-token').value = '';
    document.getElementById('cl-fone-humano').value = '';
    document.getElementById('cl-prompt').value = '';
    carregarClinicas();
  } else {
    mostrarMsg('Erro: ' + (data.erro || 'falha desconhecida'), 'erro');
  }
}

// ============ DROPDOWN DE CLÍNICAS ============
async function carregarClinicasSelect(selectId) {
  const r = await fetch('/painel/admin/usuarios');
  if (!r.ok) return;
  const clinicas = await r.json();
  const sel = document.getElementById(selectId);
  const valorAtual = sel.value;
  sel.innerHTML = '<option value="">— Selecione —</option>' +
    clinicas.map(c => `<option value="${c.id}">${escapar(c.nome)}</option>`).join('');
  sel.value = valorAtual;
}

// ============ USUÁRIOS — FORMULÁRIO ============
async function criarUsuarioSubmit(e) {
  e.preventDefault();
  const body = {
    clinica_id: parseInt(document.getElementById('us-clinica').value),
    email: document.getElementById('us-email').value.trim(),
    senha: document.getElementById('us-senha').value,
    nome: document.getElementById('us-nome').value.trim(),
  };
  if (!body.clinica_id) {
    mostrarMsg('Selecione um cliente.', 'erro');
    return;
  }
  const r = await fetch('/painel/admin/usuarios', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await r.json();
  if (r.ok) {
    mostrarMsg(
      `Usuário <strong>${escapar(body.email)}</strong> criado. ` +
      `Anote a senha: <code>${escapar(body.senha)}</code> — ` +
      `ela não vai aparecer de novo.`,
      'sucesso'
    );
    document.getElementById('us-email').value = '';
    document.getElementById('us-nome').value = '';
    document.getElementById('us-senha').value = '';
  } else {
    mostrarMsg('Erro: ' + (data.erro || 'falha desconhecida'), 'erro');
  }
}

// ============ PROMPT ============
async function carregarPrompt() {
  const clinicaId = document.getElementById('pr-clinica').value;
  const editor = document.getElementById('pr-editor');
  if (!clinicaId) {
    editor.style.display = 'none';
    return;
  }
  const r = await fetch('/painel/admin/clinicas/' + clinicaId);
  if (!r.ok) {
    mostrarMsg('Erro ao carregar o prompt.', 'erro');
    return;
  }
  const data = await r.json();
  document.getElementById('pr-texto').value = data.system_prompt || '';
  editor.style.display = 'block';
}

async function salvarPrompt() {
  const clinicaId = document.getElementById('pr-clinica').value;
  const texto = document.getElementById('pr-texto').value;
  if (!clinicaId) return;
  const r = await fetch('/painel/admin/clinicas/' + clinicaId + '/prompt', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ system_prompt: texto })
  });
  if (r.ok) {
    mostrarMsg('Prompt atualizado com sucesso. As próximas mensagens já vão usar o novo prompt.', 'sucesso');
  } else {
    const data = await r.json().catch(() => ({}));
    mostrarMsg('Erro: ' + (data.erro || 'falha desconhecida'), 'erro');
  }
}

// ============ HORÁRIOS ============
// Ao trocar de clínica: popula o seletor de profissional (se houver) e carrega.
async function aoTrocarClinicaHorarios() {
  const clinicaId = document.getElementById('hr-clinica').value;
  const wrap = document.getElementById('hr-prof-wrap');
  const selProf = document.getElementById('hr-profissional');
  selProf.innerHTML = '<option value="">Padrão da clínica</option>';
  if (!clinicaId) {
    wrap.style.display = 'none';
    document.getElementById('hr-editor').style.display = 'none';
    return;
  }
  try {
    const r = await fetch('/painel/admin/clinicas/' + clinicaId + '/profissionais');
    const profs = r.ok ? await r.json() : [];
    const ativos = profs.filter(p => p.ativo);
    if (ativos.length > 0) {
      selProf.innerHTML = '<option value="">Padrão da clínica</option>' +
        ativos.map(p => `<option value="${p.id}">${escapar(p.nome)}</option>`).join('');
      wrap.style.display = 'block';
    } else {
      wrap.style.display = 'none';
    }
  } catch (e) {
    wrap.style.display = 'none';
  }
  carregarHorarios();
}

async function carregarHorarios() {
  const clinicaId = document.getElementById('hr-clinica').value;
  const editor = document.getElementById('hr-editor');
  if (!clinicaId) {
    editor.style.display = 'none';
    return;
  }
  const profSel = document.getElementById('hr-profissional');
  const profId = profSel ? profSel.value : '';
  const url = '/painel/admin/clinicas/' + clinicaId + '/horarios' +
    (profId ? ('?profissional_id=' + encodeURIComponent(profId)) : '');
  const r = await fetch(url);
  if (!r.ok) {
    mostrarMsg('Erro ao carregar horários.', 'erro');
    return;
  }
  const cfg = await r.json();

  // Dias da semana
  const diasAtivos = (cfg.dias_semana || '').split(',').map(d => d.trim());
  document.querySelectorAll('.hr-dia').forEach(cb => {
    cb.checked = diasAtivos.includes(cb.value);
  });

  // Horários (vêm como "HH:MM:SS", input type=time aceita só "HH:MM")
  document.getElementById('hr-inicio').value = (cfg.hora_inicio || '').substring(0, 5);
  document.getElementById('hr-fim').value = (cfg.hora_fim || '').substring(0, 5);
  document.getElementById('hr-almoco-inicio').value = (cfg.almoco_inicio || '').substring(0, 5);
  document.getElementById('hr-almoco-fim').value = (cfg.almoco_fim || '').substring(0, 5);
  document.getElementById('hr-duracao').value = cfg.duracao_minutos;
  document.getElementById('hr-antecedencia').value = cfg.antecedencia_minima_minutos;

  editor.style.display = 'block';
}

async function salvarHorarios() {
  const clinicaId = document.getElementById('hr-clinica').value;
  if (!clinicaId) return;

  const dias = Array.from(document.querySelectorAll('.hr-dia'))
    .filter(cb => cb.checked).map(cb => cb.value);
  if (dias.length === 0) {
    mostrarMsg('Selecione pelo menos um dia da semana.', 'erro');
    return;
  }

  const profSel = document.getElementById('hr-profissional');
  const profId = profSel ? profSel.value : '';
  const body = {
    dias_semana: dias.join(','),
    hora_inicio: document.getElementById('hr-inicio').value,
    hora_fim: document.getElementById('hr-fim').value,
    almoco_inicio: document.getElementById('hr-almoco-inicio').value || null,
    almoco_fim: document.getElementById('hr-almoco-fim').value || null,
    duracao_minutos: parseInt(document.getElementById('hr-duracao').value, 10),
    antecedencia_minima_minutos: parseInt(document.getElementById('hr-antecedencia').value, 10),
  };
  if (profId) body.profissional_id = parseInt(profId, 10);

  const r = await fetch('/painel/admin/clinicas/' + clinicaId + '/horarios', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (r.ok) {
    const alvo = profId
      ? 'do profissional selecionado'
      : 'padrão da clínica';
    mostrarMsg('Horários ' + alvo + ' atualizados. A Ana já respeita a nova configuração.', 'sucesso');
  } else {
    const data = await r.json().catch(() => ({}));
    mostrarMsg('Erro: ' + (data.erro || 'falha desconhecida'), 'erro');
  }
}

function escapar(s) {
  return (s || '').toString()
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;');
}

carregarClinicas();
</script>
</body>
</html>
"""



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
            contexto = "Todos os clientes"
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
        clinica_id_sessao = session.get("clinica_id")
        # Usuário comum: SEMPRE vê só a clínica dele (segurança).
        # Admin: pode filtrar via querystring ?clinica_id=X (ou ver tudo se vazio).
        if clinica_id_sessao is not None:
            filtro = clinica_id_sessao
        else:
            filtro_str = request.args.get("clinica_id")
            filtro = int(filtro_str) if filtro_str and filtro_str.isdigit() else None
        rows = listar_conversas(clinica_id=filtro)
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
        # NUNCA expõe o token do WhatsApp pro navegador (segurança).
        data["info"].pop("whatsapp_token", None)
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
        # Token específico da clínica (vai vir do banco através do buscar_conversa_completa)
        # Se não tiver, usa o global do Render como fallback
        token_clinica = info.get("whatsapp_token") or os.getenv("WHATSAPP_TOKEN")
        try:
            url = f"https://graph.facebook.com/v21.0/{info['phone_number_id']}/messages"
            headers = {
                "Authorization": f"Bearer {token_clinica}",
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

    @app.route("/painel/api/conversas/<int:conversa_id>/venda", methods=["POST"])
    @login_required
    def api_registrar_venda(conversa_id):
        """Registra uma venda (fecho) numa conversa e dispara o evento Purchase
        pra Meta (se o tenant tiver CAPI ativo e o lead veio de anúncio)."""
        data = buscar_conversa_completa(conversa_id)
        if not data:
            return jsonify({"erro": "nao encontrada"}), 404
        if session.get("clinica_id") is not None:
            if data["info"]["clinica_id"] != session["clinica_id"]:
                return jsonify({"erro": "sem permissao"}), 403

        body = request.get_json() or {}
        valor = body.get("valor")
        try:
            valor = float(valor) if valor not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"erro": "valor inválido"}), 400
        if valor is not None and valor < 0:
            return jsonify({"erro": "valor inválido"}), 400
        descricao = (body.get("descricao") or "").strip() or None

        clinica_id = data["info"]["clinica_id"]
        venda_id = registrar_venda(clinica_id, conversa_id,
                                   valor=valor, descricao=descricao)

        # Dispara Purchase em thread — não segura a resposta do painel, e falha
        # de CAPI nunca afeta o registro da venda (que já está salvo no banco).
        clinica = obter_clinica(clinica_id)
        if clinica and clinica.get("capi_ativo"):
            conversa = obter_conversa(conversa_id)
            custom = {"value": valor, "currency": "BRL"} if valor is not None else None
            threading.Thread(
                target=capi.enviar_evento,
                args=(clinica, conversa, "Purchase", f"purchase:{venda_id}"),
                kwargs={"custom_data": custom},
                daemon=True
            ).start()

        return jsonify({"ok": True, "venda_id": venda_id})

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

    # ---------- Admin: tela HTML completa ----------
    @app.route("/painel/admin", methods=["GET"])
    @login_required
    @admin_required
    def admin_pagina():
        nome = session.get("nome", "—")
        inicial = (nome[:1] or "?").upper()
        return render_template_string(ADMIN_HTML, nome=nome, inicial=inicial)

    # ---------- Admin: gerenciar CLÍNICAS ----------
    @app.route("/painel/admin/clinicas", methods=["GET"])
    @login_required
    @admin_required
    def admin_listar_clinicas_stats():
        """Lista clínicas com estatísticas (conversas, usuários)."""
        rows = listar_clinicas_com_stats()
        for r in rows:
            if r.get("criada_em"):
                r["criada_em"] = r["criada_em"].isoformat()
        return jsonify(rows)

    @app.route("/painel/admin/clinicas", methods=["POST"])
    @login_required
    @admin_required
    def admin_criar_clinica():
        """Cria uma clínica nova."""
        body = request.get_json() or {}
        nome = (body.get("nome") or "").strip()
        phone_id = (body.get("phone_number_id") or "").strip()
        prompt = (body.get("system_prompt") or "").strip()
        telefone_humano = (body.get("telefone_humano") or "").strip()
        whatsapp_token = (body.get("whatsapp_token") or "").strip()
        if not (nome and phone_id and prompt):
            return jsonify({
                "erro": "campos obrigatórios: nome, phone_number_id, system_prompt"
            }), 400
        try:
            cid = criar_clinica(nome, phone_id, prompt, telefone_humano, whatsapp_token)
            return jsonify({"id": cid, "nome": nome})
        except Exception as e:
            msg = str(e)
            if "duplicate key" in msg.lower() or "unique" in msg.lower():
                return jsonify({
                    "erro": "Já existe um cliente com esse Phone Number ID."
                }), 400
            return jsonify({"erro": msg}), 400

    @app.route("/painel/admin/clinicas/<int:clinica_id>", methods=["GET"])
    @login_required
    @admin_required
    def admin_obter_clinica(clinica_id):
        """Retorna os dados de uma clínica (inclui o prompt — só admin pode ver)."""
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "não encontrada"}), 404
        if clinica.get("criada_em"):
            clinica["criada_em"] = clinica["criada_em"].isoformat()
        return jsonify(clinica)

    @app.route("/painel/admin/clinicas/<int:clinica_id>/prompt", methods=["PATCH"])
    @login_required
    @admin_required
    def admin_atualizar_prompt(clinica_id):
        """Atualiza o prompt da Ana de uma clínica."""
        body = request.get_json() or {}
        novo_prompt = (body.get("system_prompt") or "").strip()
        if not novo_prompt:
            return jsonify({"erro": "prompt vazio"}), 400
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "clínica não encontrada"}), 404
        atualizar_prompt_clinica(clinica_id, novo_prompt)
        return jsonify({"ok": True})

    @app.route("/painel/admin/clinicas/<int:clinica_id>", methods=["PATCH"])
    @login_required
    @admin_required
    def admin_atualizar_clinica(clinica_id):
        """Atualiza qualquer campo de uma clínica (edição completa)."""
        body = request.get_json() or {}
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "clínica não encontrada"}), 404

        try:
            atualizar_clinica(
                clinica_id,
                nome=body.get("nome"),
                phone_number_id=body.get("phone_number_id"),
                telefone_humano=body.get("telefone_humano"),
                whatsapp_token=body.get("whatsapp_token"),
                system_prompt=body.get("system_prompt"),
                meta_dataset_id=body.get("meta_dataset_id"),
                meta_capi_token=body.get("meta_capi_token"),
                capi_ativo=body.get("capi_ativo"),
                meta_test_event_code=body.get("meta_test_event_code"),
                meta_page_id=body.get("meta_page_id"),
            )
            return jsonify({"ok": True})
        except Exception as e:
            msg = str(e)
            if "duplicate key" in msg.lower() or "unique" in msg.lower():
                return jsonify({
                    "erro": "Já existe outro cliente com esse Phone Number ID."
                }), 400
            return jsonify({"erro": msg}), 400

    # ---------- Admin: horários de atendimento por clínica ----------
    @app.route("/painel/admin/clinicas/<int:clinica_id>/horarios", methods=["GET"])
    @login_required
    @admin_required
    def admin_obter_horarios(clinica_id):
        """Retorna a config de horários da clínica, ou de um profissional
        (query ?profissional_id=X). Sem profissional = default da clínica."""
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "clínica não encontrada"}), 404
        prof_str = request.args.get("profissional_id")
        prof_id = int(prof_str) if prof_str and prof_str.isdigit() else None
        cfg = obter_config_horarios(clinica_id, prof_id)
        # Converte tipos pra JSON
        for campo in ("hora_inicio", "hora_fim", "almoco_inicio", "almoco_fim"):
            if cfg.get(campo) is not None:
                cfg[campo] = cfg[campo].strftime("%H:%M:%S")
        if cfg.get("atualizada_em"):
            cfg["atualizada_em"] = cfg["atualizada_em"].isoformat()
        return jsonify(cfg)

    @app.route("/painel/admin/clinicas/<int:clinica_id>/horarios", methods=["PATCH"])
    @login_required
    @admin_required
    def admin_atualizar_horarios(clinica_id):
        """Atualiza a config de horários da clínica, ou de um profissional
        (body profissional_id=X cria/atualiza o override daquele profissional)."""
        body = request.get_json() or {}
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "clínica não encontrada"}), 404
        prof_id = body.get("profissional_id")
        prof_id = int(prof_id) if prof_id else None
        try:
            atualizar_config_horarios(
                clinica_id,
                duracao_minutos=body.get("duracao_minutos"),
                antecedencia_minima_minutos=body.get("antecedencia_minima_minutos"),
                dias_semana=body.get("dias_semana"),
                hora_inicio=body.get("hora_inicio"),
                hora_fim=body.get("hora_fim"),
                almoco_inicio=body.get("almoco_inicio"),
                almoco_fim=body.get("almoco_fim"),
                profissional_id=prof_id,
            )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"erro": str(e)}), 400

    # ---------- Admin: diagnóstico do rastreamento ----------
    @app.route("/painel/admin/capi/diagnostico", methods=["GET"])
    @login_required
    @admin_required
    def admin_capi_diagnostico():
        d = diagnostico_capi()
        for e in d["eventos"]:
            if e.get("criado_em"):
                e["criado_em"] = e["criado_em"].isoformat()
        for r in d["referrals"]:
            if r.get("referral_captado_em"):
                r["referral_captado_em"] = r["referral_captado_em"].isoformat()
        return jsonify(d)

    # ---------- Admin: reenviar eventos CAPI que falharam ----------
    @app.route("/painel/admin/capi/reenviar", methods=["POST"])
    @login_required
    @admin_required
    def admin_reenviar_capi():
        """
        Reenvia o evento LeadSubmitted dos agendamentos recentes que ainda não
        foram contabilizados na Meta (ex: falharam por falta de page_id).
        Usa a data REAL do agendamento como event_time. Deduplicado: agendamento
        já enviado com sucesso é pulado.
        """
        body = request.get_json() or {}
        try:
            dias = min(max(int(body.get("dias") or 7), 1), 7)
        except (TypeError, ValueError):
            dias = 7
        # forcar=True reenvia mesmo os já enviados (pra testar page_id no Test Events).
        forcar = bool(body.get("forcar"))

        ags = listar_agendamentos_para_reenvio_capi(dias)
        res = {
            "dias": dias, "total_agendamentos": len(ags), "enviados": 0,
            "ja_enviados": 0, "sem_atribuicao": 0, "tenant_sem_capi": 0,
            "falhas": 0, "detalhes": [],
        }
        cache = {}

        for ag in ags:
            cid = ag["clinica_id"]
            if cid not in cache:
                cache[cid] = obter_clinica(cid)
            clinica = cache[cid]

            if not clinica or not clinica.get("capi_ativo"):
                res["tenant_sem_capi"] += 1
                continue

            # Acha a conversa: pelo vínculo, ou pelo número (agendamento manual
            # feito no painel não tem conversa_id).
            conversa = obter_conversa(ag["conversa_id"]) if ag["conversa_id"] else None
            if not conversa or not conversa.get("ctwa_clid"):
                conversa = buscar_conversa_por_numero(cid, ag["numero_lead"]) or conversa
            if not conversa or not conversa.get("ctwa_clid"):
                res["sem_atribuicao"] += 1
                res["detalhes"].append(
                    f"#{ag['id']} {ag.get('nome_lead') or ''} ({ag['clinica_nome']}): "
                    f"sem ctwa_clid na conversa (anterior ao rastreamento, ou referral "
                    f"sem click id) — não dá pra atribuir"
                )
                continue

            event_id = f"leadsubmitted:{ag['id']}"
            if not forcar and capi_evento_ja_enviado(event_id):
                res["ja_enviados"] += 1
                continue

            ok = capi.enviar_evento(
                clinica, conversa, "LeadSubmitted", event_id,
                event_time=int(ag["criado_em"].timestamp())
            )
            if ok:
                res["enviados"] += 1
                res["detalhes"].append(
                    f"#{ag['id']} {ag.get('nome_lead') or ''} ({ag['clinica_nome']}): enviado ✅"
                )
            else:
                res["falhas"] += 1
                res["detalhes"].append(
                    f"#{ag['id']} {ag.get('nome_lead') or ''} ({ag['clinica_nome']}): "
                    f"falhou — ver log do servidor"
                )

        return jsonify(res)

    # ---------- Admin: profissionais por clínica (multi-profissional) ----------
    @app.route("/painel/admin/clinicas/<int:clinica_id>/profissionais", methods=["GET"])
    @login_required
    @admin_required
    def admin_listar_profissionais(clinica_id):
        """Lista profissionais de uma clínica (inclui inativos pro admin gerenciar)."""
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "clínica não encontrada"}), 404
        profs = listar_profissionais(clinica_id, incluir_inativos=True)
        for p in profs:
            if p.get("criado_em"):
                p["criado_em"] = p["criado_em"].isoformat()
        return jsonify(profs)

    @app.route("/painel/admin/clinicas/<int:clinica_id>/profissionais", methods=["POST"])
    @login_required
    @admin_required
    def admin_criar_profissional(clinica_id):
        """Cadastra um profissional novo na clínica."""
        clinica = obter_clinica(clinica_id)
        if not clinica:
            return jsonify({"erro": "clínica não encontrada"}), 404
        body = request.get_json() or {}
        try:
            pid = criar_profissional(clinica_id, body.get("nome"))
            return jsonify({"id": pid})
        except ValueError as e:
            return jsonify({"erro": str(e)}), 400

    @app.route("/painel/admin/profissionais/<int:profissional_id>", methods=["PATCH"])
    @login_required
    @admin_required
    def admin_atualizar_profissional(profissional_id):
        """Renomeia ou ativa/desativa um profissional."""
        prof = obter_profissional(profissional_id)
        if not prof:
            return jsonify({"erro": "profissional não encontrado"}), 404
        body = request.get_json() or {}
        try:
            atualizar_profissional(
                profissional_id,
                nome=body.get("nome"),
                ativo=body.get("ativo"),
            )
            return jsonify({"ok": True})
        except ValueError as e:
            return jsonify({"erro": str(e)}), 400

    # ============================================================
    # AGENDA (view semanal no painel)
    # ============================================================
    @app.route("/painel/api/agenda", methods=["GET"])
    @login_required
    def api_agenda():
        """
        Retorna agendamentos + bloqueios + config de horários
        do intervalo pedido (query params: inicio, fim em YYYY-MM-DD).
        Admin passa clinica_id na query. Cliente comum usa a da sessão.
        """
        from datetime import datetime, timezone, timedelta

        # Descobre a clínica alvo
        clinica_sessao = session.get("clinica_id")
        clinica_query = request.args.get("clinica_id")
        if clinica_sessao is not None:
            # cliente comum: força a própria clínica
            clinica_id = clinica_sessao
        else:
            # admin: precisa passar clinica_id
            if not clinica_query or not clinica_query.isdigit():
                return jsonify({"erro": "clinica_id obrigatório pra admin"}), 400
            clinica_id = int(clinica_query)

        # Datas
        try:
            data_inicio_str = request.args.get("inicio")
            data_fim_str = request.args.get("fim")
            tz = timezone(timedelta(hours=-3))  # Brasília
            data_inicio = datetime.strptime(data_inicio_str, "%Y-%m-%d").replace(tzinfo=tz)
            data_fim = datetime.strptime(data_fim_str, "%Y-%m-%d").replace(tzinfo=tz)
        except Exception:
            return jsonify({"erro": "datas inválidas (use YYYY-MM-DD)"}), 400

        if (data_fim - data_inicio).days > 60:
            return jsonify({"erro": "intervalo muito longo"}), 400

        # Config de horários
        cfg = obter_config_horarios(clinica_id)
        # Serializa campos time/date
        for campo in ("hora_inicio", "hora_fim", "almoco_inicio", "almoco_fim"):
            if cfg.get(campo) is not None:
                cfg[campo] = cfg[campo].strftime("%H:%M:%S")
        if cfg.get("atualizada_em"):
            cfg["atualizada_em"] = cfg["atualizada_em"].isoformat()

        # Agendamentos
        ags = listar_agendamentos(clinica_id, data_inicio, data_fim)
        for a in ags:
            if a.get("data_hora"):
                a["data_hora"] = a["data_hora"].isoformat()
            if a.get("criado_em"):
                a["criado_em"] = a["criado_em"].isoformat()

        # Bloqueios
        blqs = listar_bloqueios(clinica_id, data_inicio, data_fim)
        for b in blqs:
            if b.get("inicio"):
                b["inicio"] = b["inicio"].isoformat()
            if b.get("fim"):
                b["fim"] = b["fim"].isoformat()
            if b.get("criado_em"):
                b["criado_em"] = b["criado_em"].isoformat()

        # Profissionais ativos da clínica (pro filtro/cor e seletor de criação).
        # Lista vazia = clínica no modo agenda única (comportamento antigo).
        profs = listar_profissionais(clinica_id)
        for p in profs:
            if p.get("criado_em"):
                p["criado_em"] = p["criado_em"].isoformat()

        return jsonify({
            "config": cfg,
            "agendamentos": ags,
            "bloqueios": blqs,
            "profissionais": profs,
        })

    @app.route("/painel/api/agendamentos/<int:agendamento_id>/cancelar",
               methods=["POST"])
    @login_required
    def api_cancelar_agendamento(agendamento_id):
        """Cancela agendamento (apenas o dono da clínica ou admin)."""
        ag = obter_agendamento(agendamento_id)
        if not ag:
            return jsonify({"erro": "agendamento não encontrado"}), 404

        clinica_sessao = session.get("clinica_id")
        if clinica_sessao is not None and ag["clinica_id"] != clinica_sessao:
            return jsonify({"erro": "sem permissão"}), 403

        ok = cancelar_agendamento(agendamento_id)
        return jsonify({"ok": ok})

    @app.route("/painel/api/bloqueios/<int:bloqueio_id>",
               methods=["DELETE"])
    @login_required
    def api_remover_bloqueio(bloqueio_id):
        """Remove bloqueio (apenas o dono da clínica ou admin)."""
        # Não temos obter_bloqueio, então validamos via listar_bloqueios
        # com filtro amplo. Como bloqueio_id é global, precisamos garantir
        # que pertence à clínica do usuário.
        clinica_sessao = session.get("clinica_id")

        if clinica_sessao is not None:
            # Cliente comum: verifica se o bloqueio é da sua clínica
            from datetime import datetime, timezone, timedelta
            tz = timezone(timedelta(hours=-3))
            # Range grande pra pegar qualquer bloqueio ativo
            ini = datetime(2020, 1, 1, tzinfo=tz)
            fim = datetime(2100, 1, 1, tzinfo=tz)
            blqs = listar_bloqueios(clinica_sessao, ini, fim)
            if not any(b["id"] == bloqueio_id for b in blqs):
                return jsonify({"erro": "sem permissão ou bloqueio inexistente"}), 403

        ok = remover_bloqueio(bloqueio_id)
        return jsonify({"ok": ok})

    # ============================================================
    # AGENDA EDITÁVEL (criar/editar pelo painel)
    # ============================================================
    def _clinica_alvo():
        """
        Resolve a clínica alvo de uma escrita na agenda.
        Cliente comum: sempre a da sessão. Admin: passa clinica_id no body.
        Retorna (clinica_id, None) ou (None, resposta_de_erro).
        """
        clinica_sessao = session.get("clinica_id")
        if clinica_sessao is not None:
            return clinica_sessao, None
        body = request.get_json(silent=True) or {}
        cid = body.get("clinica_id")
        try:
            return int(cid), None
        except (TypeError, ValueError):
            return None, (jsonify({"erro": "clinica_id obrigatório pra admin"}), 400)

    def _parse_dt_brasil(valor):
        """Converte 'AAAA-MM-DDTHH:MM' pra datetime com fuso de Brasília. None se inválido."""
        from datetime import datetime, timezone, timedelta
        try:
            tz = timezone(timedelta(hours=-3))
            return datetime.strptime(valor or "", "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        except ValueError:
            return None

    def _resolver_prof_body(clinica_id, body, obrigatorio):
        """
        Valida o profissional_id vindo do body pra escritas na agenda.
        Retorna (profissional_id, erro_response):
          - clínica sem profissionais: (None, None) — modo agenda única.
          - obrigatorio e ausente/inválido: (None, resposta 400).
          - opcional e ausente: (None, None) — vale pra clínica toda (bloqueio).
        """
        profs = listar_profissionais(clinica_id)
        if not profs:
            return None, None
        ids = {p["id"] for p in profs}
        raw = body.get("profissional_id")
        pid = int(raw) if raw not in (None, "") and str(raw).isdigit() else None
        if pid is None:
            if obrigatorio:
                return None, (jsonify({
                    "erro": "esta clínica tem profissionais; selecione o profissional"
                }), 400)
            return None, None
        if pid not in ids:
            return None, (jsonify({"erro": "profissional inválido"}), 400)
        return pid, None

    @app.route("/painel/api/agendamentos", methods=["POST"])
    @login_required
    def api_criar_agendamento():
        """Cria agendamento manual pelo painel (clique em horário livre da agenda)."""
        clinica_id, erro = _clinica_alvo()
        if erro:
            return erro

        body = request.get_json() or {}
        nome = (body.get("nome") or "").strip()
        if len(nome) < 3:
            return jsonify({"erro": "nome do paciente é obrigatório"}), 400

        dt = _parse_dt_brasil(body.get("data_hora"))
        if not dt:
            return jsonify({"erro": "data/hora inválida"}), 400

        prof_id, erro_prof = _resolver_prof_body(clinica_id, body, obrigatorio=True)
        if erro_prof:
            return erro_prof

        telefone = (body.get("telefone") or "").strip()
        observacao = (body.get("observacao") or "").strip() or None
        try:
            ag_id = criar_agendamento(
                clinica_id=clinica_id,
                numero_lead=telefone or "manual",
                data_hora=dt,
                nome_lead=nome,
                origem="manual",
                observacao=observacao,
                profissional_id=prof_id,
            )
            return jsonify({"id": ag_id})
        except ValueError as e:
            if "ocupado" in str(e):
                return jsonify({"erro": "esse horário já está ocupado"}), 409
            return jsonify({"erro": str(e)}), 400

    @app.route("/painel/api/agendamentos/<int:agendamento_id>", methods=["PATCH"])
    @login_required
    def api_editar_agendamento(agendamento_id):
        """Edita agendamento: nome, motivo, data_hora e/ou profissional.
        Trocar de profissional move o agendamento pra agenda do novo profissional,
        validando que ele está livre naquele horário."""
        ag = obter_agendamento(agendamento_id)
        if not ag:
            return jsonify({"erro": "agendamento não encontrado"}), 404
        clinica_sessao = session.get("clinica_id")
        if clinica_sessao is not None and ag["clinica_id"] != clinica_sessao:
            return jsonify({"erro": "sem permissão"}), 403

        body = request.get_json() or {}

        # Profissional de destino (só faz sentido em clínica multi).
        # "profissional_id" presente no body = intenção de definir/trocar.
        prof_novo = ag["profissional_id"]
        if "profissional_id" in body:
            prof_novo, erro_prof = _resolver_prof_body(
                ag["clinica_id"], body, obrigatorio=True
            )
            if erro_prof:
                return erro_prof
        mudou_prof = ("profissional_id" in body) and (prof_novo != ag["profissional_id"])

        # Nova data/hora (opcional).
        nova_data = body.get("data_hora")
        dt = None
        if nova_data:
            dt = _parse_dt_brasil(nova_data)
            if not dt:
                return jsonify({"erro": "data/hora inválida"}), 400

        if mudou_prof:
            # Troca de profissional (com ou sem mudança de horário): checa a agenda
            # do profissional de DESTINO no horário alvo antes de mover.
            tempo_alvo = dt or ag["data_hora"]
            if existe_conflito(ag["clinica_id"], tempo_alvo, ag["duracao_minutos"],
                               ignorar_id=agendamento_id, profissional_id=prof_novo):
                return jsonify({
                    "erro": "esse horário já está ocupado na agenda desse profissional"
                }), 409
            atualizar_agendamento(agendamento_id, profissional_id=prof_novo,
                                  data_hora=tempo_alvo)
        elif dt:
            # Só mudou o horário (mesmo profissional): remarcar valida o conflito
            # na agenda do próprio profissional.
            try:
                remarcar_agendamento(agendamento_id, dt)
            except ValueError as e:
                if "ocupado" in str(e):
                    return jsonify({"erro": "o novo horário já está ocupado"}), 409
                return jsonify({"erro": str(e)}), 400

        atualizar_agendamento(
            agendamento_id,
            nome_lead=body.get("nome_lead"),
            observacao=body.get("observacao"),
        )
        return jsonify({"ok": True})

    @app.route("/painel/api/bloqueios", methods=["POST"])
    @login_required
    def api_criar_bloqueio():
        """Cria bloqueio pelo painel (clique em horário livre da agenda)."""
        clinica_id, erro = _clinica_alvo()
        if erro:
            return erro

        body = request.get_json() or {}
        inicio = _parse_dt_brasil(body.get("inicio"))
        fim = _parse_dt_brasil(body.get("fim"))
        if not inicio or not fim:
            return jsonify({"erro": "datas inválidas"}), 400
        if fim <= inicio:
            return jsonify({"erro": "o fim precisa ser depois do início"}), 400

        prof_id, erro_prof = _resolver_prof_body(clinica_id, body, obrigatorio=False)
        if erro_prof:
            return erro_prof

        motivo = (body.get("motivo") or "").strip() or "Bloqueio manual"
        bid = criar_bloqueio(clinica_id, inicio, fim, motivo, profissional_id=prof_id)
        return jsonify({"id": bid})
