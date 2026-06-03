"""
Painel web do produto Ana.

- Tela de login (por email/senha).
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
    """Decorador: bloqueia rota se o usuário não estiver logado."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/painel/api"):
                return jsonify({"erro": "nao autenticado"}), 401
            return redirect(url_for("painel_login_page"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Decorador: bloqueia rota se não for admin (clinica_id NULL)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("clinica_id") is not None:
            return jsonify({"erro": "apenas admin"}), 403
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# HTML — usa render_template_string pra ficar tudo num arquivo
# ============================================================
LOGIN_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — Painel Ana</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
         background: #f3f4f6; margin: 0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; }
  .card { background: #fff; padding: 36px; border-radius: 12px;
          box-shadow: 0 4px 24px rgba(0,0,0,.06); width: 360px; }
  h1 { font-size: 22px; margin: 0 0 6px; color: #111827; }
  .sub { color: #6b7280; font-size: 14px; margin-bottom: 24px; }
  label { display: block; font-size: 13px; color: #374151; margin-bottom: 6px; }
  input { width: 100%; padding: 10px 12px; border: 1px solid #d1d5db;
          border-radius: 8px; font-size: 15px; margin-bottom: 16px; }
  input:focus { outline: none; border-color: #2563eb; }
  button { width: 100%; padding: 11px; background: #111827; color: #fff;
           border: none; border-radius: 8px; font-size: 15px; cursor: pointer; }
  button:hover { background: #1f2937; }
  .erro { background: #fee2e2; color: #991b1b; padding: 10px 12px;
          border-radius: 8px; font-size: 14px; margin-bottom: 16px; }
</style>
</head>
<body>
  <form class="card" method="post" action="/painel/login">
    <h1>Painel Ana</h1>
    <div class="sub">Entre pra acompanhar as conversas</div>
    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
    <label>Email</label>
    <input type="email" name="email" required autofocus>
    <label>Senha</label>
    <input type="password" name="senha" required>
    <button type="submit">Entrar</button>
  </form>
</body>
</html>
"""


PAINEL_HTML = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel Ana</title>
<style>
  * { box-sizing: border-box; }
  body, html { margin: 0; height: 100%;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    background: #f9fafb; color: #111827; }
  .app { display: flex; height: 100vh; }

  /* Sidebar */
  .sidebar { width: 360px; background: #fff; border-right: 1px solid #e5e7eb;
             display: flex; flex-direction: column; }
  .topbar { padding: 14px 18px; border-bottom: 1px solid #e5e7eb;
            display: flex; justify-content: space-between; align-items: center; }
  .topbar .nome { font-size: 14px; font-weight: 600; }
  .topbar .sair { font-size: 12px; color: #6b7280; text-decoration: none; }
  .topbar .sair:hover { color: #111827; }
  .lista { flex: 1; overflow-y: auto; }
  .conversa-item { padding: 14px 18px; border-bottom: 1px solid #f3f4f6;
                   cursor: pointer; }
  .conversa-item:hover { background: #f9fafb; }
  .conversa-item.ativa { background: #eff6ff; }
  .conversa-item .top { display: flex; justify-content: space-between;
                        margin-bottom: 4px; }
  .conversa-item .lead { font-weight: 600; font-size: 14px; color: #111827; }
  .conversa-item .clinica { font-size: 11px; color: #2563eb;
                            text-transform: uppercase; letter-spacing: 0.4px; }
  .conversa-item .preview { font-size: 13px; color: #6b7280;
                            white-space: nowrap; overflow: hidden;
                            text-overflow: ellipsis; }
  .conversa-item .badge { display: inline-block; font-size: 10px;
                          padding: 2px 6px; border-radius: 10px;
                          background: #fef3c7; color: #92400e;
                          margin-left: 6px; font-weight: 600; }
  .vazio { padding: 24px; color: #9ca3af; text-align: center; font-size: 14px; }

  /* Detalhe */
  .detalhe { flex: 1; display: flex; flex-direction: column; }
  .detalhe-topo { padding: 16px 24px; border-bottom: 1px solid #e5e7eb;
                  background: #fff; display: flex; justify-content: space-between;
                  align-items: center; }
  .detalhe-topo .titulo { font-weight: 600; }
  .detalhe-topo .sub { font-size: 13px; color: #6b7280; }
  .btn { padding: 8px 14px; border-radius: 8px; border: none;
         font-size: 13px; cursor: pointer; font-weight: 500; }
  .btn-primario { background: #111827; color: #fff; }
  .btn-primario:hover { background: #1f2937; }
  .btn-verde { background: #059669; color: #fff; }
  .btn-verde:hover { background: #047857; }

  .mensagens { flex: 1; overflow-y: auto; padding: 24px;
               display: flex; flex-direction: column; gap: 8px; }
  .msg { max-width: 70%; padding: 10px 14px; border-radius: 14px;
         font-size: 14px; line-height: 1.45; word-wrap: break-word; }
  .msg-lead { background: #fff; border: 1px solid #e5e7eb;
              align-self: flex-start; border-bottom-left-radius: 4px; }
  .msg-ana { background: #dcfce7; align-self: flex-end;
             border-bottom-right-radius: 4px; }
  .msg .hora { font-size: 10px; color: #9ca3af; margin-top: 4px; }

  .composer { padding: 14px 24px; border-top: 1px solid #e5e7eb;
              background: #fff; display: flex; gap: 10px; }
  .composer textarea { flex: 1; resize: none; padding: 10px 12px;
                       border: 1px solid #d1d5db; border-radius: 8px;
                       font-family: inherit; font-size: 14px; height: 44px;
                       max-height: 120px; }
  .composer textarea:focus { outline: none; border-color: #2563eb; }
  .composer.desativado { display: none; }
  .aviso { padding: 12px 24px; background: #fef3c7; color: #92400e;
           font-size: 13px; text-align: center; border-top: 1px solid #fde68a; }

  .placeholder { flex: 1; display: flex; align-items: center;
                 justify-content: center; color: #9ca3af; font-size: 15px; }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="topbar">
      <div class="nome">{{ nome }}</div>
      <a href="/painel/logout" class="sair">Sair</a>
    </div>
    <div class="lista" id="lista">
      <div class="vazio">Carregando...</div>
    </div>
  </aside>

  <main class="detalhe">
    <div class="placeholder" id="placeholder">
      Selecione uma conversa pra começar
    </div>

    <div class="detalhe-topo" id="detalhe-topo" style="display:none">
      <div>
        <div class="titulo" id="det-titulo">—</div>
        <div class="sub" id="det-sub">—</div>
      </div>
      <button class="btn" id="btn-acao" onclick="alternarPausa()">—</button>
    </div>
    <div class="mensagens" id="mensagens" style="display:none"></div>
    <div class="aviso" id="aviso-pausada" style="display:none">
      Você assumiu essa conversa. A Ana não vai responder até você devolver o controle.
    </div>
    <form class="composer" id="composer" style="display:none"
          onsubmit="enviarMsg(event)">
      <textarea id="msg-texto" placeholder="Digite a mensagem..." required></textarea>
      <button class="btn btn-primario" type="submit">Enviar</button>
    </form>
  </main>
</div>

<script>
let conversaSelecionada = null;
let conversas = [];

async function carregarConversas() {
  const r = await fetch('/painel/api/conversas');
  if (!r.ok) return;
  conversas = await r.json();

  const lista = document.getElementById('lista');
  if (conversas.length === 0) {
    lista.innerHTML = '<div class="vazio">Nenhuma conversa ainda</div>';
    return;
  }

  lista.innerHTML = conversas.map(c => `
    <div class="conversa-item ${c.id === conversaSelecionada ? 'ativa' : ''}"
         onclick="abrirConversa(${c.id})">
      <div class="top">
        <span class="lead">${c.numero_lead}</span>
        <span class="clinica">${c.clinica_nome}</span>
      </div>
      <div class="preview">
        ${c.ultima_role === 'user' ? '' : 'Ana: '}${(c.ultima_mensagem || '—').substring(0, 80)}
        ${c.pausada ? '<span class="badge">HUMANO</span>' : ''}
      </div>
    </div>
  `).join('');
}

async function abrirConversa(id) {
  conversaSelecionada = id;
  const r = await fetch('/painel/api/conversas/' + id);
  if (!r.ok) return;
  const data = await r.json();

  document.getElementById('placeholder').style.display = 'none';
  document.getElementById('detalhe-topo').style.display = 'flex';
  document.getElementById('mensagens').style.display = 'flex';

  document.getElementById('det-titulo').textContent = data.info.numero_lead;
  document.getElementById('det-sub').textContent = data.info.clinica_nome;

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
  ms.innerHTML = data.mensagens.map(m => `
    <div class="msg ${m.role === 'user' ? 'msg-lead' : 'msg-ana'}">
      ${escapar(m.conteudo)}
    </div>
  `).join('');
  ms.scrollTop = ms.scrollHeight;

  // atualiza lista pra destacar selecionada
  carregarConversas();
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
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                   .replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
}

// Atualiza a cada 5s
carregarConversas();
setInterval(() => {
  carregarConversas();
  if (conversaSelecionada) {
    // recarrega só as mensagens (sem mexer no scroll se nada novo)
    abrirConversa(conversaSelecionada);
  }
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

    # ---------- Login / Logout ----------
    @app.route("/painel", methods=["GET"])
    def painel_home():
        if "user_id" not in session:
            return redirect(url_for("painel_login_page"))
        return render_template_string(PAINEL_HTML, nome=session.get("nome", "—"))

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
        session["clinica_id"] = user.get("clinica_id")  # None = admin
        return redirect(url_for("painel_home"))

    @app.route("/painel/logout")
    def painel_logout():
        session.clear()
        return redirect(url_for("painel_login_page"))

    # ---------- API JSON ----------
    @app.route("/painel/api/conversas", methods=["GET"])
    @login_required
    def api_listar_conversas():
        clinica_id = session.get("clinica_id")  # None = admin = vê tudo
        rows = listar_conversas(clinica_id=clinica_id)
        # Converte datetime pra string pro JSON
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
        # Checa permissão: usuário só vê conversas da própria clínica
        if session.get("clinica_id") is not None:
            if data["info"]["clinica_id"] != session["clinica_id"]:
                return jsonify({"erro": "sem permissao"}), 403
        # Datas pra string
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
        # Envia via WhatsApp Cloud API
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

        # Salva no histórico como se fosse da Ana (mantém continuidade visual).
        salvar_mensagem(conversa_id, "assistant", texto)
        return jsonify({"ok": True})

    # ---------- Admin: criar usuário pra clínica ----------
    @app.route("/painel/admin/usuarios", methods=["GET"])
    @login_required
    @admin_required
    def admin_listar_clinicas():
        """Lista clínicas pra o admin saber qual ID usar."""
        return jsonify(listar_clinicas())

    @app.route("/painel/admin/usuarios", methods=["POST"])
    @login_required
    @admin_required
    def admin_criar_usuario():
        """Cria um usuário pra uma clínica. JSON: email, senha, nome, clinica_id."""
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
